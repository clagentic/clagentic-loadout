"""scoped_test.py — sanctioned long-running scoped-test wait primitive.

Ported from the reference implementation's scoped-test-wait (Wave A slice 4,
tome #688). The source copy stays primary until that project's separate
CUT OVER + RETIRE + VERIFY-GONE task for this slice.

BACKGROUND: running a non-trivial test suite in the foreground can block an
agent's tool-call budget for the full test duration; backgrounding it with
shell `&` avoids that but then requires polling primitives (bare `sleep`,
`ps aux`, `until` loops) that a restrictive Bash allowlist may not admit.

This module runs the scoped command itself, in the foreground, as a single
subprocess with a bounded wall-clock timeout, streaming combined
stdout+stderr to an output file — the caller issues one call that blocks
until the command finishes or the timeout expires, with no backgrounding
and no polling loop required at all.

This is NOT a general command runner. `cmd` is validated against a
caller-supplied (or default) scoped build/test/lint pattern set — see
`clagentic_loadout.wait.config` — before it is ever executed. Execution is
subprocess.run() with an argv list (never shell=True), so the executed
command cannot smuggle a second command via `;`, `&&`, `|`, or backticks —
those characters are inert argv-list data, not shell syntax, in this path.

Public surface:
    scoped_test_wait(cmd, output_file, *, timeout=1800.0, cwd=None,
                      patterns=None) -> ScopedTestWaitResult

Raises NotScopedTestCommandError when `cmd` does not match the allowed
pattern set — callers translate that into their own exit-code convention
(see cli.py).
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.wait.config import is_scoped_test_command

DEFAULT_TIMEOUT = 1800.0  # 30 minutes


class NotScopedTestCommandError(ValueError):
    """Raised when `cmd` does not match any pattern in the scoped-test
    allowlist. This is an admission-boundary refusal, not a runtime error —
    the command is never executed."""


@dataclass(frozen=True)
class ScopedTestWaitResult:
    """Outcome of a scoped_test_wait() call.

    timed_out: True when the subprocess was killed after `timeout` seconds
        (returncode is None in that case).
    returncode: the command's own exit code, or None on timeout.
    output_file: path the combined stdout+stderr was streamed to.
    """

    timed_out: bool
    returncode: int | None
    output_file: str


def scoped_test_wait(
    cmd: str,
    output_file: str,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    cwd: str | None = None,
    patterns: tuple[re.Pattern[str], ...] | list[re.Pattern[str]] | list[str] | None = None,
) -> ScopedTestWaitResult:
    """Run one scoped build/test/lint command to completion, streaming
    output to a file.

    Args:
        cmd: the scoped command to run, e.g. "python3 -m pytest tests/ -v".
            Must match one of `patterns` (or the config-loaded default set)
            or NotScopedTestCommandError is raised without executing it.
        output_file: path to stream combined stdout+stderr to. Parent
            directories are created if needed.
        timeout: maximum wall-clock seconds to allow the command to run.
            Defaults to 1800 (30 minutes). On timeout the subprocess is
            killed and the result reports timed_out=True.
        cwd: working directory to run the command in. Defaults to the
            current directory.
        patterns: the allowed scoped-test pattern set to validate `cmd`
            against. When None, uses the built-in default set
            (clagentic_loadout.wait.config.DEFAULT_SCOPED_TEST_PATTERNS).
            Callers that have loaded per-repo config via
            load_scoped_test_patterns() pass its `.patterns` here.

    Returns:
        ScopedTestWaitResult describing how the command finished.

    Raises:
        NotScopedTestCommandError: `cmd` does not match the allowed pattern
            set — the command is never executed.
        ValueError: `cmd` could not be parsed as a shell command (shlex).
        OSError: the command could not be launched, or output_file's parent
            directory could not be created.
    """
    if not is_scoped_test_command(cmd, patterns):
        raise NotScopedTestCommandError(
            f"{cmd!r} is not a sanctioned scoped build/test/lint command. "
            "scoped_test_wait only runs commands matching the configured "
            "scoped-test pattern set (go build/test/vet/fmt, python3 -m "
            "pytest/py_compile/ruff/flake8/mypy/unittest/build/venv, bare "
            "pytest/ruff/flake8/mypy, make build/test/lint/vet/fmt/check, "
            "npm test/run/ci, sh scripts/smoke.sh by default). "
            "It is not a general command runner."
        )

    argv = shlex.split(cmd)

    out_path = Path(output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8") as fh:
        try:
            result = subprocess.run(
                argv,
                stdout=fh,
                stderr=subprocess.STDOUT,
                cwd=cwd,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ScopedTestWaitResult(
                timed_out=True, returncode=None, output_file=output_file
            )

    return ScopedTestWaitResult(
        timed_out=False, returncode=result.returncode, output_file=output_file
    )
