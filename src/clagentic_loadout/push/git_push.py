"""push.git_push — token-safe `git push` via GIT_ASKPASS + HOME isolation.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push
transport's token-injection strategy (git_push_with_token, lr-350c/lr-82df)
— this is a SECURITY POSTURE the task explicitly requires preserved
verbatim, not identity to strip:

  - Token is written to a temp file (mode 0600), never placed on argv or in
    the remote URL.
  - GIT_ASKPASS points at a tiny generated shell script that reads the token
    from that file — git calls the script once per credential prompt; the
    token never appears in a log line git itself emits.
  - HOME is isolated to an empty per-call temp dir for the push subprocess.
    GIT_ASKPASS does NOT take precedence over ~/.netrc or
    ~/.git-credentials for http(s) remotes — libcurl resolves those BEFORE
    GIT_ASKPASS is ever consulted. An ambient credential under the real HOME
    (mapping the target host to a different identity) would silently win
    over the configured token. Isolating HOME to an empty dir removes every
    ambient credential source, making GIT_ASKPASS the sole credential input.
  - The whole token-carrying temp dir is removed in a finally block.

No token value is ever included in a raised exception message — only git's
own stderr (which git-credential does not populate with the token) and,
optionally, a platform-mismatch hint string built from static labels.

Push-failure observability (ported for parity from the reference push
transport's lr-a1fd4d / lr-8460a4 fixes): a bare client-side summary line
("error: failed to push some refs") is undiagnosable on its own. On
failure this module additionally:
  - extracts and surfaces "remote: "-prefixed lines verbatim (the actual
    pre-receive/hook rejection reason a server sends back);
  - detects and surfaces a LOCAL .git/hooks/pre-push abort distinctly from
    a remote rejection or a transport failure — a local hook exits
    non-network, prints its own unprefixed message, and previously
    collapsed to the same undiagnosable "unknown" bucket as a dead
    connection;
  - classifies the failure into a coarse sub_cause label (pre-receive-
    rejected / local-hook-rejected / non-fast-forward / transport /
    unknown) folded into the raised message text; and
  - honors an opt-in GIT_TRACE passthrough (see _GIT_TRACE_ENV_VAR) so
    transport vs. local-hook vs. remote-hook phase is distinguishable at
    the source when the coarse classification isn't enough.

None of this ever surfaces the token: remote/hook text is the server's or
the local repo's own message, never anything git-credential populates.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from clagentic_loadout.push.errors import GitPushError

#: git-transport auth failure substrings that plausibly indicate a
#: platform/credential mismatch rather than a dead credential.
_AUTH_MISMATCH_MARKERS = ("invalid username or token", "authentication failed", "403")

#: Max characters of the raw git stderr to include in the failure message
#: when no "remote: " lines were found. Kept generous — this is the
#: client-side-only fallback path, not the common case.
_PUSH_ERROR_STDERR_LIMIT = 800

#: Max characters of the extracted remote-side message to include. Remote
#: lines carry the actual rejection reason (pre-receive hook output, policy
#: gate text) and are never secret — the server emits its own text.
_PUSH_ERROR_REMOTE_LIMIT = 800

#: Max characters of the extracted local pre-push hook message to include.
#: Hook output is the LOCAL repo's own script text (e.g. a docs-staleness
#: gate message) — never secret, same non-secrecy rationale as
#: _PUSH_ERROR_REMOTE_LIMIT above.
_PUSH_ERROR_LOCAL_HOOK_LIMIT = 800

#: Env var name for the opt-in GIT_TRACE passthrough. When set to a truthy
#: value, git's own trace output (packet negotiation, hook invocation,
#: transport phase) is captured into the push subprocess's stderr so the
#: failure message can show, at the source, which phase (local hook /
#: transport / remote) the push actually reached — without depending on
#: server-side logs. Off by default: GIT_TRACE output is verbose and not
#: needed for the common case where remote/local-hook extraction below
#: already names the cause.
_GIT_TRACE_ENV_VAR = "CLAGENTIC_LOADOUT_PUSH_GIT_TRACE"

#: The exact client-side summary line git prints on any push rejection,
#: regardless of cause. Used as the anchor for _extract_local_hook_lines:
#: any non-blank stderr line ABOVE this summary, when there are no
#: "remote: " lines, is the local pre-push hook's own output (the hook runs
#: client-side, before any network I/O, and git does not prefix its output
#: at all — unlike a rejecting remote, which git DOES prefix with "remote:").
_PUSH_FAILURE_SUMMARY_MARKER = "error: failed to push some refs"

#: Prefixes of git's OWN client-side status lines that can legitimately
#: precede the summary line without a hook being involved — e.g. a plain
#: non-fast-forward rejection prints "To <url>" then
#: " ! [rejected]  <refspec> (non-fast-forward)" before the generic summary.
#: These must NOT be misattributed to a local pre-push hook: a hook's own
#: output is never one of git's own fixed status-line shapes. Matched by
#: prefix so refspec/URL text after the marker doesn't defeat the match.
_GIT_OWN_STATUS_LINE_PREFIXES = ("To ", "! [", "!  [")


def _extract_remote_lines(stderr: str) -> list[str]:
    """Extract the remote-side lines from git push stderr.

    git prefixes every line the remote (receive-pack / pre-receive hook)
    sends back with "remote: " — these lines carry the ACTUAL rejection
    reason (pre-receive hook policy text, e.g.) and are otherwise dropped
    when the caller only surfaces the generic client-side summary line
    ("error: failed to push some refs to <url>").

    Blank "remote:" separator lines (git emits a bare "remote: " between
    hook sections) are dropped; only lines carrying real content survive.

    Returns an empty list when stderr contains no "remote: " lines (e.g. a
    transport-level failure that never reached the server).
    """
    lines = []
    for line in stderr.splitlines():
        stripped = line.strip()
        if stripped.startswith("remote:"):
            content = stripped[len("remote:") :].strip()
            if content:
                lines.append(content)
    return lines


def _is_git_own_status_line(stripped_line: str) -> bool:
    """True when *stripped_line* is one of git's own fixed client-side
    status lines (push destination / per-ref rejection annotation), not
    hook output."""
    return stripped_line.startswith(_GIT_OWN_STATUS_LINE_PREFIXES)


def _extract_local_hook_lines(stderr: str) -> list[str]:
    """Extract a local pre-push hook's own stdout/stderr lines from git
    push stderr.

    A client-side ``.git/hooks/pre-push`` script runs BEFORE any network
    negotiation. When it exits non-zero, git aborts the push and prints:
      1. the hook's own stdout/stderr, completely unprefixed (git does not
         add "remote: " or any other marker to hook output — that prefix
         is reserved for text the REMOTE sends back over the wire, e.g. a
         server-side pre-receive hook).
      2. git's own generic summary line, always the same regardless of
         cause: "error: failed to push some refs to '<url>'".

    A PLAIN non-fast-forward (or other client-side-only) rejection ALSO has
    no "remote: " lines and ALSO ends in that same generic summary line —
    but its preceding lines are git's own fixed status-line shapes ("To
    <url>", " ! [rejected] ..."), never arbitrary hook text. Those lines
    are excluded via _is_git_own_status_line so a plain non-fast-forward is
    not misclassified as a hook rejection.

    Returns the hook's own content (in order), or an empty list when:
      - stderr contains "remote: " lines (this is a remote-side rejection,
        not a local one — see _extract_remote_lines);
      - the summary marker itself is absent (e.g. a "fatal: unable to
        access..." transport error that never even reaches the point of
        printing git's generic summary line — there is no summary line to
        anchor against, so nothing is attributable to a hook); or
      - every line before the summary is one of git's own fixed status
        lines, or there is no content at all before the summary (a bare
        transport or non-fast-forward failure with nothing to attribute to
        a hook).
    """
    if _extract_remote_lines(stderr):
        return []
    lines = stderr.splitlines()
    hook_lines: list[str] = []
    saw_summary = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(_PUSH_FAILURE_SUMMARY_MARKER):
            saw_summary = True
            break
        if stripped and not _is_git_own_status_line(stripped):
            hook_lines.append(stripped)
    if not saw_summary:
        return []
    return hook_lines


def _classify_push_failure(stderr: str) -> str:
    """Classify a failed git push into a coarse sub-cause.

    Returns one of:
        "pre-receive-rejected"  — remote sent lines back (hook/policy output)
        "local-hook-rejected"   — a LOCAL pre-push hook aborted the push
                                  before any network negotiation
        "non-fast-forward"      — client-side rejection, no remote response
        "transport"             — connection-level failure (could not reach host)
        "unknown"               — none of the above patterns matched

    Ordering matters: local-hook-rejected is checked before non-fast-forward
    and transport, because a local pre-push hook can print text that
    happens to contain those substrings incidentally (e.g. a hook that
    quotes a diagnostic mentioning "connection"). The presence of
    unprefixed content ahead of git's own summary line is a stronger, more
    specific signal than a substring match, so it takes precedence.

    This is a best-effort classification for the failure message; callers
    should still surface the raw (and remote-extracted / hook-extracted)
    text rather than relying on the label alone.
    """
    lower = stderr.lower()
    if _extract_remote_lines(stderr):
        return "pre-receive-rejected"
    if _extract_local_hook_lines(stderr):
        return "local-hook-rejected"
    if "non-fast-forward" in lower or "fetch first" in lower:
        return "non-fast-forward"
    if (
        "could not resolve host" in lower
        or "could not connect" in lower
        or "connection timed out" in lower
        or "connection refused" in lower
    ):
        return "transport"
    return "unknown"


def make_askpass_script(token_path: str) -> str:
    """Build the GIT_ASKPASS shell script text.

    The script reads the token from *token_path* at call time rather than
    embedding the value inline, so the token is never present in the script
    file's own text, in process argv, or in any human-readable env dump of
    the parent process. git invokes the script once per credential prompt,
    passing the prompt string as $1: "Username" prompts get a fixed
    placeholder username, "Password" prompts get the token file's contents.

    PUBLIC (not underscore-prefixed): this is the single source of truth for
    GIT_ASKPASS-script construction across this package's token-injected git
    pushes — any caller pushing an explicit refspec via a token (rather than
    a bare local branch push) reuses this primitive instead of a second
    private copy.
    """
    return (
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  *Username*) echo x-access-token ;;\n"
        f'  *Password*) cat "{token_path}" ;;\n'
        "  *) echo '' ;;\n"
        "esac\n"
    )


def git_push_with_token(
    remote: str,
    branch: str,
    token: str,
    git_cwd: Path | None = None,
    *,
    force_with_lease: bool = False,
    platform: str | None = None,
    other_platform_label: str | None = None,
) -> None:
    """Push *branch* to *remote* using *token* for authentication, via
    GIT_ASKPASS (never the remote URL, never git config, never argv).

    *platform* / *other_platform_label*: when supplied, an auth-shaped
    rejection (see _AUTH_MISMATCH_MARKERS) is annotated with a
    platform-mismatch hint naming *other_platform_label* as the likely
    actual target — both are caller-supplied labels (e.g. 'forgejo'/
    'github'), never a value this module invents. Omit either to skip the
    hint.

    *force_with_lease*: pass True only after a history rewrite (identity
    re-authoring) changed the branch's commit SHAs, so a normal push would
    be rejected as non-fast-forward against the previously-pushed history.

    Raises GitPushError (never includes the token value) on any non-zero
    `git push` exit.
    """
    tmp_dir = tempfile.mkdtemp(prefix="loadout_push_")
    try:
        token_file = os.path.join(tmp_dir, "tok")
        askpass_file = os.path.join(tmp_dir, "askpass.sh")
        isolated_home = os.path.join(tmp_dir, "home")
        os.makedirs(isolated_home, exist_ok=True)

        with open(token_file, "w") as f:
            f.write(token)
        os.chmod(token_file, 0o600)

        with open(askpass_file, "w") as f:
            f.write(make_askpass_script(token_file))
        os.chmod(askpass_file, 0o700)

        env = os.environ.copy()
        env["GIT_ASKPASS"] = askpass_file
        env["GIT_TERMINAL_PROMPT"] = "0"
        # Empty HOME: removes ~/.netrc / ~/.git-credentials / ~/.gitconfig
        # as discoverable credential sources, making GIT_ASKPASS the sole
        # source libcurl can find for this subprocess.
        env["HOME"] = isolated_home
        # Opt-in diagnostic verbosity passthrough: when the caller sets
        # CLAGENTIC_LOADOUT_PUSH_GIT_TRACE, git's own packet/hook/transport
        # trace is folded into this subprocess's stderr, making the
        # local-hook-vs-transport-vs-remote phase distinguishable at the
        # source. GIT_TRACE writes to stderr by default (no value needed);
        # setting it to "1" is git's own documented on-switch.
        if os.environ.get(_GIT_TRACE_ENV_VAR, "").strip().lower() in ("1", "true", "yes"):
            env["GIT_TRACE"] = "1"

        push_cmd = ["git", "push", remote, f"{branch}:{branch}", "--set-upstream"]
        if force_with_lease:
            push_cmd.append("--force-with-lease")

        result = subprocess.run(
            push_cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(git_cwd) if git_cwd is not None else None,
        )
        if result.returncode != 0:
            stderr_safe = result.stderr
            mismatch_hint = ""
            if (
                platform
                and other_platform_label
                and any(marker in stderr_safe.lower() for marker in _AUTH_MISMATCH_MARKERS)
            ):
                mismatch_hint = (
                    f" POSSIBLE PLATFORM MISMATCH: pushing to remote {remote!r} "
                    f"using {platform!r} credentials, but the auth rejection looks "
                    f"like the remote actually expects {other_platform_label!r} "
                    f"credentials (or vice versa). This is NOT necessarily a dead/"
                    f"expired credential. Verify with 'git remote get-url {remote}' "
                    f"and re-run with the matching platform if the host doesn't "
                    f"match."
                )

            # Surface the remote-side rejection reason (the "remote: ..."
            # lines receive-pack / a pre-receive hook prints) explicitly and
            # separately from the generic client-side summary. Previously
            # only the client-side "error: failed to push some refs..."
            # line survived truncation in practice when the remote lines
            # came first in stderr — a server-side policy rejection
            # collapsed to an undiagnosable one-liner. The remote text
            # carries no secret (it is the server's own message).
            sub_cause = _classify_push_failure(stderr_safe)
            remote_lines = _extract_remote_lines(stderr_safe)
            remote_block = ""
            if remote_lines:
                remote_text = "\n".join(remote_lines)[:_PUSH_ERROR_REMOTE_LIMIT]
                remote_block = f" REMOTE MESSAGE ({sub_cause}): {remote_text}"

            # Surface a LOCAL pre-push hook's own output distinctly from a
            # transport failure. A local hook runs client-side before any
            # network negotiation — its stdout/stderr is unprefixed (unlike
            # "remote: " lines) and was previously indistinguishable from a
            # dead connection, both collapsing to sub_cause=unknown.
            local_hook_lines = _extract_local_hook_lines(stderr_safe)
            local_hook_block = ""
            if local_hook_lines:
                local_hook_text = "\n".join(local_hook_lines)[:_PUSH_ERROR_LOCAL_HOOK_LIMIT]
                local_hook_block = (
                    f" LOCAL PRE-PUSH HOOK MESSAGE ({sub_cause}): {local_hook_text}"
                )

            raise GitPushError(
                f"git push failed (exit {result.returncode}, {sub_cause}): "
                f"{stderr_safe.strip()[:_PUSH_ERROR_STDERR_LIMIT]}"
                f"{remote_block}{local_hook_block}{mismatch_hint}"
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = ["git_push_with_token", "make_askpass_script"]
