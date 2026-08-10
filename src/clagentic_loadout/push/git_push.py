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

REJECT-REASON PARSER, NOT A SUBSTRING CLASSIFIER (lr-f57f13, D6 DECIDED):
prior revisions of this module classified a push failure by substring-
matching known phrases across the WHOLE stderr blob
("non-fast-forward"/"fetch first" in lower(), etc.) — an ENUMERATE-THE-GOOD
taxonomy that is fail-OPEN by construction on the diagnostic axis: any cause
nobody anticipated silently falls into "unknown," which reads to an operator
as "the tool has no information" even when raw stderr (always carried,
never gated on classification -- see push.errors.GitPushError) already had
the answer in it. Three real, proven bugs of exactly this shape shipped
under that construction (git 2.43.0, verified):
  1. A --force-with-lease rejection prints "(stale info)" as a DISTINCT
     THIRD literal beside "non-fast-forward"/"fetch first" -- neither
     substring matched, so a genuine lease-staleness rejection (the OPPOSITE
     of "no information": git's own hint text for THIS one case is a bare
     three lines with no explanatory hint block at all, unlike a plain
     "fetch first" rejection's five hint: lines) fell through to "unknown."
  2. A "cannot lock ref" server-side race falls through to "unknown" when
     it arrives with no "remote: "-prefixed line.
  3. "error: src refspec X does not match any" (a LOCAL refspec-resolution
     failure, e.g. loadout pushing a branch name that does not exist
     locally under that exact name) was misattributed to
     local-hook-rejected -- WORSE than unknown, a confidently FALSE label,
     because _extract_local_hook_lines treated any unprefixed line above
     the summary as hook output with no verification a hook actually ran.

THE FIX: git states the per-ref rejection reason in a FIXED, PARSEABLE
POSITION -- the parenthetical in
`! [rejected]        <refspec> (<reason>)` or
`! [remote rejected] <refspec> (<reason>)`. `_parse_reject_reason` lifts
that reason VERBATIM rather than re-deriving it via substring guesswork.
This is exhaustive BY CONSTRUCTION over every reason git's porcelain output
can produce (a new git version adding a new reason literal is still parsed
correctly, since the parser reads the position, not an enumerated string
list) and immune to the enumerate-the-good trap: adding a fourth or fifth
substring branch to the old classifier would have preserved the exact
construction that produced all three bugs above, just moved the boundary.
`_classify_push_failure` now derives its coarse sub_cause LABEL (see
push.push_failure_labels) from the parsed reason plus the pre-existing
remote-line/local-hook-line/transport-substring signals, but the reason
text itself is always parsed structurally first.

LEASE CONTROL — REMOVED FROM verb.py'S OWN DERIVATION (lr-f57f13, D5
DECIDED): `git_push_with_token`'s `force_with_lease` parameter is UNCHANGED
in shape, but callers (see push.verb) no longer derive it from "did we
rewrite commit authorship" -- that derivation was a non-sequitur (re-
authoring is loadout's own act and says nothing about remote state) and,
combined with loadout never fetching before pushing, silently forced a
lease evaluation against a STALE local remote-tracking ref on essentially
every push, converting a genuine conflict into the one rejection shape git
explains least. See push.verb's own `--force-with-lease`/`--no-force-with-
lease` flags (push.lease_control.resolve_lease performs the pre-lease
remote fetch automatically whenever the resolved decision is to force) for
the explicit CLI control and printed resolved-lease-state contract this
task requires.

Push-failure observability (ported for parity from the reference push
transport's lr-a1fd4d / lr-8460a4 fixes, refined by lr-f57f13): a bare
client-side summary line ("error: failed to push some refs") is
undiagnosable on its own. On failure this module additionally:
  - extracts and surfaces "remote: "-prefixed lines verbatim (the actual
    pre-receive/hook rejection reason a server sends back);
  - detects and surfaces a LOCAL .git/hooks/pre-push abort distinctly from
    a remote rejection or a transport failure;
  - parses the per-ref reject-reason parenthetical structurally (see
    above), rather than substring-matching the whole blob;
  - classifies the failure into a coarse sub_cause label (see
    push.push_failure_labels for the full enumerable set) folded into the
    raised message text, ALWAYS alongside the full raw transcript -- never
    instead of it (classification is additive metadata, never a
    replacement for the evidence -- see push.errors.GitPushError); and
  - honors an opt-in GIT_TRACE passthrough (see _GIT_TRACE_ENV_VAR) so
    transport vs. local-hook vs. remote-hook phase is distinguishable at
    the source when the coarse classification isn't enough.

DISCOVERABLE --verbose/--trace, SAME PASSTHROUGH (lr-68039e): the
GIT_TRACE passthrough above was, until this task, reachable ONLY via the
undiscoverable CLAGENTIC_LOADOUT_PUSH_GIT_TRACE env var -- absent from
`push --help` entirely. `push.verb`'s `--verbose`/`--trace` flag now sets
this SAME env var's effect programmatically (see *verbose* below) rather
than adding a second trace mechanism; the env var keeps working as a
compat alias for a caller that already sets it. `git push -v` is also
added to the argv on the same flag, for the ordinary (non-GIT_TRACE)
verbose-porcelain output git itself defines.

--DRY-RUN, THE SAME CALL SITE (lr-68039e): a read-only substitute for an
agent shelling out to raw git when a push fails opaquely. *dry_run* below
appends `--dry-run` to the SAME argv this function always builds -- through
the SAME `_credentialed_git_env` envelope, the SAME single
`subprocess.run` call, the SAME hermeticity pre-flight (`check_git_version`,
`check_repo_local_config_hazards`) a real push runs. This is deliberate:
a dry-run that used a second call site or skipped pre-flight would report
success where a real push would refuse, which is a misleading affordance,
worse than no affordance at all. On a dry-run, this function prints the
full (redacted) stdout+stderr transcript to stderr UNCONDITIONALLY --
including on the exit-0 case a real push's own success path never prints
anything for, since a dry-run's only purpose is to surface that
transcript to the caller.

REPORTING RESOLVED FACTS, NOT GUESSES (CLAUDE.md hard rule 4; lr-f57f13
REJECTED an alternative that would have violated it): even when
classification lands on "unknown," this module reports what it OBSERVED --
exit code, stderr byte count, whether a "To <url>" line was present (the
real did-we-reach-transport signal), the resolved remote/refspec, and the
lease flag with its derivation -- never a narrative claim like "the push
either never reached the server-side hook or was refused before it," which
would be flatly false for a locally-detected non-fast-forward/lease failure
(absence of remote sideband is EXPECTED and carries zero information in
that case; the push was rejected client-side and the server was never
asked at all).

SECRET REDACTION, STRUCTURAL AT GitPushError CONSTRUCTION (lr-f57f13,
hardened per a pre-merge security review): every field this module passes
into `GitPushError` is redacted INSIDE that class's own `__init__` (see
push.errors.GitPushError) — this module passes *known_secrets=(token,)*
through to the constructor rather than pre-redacting each argument itself,
so the guarantee holds regardless of which module constructs a
`GitPushError` in the future, not merely because this module's own call
site happens to redact first.

CREDENTIALED PRE-LEASE FETCH (lr-f57f13, pre-merge security review
finding, fixed here): `push.lease_control.resolve_lease` needs to fetch the
target branch's remote-tracking ref before a forced lease is evaluated (see
that module's own docstring for why). The FIRST shipped version of that
fetch ran a bare ambient `git fetch` with no credential envelope at all —
outside the minted-token path this module exists to enforce, and on
failure folded raw, unredacted stderr into a printed warning.
`_credentialed_git_env` below is the SHARED environment-construction
primitive (GIT_ASKPASS + isolated HOME + opt-in GIT_TRACE) both
`git_push_with_token` and the new `git_fetch_with_token` build their
subprocess call on top of — a single tested implementation of "how this
package talks to a remote with a minted token," rather than a second,
divergent copy for the pre-lease fetch to reinvent (and potentially get
wrong) on its own.

HERMETIC AGAINST AMBIENT CREDENTIAL MACHINERY, NOT MERELY HOME-ISOLATED
(lr-a868d2): HOME isolation alone (the posture above, unchanged) removes
~/.netrc / ~/.git-credentials / ~/.gitconfig as ambient sources but does NOT
reach a `credential.helper` configured at SYSTEM scope (/etc/gitconfig) or
at REPO-LOCAL scope (the target repo's own `.git/config`, which git reads
unconditionally — no environment variable disables it). The operator
constraint driving this fix: workspace credentials may ALWAYS exist and
differ across environments, so the guarantee cannot depend on the host
having been cleaned. `_credentialed_git_env` now additionally calls
`push.git_hermeticity.neutralize_ambient_git_env` (GIT_CONFIG_GLOBAL/SYSTEM
redirected to /dev/null, GIT_CONFIG_NOSYSTEM=1, any ambient GIT_ASKPASS/
SSH_ASKPASS/GIT_SSH/GIT_SSH_COMMAND stripped before this module's own
GIT_ASKPASS is set, and any GIT_CONFIG_COUNT/KEY_<n>/VALUE_<n> injection
channel removed) and adds `-c credential.helper=""` as a command-scope
override on the subprocess argv itself (see push.git_hermeticity's own
module docstring for why the env-level recipe is required and the `-c`
override alone is not sufficient). Repo-local config cannot be suppressed by
any of the above — see `push.git_hermeticity.check_repo_local_config_hazards`
and `git_push_with_token`/`git_fetch_with_token`'s own fail-closed
validation of it before either ever spawns a subprocess. A minimum git
version (`push.git_hermeticity.MIN_GIT_VERSION`, 2.20) is also enforced
before any credentialed call, for git's own protected-configuration
guarantee that a command-line `-c` override wins over repo-local config for
security-sensitive keys.

None of this ever surfaces the token: remote/hook text is the server's or
the local repo's own message, never anything git-credential populates.
"""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from clagentic_loadout.push.errors import GitPushError
from clagentic_loadout.push.git_hermeticity import (
    GitVersionTooOldError,
    RepoLocalConfigHazardError,
    check_git_version,
    check_repo_local_config_hazards,
    neutralize_ambient_git_env,
)
from clagentic_loadout.push.push_failure_labels import (
    SUB_CAUSE_AUTH_FAILED,
    SUB_CAUSE_BAD_REFSPEC,
    SUB_CAUSE_LOCAL_HOOK_REJECTED,
    SUB_CAUSE_NON_FAST_FORWARD,
    SUB_CAUSE_OTHER_REJECT_REASON,
    SUB_CAUSE_PRE_RECEIVE_REJECTED,
    SUB_CAUSE_TRANSPORT,
    SUB_CAUSE_UNKNOWN,
)
from clagentic_loadout.push.push_redaction import redact_push_secrets

#: git-transport auth failure substrings that plausibly indicate a
#: platform/credential mismatch rather than a dead credential.
_AUTH_MISMATCH_MARKERS = ("invalid username or token", "authentication failed", "403")

#: Max characters of the raw git stderr to include in the failure message.
#: Kept generous -- the full transcript is always carried on the exception
#: object regardless (see push.errors.GitPushError.raw_stderr); this limit
#: only bounds what is folded into the formatted display string.
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
#: already names the cause. See push.push_redaction: this trace output
#: passes through the same redaction choke point as everything else before
#: it can reach a raised message.
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

#: The literal prefix of git's client-side destination line, e.g.
#: "To /path/to/remote.git" or "To https://host/owner/repo.git" -- presence
#: of this line is the REAL did-we-reach-transport signal (per this task's
#: own REPORTING RESOLVED FACTS section): git prints it once it has resolved
#: and begun talking to the named remote, independent of whether the remote
#: ultimately accepted or rejected anything.
_TO_LINE_PREFIX = "To "

#: A single stderr line reporting the LOCAL refspec itself did not resolve
#: — e.g. loadout building "<branch>:<branch>" from a branch name that does
#: not exist locally under that exact name. This is a usage/argument-shape
#: defect, never a remote or local-hook rejection (lr-f57f13 bug 3: this
#: shape was previously misattributed to local-hook-rejected because it has
#: no "To "/"! [" line above the summary to exclude it structurally).
_BAD_REFSPEC_PREFIX = "error: src refspec "

#: Structural parser for git's per-ref rejection line, matching EITHER
#: porcelain shape:
#:   " ! [rejected]        <refspec> (<reason>)"
#:   " ! [remote rejected] <refspec> (<reason>)"
#: Lifting the parenthetical VERBATIM is exhaustive by construction over
#: every reason git's porcelain output can produce (lr-f57f13, D6 DECIDED)
#: — a future git version adding a new literal is still parsed correctly,
#: since this reads the FIXED POSITION, not an enumerated string list.
#: Whitespace after "[rejected]"/"[remote rejected]" is variable-width
#: (git pads it for column alignment), hence `\s+`.
_REJECT_REASON_RE = re.compile(
    r"^\s*!\s*\[(?:rejected|remote rejected)\]\s+\S+\s+->\s+\S+\s+\((?P<reason>[^)]+)\)\s*$"
)

#: Known non-fast-forward-shaped reject reasons (lr-f57f13 bug 1 fix): git
#: emits "non-fast-forward" for a bare `--force`-less rejection, "fetch
#: first" for the "someone else already pushed" case, and "stale info" for
#: a --force-with-lease rejection whose remote-tracking ref loadout never
#: refreshed before evaluating the lease against it. All three are the SAME
#: coarse shape from an operator's perspective — a client-side conflict
#: against remote state the local repo does not yet have — and all three
#: get the SAME sub_cause label; the individual literal is still preserved
#: verbatim in `GitPushError.reject_reason` for anyone who needs the
#: distinction.
_NON_FAST_FORWARD_REASONS = frozenset({"non-fast-forward", "fetch first", "stale info"})


#: git's own client-side transport-auth failure line, e.g.:
#:   fatal: Authentication failed for 'http://host/owner/repo.git/'
#: This is HOST-INDEPENDENT: git itself emits this literal (not a vendor
#: message) whenever libcurl's credential negotiation is rejected, regardless
#: of which platform is on the other end. lr-91bac6: a Forgejo 401 response
#: body is ALSO prefixed "remote: " -- exactly like genuine pre-receive/
#: policy text -- so remote-line presence alone cannot distinguish a dead
#: credential from a branch-protection gate. This marker is checked BEFORE
#: the remote-lines branch in _classify_push_failure for that reason.
_AUTH_FAILED_MARKER = "authentication failed"


def _is_auth_failure(stderr: str) -> bool:
    """True when *stderr* carries git's own host-independent transport-auth
    failure shape ("fatal: Authentication failed for '<url>'") -- see
    _AUTH_FAILED_MARKER. Anchored on git's own literal, never on
    platform-specific vendor wording (e.g. Forgejo's "Credentials are
    incorrect or have expired" body text), so this holds across hosts."""
    return _AUTH_FAILED_MARKER in stderr.lower()


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


def _has_to_line(stderr: str) -> bool:
    """True when stderr contains git's client-side destination line ("To
    <remote>") — the REAL did-we-reach-transport signal (see module
    docstring, REPORTING RESOLVED FACTS). git prints this line once it has
    resolved the remote and begun negotiating with it, regardless of
    whether that negotiation is ultimately accepted or rejected — a bad
    refspec or an early transport failure never reaches this point."""
    return any(line.strip().startswith(_TO_LINE_PREFIX) for line in stderr.splitlines())


def _is_bad_refspec_line(stripped_line: str) -> bool:
    """True when *stripped_line* is git's own "src refspec ... does not
    match any" line (lr-f57f13 bug 3) — a LOCAL refspec-resolution failure,
    never local-hook output."""
    return stripped_line.startswith(_BAD_REFSPEC_PREFIX)


def _has_bad_refspec_line(stderr: str) -> bool:
    return any(_is_bad_refspec_line(line.strip()) for line in stderr.splitlines())


def _parse_reject_reason(stderr: str) -> str | None:
    """Parse git's per-ref reject-reason parenthetical from a fixed,
    structural position (module docstring, D6 DECIDED) rather than
    substring-matching the whole blob. Returns the reason VERBATIM (e.g.
    "stale info", "non-fast-forward", "cannot lock ref 'refs/heads/main': ...",
    "pre-receive hook declined"), or None when no such line is present
    (e.g. a bad-refspec or transport failure, which has no "! [...]" line
    at all)."""
    for line in stderr.splitlines():
        match = _REJECT_REASON_RE.match(line)
        if match:
            return match.group("reason").strip()
    return None


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

    A "src refspec ... does not match any" failure (lr-f57f13 bug 3) is ALSO
    excluded here: it is git's own fixed error-line shape, produced before
    any hook could possibly run (refspec resolution happens before the
    pre-push hook fires), not a local hook's own text — see
    _is_bad_refspec_line.

    Returns the hook's own content (in order), or an empty list when:
      - stderr contains "remote: " lines (this is a remote-side rejection,
        not a local one — see _extract_remote_lines);
      - the summary marker itself is absent (e.g. a "fatal: unable to
        access..." transport error that never even reaches the point of
        printing git's generic summary line — there is no summary line to
        anchor against, so nothing is attributable to a hook); or
      - every line before the summary is one of git's own fixed status
        lines (or a bad-refspec line), or there is no content at all before
        the summary (a bare transport or non-fast-forward failure with
        nothing to attribute to a hook).
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
        if stripped and not _is_git_own_status_line(stripped) and not _is_bad_refspec_line(stripped):
            hook_lines.append(stripped)
    if not saw_summary:
        return []
    return hook_lines


def _classify_push_failure(stderr: str) -> str:
    """Classify a failed git push into a coarse sub-cause (see
    push.push_failure_labels for the full enumerable label set).

    lr-f57f13 D6 DECIDED: derives from `_parse_reject_reason`'s structural
    parse FIRST, rather than substring-matching the whole stderr blob — see
    module docstring for the three proven bugs this replaces
    (stale-info-as-unknown, cannot-lock-ref-as-unknown,
    src-refspec-as-local-hook-rejected).

    Ordering:
      1. A "src refspec ... does not match any" line (bad-refspec) is
         checked FIRST and unconditionally — this shape is a local
         refspec-resolution failure that occurs before ANY hook could run
         or any network call could happen, and previously fell into
         local-hook-rejected purely because it has no "To "/"! [" line to
         exclude it (lr-f57f13 bug 3).
      2. git's own host-independent transport-auth failure shape
         ("fatal: Authentication failed for '<url>'", see _is_auth_failure)
         -> auth-failed. Checked BEFORE the remote-lines branch below
         (lr-91bac6): Forgejo (and other hosts) prefix their HTTP 401
         response body with "remote: " exactly as they prefix genuine
         pre-receive/policy text, so remote-line presence alone cannot tell
         a dead credential from a branch-protection gate. Anchoring on
         git's own literal instead of any vendor wording keeps this correct
         across hosts.
      3. Remote lines present (and not an auth failure) -> pre-receive-
         rejected (a "cannot lock ref" server race also arrives this way
         when it carries a "remote: " line — lr-f57f13 bug 2's proven
         repro).
      4. Local hook lines present (and no remote lines, no bad-refspec) ->
         local-hook-rejected.
      5. A parsed reject reason matching a known non-fast-forward-shaped
         literal (non-fast-forward / fetch first / stale info) ->
         non-fast-forward.
      6. A parsed reject reason that does not match any known shape ->
         other-reject-reason (classification found A reason, just not one
         this taxonomy names yet — never silently relabeled "unknown").
      7. No reject reason, but a recognized transport-failure substring ->
         transport.
      8. None of the above -> unknown. ALWAYS carries the full raw
         transcript on the raised GitPushError regardless (see that class's
         own docstring) — this label states "the classifier could not name
         a cause," never "the remote said nothing" (see module docstring,
         REPORTING RESOLVED FACTS, for why the latter would be an unproven
         and possibly false narrative).

    This is a best-effort classification for the failure message; callers
    should still surface the raw (and remote-extracted / hook-extracted /
    reject-reason) text rather than relying on the label alone —
    classification is ADDITIVE METADATA, never a substitute for the
    transcript (see push.errors.GitPushError).
    """
    if _has_bad_refspec_line(stderr):
        return SUB_CAUSE_BAD_REFSPEC
    if _is_auth_failure(stderr):
        return SUB_CAUSE_AUTH_FAILED
    if _extract_remote_lines(stderr):
        return SUB_CAUSE_PRE_RECEIVE_REJECTED
    if _extract_local_hook_lines(stderr):
        return SUB_CAUSE_LOCAL_HOOK_REJECTED

    reason = _parse_reject_reason(stderr)
    if reason is not None:
        if reason.lower() in _NON_FAST_FORWARD_REASONS:
            return SUB_CAUSE_NON_FAST_FORWARD
        return SUB_CAUSE_OTHER_REJECT_REASON

    lower = stderr.lower()
    if (
        "could not resolve host" in lower
        or "could not connect" in lower
        # git/libcurl's actual real-world phrasing (verified against git
        # 2.43.0's own libcurl backend, lr-f57f13): "Couldn't connect to
        # server", not "could not connect" -- both are kept since the exact
        # wording is libcurl-version-dependent and neither superseded the
        # other in observed output.
        or "couldn't connect to server" in lower
        or "connection timed out" in lower
        or "connection refused" in lower
    ):
        return SUB_CAUSE_TRANSPORT
    return SUB_CAUSE_UNKNOWN


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


#: Command-scope override prepended to every credentialed git invocation
#: (lr-a868d2): defense-in-depth alongside the env-level neutralization in
#: `neutralize_ambient_git_env` -- NOT sufficient alone (see
#: push.git_hermeticity's own module docstring for why an empty `-c`
#: override cannot clear an inherited MULTI-VALUED credential.helper list by
#: itself), but still worth applying once the env-level fix already makes
#: the multi-valued-list problem moot: this override wins for THIS
#: invocation specifically over anything that could otherwise re-enter via
#: repo-local config precedence rules on a git version at or above the
#: enforced minimum.
_HERMETIC_ARGV_PREFIX = ["-c", "credential.helper="]


@contextlib.contextmanager
def _credentialed_git_env(token: str, *, force_trace: bool = False):
    """Construct the isolated, token-injected, HERMETIC environment every
    credentialed git subprocess in this module runs under, and yield it.

    *force_trace* (lr-68039e): set True to enable GIT_TRACE the SAME way the
    CLAGENTIC_LOADOUT_PUSH_GIT_TRACE env var already does below --
    programmatically, for `git_push_with_token`'s `verbose` parameter (the
    passthrough `push.verb`'s discoverable --verbose/--trace flag turns on).
    Not a second trace mechanism: both this parameter and the env var set
    the identical `env["GIT_TRACE"] = "1"` this function already applies.

    SHARED PRIMITIVE (lr-f57f13, pre-merge security review finding): both
    `git_push_with_token` and `git_fetch_with_token` build their subprocess
    call on top of this SAME construction — a token written to a mode-0600
    temp file, GIT_ASKPASS pointed at a generated script that reads it,
    HOME isolated to an empty temp dir (removing ~/.netrc/~/.git-credentials/
    ~/.gitconfig as ambient credential sources), the opt-in GIT_TRACE
    passthrough, and (lr-a868d2) full neutralization of the ambient
    credential-machinery surface beyond HOME -- so a caller adding a THIRD
    credentialed git call to this module in the future reuses this envelope
    rather than reinventing it (and potentially getting the isolation wrong,
    exactly the defect class the ORIGINAL pre-lease fetch fix introduced: it
    ran a bare ambient `git fetch` with no credential envelope at all,
    authenticating via whatever ambient credential helper happened to be
    configured for the remote rather than the minted token this call
    actually resolved).

    HERMETICITY BEYOND HOME (lr-a868d2, see push.git_hermeticity's own module
    docstring for the full rationale): HOME isolation alone does not reach a
    `credential.helper` configured at system scope (/etc/gitconfig) or
    repo-local scope (the target repo's own `.git/config`, always read,
    never suppressible by environment alone). This function additionally
    calls `push.git_hermeticity.neutralize_ambient_git_env`, which redirects
    global/system config scope to /dev/null, independently disables system
    config reading, strips any ambient GIT_ASKPASS/SSH_ASKPASS/GIT_SSH/
    GIT_SSH_COMMAND the real environment happens to export (BEFORE this
    function lays its own deliberate GIT_ASKPASS on top, immediately below),
    and removes any GIT_CONFIG_COUNT/KEY_<n>/VALUE_<n> config-injection
    channel. Repo-local config itself cannot be suppressed this way -- this
    function does not attempt to; see `git_push_with_token`/
    `git_fetch_with_token`'s own fail-closed repo-local hazard check, which
    runs BEFORE this context manager is ever entered.

    The whole token-carrying temp dir is removed when the context exits,
    regardless of what the caller did inside it.
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

        # Neutralize the ambient surface FIRST (global/system config scope,
        # ambient askpass/SSH env vars, config-injection channel) -- this
        # function's own deliberate GIT_ASKPASS/HOME assignments below are
        # applied AFTER, so neither is shadowed or raced by anything the
        # neutralization pass would otherwise have left behind.
        env = neutralize_ambient_git_env(os.environ.copy())
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
        if force_trace or os.environ.get(_GIT_TRACE_ENV_VAR, "").strip().lower() in (
            "1",
            "true",
            "yes",
        ):
            env["GIT_TRACE"] = "1"

        yield env
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


class GitFetchError(Exception):
    """Raised when the underlying `git fetch` subprocess (via
    `git_fetch_with_token`) exits non-zero. Carries the ALREADY-REDACTED
    stderr (see `push.push_redaction.redact_push_secrets`, applied here
    with the same `known_secrets=(token,)` the push path uses) — a caller
    (push.lease_control.resolve_lease) folds `str(exc)` directly into a
    printed warning with no further redaction step required."""


def git_fetch_with_token(
    remote: str,
    branch: str,
    token: str,
    git_cwd: Path | None = None,
) -> None:
    """Fetch *branch* from *remote* using *token* for authentication, via
    the SAME credentialed environment `git_push_with_token` uses (see
    `_credentialed_git_env`) — never an ambient credential.

    lr-f57f13 (pre-merge security review finding, fixed here): this exists
    because `push.lease_control.resolve_lease` needs to refresh the
    remote-tracking ref before evaluating a forced `--force-with-lease`
    push, and the first shipped version of that refresh ran a bare
    `subprocess.run(["git", "fetch", ...])` with no credential envelope at
    all — authenticating via whatever ambient credential helper happened to
    be configured for the remote, entirely outside the minted-token path
    this module exists to enforce.

    Raises GitFetchError (never includes the token value; stderr is
    redacted via `push.push_redaction.redact_push_secrets` before it is
    ever attached to the exception) on any non-zero `git fetch` exit.

    HERMETICITY PRE-FLIGHT (lr-a868d2): before any subprocess spawns, this
    checks the resolved git version (GitVersionTooOldError if below
    `push.git_hermeticity.MIN_GIT_VERSION`) and the target repo's LOCAL
    config for the unsuppressable hazards (RepoLocalConfigHazardError
    if any is found) — see `push.git_hermeticity`'s own module docstring for
    why environment isolation alone cannot cover repo-local config. Both
    checks fail closed; neither is optional or configurable, matching the
    operator constraint this fix is built against (ambient credentials may
    always exist and must never be allowed to silently win).
    """
    check_git_version(git_cwd=git_cwd)
    hazards = check_repo_local_config_hazards(git_cwd)
    if hazards:
        raise RepoLocalConfigHazardError(
            f"refusing to fetch {remote!r} {branch!r}: the target repo's "
            f"LOCAL .git/config carries a hermeticity hazard this package "
            f"cannot neutralize via environment isolation alone -- "
            f"{sorted(set(hazards))!r}. Repo-local config is always read by "
            f"git; there is no environment variable that suppresses it. "
            f"Remove the offending repo-local config entry (credential.*, "
            f"http.*.extraheader, includeIf.*, or url.*.insteadOf/"
            f"pushInsteadOf) before retrying -- this check fails closed "
            f"with no override."
        )
    with _credentialed_git_env(token) as env:
        result = subprocess.run(
            ["git", *_HERMETIC_ARGV_PREFIX, "fetch", remote, branch],
            capture_output=True,
            text=True,
            env=env,
            cwd=str(git_cwd) if git_cwd is not None else None,
        )
        if result.returncode != 0:
            stderr_safe = redact_push_secrets(result.stderr, known_secrets=(token,))
            raise GitFetchError(
                f"git fetch {remote!r} {branch!r} exited {result.returncode}: "
                f"{stderr_safe.strip()[:400]}"
            )


def git_push_with_token(
    remote: str,
    branch: str,
    token: str,
    git_cwd: Path | None = None,
    *,
    force_with_lease: bool = False,
    lease_origin: str = "",
    platform: str | None = None,
    other_platform_label: str | None = None,
    dry_run: bool = False,
    verbose: bool = False,
) -> str | None:
    """Push *branch* to *remote* using *token* for authentication, via
    GIT_ASKPASS (never the remote URL, never git config, never argv) — see
    `_credentialed_git_env` for the shared environment-construction
    primitive this and `git_fetch_with_token` both build on.

    *platform* / *other_platform_label*: when supplied, an auth-shaped
    rejection (see _AUTH_MISMATCH_MARKERS) is annotated with a
    platform-mismatch hint naming *other_platform_label* as the likely
    actual target — both are caller-supplied labels (e.g. 'forgejo'/
    'github'), never a value this module invents. Omit either to skip the
    hint.

    *force_with_lease*: pass True to push with `--force-with-lease`. lr-f57f13
    REMOVAL: this module no longer derives the value from "did we rewrite
    history" -- see push.verb for the CLI-exposed control
    (`--force-with-lease`/`--no-force-with-lease`) and the pre-lease remote
    fetch (`git_fetch_with_token`, via the SAME credentialed envelope) that
    makes forcing safe to evaluate against a CURRENT remote-tracking ref
    rather than a stale one (module docstring, LEASE CONTROL).

    *lease_origin*: a caller-supplied label describing WHERE the
    *force_with_lease* value came from (e.g. "cli-flag", "history-rewritten
    + fetched", "default-false") -- folded into the raised message so a
    lease-related failure never requires re-deriving why forcing was (or
    was not) in effect; see push.verb's own resolve-and-print contract.

    *dry_run* (lr-68039e): pass True to append `--dry-run` to the SAME argv
    this function always builds, through the SAME call site and hermeticity
    pre-flight a real push runs (module docstring, "--DRY-RUN, THE SAME
    CALL SITE") -- no ref is updated on the remote. This function then
    RETURNS the full (redacted) transcript as a string, printing it to
    stderr as well, instead of returning None -- a dry-run's only purpose
    is to surface the transcript, including any `remote: `-prefixed
    sideband, on the caller's own identity, regardless of whether git's own
    dry-run exit code is 0 or non-zero. A non-zero dry-run exit still
    raises GitPushError exactly like a real push would (proving what a real
    push would do), but the transcript is ALSO printed before that raise so
    a caller reading only stderr (not catching the exception) still sees it.

    *verbose* (lr-68039e): pass True to enable the SAME opt-in GIT_TRACE
    passthrough the CLAGENTIC_LOADOUT_PUSH_GIT_TRACE env var already
    provides (see _GIT_TRACE_ENV_VAR) -- programmatically, so `push.verb`'s
    --verbose/--trace flag needs no second trace mechanism -- plus `-v` on
    the git push argv itself for git's own verbose-porcelain output. Redacted
    exactly like every other field this module produces (see
    push.push_redaction) before it can ever reach a raised message or
    stdout/stderr.

    Raises GitPushError (never includes the token value) on any non-zero
    `git push` exit. `GitPushError.__init__` redacts every field
    structurally (see push.errors.GitPushError) — this function passes
    *known_secrets=(token,)* through rather than pre-redacting each
    argument itself.

    HERMETICITY PRE-FLIGHT (lr-a868d2): before any subprocess spawns, this
    checks the resolved git version (GitVersionTooOldError if below
    `push.git_hermeticity.MIN_GIT_VERSION`) and the target repo's LOCAL
    config for the unsuppressable hazards (RepoLocalConfigHazardError
    if any is found) — see `push.git_hermeticity`'s own module docstring for
    why environment isolation alone cannot cover repo-local config. Both
    checks fail closed; neither is optional or configurable, matching the
    operator constraint this fix is built against (ambient credentials may
    always exist and must never be allowed to silently win). *dry_run* runs
    the IDENTICAL pre-flight -- a dry-run that skipped it would report
    success where a real push would refuse, a misleading affordance worse
    than none (module docstring).
    """
    check_git_version(git_cwd=git_cwd)
    hazards = check_repo_local_config_hazards(git_cwd)
    if hazards:
        raise RepoLocalConfigHazardError(
            f"refusing to push {branch!r} to {remote!r}: the target repo's "
            f"LOCAL .git/config carries a hermeticity hazard this package "
            f"cannot neutralize via environment isolation alone -- "
            f"{sorted(set(hazards))!r}. Repo-local config is always read by "
            f"git; there is no environment variable that suppresses it. "
            f"Remove the offending repo-local config entry (credential.*, "
            f"http.*.extraheader, includeIf.*, or url.*.insteadOf/"
            f"pushInsteadOf) before retrying -- this check fails closed "
            f"with no override."
        )
    with _credentialed_git_env(token, force_trace=verbose) as env:
        refspec = f"{branch}:{branch}"
        push_cmd = ["git", *_HERMETIC_ARGV_PREFIX, "push", remote, refspec, "--set-upstream"]
        if force_with_lease:
            push_cmd.append("--force-with-lease")
        if dry_run:
            push_cmd.append("--dry-run")
        if verbose:
            push_cmd.append("-v")

        result = subprocess.run(
            push_cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=str(git_cwd) if git_cwd is not None else None,
        )

        if dry_run:
            # DRY-RUN TRANSCRIPT (lr-68039e): surfaced UNCONDITIONALLY,
            # regardless of exit code -- a dry-run's sole purpose is
            # exposing the full transcript (including any "remote: "
            # sideband) on the caller's own identity, whether or not git
            # itself would have accepted the push. Redacted through the
            # SAME choke point every other field in this module uses
            # (push.push_redaction), never a second implementation.
            dry_run_transcript = redact_push_secrets(
                f"[dry-run exit={result.returncode}] stdout: {result.stdout}"
                f" stderr: {result.stderr}",
                known_secrets=(token,),
            )
            print(f"push: --dry-run transcript -- {dry_run_transcript}", file=sys.stderr)

        if result.returncode != 0:
            mismatch_hint = ""
            if (
                platform
                and other_platform_label
                and any(marker in result.stderr.lower() for marker in _AUTH_MISMATCH_MARKERS)
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

            # Classification runs against the RAW stderr: a redacted-token
            # substring could otherwise mask a substring a classifier or
            # extractor keys on. GitPushError.__init__ (not this function)
            # is what redacts every field before it is ever stored on the
            # raised object -- see that class's own docstring.
            sub_cause = _classify_push_failure(result.stderr)
            reject_reason = _parse_reject_reason(result.stderr)
            remote_lines = tuple(_extract_remote_lines(result.stderr))
            local_hook_lines = tuple(_extract_local_hook_lines(result.stderr))
            reached_transport = _has_to_line(result.stderr)

            remote_block = ""
            if remote_lines:
                remote_text = "\n".join(remote_lines)[:_PUSH_ERROR_REMOTE_LIMIT]
                remote_block = f" REMOTE MESSAGE ({sub_cause}): {remote_text}"

            local_hook_block = ""
            if local_hook_lines:
                local_hook_text = "\n".join(local_hook_lines)[:_PUSH_ERROR_LOCAL_HOOK_LIMIT]
                local_hook_block = (
                    f" LOCAL PRE-PUSH HOOK MESSAGE ({sub_cause}): {local_hook_text}"
                )

            # REPORTING RESOLVED FACTS, NOT GUESSES (module docstring;
            # CLAUDE.md hard rule 4): every field here is something this
            # call OBSERVED -- exit code, byte count, the parsed reason (or
            # its absence), whether transport was reached, the resolved
            # remote/refspec, and the lease flag with its origin. Never a
            # narrative claim about why a signal is absent. stderr_bytes is
            # measured on the RAW stderr length -- a byte count is not
            # secret-shaped and redaction can only shrink or replace
            # substrings, never usefully change what this count means
            # diagnostically.
            reason_note = f" reject-reason={reject_reason!r}" if reject_reason else " reject-reason=none"
            facts_note = (
                f" [exit={result.returncode} stderr_bytes={len(result.stderr)}"
                f" reached_transport={reached_transport} remote={remote!r}"
                f" refspec={refspec!r} force_with_lease={force_with_lease}"
                f" lease_origin={lease_origin!r}{reason_note}]"
            )

            message = (
                f"git push failed (exit {result.returncode}, {sub_cause}): "
                f"{result.stderr.strip()[:_PUSH_ERROR_STDERR_LIMIT]}"
                f"{remote_block}{local_hook_block}{mismatch_hint}{facts_note}"
            )

            raise GitPushError(
                message,
                exit_code=result.returncode,
                sub_cause=sub_cause,
                raw_stderr=result.stderr,
                reject_reason=reject_reason,
                remote_lines=remote_lines,
                local_hook_lines=local_hook_lines,
                reached_transport=reached_transport,
                remote=remote,
                refspec=refspec,
                lease_forced=force_with_lease,
                lease_origin=lease_origin,
                known_secrets=(token,),
            )

        if dry_run:
            # SUCCESSFUL dry-run: return the same redacted transcript this
            # function already printed above, so a caller that wants the
            # text programmatically (not just on stderr) has it -- never a
            # bare None a real push's own success path still returns.
            return dry_run_transcript
    return None


__all__ = [
    "GitFetchError",
    "GitVersionTooOldError",
    "RepoLocalConfigHazardError",
    "git_fetch_with_token",
    "git_push_with_token",
    "make_askpass_script",
]
