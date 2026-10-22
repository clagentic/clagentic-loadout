"""repo_test.verb — ``clagentic-loadout test``: run a repository's declared tests
and read the results by run id.

  clagentic-loadout test run full|impacted [--repo-root DIR] [--base REV] [--timeout S]
  clagentic-loadout test run files PATH... [--repo-root DIR] [--timeout S]
  clagentic-loadout test run tier NAME [--repo-root DIR] [--timeout S]
  clagentic-loadout test report [RUN_ID|latest] [--repo R] [--sha S]
                                [--summary | --failing-ids | --diff BASE_RUN_ID]

The exit codes are in ``repo_test.exits``; a test command's own exit code is
recorded in the run and never returned. Both ``run`` and ``report`` print JSON
(``--failing-ids`` prints one id per line); diagnostics go to stderr.

Each subcommand has its own parser so options and positionals may be given in any
order (``run files --repo-root DIR a.py``), which a shared parser with
subcommands cannot do for an optional positional. Everything after a literal
``--`` is positional, so a path that starts with ``-`` can still be named (and is
then refused by ``run`` with the path in the message).
"""

from __future__ import annotations

import argparse
import json
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.repo_test import exits, report_cmd, store
from clagentic_loadout.repo_test.report_cmd import LATEST, ReportRefused
from clagentic_loadout.repo_test.run_cmd import RunRefused, execute_run
from clagentic_loadout.repo_test.spec import MODE_TIER, RUN_MODES

_PROG = "clagentic-loadout test"

_EXIT_CODES_TEXT = (
    "Exit codes: 0 ok, 1 usage, 70 tests failed, 71 no valid profile, 72 profile does not "
    "declare the command (or base), 73 command could not run or left no JUnit report, "
    "74 run could not be recorded, 75 no reports root or no matching run, 76 bad or unknown "
    "run id, 77 no run for --sha, 78 report refused (different repositories, unlike tiers, or a run "
    "without results)."
)

_TOP_HELP = f"""{_PROG} -- run the tests a repository declares, and read the results by run id.

Usage: {_PROG} run full|impacted [--repo-root DIR] [--base REV] [--timeout SECONDS]
       {_PROG} run files PATH... [--repo-root DIR] [--timeout SECONDS]
       {_PROG} run tier NAME [--repo-root DIR] [--timeout SECONDS]
       {_PROG} report [RUN_ID|latest] [--repo R] [--sha S]
                                    [--summary | --failing-ids | --diff BASE_RUN_ID]
       {_PROG} --help | --version

Commands:
  run     Run the profile's declared tests and record the run.
  report  Read a recorded run by id (read-only).

Run `{_PROG} <command> --help` for a command's own arguments.
{_EXIT_CODES_TEXT}"""


def _new_parser(command: str, help_text: str) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=f"{_PROG} {command}",
        description=help_text,
        epilog=_EXIT_CODES_TEXT,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{_PROG} {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    return parser


def _run_parser() -> argparse.ArgumentParser:
    run = _new_parser(
        "run",
        "Run the command the repo profile declares as test.<mode>, record the run, and print "
        "its summary as JSON. A non-zero command exit counts as a failed run.",
    )
    run.add_argument("mode", choices=RUN_MODES, help="Which declared command to run.")
    run.add_argument("paths", nargs="*", help="Test files for `files`; the tier name for `tier`.")
    run.add_argument(
        "--repo-root",
        default=".",
        help="Directory holding the repo profile (default: the current directory). It need "
        "not be a git work tree.",
    )
    run.add_argument(
        "--base",
        default=None,
        help="Base revision: substituted for {base} in `impacted`, defaulting to test.base "
        "from the profile; recorded with the run.",
    )
    run.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Seconds after which the command and its child processes are killed.",
    )
    return run


def _report_parser() -> argparse.ArgumentParser:
    report = _new_parser(
        "report",
        "Print a summary, the failing test ids, or a comparison of a recorded run. A run is "
        "chosen by run id, or `latest` (the default), optionally narrowed with --repo and "
        "--sha. --sha never falls back to the latest run. Read-only.",
    )
    report.add_argument("run_id", nargs="?", default=None, help="A run id, or `latest` (default).")
    report.add_argument(
        "--repo", default=None, help="Narrow `latest` to a repository: path, name or remote URL."
    )
    report.add_argument(
        "--sha",
        default=None,
        help="Select the newest clean run for this commit (full SHA or a prefix of 7 or more).",
    )
    shape = report.add_mutually_exclusive_group()
    shape.add_argument("--summary", action="store_true", help="Print the run summary (default).")
    shape.add_argument(
        "--failing-ids", action="store_true", help="Print the failing test ids, one per line."
    )
    shape.add_argument(
        "--diff",
        metavar="BASE_RUN_ID",
        default=None,
        help="Print the failing ids only in this run, only in BASE_RUN_ID, and the shared "
        "count, as JSON. Refuses runs from different repositories.",
    )
    return report


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _run(args: argparse.Namespace) -> int:
    paths, tier = list(args.paths), None
    if args.mode == MODE_TIER:
        if len(paths) != 1:
            print(f"{_PROG}: `tier` takes exactly one tier name, got {len(paths)}", file=sys.stderr)
            return exits.EXIT_USAGE
        tier, paths = paths[0], []
    try:
        outcome = execute_run(
            mode=args.mode,
            paths=paths,
            repo_root=args.repo_root,
            base=args.base,
            timeout_seconds=args.timeout,
            tier=tier,
        )
    except RunRefused as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return exc.code
    _print_json(outcome.summary)
    if outcome.failure_log_tail:
        print(f"{_PROG}: end of the command's output:\n{outcome.failure_log_tail}", file=sys.stderr)
    return outcome.exit_code


def _report(args: argparse.Namespace) -> int:
    root = store.reports_root()
    try:
        run = report_cmd.select_run(root, args.run_id, repo=args.repo, sha=args.sha)
        if args.diff is not None:
            _print_json(report_cmd.diff_of(run, report_cmd.load_base_run(root, args.diff)))
        elif args.failing_ids:
            for test_id in report_cmd.failing_ids_of(run):
                print(test_id)
        else:
            _print_json(report_cmd.summary_of(run))
    except ReportRefused as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return exc.code
    return exits.EXIT_OK


def _parse(parser: argparse.ArgumentParser, argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Parse *argv* with options and positionals in any order. Tokens after a
    literal ``--`` are returned separately, untouched, as positionals."""
    if "--" in argv:
        split = argv.index("--")
        argv, after = argv[:split], argv[split + 1 :]
    else:
        after = []
    return parser.parse_intermixed_args(argv), after


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(_TOP_HELP)
        return exits.EXIT_OK
    if argv[0] == "--version":
        print(f"{_PROG} {get_version()}")
        return exits.EXIT_OK
    command, rest = argv[0], argv[1:]
    builders = {"run": _run_parser, "report": _report_parser}
    if command not in builders:
        print(
            f"{_PROG}: unknown command {command!r}. Known commands: {', '.join(builders)}. "
            f"Run `{_PROG} --help` for details.",
            file=sys.stderr,
        )
        return exits.EXIT_USAGE
    parser = builders[command]()
    try:
        args, after = _parse(parser, rest)
        if command == "run":
            args.paths = [*args.paths, *after]
        elif command == "report":
            if after and (len(after) > 1 or args.run_id is not None):
                parser.error(f"unexpected arguments after '--': {' '.join(after)}")
            args.run_id = after[0] if after else (args.run_id or LATEST)
        elif after:
            parser.error(f"unexpected arguments after '--': {' '.join(after)}")
    except SystemExit as exc:
        return exits.EXIT_OK if exc.code in (0, None) else exits.EXIT_USAGE
    if command == "run":
        return _run(args)
    return _report(args)


if __name__ == "__main__":
    sys.exit(main())
