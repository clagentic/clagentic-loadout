"""review.keep_evidence_verb — the `keep-evidence` subcommand of `loadout-review`.

Copies the files a review was built from under the review evidence store
(verdict_record.review_evidence), filed by the id of the comment the review
landed as. The comment id is given, or read from the newest review record `post`
wrote for this repository, pull request, role and head. Offline: it reads no host
and binds no caller. It never refuses a review or a verdict; the only refusal is
of an unusable input, exit 64, with nothing written.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.artifact_store import StoreError
from clagentic_loadout.verdict_record.record import is_full_sha
from clagentic_loadout.verdict_record.review_evidence import (
    describe_files,
    keep_files,
    review_evidence_root,
)
from clagentic_loadout.verdict_record.review_record import (
    COMMENT_ID_RE,
    find_review_records,
    review_records_root,
)

EXIT_OK = 0
EXIT_REFUSED = 64

#: One path segment of a repository slug. '.' and '..' match this shape and are
#: refused by name; a leading dot is allowed (an org profile repository).
_NAME_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
_RESERVED_NAMES = (".", "..")


def add_keep_evidence_parser(subparsers: Any) -> None:
    keep = subparsers.add_parser(
        "keep-evidence",
        help="Keep a review's files under the id of the comment it landed as.",
        description="Copies each --file, byte for byte, to <review evidence root>/<head sha>/"
        "<comment id>/<file name>. Without --comment-id the comment id is the one on the "
        "newest review record `post` wrote for this repository, pull request and role at "
        "--head-sha. Run again for the same comment it replaces the earlier copy as a whole, "
        "and the earlier copy survives any failure of the replacement. A missing or unreadable "
        "file, two files with one name, or no matching review record writes nothing (exit 64). "
        "Prints one JSON object. Offline: no host read, no caller.",
    )
    keep.add_argument("--repo", required=True, help="OWNER/NAME of the pull request.")
    keep.add_argument("--pr", required=True, type=int, help="Pull request number.")
    keep.add_argument("--head-sha", required=True, help="The full head SHA that was reviewed.")
    keep.add_argument("--role", required=True, help="The reviewer role that posted the review.")
    keep.add_argument(
        "--comment-id",
        default=None,
        help="The comment id (decimal digits). When omitted it is read from the newest "
        "review record at --head-sha for this repository, pull request and role.",
    )
    keep.add_argument(
        "--file",
        action="append",
        required=True,
        default=None,
        dest="files",
        metavar="PATH",
        help="A file to keep. Repeatable; the file names must differ.",
    )
    keep.add_argument(
        "--version",
        action="version",
        version=f"loadout-review {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )


class _Refused(Exception):
    pass


def _refuse(message: str) -> int:
    print(f"loadout-review: {message}", file=sys.stderr)
    return EXIT_REFUSED


def _check_repo(value: str) -> str:
    parts = value.strip().split("/")
    if len(parts) != 2 or not all(
        part and _NAME_RE.match(part) and part not in _RESERVED_NAMES for part in parts
    ):
        raise _Refused(
            f"--repo must be OWNER/NAME with each part made of letters, digits, '.', '_' or "
            f"'-' and neither part '.' or '..', got {value!r}"
        )
    return "/".join(parts)


def _read_files(paths: list[str]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    named: dict[str, str] = {}
    for raw in paths:
        path = Path(raw).expanduser()
        if path.name in ("", ".", ".."):
            raise _Refused(f"--file {raw!r} does not name a file")
        resolved = str(path.resolve())
        if path.name in named:
            raise _Refused(
                f"two --file values have the file name {path.name!r}: {named[path.name]!r} "
                f"and {resolved!r}"
            )
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise _Refused(f"cannot read --file {resolved!r}: {exc}") from exc
        named[path.name] = resolved
        files[path.name] = data
    return files


def _comment_id(args: argparse.Namespace, repo: str, config_root) -> tuple[str, str]:
    if args.comment_id is not None:
        if COMMENT_ID_RE.match(args.comment_id) is None:
            raise _Refused(f"--comment-id must be decimal digits, got {args.comment_id!r}")
        return args.comment_id, "argument"
    root = review_records_root(config_root=config_root)
    try:
        records = find_review_records(
            root, head=args.head_sha, repo=repo, pr=args.pr, role=args.role
        )
    except StoreError as exc:
        raise _Refused(str(exc)) from exc
    if not records:
        raise _Refused(
            f"no review record under {str(root)!r} for repo {repo!r}, pr {args.pr}, role "
            f"{args.role!r} at head {args.head_sha}; pass --comment-id"
        )
    return records[0]["comment_id"], "review record"


def run_keep_evidence(args: argparse.Namespace, *, config_root=None) -> int:
    try:
        repo = _check_repo(args.repo)
        if args.pr <= 0:
            raise _Refused(f"--pr must be a positive integer, got {args.pr!r}")
        if not is_full_sha(args.head_sha):
            raise _Refused(
                f"--head-sha must be a full commit sha (40 or 64 lowercase hex digits), got "
                f"{args.head_sha!r}"
            )
        if not args.role.strip():
            raise _Refused("--role must not be empty")
        files = _read_files(args.files)
        comment_id, source = _comment_id(args, repo, config_root)
        directory = keep_files(
            review_evidence_root(config_root=config_root), args.head_sha, comment_id, files
        )
    except (_Refused, StoreError) as exc:
        return _refuse(str(exc))
    print(
        json.dumps(
            {
                "result": "kept",
                "directory": str(directory),
                "comment_id": comment_id,
                "comment_id_source": source,
                "files": describe_files(files),
            }
        )
    )
    return EXIT_OK


__all__ = ["EXIT_OK", "EXIT_REFUSED", "add_keep_evidence_parser", "run_keep_evidence"]
