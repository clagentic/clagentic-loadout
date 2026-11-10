"""repo_test.verb — ``clagentic-loadout test``: run a repository's declared tests
and read the results by run id.

  clagentic-loadout test run full|impacted [--repo-root DIR] [--base REV] [--timeout S] [--detach]
  clagentic-loadout test run files PATH... [--repo-root DIR] [--timeout S] [--detach]
  clagentic-loadout test run tier NAME [--repo-root DIR] [--timeout S] [--detach]
  clagentic-loadout test report [RUN_ID|latest] [--repo R] [--sha S] [--wait SECONDS]
                                [--summary | --failing-ids | --failures | --diff BASE_RUN_ID]

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
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.repo_test import exits, report_cmd, store
from clagentic_loadout.repo_test.report_cmd import LATEST, ReportRefused
from clagentic_loadout.repo_test.detach import start_detached
from clagentic_loadout.repo_test.run_cmd import RunRefused, execute_run, prepare_run
from clagentic_loadout.repo_test.spec import MODE_TIER, RUN_MODES

_PROG = "clagentic-loadout test"

# How often `report --wait` re-reads the run.
_WAIT_POLL_SECONDS = 0.5

_EXIT_CODES_TEXT = (
    "Exit codes: 0 ok, 1 usage, 70 tests failed, 71 no valid profile, 72 profile does not "
    "declare the command (or base), 73 command could not run or left no JUnit report, "
    "74 run could not be recorded, 75 no reports root or no matching run, 76 bad or unknown "
    "run id, 77 no run for --sha, 78 refused (not the expected repository or commit, a repository "
    "to scope `latest` to is ambiguous, different repositories, unlike tiers, or a run without results), "
    "79 report: the run is still running (--wait timed out)."
)

_TOP_HELP = f"""{_PROG} -- run the tests a repository declares, and read the results by run id.

Usage: {_PROG} run full|impacted [--repo-root DIR] [--base REV] [--timeout SECONDS]
                                    [--expect-repo R] [--expect-head SHA] [--detach]
       {_PROG} run files PATH... [--repo-root DIR] [--timeout SECONDS] [--detach]
       {_PROG} run tier NAME [--repo-root DIR] [--timeout SECONDS] [--detach]
       {_PROG} report [RUN_ID|latest] [--repo R] [--sha S] [--wait SECONDS]
                                    [--summary | --failing-ids | --failures | --diff BASE_RUN_ID]
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
    run.add_argument(
        "--expect-repo",
        default=None,
        help="Refuse (exit 78) unless the tree to test is this repository: path, name or remote "
        "URL. Nothing is run or recorded on a mismatch.",
    )
    run.add_argument(
        "--expect-head",
        default=None,
        help="Refuse (exit 78) unless HEAD is this commit: full SHA or a prefix of 7 or more hex "
        "characters. Also refused when the head is unknown.",
    )
    run.add_argument(
        "--detach",
        action="store_true",
        help="Check the arguments and the tree as usual, then start the run in a separate "
        "session that outlives this command, print its run id as JSON and exit 0. Read it "
        "with `report RUN_ID`, optionally with --wait.",
    )
    return run


def _report_parser() -> argparse.ArgumentParser:
    report = _new_parser(
        "report",
        "Print a summary, the failing test ids, the failure messages, or a comparison of a recorded run. A run is "
        "chosen by run id, or `latest` (the default), optionally narrowed with --repo and "
        "--sha. Without --repo, `latest` is scoped to the git repository of the current directory, "
        "or refused when that is none and several repositories are on record. With a run id, "
        "--repo asserts the run's repository. --sha never falls back to the latest run. Read-only.",
    )
    report.add_argument("run_id", nargs="?", default=None, help="A run id, or `latest` (default).")
    report.add_argument(
        "--repo", default=None, help="Narrow `latest` to a repository, or assert the repository of a run id: path, name or remote URL."
    )
    report.add_argument(
        "--sha",
        default=None,
        help="Select the newest clean run for this commit (full SHA or a prefix of 7 or more).",
    )
    report.add_argument(
        "--wait",
        metavar="SECONDS",
        type=_seconds,
        default=None,
        help="Wait up to this many seconds for the run to end (complete, unrunnable or "
        "interrupted) before printing. Exit 79 when it is still running after that.",
    )
    shape = report.add_mutually_exclusive_group()
    shape.add_argument("--summary", action="store_true", help="Print the run summary (default).")
    shape.add_argument(
        "--failing-ids", action="store_true", help="Print the failing test ids, one per line."
    )
    shape.add_argument(
        "--failures",
        action="store_true",
        help="Print a JSON list with one record per failed test case, in report order: id, kind "
        "(failure or error), message and a text excerpt, each redacted and size-capped (a cut "
        "value ends with a marker stating the characters dropped). A run with no failed case "
        "prints an empty list. If the total cap is reached, a last record without an id gives "
        "omitted_cases.",
    )
    shape.add_argument(
        "--diff",
        metavar="BASE_RUN_ID",
        default=None,
        help="Print the failing ids only in this run, only in BASE_RUN_ID, and the shared "
        "count, as JSON. Refuses runs from different repositories.",
    )
    return report


def _seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not value >= 0:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds of zero or more")
    return value


def _print_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _describe_start(run_id: str, recorded: Mapping[str, Any], phase: str) -> str:
    repo = recorded.get("repo") if isinstance(recorded.get("repo"), dict) else {}
    dirty = recorded.get("dirty")
    return (
        f"{_PROG}: run {run_id} {phase}: repo={repo.get('name') or 'unknown'} "
        f"path={repo.get('path') or 'unknown'} head={recorded.get('head_sha') or 'unknown'} "
        f"dirty={'unknown' if dirty is None else str(dirty).lower()}"
    )


def _run(args: argparse.Namespace) -> int:
    paths, tier = list(args.paths), None
    if args.mode == MODE_TIER:
        if len(paths) != 1:
            print(f"{_PROG}: `tier` takes exactly one tier name, got {len(paths)}", file=sys.stderr)
            return exits.EXIT_USAGE
        tier, paths = paths[0], []
    if args.detach:
        return _run_detached(args, paths, tier)
    started: list[tuple[str, Mapping[str, Any]]] = []

    def announce(run_id: str, recorded: Mapping[str, Any]) -> None:
        started.append((run_id, recorded))
        print(_describe_start(run_id, recorded, "started"), file=sys.stderr, flush=True)

    try:
        outcome = execute_run(
            mode=args.mode,
            paths=paths,
            repo_root=args.repo_root,
            base=args.base,
            timeout_seconds=args.timeout,
            tier=tier,
            expect_repo=args.expect_repo,
            expect_head=args.expect_head,
            on_start=announce,
        )
    except RunRefused as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        if started:
            # A refusal after the start line (the result could not be recorded) still
            # closes the pair so a reader never sees a run that began and never ended.
            run_id, recorded = started[0]
            print(
                _describe_start(run_id, recorded, f"finished (refused, exit {exc.code})"),
                file=sys.stderr,
                flush=True,
            )
        return exc.code
    _print_json(outcome.summary)
    if outcome.failure_log_tail:
        print(f"{_PROG}: end of the command's output:\n{outcome.failure_log_tail}", file=sys.stderr)
    print(
        _describe_start(outcome.summary["run_id"], outcome.summary, f"finished ({outcome.summary['state']})"),
        file=sys.stderr,
    )
    return outcome.exit_code


def _run_detached(args: argparse.Namespace, paths: list[str], tier: str | None) -> int:
    try:
        prepared = prepare_run(
            mode=args.mode,
            paths=paths,
            repo_root=args.repo_root,
            base=args.base,
            timeout_seconds=args.timeout,
            tier=tier,
            expect_repo=args.expect_repo,
            expect_head=args.expect_head,
        )
        summary = start_detached(prepared)
    except RunRefused as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return exc.code
    _print_json(summary)
    print(_describe_start(summary["run_id"], summary, "started (detached)"), file=sys.stderr)
    return exits.EXIT_OK


def _scoping_cwd(args: argparse.Namespace) -> Path | None:
    """The working directory, read only where `latest` scoping uses it (no run id,
    no --repo). A directory that no longer exists or cannot be read counts as
    not inside a git work tree rather than aborting the report."""
    if args.run_id != LATEST or args.repo is not None:
        return None
    try:
        return Path.cwd()
    except OSError:
        return None


def _report(args: argparse.Namespace) -> int:
    root = store.reports_root()

    def note(text: str) -> None:
        print(f"{_PROG}: {text}", file=sys.stderr)

    deadline = None if args.wait is None else time.monotonic() + args.wait
    try:
        run = report_cmd.select_run(
            root,
            args.run_id,
            repo=args.repo,
            sha=args.sha,
            cwd=_scoping_cwd(args),
            note=note,
        )
        while deadline is not None and not report_cmd.is_terminal(run):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_WAIT_POLL_SECONDS, remaining))
            run = report_cmd.select_run(
                root, args.run_id, repo=args.repo, sha=args.sha, cwd=_scoping_cwd(args)
            )
        if args.diff is not None:
            _print_json(report_cmd.diff_of(run, report_cmd.load_base_run(root, args.diff, note=note)))
        elif args.failures:
            _print_json(report_cmd.failures_of(root, run))
        elif args.failing_ids:
            for test_id in report_cmd.failing_ids_of(run):
                print(test_id)
        else:
            _print_json(report_cmd.summary_of(run))
    except ReportRefused as exc:
        print(f"{_PROG}: {exc}", file=sys.stderr)
        return exc.code
    if args.wait is not None and not report_cmd.is_terminal(run):
        return exits.EXIT_STILL_RUNNING
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
