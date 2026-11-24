"""review.procedure -- the `procedure` subcommand of `loadout-review`.

For a reviewer role, a pull request and an optional sealed spec, `procedure` pins the
head and stages its files (the same host read and staging `stage` performs), creates a
work directory and prints the finished command lines the reviewer runs, in order, to
review, judge its drops, check them, post and keep its files. It prints; it never
runs a line and it adds no refusal of its own beyond an unusable input.

Every printed line is one physical line that runs as written: a flag name is a literal
taken from the verb that owns it, every value is shell-quoted, and no line holds a
placeholder, a heredoc or a shell variable. The reviewer's judgement reaches the lines
only through a records file at a printed path (see review.apply_drops).

Layout under ``--out DIR``: ``DIR/head`` is the stage directory, ``DIR/work`` is the
caller's (the run directory, the records file, and every output the lines write).
"""

from __future__ import annotations

import argparse
import shlex
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO
from clagentic_loadout.review.atomic_io import write_bytes_atomic
from clagentic_loadout.review.profile_spec_section import show_line
from clagentic_loadout.review.stage_manifest import MANIFEST_NAME

STAGE_DIR_NAME = "head"
WORK_DIR_NAME = "work"
RUN_DIR_NAME = "run"
FINDINGS_NAME = "findings.json"
RECORDS_NAME = "records.txt"
JUDGED_NAME = "judged.json"
DROPPED_NAME = "dropped.json"
COVERAGE_NAME = "coverage.json"
REJECT_REASON_NAME = "reject-reason.txt"
HITS_RECORDS_NAME = "hits-records.txt"
HITS_JUDGED_NAME = "hits-judged.json"
HITS_DROPPED_NAME = "hits-dropped.json"

STEP_CRITERIA = "criteria"
STEP_RUN = "run"
STEP_HITS = "hits"
STEP_APPLY = "apply"
STEP_CHECK = "check"
STEP_POST_CLEAN = "post_clean"
STEP_POST_BLOCKING = "post_blocking"
STEP_POST_CLEAN_REJECT = "post_clean_reject"
STEP_POST_BLOCKING_REJECT = "post_blocking_reject"
STEP_KEEP = "keep"
STEP_TEARDOWN = "teardown"


class ProcedureInputError(Exception):
    """An input cannot be printed into a command line; nothing was staged."""


def add_procedure_parser(
    subparsers: Any, add_common: Callable[[argparse.ArgumentParser], None]
) -> None:
    procedure = subparsers.add_parser(
        "procedure",
        help="Pin a pull request's head, stage it and print the review steps as command lines.",
        description="Reads the pull request through the host API as `stage` does (--caller "
        "binds that read), writes the head copies and their manifest under OUT/head, creates "
        "OUT/work with an empty records file, and prints one JSON object: the pinned head, the "
        "manifest, the files the steps read and write, and an ordered list of {step, line}. "
        "Each line runs as written: no placeholder, heredoc or shell variable, every value "
        "shell-quoted. With --spec-hash the lines also read the spec's criteria, carry the "
        "coverage and add the two reject variants of the post. Nothing is run and no rule is "
        "applied; the only refusals are of an unusable input.",
    )
    add_common(procedure)
    procedure.add_argument(
        "--role",
        required=True,
        help="The reviewer role, printed into the lines (--caller on run and post, --role on "
        "keep-evidence). It is an opaque word; no behaviour depends on it.",
    )
    procedure.add_argument(
        "--out",
        required=True,
        help="A new directory, or an existing empty one (else exit 64, nothing written). The "
        "stage goes in OUT/head and the reviewer's work files in OUT/work.",
    )
    procedure.add_argument(
        "--spec-hash",
        default=None,
        help="The sealed spec the review is against (its sha256, as printed by `loadout-spec "
        "seal`). It must be in the specs store.",
    )
    procedure.add_argument(
        "--scanner-input",
        default=None,
        help="The path where the role's scanner input will be written. It is not read now: it "
        "is carried by the apply and post lines and kept with the review's files, and a step "
        "is added that lists its numbered hits.",
    )
    procedure.add_argument(
        "--version",
        action="version",
        version=f"loadout-review {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )


@dataclass(frozen=True)
class ProcedureInputs:
    """What the lines are built from. Paths are absolute and resolved."""

    role: str
    repo: str
    pr: int
    platform: str
    head_sha: str
    stage_dir: Path
    work_dir: Path
    spec_hash: str | None = None
    scanner_input: str | None = None
    git_host_base_url: str | None = None


def kept_file_names(spec_hash: str | None) -> tuple[str, ...]:
    """The file names `keep-evidence` is given, which must differ from one another
    and from the scanner input's own name."""
    names = [FINDINGS_NAME, JUDGED_NAME, DROPPED_NAME, RECORDS_NAME]
    if spec_hash is not None:
        names.append(COVERAGE_NAME)
    return tuple(names)


def check_printable(values: dict[str, str]) -> None:
    """Refuse a value that cannot sit inside one physical command line."""
    for label, value in values.items():
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
            raise ProcedureInputError(
                f"{label} holds a control character or a newline, which cannot be printed "
                f"into a single command line: {value!r}"
            )


def check_scanner_name(scanner_input: str | None, spec_hash: str | None) -> None:
    """The scanner input is kept beside the review's own files, by file name."""
    if scanner_input is None:
        return
    name = Path(scanner_input).name
    if name in kept_file_names(spec_hash):
        raise ProcedureInputError(
            f"--scanner-input {scanner_input!r} has the file name {name!r}, which is also a "
            "file the review keeps; give the scanner input another file name"
        )


def _command(*parts: str) -> str:
    return " ".join(shlex.quote(part) for part in parts)


def work_files(inputs: ProcedureInputs) -> dict[str, str | None]:
    work = inputs.work_dir
    spec = inputs.spec_hash is not None
    scanner = inputs.scanner_input is not None
    return {
        "manifest": str(inputs.stage_dir / MANIFEST_NAME),
        "stage_dir": str(inputs.stage_dir),
        "work_dir": str(work),
        "run_dir": str(work / RUN_DIR_NAME),
        "findings": str(work / RUN_DIR_NAME / FINDINGS_NAME),
        "records": str(work / RECORDS_NAME),
        "judged": str(work / JUDGED_NAME),
        "dropped": str(work / DROPPED_NAME),
        "coverage": str(work / COVERAGE_NAME) if spec else None,
        "reject_reason": str(work / REJECT_REASON_NAME) if spec else None,
        "scanner_input": inputs.scanner_input,
        "hits_records": str(work / HITS_RECORDS_NAME) if scanner else None,
        "hits_judged": str(work / HITS_JUDGED_NAME) if scanner else None,
        "hits_dropped": str(work / HITS_DROPPED_NAME) if scanner else None,
    }


def _host_flags(inputs: ProcedureInputs) -> list[str]:
    flags = ["--repo", inputs.repo, "--pr", str(inputs.pr), "--platform", inputs.platform]
    if inputs.platform == PLATFORM_FORGEJO and inputs.git_host_base_url is not None:
        flags += ["--git-host-base-url", inputs.git_host_base_url]
    return flags


def build_lines(inputs: ProcedureInputs) -> list[dict[str, str]]:
    """The ordered ``{step, line}`` list for *inputs*."""
    files = work_files(inputs)
    spec, scanner = inputs.spec_hash, inputs.scanner_input
    steps: list[dict[str, str]] = []

    def add(step: str, *parts: str) -> None:
        steps.append({"step": step, "line": _command(*parts)})

    if spec is not None:
        steps.append(
            {"step": STEP_CRITERIA, "line": show_line(spec, "acceptance_criteria")}
        )
    run = ["loadout-review", "run", "--caller", inputs.role, *_host_flags(inputs)]
    run += ["--out", files["run_dir"]]
    if spec is not None:
        run += ["--spec-hash", spec]
    add(STEP_RUN, *run)
    if scanner is not None:
        add(
            STEP_HITS, "loadout-review", "apply-drops",
            "--findings", files["findings"], "--records", files["hits_records"],
            "--judged-out", files["hits_judged"], "--dropped-out", files["hits_dropped"],
            "--scanner-input", scanner,
        )
    apply = [
        "loadout-review", "apply-drops",
        "--findings", files["findings"], "--records", files["records"],
        "--judged-out", files["judged"], "--dropped-out", files["dropped"],
        "--manifest", files["manifest"],
    ]
    if scanner is not None:
        apply += ["--scanner-input", scanner]
    if spec is not None:
        apply += ["--coverage-out", files["coverage"], "--reject-reason-out", files["reject_reason"]]
    add(STEP_APPLY, *apply)
    add(
        STEP_CHECK, "loadout-review", "check-drops",
        "--manifest", files["manifest"], "--dropped", files["dropped"],
        "--findings", files["judged"],
    )

    def post(status: str, *extra: str) -> list[str]:
        parts = [
            "loadout-review", "post", "--caller", inputs.role, *_host_flags(inputs),
            "--findings", files["judged"], "--status", status, "--dropped", files["dropped"],
        ]
        if scanner is not None:
            parts += ["--scanner-input", scanner]
        if spec is not None:
            parts += ["--spec-hash", spec, "--spec-coverage", files["coverage"]]
        return [*parts, *extra]

    add(STEP_POST_CLEAN, *post("clean"))
    add(STEP_POST_BLOCKING, *post("blocking"))
    if spec is not None:
        reject = ("--reject-justification-file", files["reject_reason"])
        add(STEP_POST_CLEAN_REJECT, *post("clean", *reject))
        add(STEP_POST_BLOCKING_REJECT, *post("blocking", *reject))
    keep = [
        "loadout-review", "keep-evidence", "--repo", inputs.repo, "--pr", str(inputs.pr),
        "--head-sha", inputs.head_sha, "--role", inputs.role,
    ]
    kept = [files["findings"], files["judged"], files["dropped"], files["records"]]
    if spec is not None:
        kept.append(files["coverage"])
    if scanner is not None:
        kept.append(scanner)
    for path in kept:
        keep += ["--file", path]
    add(STEP_KEEP, *keep)
    add(STEP_TEARDOWN, "loadout-review", "teardown", "--manifest", files["manifest"])
    return steps


def create_work_area(inputs: ProcedureInputs) -> None:
    """Create the work directory with an empty records file (and an empty records
    file for the scanner-hits step)."""
    inputs.work_dir.mkdir(parents=True, exist_ok=True)
    write_bytes_atomic(inputs.work_dir / RECORDS_NAME, b"")
    if inputs.scanner_input is not None:
        write_bytes_atomic(inputs.work_dir / HITS_RECORDS_NAME, b"")


__all__ = [
    "RECORDS_NAME",
    "STAGE_DIR_NAME",
    "WORK_DIR_NAME",
    "ProcedureInputError",
    "ProcedureInputs",
    "add_procedure_parser",
    "build_lines",
    "check_printable",
    "check_scanner_name",
    "create_work_area",
    "kept_file_names",
    "work_files",
]
