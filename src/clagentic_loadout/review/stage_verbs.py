"""review.stage_verbs — the `stage`, `teardown` and `check-drops` subcommands of `loadout-review`.

``stage`` gives a reviewer a read-at-head source: the head version of every file a
pull request changes, pinned from the host API when it runs. ``teardown`` removes
what a stage wrote. ``check-drops`` reports whether each candidate a reviewer
dropped was read at that head. ``teardown`` and ``check-drops`` are offline: they
take a manifest by path and bind no caller. None of the three refuses a review or
a verdict; the only refusals are of an unusable path or file, and each names the
resolved path.

Exit codes of these three subcommands (64 is shared with every refusal of an
unusable input here; the rest are the verb's own):

    0   done (a stage with a staging_gap, and any drop outcome, are still 0)
    5   the host could not be read
    64  refused: the --out path is not new or empty, a manifest or input file is
        unusable, or the manifest is not the one that holds its directory
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.review import drop_check, head_stage, stage_manifest

EXIT_OK = 0
EXIT_REFUSED = 64


def add_stage_parsers(
    subparsers: Any, add_common: Callable[[argparse.ArgumentParser], None]
) -> None:
    stage = subparsers.add_parser(
        "stage",
        help="Write read-only copies of a pull request's head files and a manifest.",
        description="Pins the pull request's head as the host API reports it now and writes, "
        "under --out, the head version of every changed file that exists there, each at its "
        "repository path, with a manifest (path and sha256 of each written copy). A changed "
        "file with no retrievable head content, or whose path would resolve outside --out, is "
        "not written and is listed under 'missing' with its reason; a file the pull request "
        "removes is listed under 'deleted'. Everything is read through the host API: no "
        "checkout, worktree or fetch. Prints one JSON object with the manifest path and the "
        "head SHA. A count the host states that the stage does not account for, or a stage "
        "with no copies, is reported as 'staging_gap' and never changes the exit code.",
    )
    add_common(stage)
    stage.add_argument(
        "--out",
        required=True,
        help="A new directory, or an existing empty one. Nothing is written when the path "
        "exists and is not an empty directory (exit 64): an existing stage is never replaced.",
    )

    teardown = subparsers.add_parser(
        "teardown",
        help="Remove what a `stage` call wrote, named by its manifest.",
        description="Removes the staged copies the manifest lists, the manifest and its "
        "directory. Run again after the directory is gone it exits 0 and says so. A file that "
        "is not a stage manifest, or a manifest whose recorded directory is not the directory "
        "that holds it, removes nothing (exit 64). Files in the directory that the manifest "
        "does not name are never removed.",
    )
    teardown.add_argument("--manifest", required=True, help="The manifest `stage` wrote.")

    check = subparsers.add_parser(
        "check-drops",
        help="Report whether each dropped candidate was read at the staged head.",
        description="Report only. For each entry of the dropped file (the shape `post --dropped` "
        "takes, with an optional 'read_at'), says whether it is proven: 'read_at' is the head "
        "the manifest pinned and the file has a staged copy or is one the pull request deleted. "
        "An unproven entry carries its reason (no read_at, read at another revision, or no head "
        "copy). 'gaps' lists the files the stage could not provide, and every dropped or "
        "finding file the stage neither holds nor records as deleted. Exit 0 whatever the "
        "outcome; exit 64 only for an unreadable or malformed input.",
    )
    check.add_argument("--manifest", required=True, help="The manifest `stage` wrote.")
    check.add_argument("--dropped", required=True, help="The reviewer's dropped file (JSON).")
    check.add_argument(
        "--findings",
        default=None,
        help="The reviewer's findings file (a JSON array, or the output of `run`); the files "
        "of its findings are checked for a head copy as well.",
    )
    for sub in (stage, teardown, check):
        sub.add_argument(
            "--version",
            action="version",
            version=f"loadout-review {get_version()}",
            help="Show the clagentic-loadout package version and exit.",
        )


def _refuse(message: str) -> int:
    print(f"loadout-review: {message}", file=sys.stderr)
    return EXIT_REFUSED


def run_teardown(args: argparse.Namespace) -> int:
    path = Path(args.manifest).expanduser().resolve()
    if not path.exists():
        print(
            json.dumps(
                {"result": "teardown", "already_gone": True, "directory": str(path.parent)}
            )
        )
        print(
            f"loadout-review: the manifest {str(path)!r} is gone; nothing to remove",
            file=sys.stderr,
        )
        return EXIT_OK
    try:
        manifest = stage_manifest.read_manifest(args.manifest, require_own_directory=True)
    except stage_manifest.ManifestError as exc:
        return _refuse(str(exc))
    left = head_stage.teardown_stage(manifest)
    print(
        json.dumps(
            {
                "result": "teardown",
                "already_gone": False,
                "directory": str(manifest.directory),
                "removed_directory": not left,
                "remaining": left,
            }
        )
    )
    if left:
        print(
            f"loadout-review: {str(manifest.directory)!r} still holds {len(left)} file(s) the "
            "manifest does not name; they were left in place",
            file=sys.stderr,
        )
    return EXIT_OK


def run_check_drops(args: argparse.Namespace) -> int:
    try:
        manifest = stage_manifest.read_manifest(args.manifest, require_own_directory=False)
        dropped = drop_check.load_dropped(args.dropped)
        finding_files = (
            drop_check.load_findings_files(args.findings) if args.findings is not None else []
        )
    except (stage_manifest.ManifestError, drop_check.DropInputError) as exc:
        return _refuse(str(exc))
    report = drop_check.check_drops(manifest, dropped, finding_files)
    print(json.dumps(report))
    for item in report["drops"]:
        if not item["proven"]:
            print(
                f"loadout-review: notice: dropped {item['id']} is unproven ({item['reason']})",
                file=sys.stderr,
            )
    return EXIT_OK


__all__ = [
    "EXIT_OK",
    "EXIT_REFUSED",
    "add_stage_parsers",
    "run_check_drops",
    "run_teardown",
]
