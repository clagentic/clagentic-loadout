"""cli.py — standalone console-script entry points for the wait primitives.

Thin argv-parsing wrappers around clagentic_loadout.wait.poll.poll_wait and
clagentic_loadout.wait.scoped_test.scoped_test_wait, registered as
console_scripts in pyproject.toml (`loadout-poll-wait`, `loadout-scoped-test-
wait`). Kept separate from clagentic_loadout.cli — the wait verbs are not yet
wired into a general subcommand-dispatch framework (deferred to the
release-readiness phase per tome #688 Wave A scope); these are independent
entry points, matching how the source primitives shipped as standalone
scripts.

Exit codes (documented behavior, unchanged from the source primitives):
    poll-wait:         0 condition met, 1 usage error, 2 timeout expired
    scoped-test-wait:  0 command exited 0, 1 usage error / not-a-scoped-
                        command, 124 timeout expired, other = the command's
                        own exit code, propagated unchanged
"""

from __future__ import annotations

import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.wait.config import (
    InvalidScopedTestConfigError,
    load_scoped_test_patterns,
)
from clagentic_loadout.wait.poll import (
    DEFAULT_INTERVAL,
    DEFAULT_MIN_LINES,
    DEFAULT_TIMEOUT as POLL_DEFAULT_TIMEOUT,
    InvalidGrepPatternError,
    poll_wait,
)
from clagentic_loadout.wait.scoped_test import (
    DEFAULT_TIMEOUT as SCOPED_TEST_DEFAULT_TIMEOUT,
    NotScopedTestCommandError,
    scoped_test_wait,
)

_EXIT_OK = 0
_EXIT_USAGE = 1
_EXIT_TIMEOUT = 2
_EXIT_SCOPED_TEST_TIMEOUT = 124

_POLL_WAIT_USAGE = (
    "Usage: poll-wait --file PATH [--min-lines N] [--grep PATTERN] "
    "[--timeout SECONDS] [--interval SECONDS]"
)
_SCOPED_TEST_WAIT_USAGE = (
    'Usage: scoped-test-wait --cmd "<scoped-test-command>" --output-file PATH '
    "[--timeout SECONDS] [--cwd PATH] [--repo-root PATH]"
)


def _die(prog: str, msg: str, code: int = _EXIT_USAGE) -> None:
    print(f"{prog}: {msg}", file=sys.stderr)
    sys.exit(code)


def _handle_help_or_version(prog: str, usage: str, argv: list[str]) -> None:
    """Shared --help/--version short-circuit for the hand-rolled argv loops
    below (these two entry points predate argparse adoption and parse argv
    manually — see the module docstring — so --help/--version get the same
    manual treatment rather than a partial argparse migration for two flags)."""
    if any(arg in ("--help", "-h") for arg in argv):
        print(usage)
        sys.exit(_EXIT_OK)
    if "--version" in argv:
        print(f"{prog} {get_version()}")
        sys.exit(_EXIT_OK)


def poll_wait_main(argv: list[str] | None = None) -> None:
    """Entry point for the `loadout-poll-wait` console script.

    Usage: loadout-poll-wait --file PATH [--min-lines N] [--grep PATTERN]
                              [--timeout SECONDS] [--interval SECONDS]
    """
    if argv is None:
        argv = sys.argv[1:]
    prog = "poll-wait"
    _handle_help_or_version(prog, _POLL_WAIT_USAGE, argv)

    file_path: str | None = None
    min_lines = DEFAULT_MIN_LINES
    grep_pattern: str | None = None
    timeout = POLL_DEFAULT_TIMEOUT
    interval = DEFAULT_INTERVAL

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--file" and i + 1 < len(argv):
            file_path = argv[i + 1]
            i += 2
        elif arg == "--min-lines" and i + 1 < len(argv):
            try:
                min_lines = int(argv[i + 1])
            except ValueError:
                _die(prog, f"--min-lines must be an integer, got {argv[i + 1]!r}.")
            i += 2
        elif arg == "--grep" and i + 1 < len(argv):
            grep_pattern = argv[i + 1]
            i += 2
        elif arg == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                _die(prog, f"--timeout must be a number, got {argv[i + 1]!r}.")
            i += 2
        elif arg == "--interval" and i + 1 < len(argv):
            try:
                interval = float(argv[i + 1])
            except ValueError:
                _die(prog, f"--interval must be a number, got {argv[i + 1]!r}.")
            i += 2
        else:
            _die(
                prog,
                f"unknown argument: {arg!r}. Usage: {prog} --file PATH "
                "[--min-lines N] [--grep PATTERN] [--timeout SECONDS] "
                "[--interval SECONDS]",
            )

    if file_path is None:
        _die(prog, "--file PATH is required.")

    try:
        result = poll_wait(
            file_path,
            min_lines=min_lines,
            grep_pattern=grep_pattern,
            timeout=timeout,
            interval=interval,
        )
    except (InvalidGrepPatternError, ValueError) as exc:
        _die(prog, str(exc))
        return

    if result.met:
        sys.exit(0)
    print(
        f"{prog}: timeout after {timeout}s — file {file_path!r} did not meet "
        f"condition (min_lines={min_lines}, grep={grep_pattern!r}).",
        file=sys.stderr,
    )
    sys.exit(_EXIT_TIMEOUT)


def scoped_test_wait_main(argv: list[str] | None = None) -> None:
    """Entry point for the `loadout-scoped-test-wait` console script.

    Usage: loadout-scoped-test-wait --cmd "<scoped-test-command>"
               --output-file PATH [--timeout SECONDS] [--cwd PATH]
               [--repo-root PATH]

    --repo-root PATH: repo root to load the per-repo scoped-test pattern
        config from (.clagentic/loadout/config.yaml, wait: section).
        Defaults to --cwd, or the current directory when --cwd is also
        omitted.
    """
    if argv is None:
        argv = sys.argv[1:]
    prog = "scoped-test-wait"
    _handle_help_or_version(prog, _SCOPED_TEST_WAIT_USAGE, argv)

    cmd: str | None = None
    output_file: str | None = None
    timeout = SCOPED_TEST_DEFAULT_TIMEOUT
    cwd: str | None = None
    repo_root: str | None = None

    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--cmd" and i + 1 < len(argv):
            cmd = argv[i + 1]
            i += 2
        elif arg == "--output-file" and i + 1 < len(argv):
            output_file = argv[i + 1]
            i += 2
        elif arg == "--timeout" and i + 1 < len(argv):
            try:
                timeout = float(argv[i + 1])
            except ValueError:
                _die(prog, f"--timeout must be a number, got {argv[i + 1]!r}.")
            i += 2
        elif arg == "--cwd" and i + 1 < len(argv):
            cwd = argv[i + 1]
            i += 2
        elif arg == "--repo-root" and i + 1 < len(argv):
            repo_root = argv[i + 1]
            i += 2
        else:
            _die(
                prog,
                f"unknown argument: {arg!r}. Usage: {prog} --cmd "
                '"<scoped-test-command>" --output-file PATH '
                "[--timeout SECONDS] [--cwd PATH] [--repo-root PATH]",
            )

    if cmd is None:
        _die(prog, "--cmd is required.")
    if output_file is None:
        _die(prog, "--output-file is required.")

    effective_root = repo_root or cwd
    try:
        config = load_scoped_test_patterns(
            Path(effective_root) if effective_root else None
        )
    except InvalidScopedTestConfigError as exc:
        _die(prog, str(exc))
        return

    try:
        result = scoped_test_wait(
            cmd,
            output_file,
            timeout=timeout,
            cwd=cwd,
            patterns=config.patterns,
        )
    except NotScopedTestCommandError as exc:
        _die(prog, str(exc))
        return
    except ValueError as exc:
        _die(prog, f"--cmd could not be parsed as a shell command: {exc}.")
        return
    except OSError as exc:
        _die(prog, f"failed to run {cmd!r}: {exc}.")
        return

    if result.timed_out:
        print(
            f"{prog}: timeout after {timeout}s running {cmd!r}; process killed. "
            f"Partial output in {output_file!r}.",
            file=sys.stderr,
        )
        sys.exit(_EXIT_SCOPED_TEST_TIMEOUT)

    sys.exit(result.returncode)
