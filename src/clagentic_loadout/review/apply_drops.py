"""review.apply_drops — the `apply-drops` subcommand of `loadout-review`.

A reviewer's judgement of its candidates is a records file (review.drop_records):
which findings and scanner hits it drops and why, and in a spec review the tag of
each kept finding, the coverage of each criterion and, optionally, a rejection of
the docs-only justification. `apply-drops` turns it into the files `post` reads:
the judged findings, the dropped entries, the coverage and the rejection reason.

Nothing the reviewer wrote is passed to a shell, eval or template; reasons and
texts are stored as strings. Offline: no host read, no caller. Every input is
checked before any output is written, so a refusal (exit 64) leaves every output
path as it was.

Exit codes: 0 written; 64 an input is unusable (the message names the path or the
line of the records file).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.review import atomic_io, stage_manifest
from clagentic_loadout.review.drop_records import Records, RecordsError, parse_records
from clagentic_loadout.review.scanner_input import (
    ScannerHit,
    ScannerInput,
    ScannerInputError,
    parse_scanner_input,
)
from clagentic_loadout.sha import FULL_SHA_RE

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_REFUSED = 64


class ApplyError(Exception):
    """An input cannot be used; the message names the path or the records line."""


def add_apply_drops_parser(subparsers: Any) -> None:
    apply = subparsers.add_parser(
        "apply-drops",
        help="Turn a reviewer's drop records into the files `post` reads.",
        description="Reads the findings document of `run`, a records file (one record per "
        "line: 'drop <id> <reason>', 'tag <n> <tag>', 'cover <criterion> <met|unmet>', "
        "'reject <text>'; <id> is a finding's zero-based position or sN, a scanner hit's "
        "number) and optionally the scanner input and a stage manifest. Writes the judged "
        "findings (the document without the dropped findings), the dropped entries `post "
        "--dropped` takes (with read_at, the manifest's head, when --manifest is given) and, "
        "in spec mode, the coverage and the rejection reason. Prints one JSON object with the "
        "paths, the drop counts and every scanner hit with its sN id; a records file with no "
        "records lists the hits. Nothing is written when any input is unusable (exit 64). "
        "Offline: no host read, no caller.",
    )
    apply.add_argument("--findings", required=True, help="The findings document `run` wrote.")
    apply.add_argument("--records", required=True, help="The records file (UTF-8 text).")
    apply.add_argument("--judged-out", required=True, help="Where to write the judged findings.")
    apply.add_argument("--dropped-out", required=True, help="Where to write the dropped entries.")
    apply.add_argument(
        "--scanner-input",
        default=None,
        help="The scanner input of the review: its hits are numbered s1, s2, ... in file order "
        "and may be dropped by that id.",
    )
    apply.add_argument(
        "--manifest",
        default=None,
        help="The stage manifest of the review: every dropped entry then carries read_at, "
        "the manifest's head.",
    )
    apply.add_argument(
        "--coverage-out",
        default=None,
        help="Spec mode: where to write the coverage (needs --reject-reason-out). Turns on "
        "tag, cover and reject records: every kept finding then needs one tag and at least "
        "one cover record is required.",
    )
    apply.add_argument(
        "--reject-reason-out",
        default=None,
        help="Spec mode: where to write the reject text for `post --reject-justification-file`. "
        "A file left here by an earlier run is removed when there is no reject record.",
    )
    apply.add_argument(
        "--version",
        action="version",
        version=f"loadout-review {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )


def _refuse(message: str) -> int:
    print(f"loadout-review: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _load_findings(path: str) -> tuple[Any, list[Any]]:
    """The findings document as read and the findings array inside it."""
    resolved = str(Path(path).expanduser().resolve())
    try:
        document = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ApplyError(f"cannot read the findings file {resolved!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ApplyError(f"the findings file {resolved!r} is not valid JSON: {exc}") from exc
    findings = document.get("findings") if isinstance(document, dict) else document
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        raise ApplyError(
            f"the findings file {resolved!r} must be a JSON array of objects (or an object "
            "with a 'findings' array of objects)"
        )
    return document, findings


def _load_scanner_input(path: str, head: str | None) -> ScannerInput:
    resolved = str(Path(path).expanduser().resolve())
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ApplyError(f"cannot read the scanner input {resolved!r}: {exc}") from exc
    # Without a head to hold it to, the file's own stamp is the head it is read at.
    stamped = data.get("head_sha") if isinstance(data, dict) else None
    reviewed = head or (stamped if isinstance(stamped, str) else "")
    try:
        return parse_scanner_input(data, reviewed_head=reviewed, source=f"scanner input {resolved!r}")
    except ScannerInputError as exc:
        raise ApplyError(str(exc)) from exc


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _entry(source: dict[str, Any], reason: str, where: str) -> dict[str, Any]:
    file_name, line = source.get("file"), source.get("line")
    rule_id, message = source.get("rule_id"), source.get("message")
    if not isinstance(file_name, str) or not file_name:
        raise ApplyError(f"{where}: the dropped candidate has no string 'file'")
    if isinstance(line, bool) or not isinstance(line, int):
        raise ApplyError(f"{where}: the dropped candidate has no integer 'line'")
    if not isinstance(rule_id, str) or not rule_id:
        raise ApplyError(f"{where}: the dropped candidate has no string 'rule_id'")
    if not isinstance(message, str):
        raise ApplyError(f"{where}: the dropped candidate has no string 'message'")
    return {
        "file": file_name,
        "line": line,
        "rule_id": rule_id,
        "message": _one_line(message),
        "reason": reason,
    }


def _hit_source(hit: ScannerHit) -> dict[str, Any]:
    return {"file": hit.file, "line": hit.line, "rule_id": hit.rule_id, "message": hit.message}


def _check_drops(
    records: Records, findings: list[Any], hits: tuple[ScannerHit, ...]
) -> list[dict[str, Any]]:
    """The dropped entries in record order. Raises ApplyError for an id that names
    nothing, or is dropped twice."""
    seen: dict[tuple[str, int], int] = {}
    entries: list[dict[str, Any]] = []
    for drop in records.drops:
        where = f"records file {records.path!r}, line {drop.line}"
        kind, number = drop.target
        label = str(number) if kind == "finding" else f"s{number}"
        if kind == "finding" and number >= len(findings):
            raise ApplyError(
                f"{where}: {label} names no finding (the findings file holds {len(findings)})"
            )
        if kind == "hit" and number > len(hits):
            raise ApplyError(
                f"{where}: {label} names no scanner hit (the scanner input holds {len(hits)})"
            )
        if drop.target in seen:
            raise ApplyError(f"{where}: {label} is already dropped on line {seen[drop.target]}")
        seen[drop.target] = drop.line
        source = findings[number] if kind == "finding" else _hit_source(hits[number - 1])
        entries.append(_entry(source, drop.reason, where))
    return entries


def _spec_outputs(
    records: Records, findings: list[Any], dropped_positions: set[int]
) -> tuple[dict[int, str], list[dict[str, str]], str | None]:
    """The tag of each kept finding, the coverage in record order and the reject
    text. Raises ApplyError when a kept finding lacks its tag or is tagged twice,
    a tag names a dropped or missing finding, no cover record is given, or a
    criterion or the rejection is given twice."""
    tags: dict[int, str] = {}
    tag_lines: dict[int, int] = {}
    for item in records.tags:
        where = f"records file {records.path!r}, line {item.line}"
        if item.position >= len(findings):
            raise ApplyError(f"{where}: tag {item.position} names no finding")
        if item.position in dropped_positions:
            raise ApplyError(f"{where}: finding {item.position} is dropped and cannot be tagged")
        if item.position in tags:
            raise ApplyError(
                f"{where}: finding {item.position} is already tagged on line "
                f"{tag_lines[item.position]}"
            )
        tags[item.position] = item.tag
        tag_lines[item.position] = item.line
    missing = [n for n in range(len(findings)) if n not in dropped_positions and n not in tags]
    if missing:
        raise ApplyError(
            f"records file {records.path!r}: no tag record for kept finding(s) {missing}"
        )
    if not records.covers:
        raise ApplyError(f"records file {records.path!r}: spec mode needs at least one cover record")
    coverage: list[dict[str, str]] = []
    cover_lines: dict[str, int] = {}
    for cover in records.covers:
        if cover.criterion in cover_lines:
            raise ApplyError(
                f"records file {records.path!r}, line {cover.line}: criterion "
                f"{cover.criterion!r} is already covered on line {cover_lines[cover.criterion]}"
            )
        cover_lines[cover.criterion] = cover.line
        coverage.append({"criterion": cover.criterion, "status": cover.status})
    if len(records.rejects) > 1:
        raise ApplyError(
            f"records file {records.path!r}, line {records.rejects[1].line}: a second reject "
            f"record (the first is on line {records.rejects[0].line})"
        )
    reject = records.rejects[0].text if records.rejects else None
    return tags, coverage, reject


def _write(path: str, write) -> None:
    target = Path(path).expanduser()
    try:
        write(target)
    except OSError as exc:
        raise ApplyError(f"cannot write {str(target.resolve())!r}: {exc}") from exc


def run_apply_drops(args: argparse.Namespace) -> int:
    spec_mode = args.coverage_out is not None
    if spec_mode != (args.reject_reason_out is not None):
        print(
            "loadout-review: --coverage-out and --reject-reason-out must be given together",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        document, findings = _load_findings(args.findings)
        records = parse_records(args.records)
        head: str | None = None
        if args.manifest is not None:
            try:
                head = stage_manifest.read_manifest(args.manifest, require_own_directory=False).head_sha
            except stage_manifest.ManifestError as exc:
                raise ApplyError(str(exc)) from exc
        elif isinstance(document, dict) and isinstance(document.get("head_sha"), str):
            head = document["head_sha"] if FULL_SHA_RE.match(document["head_sha"]) else None
        scanner = _load_scanner_input(args.scanner_input, head) if args.scanner_input else None
        hits = scanner.hits if scanner is not None else ()
        if records.spec_records and not spec_mode:
            first = min(records.spec_records, key=lambda item: item.line)
            raise ApplyError(
                f"records file {records.path!r}, line {first.line}: a tag, cover or reject "
                "record needs --coverage-out and --reject-reason-out"
            )
        dropped = _check_drops(records, findings, hits)
        dropped_positions = {n for kind, n in (d.target for d in records.drops) if kind == "finding"}
        tags: dict[int, str] = {}
        coverage: list[dict[str, str]] = []
        reject: str | None = None
        if spec_mode:
            tags, coverage, reject = _spec_outputs(records, findings, dropped_positions)
        read_at = head if args.manifest is not None else None
        if read_at is not None:
            dropped = [{**entry, "read_at": read_at} for entry in dropped]
        kept = [
            {**item, "spec_tag": tags[n]} if spec_mode else item
            for n, item in enumerate(findings)
            if n not in dropped_positions
        ]
        judged: Any = {**document, "findings": kept} if isinstance(document, dict) else kept
        _write(args.judged_out, lambda p: atomic_io.write_json_atomic(p, judged))
        _write(args.dropped_out, lambda p: atomic_io.write_json_atomic(p, dropped))
        if spec_mode:
            _write(args.coverage_out, lambda p: atomic_io.write_json_atomic(p, coverage))
            if reject is not None:
                _write(
                    args.reject_reason_out,
                    lambda p: atomic_io.write_bytes_atomic(p, reject.encode("utf-8")),
                )
            else:
                _write(args.reject_reason_out, lambda p: p.unlink(missing_ok=True))
    except (ApplyError, RecordsError) as exc:
        return _refuse(str(exc))
    print(
        json.dumps(
            {
                "result": "written",
                "judged": str(Path(args.judged_out).expanduser().resolve()),
                "dropped": str(Path(args.dropped_out).expanduser().resolve()),
                "coverage": str(Path(args.coverage_out).expanduser().resolve()) if spec_mode else None,
                "reject_reason": str(Path(args.reject_reason_out).expanduser().resolve())
                if reject is not None
                else None,
                "drops": {
                    "findings": len(dropped_positions),
                    "hits": len(records.drops) - len(dropped_positions),
                },
                "read_at": read_at,
                "hits": [
                    {
                        "id": f"s{number}",
                        "scanner": hit.scanner,
                        "file": hit.file,
                        "line": hit.line,
                        "rule_id": hit.rule_id,
                    }
                    for number, hit in enumerate(hits, start=1)
                ],
            }
        )
    )
    return EXIT_OK


__all__ = ["EXIT_OK", "EXIT_REFUSED", "EXIT_USAGE", "add_apply_drops_parser", "run_apply_drops"]
