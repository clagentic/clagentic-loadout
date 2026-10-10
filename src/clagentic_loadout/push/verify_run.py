"""push.verify_run -- execute a repo's declared verification commands and
render the outcome for a PR body.

Pure mechanism: takes already-loaded `VerifyEntry` values (see
`push.verify_config`) and a working directory, returns `VerifyResult`s. It
knows nothing about PRs, platforms, or the push verb's exit codes.

Every command runs as an argument list with no shell, stdin closed, in its
own process group so a timeout kills the whole tree (a build tool's children
included) rather than orphaning it. Captured output is reduced to a bounded
tail and passed through the push redaction choke point before it can reach a
PR body or an error message.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.bounded_capture import (
    TailBuffer,
    join_capture,
    start_tail_capture,
)
from clagentic_loadout.push.push_redaction import redact_push_secrets
from clagentic_loadout.push.verify_config import VerifyEntry

#: Characters of each captured stream kept in a result. The TAIL is kept:
#: the failing assertion or compiler error is at the end, not the start.
OUTPUT_TAIL_CHARS = 2000

#: Bytes retained per stream while the command runs: enough for
#: OUTPUT_TAIL_CHARS characters at the widest UTF-8 encoding.
_CAPTURE_BYTES = OUTPUT_TAIL_CHARS * 4

#: Seconds to wait for the pipe readers after the process group is killed.
_DRAIN_SECONDS = 5

#: Heading of the PR-body section; one constant so the writer and any reader
#: agree on the exact text.
VERIFICATION_SECTION_HEADING = "## Verification"

#: Delimiters of the stable block that holds the section, so a later update
#: finds and replaces it instead of stacking a second one.
SECTION_BEGIN_MARKER = "<!-- clagentic-loadout:verification:begin -->"
SECTION_END_MARKER = "<!-- clagentic-loadout:verification:end -->"

_SECTION_BLOCK_RE = re.compile(
    rf"^{re.escape(VERIFICATION_SECTION_HEADING)}\n{re.escape(SECTION_BEGIN_MARKER)}\n"
    rf".*?{re.escape(SECTION_END_MARKER)}\n?",
    re.DOTALL | re.MULTILINE,
)

#: Environment variables a verification child inherits from the pushing
#: process. Everything else (tokens, API keys, cloud credentials) is dropped
#: unless an entry names it in `env_passthrough`.
CHILD_ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "TMPDIR")
_CHILD_ENV_PREFIXES = ("LC_",)


def build_child_env(
    source: Mapping[str, str], passthrough: Iterable[str] = ()
) -> dict[str, str]:
    """The environment a verification command runs under: the allowlisted
    variables of *source* plus any explicitly named in *passthrough*."""
    extra = frozenset(passthrough)
    return {
        key: value
        for key, value in source.items()
        if key in CHILD_ENV_ALLOWLIST or key.startswith(_CHILD_ENV_PREFIXES) or key in extra
    }


@dataclass(frozen=True)
class VerifyResult:
    name: str
    argv: tuple[str, ...]
    exit_code: int | None  # None: never produced one (timeout or failed to start)
    timed_out: bool
    timeout_seconds: float
    stdout_tail: str
    stderr_tail: str
    start_error: str | None = None

    @property
    def passed(self) -> bool:
        return self.exit_code == 0 and not self.timed_out and self.start_error is None

    @property
    def status_label(self) -> str:
        if self.timed_out:
            return f"TIMEOUT after {self.timeout_seconds:g}s"
        if self.start_error is not None:
            return f"COULD NOT START ({self.start_error})"
        return f"exit {self.exit_code}"


class VerificationFailedError(Exception):
    """A declared verification command did not pass. Carries the failing
    result (and every result gathered up to and including it)."""

    def __init__(self, failed: VerifyResult, results: tuple[VerifyResult, ...]) -> None:
        self.failed = failed
        self.results = results
        super().__init__(
            f"verification check {failed.name!r} failed ({failed.status_label}); "
            f"the push was refused. Command: {describe_command(failed.argv)}.\n"
            f"{format_output_tails(failed)}"
        )


def describe_command(argv: tuple[str, ...]) -> str:
    """User-facing description of a command: the executable and an argument
    count, never the arguments themselves, which may carry tokens or paths."""
    hidden = len(argv) - 1
    shown = repr(argv[0]) if argv else "''"
    return shown if hidden <= 0 else f"{shown} (+{hidden} argument(s) not shown)"


def _tail(buffer: TailBuffer) -> str:
    text = redact_push_secrets(buffer.value().decode("utf-8", errors="replace")).strip()
    if buffer.dropped or len(text) > OUTPUT_TAIL_CHARS:
        return "...[truncated]\n" + text[-OUTPUT_TAIL_CHARS:]
    return text


def run_verify_entry(entry: VerifyEntry, cwd: Path) -> VerifyResult:
    """Run one entry in *cwd* and return its result. Never raises for a
    command-level failure; those are reported in the result."""
    try:
        proc = subprocess.Popen(
            list(entry.argv),
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            env=build_child_env(os.environ, entry.env_passthrough),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        return VerifyResult(
            name=entry.name, argv=entry.argv, exit_code=None, timed_out=False,
            timeout_seconds=entry.timeout_seconds, stdout_tail="", stderr_tail="",
            start_error=f"{type(exc).__name__}: {exc}",
        )

    out_buf, err_buf, readers = start_tail_capture(proc, _CAPTURE_BYTES)
    timed_out = False
    try:
        proc.wait(timeout=entry.timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_DRAIN_SECONDS)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise

    # The bounded join keeps a daemonised descendant that still holds a pipe
    # from hanging the push.
    join_capture(readers, _DRAIN_SECONDS)
    return VerifyResult(
        name=entry.name, argv=entry.argv,
        exit_code=None if timed_out else proc.returncode, timed_out=timed_out,
        timeout_seconds=entry.timeout_seconds,
        stdout_tail=_tail(out_buf), stderr_tail=_tail(err_buf),
    )


def run_verifications(entries: tuple[VerifyEntry, ...], cwd: Path) -> tuple[VerifyResult, ...]:
    """Run *entries* in order, stopping at the first failure.

    Raises VerificationFailedError on that failure. Stopping early is
    deliberate: later checks commonly assume earlier ones (an install before
    a build), and a refused push does not need the rest of the transcript.
    """
    results: list[VerifyResult] = []
    for entry in entries:
        result = run_verify_entry(entry, cwd)
        results.append(result)
        if not result.passed:
            raise VerificationFailedError(result, tuple(results))
    return tuple(results)


def format_output_tails(result: VerifyResult) -> str:
    parts = []
    if result.stdout_tail:
        parts.append(f"--- stdout (tail) ---\n{result.stdout_tail}")
    if result.stderr_tail:
        parts.append(f"--- stderr (tail) ---\n{result.stderr_tail}")
    return "\n".join(parts)


def _fence_for(text: str) -> str:
    """A backtick fence longer than any backtick run inside *text*, so
    captured output cannot close the block early and inject markdown."""
    longest = run = 0
    for ch in text:
        run = run + 1 if ch == "`" else 0
        longest = max(longest, run)
    return "`" * max(3, longest + 1)


def _inert(text: str, *, single_line: bool = False) -> str:
    """*text* made safe to render inside the marked block.

    Every field written into the block goes through here, so none of them can
    forge or close it. Breaking the HTML-comment opener (rather than removing
    the two known markers) leaves no input that can re-assemble a marker after
    a single pass. *single_line* also flattens line breaks so a one-line field
    cannot start a heading or a list item of its own.
    """
    if single_line:
        text = " ".join(text.split())
    return text.replace("<!--", "<! --")


def render_verification_section(
    results: tuple[VerifyResult, ...], tested_sha: str | None = None
) -> str:
    """Markdown `## Verification` section: one entry per check with its name,
    exit status, and bounded output tails. *tested_sha*, when known, is
    stamped as a `Tested commit:` line so a reader can tell which commit the
    recorded results describe."""
    lines = []
    if tested_sha:
        lines.extend([f"Tested commit: {_inert(tested_sha, single_line=True)}", ""])
    for result in results:
        mark = "PASS" if result.passed else "FAIL"
        name = _inert(result.name, single_line=True)
        status = _inert(result.status_label, single_line=True)
        lines.append(f"- **{name}**: {mark} ({status})")
        tails = _inert(format_output_tails(result))
        if tails:
            fence = _fence_for(tails)
            lines.extend(["", f"{fence}text", tails, fence, ""])
    return _wrap_section("\n".join(lines).rstrip())


def render_skipped_section(entries: tuple[VerifyEntry, ...]) -> str:
    """Section recording an explicit --skip-verify, so a skipped
    verification is visible to every reader of the PR, never silent."""
    names = ", ".join(_inert(e.name, single_line=True) for e in entries)
    return _wrap_section(f"- **SKIPPED** via --skip-verify; declared checks NOT run: {names}")


def _wrap_section(content: str) -> str:
    return (
        f"{VERIFICATION_SECTION_HEADING}\n{SECTION_BEGIN_MARKER}\n\n"
        f"{content}\n{SECTION_END_MARKER}\n"
    )


def append_section(body: str, section: str) -> str:
    """*body* with *section* in it exactly once: an existing verification
    block is replaced where it stands, otherwise *section* is appended."""
    existing = _SECTION_BLOCK_RE.search(body)
    if existing is not None:
        return body[: existing.start()] + section + body[existing.end():]
    separator = "\n" if body.endswith("\n") else "\n\n"
    return f"{body}{separator}{section}"


def append_to_existing(current: str, addition: str, *, separator: str) -> str:
    """Join *addition* onto an existing PR body *current* (the --append-body
    path). A verification block inside *addition* replaces the one already in
    *current* rather than stacking beside it; the rest of *addition* is
    appended as ordinary text."""
    block = _SECTION_BLOCK_RE.search(addition)
    if block is None:
        return f"{current}{separator}{addition}" if current else addition
    rest = (addition[: block.start()] + addition[block.end():]).strip()
    joined = f"{current}{separator}{rest}" if current and rest else (current or rest)
    return append_section(joined, block.group(0)) if joined else block.group(0)


__all__ = [
    "OUTPUT_TAIL_CHARS",
    "VERIFICATION_SECTION_HEADING",
    "VerificationFailedError",
    "VerifyResult",
    "append_section",
    "append_to_existing",
    "build_child_env",
    "describe_command",
    "format_output_tails",
    "render_skipped_section",
    "render_verification_section",
    "run_verifications",
    "run_verify_entry",
]
