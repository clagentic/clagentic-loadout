"""repo_checks.verb — ``clagentic-loadout lint`` and ``clagentic-loadout build``:
run the commands a repository's profile declares, and read each run by id.

  clagentic-loadout lint  run [--repo-root DIR] [--only NAME]
  clagentic-loadout lint  report RUN_ID
  clagentic-loadout build run [--repo-root DIR] [--only NAME]
  clagentic-loadout build report RUN_ID

One code path serves both: ``main`` takes the profile block it runs (``lint`` or
``build``). The exit codes are in ``repo_checks.exits``; a command's own exit
code is recorded in the report and never returned. ``run`` prints the run id and
one line per check to stdout, and the end of a failing check's output to stderr;
``report`` prints the recorded run as JSON.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.push.verify_run import VerifyResult, format_output_tails
from clagentic_loadout.repo_checks import exits, store
from clagentic_loadout.repo_checks.run_cmd import RunRefused, execute_run

_COMMANDS = ("run", "report")

_EXIT_CODES_TEXT = (
    "Exit codes: 0 ok, 1 usage, 120 a check failed, timed out or could not start, 121 invalid "
    "repo profile or block, 122 the profile declares nothing to run, 123 the run could not be "
    "recorded, 124 bad, unknown or other-verb run id, 125 the recorded run cannot be read."
)


def _prog(block: str) -> str:
    return f"clagentic-loadout {block}"


def _top_help(block: str) -> str:
    prog = _prog(block)
    return f"""{prog} -- run the {block} commands a repository declares, and read the runs by run id.

Usage: {prog} run [--repo-root DIR] [--only NAME]
       {prog} report RUN_ID
       {prog} --help | --version

Commands:
  run     Run the profile's declared `{block}` entries in order, stopping at the first
          failure, and record the run.
  report  Print a recorded run as JSON (read-only).

Run `{prog} <command> --help` for a command's own arguments.
{_EXIT_CODES_TEXT}"""


def _new_parser(block: str, command: str, description: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_prog(block)} {command}", description=description, epilog=_EXIT_CODES_TEXT
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{_prog(block)} {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    return parser


def _run_parser(block: str) -> argparse.ArgumentParser:
    run = _new_parser(
        block,
        "run",
        f"Run the entries the repo profile declares under `{block}`, in order, each as an argument "
        "list with no shell, in the repository root. The run stops at the first entry that fails, "
        "times out or cannot start. Prints the run id and one line per entry.",
    )
    run.add_argument(
        "--repo-root",
        default=".",
        help="Directory holding the repo profile (default: the current directory). It need not be "
        "a git work tree.",
    )
    run.add_argument("--only", default=None, metavar="NAME", help="Run only the entry with this name.")
    return run


def _report_parser(block: str) -> argparse.ArgumentParser:
    report = _new_parser(
        block,
        "report",
        f"Print a recorded `{block}` run as JSON: run id, repository, head, dirty flag, start and "
        "finish times, and for each entry its command, exit code or timeout, and the redacted, "
        "size-capped end of its output. Read-only.",
    )
    report.add_argument("run_id", help=f"A run id, as printed by `{_prog(block)} run`.")
    return report


def _describe_start(block: str, run_id: str, recorded: Mapping[str, Any]) -> str:
    repo = recorded.get("repo") if isinstance(recorded.get("repo"), dict) else {}
    dirty = recorded.get("dirty")
    return (
        f"{_prog(block)}: run {run_id} started: repo={repo.get('name') or 'unknown'} "
        f"path={repo.get('path') or 'unknown'} head={recorded.get('head_sha') or 'unknown'} "
        f"dirty={'unknown' if dirty is None else str(dirty).lower()}"
    )


def _entry_line(result: VerifyResult) -> str:
    return f"  {result.name}: {'pass' if result.passed else 'FAIL'} ({result.status_label})"


def _run(args: argparse.Namespace, block: str) -> int:
    def announce(run_id: str, recorded: Mapping[str, Any]) -> None:
        print(f"{_prog(block)}: run {run_id}", flush=True)
        print(_describe_start(block, run_id, recorded), file=sys.stderr, flush=True)

    def entry_done(result: VerifyResult) -> None:
        print(_entry_line(result), flush=True)
        if not result.passed:
            tails = format_output_tails(result)
            if tails:
                print(f"{_prog(block)}: end of the output of {result.name!r}:\n{tails}", file=sys.stderr)

    try:
        outcome = execute_run(
            block=block,
            repo_root=args.repo_root,
            only=args.only,
            on_start=announce,
            on_result=entry_done,
        )
    except RunRefused as exc:
        print(f"{_prog(block)}: {exc}", file=sys.stderr)
        return exc.code
    skipped = f"; not run: {', '.join(outcome.not_run)}" if outcome.not_run else ""
    print(f"{_prog(block)}: run {outcome.run_id} {outcome.state}{skipped}")
    return outcome.exit_code


def _report(args: argparse.Namespace, block: str) -> int:
    try:
        report = store.load_report(store.reports_root(), args.run_id)
    except store.RunNotFound as exc:
        print(f"{_prog(block)}: {exc}", file=sys.stderr)
        return exits.EXIT_RUN_NOT_FOUND
    except store.ReportUnreadable as exc:
        print(f"{_prog(block)}: {exc}", file=sys.stderr)
        return exits.EXIT_REPORT_UNREADABLE
    if report.get("block") != block:
        print(
            f"{_prog(block)}: run {args.run_id} is a {report.get('block')!r} run, not a {block!r} run; "
            f"read it with `clagentic-loadout {report.get('block')} report`",
            file=sys.stderr,
        )
        return exits.EXIT_RUN_NOT_FOUND
    print(json.dumps(report, indent=2, sort_keys=True))
    return exits.EXIT_OK


def main(argv: list[str] | None = None, *, block: str) -> int:
    """CLI entrypoint for the verb of *block* (``lint`` or ``build``). Returns the
    process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    prog = _prog(block)
    if not argv or argv[0] in ("-h", "--help"):
        print(_top_help(block))
        return exits.EXIT_OK
    if argv[0] == "--version":
        print(f"{prog} {get_version()}")
        return exits.EXIT_OK
    command, rest = argv[0], argv[1:]
    if command not in _COMMANDS:
        print(
            f"{prog}: unknown command {command!r}. Known commands: {', '.join(_COMMANDS)}. "
            f"Run `{prog} --help` for details.",
            file=sys.stderr,
        )
        return exits.EXIT_USAGE
    parser = _run_parser(block) if command == "run" else _report_parser(block)
    try:
        args = parser.parse_args(rest)
    except SystemExit as exc:
        return exits.EXIT_OK if exc.code in (0, None) else exits.EXIT_USAGE
    return _run(args, block) if command == "run" else _report(args, block)
