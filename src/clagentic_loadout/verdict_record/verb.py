"""verdict_record.verb -- `loadout-record`: read verdict records.

  loadout-record show RECORD_ID
  loadout-record list --head SHA [--gate GATE]

`show` prints one record by id; `list` prints the records filed under one commit,
oldest first. Both are read-only: records are written by the steps that measure
(the push verification run, the review verdict post, the merge verb), never by
hand, so an operator can read what a gate recorded and cannot add to it here.

The records root is resolved as in verdict_record.store: the
CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR environment variable, the user-level
``verdict_records.dir`` setting, then the default under the user config root.

Exit codes:

  0   ok
  1   usage error
  3   the records root cannot be used or read
  40  no record has that id
  41  the record exists but is not a valid record
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.verdict_record.record import RECORD_ID_RE, is_full_sha
from clagentic_loadout.verdict_record.store import (
    RecordNotFoundError,
    StoreError,
    load_record,
    records_at_head,
    records_root,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_STORE_UNUSABLE = 3
EXIT_NOT_FOUND = 40
EXIT_RECORD_INVALID = 41


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-record",
        description=(
            "loadout-record -- read verdict records. A record is what one gate "
            "measured of one exact commit; the merge verb refuses a gate that has "
            "none at the head it is merging."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example:\n"
            "  loadout-record show 20260101T120000Z-0a1b2c3d\n"
            "  loadout-record list --head <full 40-character commit sha>\n"
            "\n"
            "Exit codes: 0 ok, 1 usage, 3 records root unusable, 40 no such record, "
            "41 record invalid."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-record {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser(
        "show",
        help="Print one record by id.",
        description="Print one verdict record as JSON; read-only.",
    )
    show.add_argument("record_id", help="The record id, as printed when it was written.")
    listing = sub.add_parser(
        "list",
        help="List the records filed under one commit.",
        description="Print the verdict records filed under one commit as JSON; read-only.",
    )
    listing.add_argument("--head", required=True, help="Full commit sha (40 or 64 hex digits).")
    listing.add_argument("--gate", default=None, help="Only records of this gate.")
    return parser


def _show(args: argparse.Namespace, root: Path) -> int:
    if RECORD_ID_RE.match(args.record_id) is None:
        print(
            f"loadout-record: {args.record_id!r} is not a record id "
            "(expected YYYYMMDDTHHMMSSZ-<8 hex digits>)",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        record = load_record(root, args.record_id)
    except RecordNotFoundError as exc:
        print(f"loadout-record: {exc}", file=sys.stderr)
        return EXIT_NOT_FOUND
    except StoreError as exc:
        print(f"loadout-record: {exc}", file=sys.stderr)
        return EXIT_RECORD_INVALID
    print(json.dumps(record.as_dict()))
    return EXIT_OK


def _list(args: argparse.Namespace, root: Path) -> int:
    if not is_full_sha(args.head):
        print(
            f"loadout-record: --head must be a full lowercase commit sha (40 or 64 hex "
            f"digits), got {args.head!r}",
            file=sys.stderr,
        )
        return EXIT_USAGE
    try:
        found = records_at_head(root, args.head)
    except StoreError as exc:
        print(f"loadout-record: {exc}", file=sys.stderr)
        return EXIT_STORE_UNUSABLE
    records = [r for r in found.records if args.gate is None or r.gate == args.gate]
    print(
        json.dumps(
            {
                "root": str(root),
                "head_sha": args.head,
                "records": [r.as_dict() for r in records],
                "skipped": list(found.skipped),
            }
        )
    )
    return EXIT_OK


def main(argv: list[str] | None = None, *, config_root: str | Path | None = None) -> int:
    """CLI entrypoint. Returns the process exit code. *config_root* is for tests
    and embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    root = records_root(config_root=config_root)
    if args.command == "show":
        return _show(args, root)
    return _list(args, root)


if __name__ == "__main__":
    sys.exit(main())
