"""transport.body_env — body-off-argv-and-pipe ingestion (lr-10a996,
BODY-TRANSPORT half; sibling of transport.note_compose, the fence-
composition half already shipped in PR #46/lr-10a996 comment #3).

THE GAP THIS CLOSES (comment #2, ratified by comment #4 after #3's premature
close): --body-stdin (transport.git_host_api, review.verb) is a real fix for
the backtick/heredoc class of guard-hostile shell -- but it is NOT body-off-
argv. A caller still has to get the JSON body INTO the process's stdin
somehow, and the two shapes available to a shell caller are:

  1. `echo '{"body": "..."}' | some-verb ...`             -- an argv PRODUCER
  2. `some-verb ... <<< '{"body": "..."}'`  / `< body.json` -- a pipe/redirect

Shape 1 puts the JSON body -- including any embedded `{`, `"`, or `'` --
directly on the shell command line as the argument to `echo`. The Claude
Code HARNESS's own static Bash analyzer (a SEPARATE gate from guard-bash.py;
confirmed absent from this deployment's own guard scripts, see lr-10a996
comment #2) flags an inline brace+quote combination as "expansion
obfuscation" and raises a manual operator Allow/Deny prompt -- on EVERY
invocation, because the body payload (and therefore the exact brace/quote
sequence) is different every time. No static allowlist rule can ever admit
an infinite family of distinct per-invocation argv strings. Shape 2 has the
identical problem when the body is inlined into a heredoc/here-string, and
even a `< body.json` redirect names a PER-INVOCATION path if the body file
itself varies in name.

THE FIX: a SECOND, ADDITIONAL body-ingestion path (--body-stdin is
UNCHANGED and remains fully supported -- this does not replace it) whose
invoking command line is a CONSTANT string with NO per-invocation
substring at all. The mechanism:

  1. The caller's harness (e.g. a Claude Code agent's own Write tool) writes
     the JSON body to ONE FIXED, PER-SPAWN path:
     `$TMPDIR/clagentic-loadout/body.json` (TMPDIR-scoped per CLAUDE.md rule
     7 -- "no scratch files in the tree"; TMPDIR itself is already a
     per-spawn-isolated directory in a properly configured harness, so this
     never collides across concurrent invocations or leaks between spawns).
  2. The caller invokes the verb with a bare, ZERO-ARGUMENT switch
     (`--body-env` -- deliberately not `--body-file <path>`: taking no
     value at all, this flag can never vary the argv by even one
     character across invocations, unlike a flag whose value is a caller-
     supplied path).
  3. This module resolves the FIXED path (see `resolve_body_path`, no
     caller-supplied path ever enters this function), reads it, and returns
     the bytes -- functionally a drop-in replacement for
     `sys.stdin.buffer.read()` at each verb's own read site.

WHY THIS IS NOT THE PREVIOUSLY-REJECTED --body-file (codex engram
2026-07-08, conf 0.85, and the "no --body-file staging" contract locked in
transport.git_host_api's own module docstring / enforced suite-wide by
tests/test_reviewer_no_disk_staging_or_hand_authored_fence.py's
_DISK_STAGING_FLAG_MARKERS guard): the rejected shape was a flag taking a
CALLER-SUPPLIED path value (`--body-file /some/caller/chosen/path`) as a
second, parallel, arbitrary-location content source -- exactly the
"disk-staging flag" that regression guard exists to catch, and correctly
so: an arbitrary path is itself a per-invocation argv value if it ever
varies, and a second free-form content source reopens the surface --body-
stdin was deliberately built to close. `--body-env` is categorically
different on both axes: (a) it takes NO argument -- the flag name is the
entire contribution to argv, constant across every invocation of a given
verb, so it can never trip a per-invocation-substring analyzer; (b) the
path it reads is NOT caller-supplied at all -- it is fully owned and
computed by this module from `resolve_body_path`'s own fixed-directory
convention (see there), never a value threaded in from any flag or env var
whose CONTENT is a path a caller chose freely. `--body-env`'s name is a
deliberate signal of that difference: this is not "tell me a file," it is
"read the one fixed spot this deployment's harness convention already
defines" (mirroring the deployment env-override seam's own
`CLAGENTIC_LOADOUT_*`-branded, config-resolved-not-caller-typed pattern —
see merge.post_merge_config).

Because the mechanism is a fixed, well-known path rather than a caller-
named one, `tests/test_reviewer_no_disk_staging_or_hand_authored_fence.py`
would still correctly flag any flag literally named with a "file"/"-path"/
"staging" substring; `--body-env` avoids all three deliberately, and this
module's own tests assert the same disk-staging-shape guard passes with
`--body-env` present, documenting the API contract this module actually
provides (a fixed-path READ, not a caller-chosen file open).

TRADE-OFF NAMED EXPLICITLY (lr-10a996 comment #2's second candidate,
envelope-delivered body per the published contracts): an envelope-delivered
body (the body arriving as a field on the dispatch envelope itself, read
by loadout from the envelope object already in memory rather than from a
side-channel file) was also considered. Rejected for THIS task because (a)
loadout explicitly does not own dispatch/envelope DELIVERY -- see this
repo's CLAUDE.md rule 2, "does NOT own agent spawning or agent-to-agent
transport" -- so a verb reading "the envelope" would require a NEW,
loadout-owned envelope-ingestion contract (where does the envelope live on
disk/in-process? whose schema? delivered how?) that does not exist today
and is arguably a harness/orchestration-layer concern, not a git-host-
transport verb's; (b) the fixed-path mechanism ships TODAY with zero new
cross-repo contract surface, reusing patterns (TMPDIR-scoped staging, a
Write-tool-populated fixed path) already proven in this deployment's own
lr-d461-sanctioned two-step PR-body pattern; (c) nothing forecloses adding
an envelope-delivered path LATER as a third ingestion tier if a consuming
harness's own envelope contract matures to carry body payloads directly --
this module's `resolve_body_path` / `read_body_bytes` split keeps that
option open without requiring it now. If a deployment's own harness
already has a stronger envelope-delivery guarantee, --body-stdin (piped
from an in-process JSON dump rather than a shell producer, when the
delivering process itself is not a shell command line at all) remains
available unchanged.

CONCURRENT SAME-TMPDIR COLLISION (lr-3a7ae8, gate-integrity fix): the
mechanism above describes ONE fixed path shared by every `--body-env`
caller on a given TMPDIR root. That is exactly right for a single caller
across retries -- and exactly wrong the moment two DIFFERENT callers
(e.g. two concurrently-dispatched reviewer subagents) share one TMPDIR
(lr-b3a7bf's TMPDIR=/tmp precondition for the console daemon and every
Agent()-spawned crew agent makes this the common case, not an edge case).
Root cause, verified via a structured diagnosis on a real incident
(lr-f00c6f): caller A stages its body at the fixed path, caller B's
staging OVERWRITES the same physical file before caller A's own verb
invocation gets to read it back, and caller A silently posts caller B's
content under caller A's own identity -- `read_body_bytes` had no way to
know the bytes it read were not the bytes its own caller staged.

THE FIX: `read_body_bytes` / `resolve_body_path` gain an optional
*caller*-namespaced sibling pair -- `resolve_caller_body_path` /
`read_caller_body_bytes` -- that compute a PER-CALLER fixed path
(`<TMPDIR>/clagentic-loadout/body.<caller>.json`) instead of the single
shared one. This is additive, not a breaking change to the original
mechanism: `resolve_body_path`'s signature and the bare
`read_body_bytes()` (no *caller* argument) are UNCHANGED byte-for-byte --
a caller that never supplies its own identity keeps the original
single-fixed-path behavior verbatim (e.g. a standalone/local run with no
concurrency concern at all). `read_body_bytes(caller=...)` is the SAME
function, extended with an optional keyword that switches it onto the
caller-namespaced path when a caller identity is available -- which is
every case a verb parser here recognizes has one (`--caller`, already a
required-shaped concept via `_SAFE_CALLER_RE`-equivalent validation, see
`_validate_caller_or_raise` below).

Because the path now differs per caller, TWO callers on the same TMPDIR
can never share a physical file at all -- caller A's write and caller B's
write land at two distinct paths, structurally, with no read-time race
possible. This is stronger than a same-path "last writer wins" ownership
check would be: there is no window where caller A's read could observe
caller B's write, because caller A never reads caller B's path in the
first place. A caller that (by a broken harness, or a caller argument the
harness got wrong) ends up reading before ITS OWN write has landed still
fails closed -- `read_caller_body_bytes` raises `BodyEnvError` exactly
like the original "no body staged" case, never falls back to a different
caller's file.

Still NOT a revival of the previously-rejected `--body-file` staging flag
(see the trade-off above): the caller-namespaced path is still fully
computed by this module from a value (`--caller`) every write-method verb
already requires and validates -- never an arbitrary caller-typed path
argument. `--body-env` remains a bare, zero-argument switch on every verb
surface; the namespacing is an internal resolution detail the verb's own
`--caller` flag now also feeds, not a new flag.

SEQUENTIAL STALE-READ (lr-becdef, PR #388 foreign-body incident): the
lr-3a7ae8 fix above closes CONCURRENT same-TMPDIR collision between two
DIFFERENT callers -- but it does nothing for a SEQUENTIAL stale read of
the SAME caller's own path. `_read_staged_bytes` never consumed the
staged file (see its old docstring's "may legitimately be re-read"
rationale), and the per-caller path is fixed and session-persistent on
TMPDIR=/tmp (lr-b3a7bf) with no per-spawn cleanup on the in-session-
sequential dispatch path. Concretely: caller X reviews PR A, its body
lands at `body.X.json`; a LATER, unrelated invocation of caller X (PR B,
or even a different repo) invokes `--body-env` again, its own staging
write is skipped/guard-denied/never runs, and the verb silently re-reads
and re-POSTS PR A's leftover body under PR B's identity. Nothing bound
the staged bytes to the invocation that was supposed to have produced
them.

THE FIX, two parts (both required; see resolve_caller_body_path's stamp
parameter, `stage_caller_body`, and `read_caller_body_bytes`'s
*expect_target_pr* below):

  1. READ-AND-CONSUME (PRIMARY): `_read_staged_bytes` now unlinks the
     staged body file (and its stamp sidecar, if present) immediately
     after a successful read. A retried invocation must RE-STAGE, not
     re-read -- a missing stage then fails closed via the existing
     `BodyEnvError` "no body staged" path, exactly like today, rather
     than ever risking a leftover foreign body. This overturns the
     previous "a fixed path may legitimately be re-read" assumption:
     retries re-stage, they do not re-read.

  2. IDENTITY STAMP + REJECT-ON-MISMATCH (defense-in-depth): the staging
     side (`stage_caller_body`) writes a JSON stamp sidecar alongside the
     body -- `body.<caller>.stamp.json` -- carrying `target_pr` (the PR
     this body was staged for), an optional `head_sha`, and `staged_at`
     (UTC timestamp of the staging write). `read_caller_body_bytes` (and
     `read_body_bytes(caller=..., expect_target_pr=...)`) REQUIRE a
     caller-supplied `expect_target_pr` whenever the read is
     provenance-checked, and reject with `BodyEnvError` -- WITHOUT
     consuming anything -- if the stamp's `target_pr` (or `head_sha`,
     when the reader supplies one to compare against) does not match.
     This converts a silent wrong-body POST into a clean fail-closed
     error even in a hypothetical future where consumption itself is
     bypassed or raced. A caller reading a body staged for a DIFFERENT
     PR must never see it as legitimate content -- see
     `TestOwnerMismatchFailsClosed`'s sibling, the stale-PR regression
     suite in tests/test_transport_body_env.py.

Both read call sites (`review.verb` and `transport.git_host_api`) migrate
to this stamped, consuming API in the SAME shared module (this file) --
one fix covers both hosts, since `transport.git_host_api` imports this
module's `read_body_bytes` unchanged rather than reimplementing its own
staging logic. `read_body_bytes(caller=...)` with NO `expect_target_pr`
keeps its prior, PRE-lr-becdef behavior (read, no stamp check, no
consume) for any caller that has not migrated yet -- but every production
write-method verb in this package now always supplies `expect_target_pr`
(see `review.verb._run` and `transport.git_host_api._run`), so that
legacy shape is dead code in production, kept only so a bare unit test of
the underlying primitive is not forced to fabricate a PR number it does
not have.

CREATE-MODE STAGING (lr-e1e2fb, PR #136 security-audit + operator design
correction): `push.verb`'s create path (opening a NEW PR) could not use
`--body-env` at all before this fix, because the identity stamp REQUIRED a
positive-int `target_pr` -- but a PR-open call has no PR number yet by
definition. THE PRIOR FIX FOR THIS (a caller-supplied `--body-file PATH`
argument, validated against a scratch-boundary allowlist at read time) was
REJECTED after a security audit and an explicit operator correction: a
validated arbitrary path still ACCEPTS a location parameter, and every
containment check is one canonicalization edge case, one symlink race, one
future refactor away from a bypass -- it left the door open by design
rather than removing the location-parameter surface entirely. The
operator's requirement: the caller supplies CONTENT, never a LOCATION: if
loadout decides where staged body text lives, there is no location to
validate and no bypass surface at all.

THE ACTUAL FIX: extend THIS ALREADY-SANCTIONED mechanism (`--body-env` /
`stage_caller_body` / `read_caller_body_bytes`, the one write path a
reviewer role's allowlist-safe harness can already reach without any raw
`*.stamp.json` redirection) to cover PR CREATION, rather than adding any
new caller-supplied-path input anywhere. `stage_caller_body` and
`read_caller_body_bytes` both now accept EITHER `target_pr` (existing-PR
binding, unchanged) OR `create_branch` (create-path binding) -- exactly
one, never both, never neither. `create_branch` is the git branch that
will open the new PR, resolved by `push.verb` itself via
`git rev-parse --abbrev-ref HEAD` BEFORE staging and BEFORE reading --
never a caller-typed value. This preserves the identity stamp's entire
point in both modes: the PLATFORM computes what a staged body is bound to,
a caller cannot hand-author it, and a caller supplies body CONTENT only,
never a filesystem location of any kind. See `stage_caller_body`'s and
`read_caller_body_bytes`'s own docstrings for the exact contract.

ABANDONED-PAIR REAPER (lr-4c1646, the durable-debris half the task's
review-corrected scope narrowed this to).

WHAT THIS DOES NOT NEED TO COVER (already owned elsewhere): the happy
path -- stage then a SUCCESSFUL `read_caller_body_bytes` -- already
deletes both the body and its stamp (see "SEQUENTIAL STALE-READ" above,
lr-becdef). A harness that stages and is later read back leaves nothing
behind. That is not this section's job.

WHAT REMAINS UNOWNED: a body+stamp pair whose producer staged it and then
crashed, was killed, or otherwise never reached the matching --body-env
read; and a pair a stamp-mismatch read deliberately left in place (correct
at read-time -- it may belong to a different, still-pending invocation --
but if that invocation never comes, in the same-caller reused-namespace
convention this module uses, that pair is now permanently unreachable, an
older sibling under the SAME caller's own path superseded by a newer
stage). On a persistent TMPDIR (lr-b3a7bf, TMPDIR=/tmp for the console
daemon and every dispatched agent spawn) these accumulate with no TTL
and no reaper -- the exact debris class this task's operator directive
(2026-07-28) is about.

THE FIX: `sweep_abandoned_pairs`, an opportunistic, age-based reaper over
the `clagentic-loadout` staging subdirectory (never a caller-supplied
path -- this walks the SAME fixed subdirectory `resolve_body_path`/
`resolve_caller_body_path` already resolve to, nothing new). It is called
from both ends of the mechanism this module already owns:
`stage_caller_body` (a new stage sweeps siblings first) and
`read_caller_body_bytes` (a read, successful or not, sweeps siblings
after its own body/stamp decision is made) -- so the sweep runs on the
SAME two verbs an operator already invokes for the ordinary staging
lifecycle, with no new verb, no new cron, no separate reaper process to
schedule or forget to run. This is deliberately "opportunistic": there is
no guarantee a sweep ever runs on a host where no loadout verb is ever
invoked again (see the honest-coverage note below) -- and that gap is
accepted, not hidden.

WARN, NEVER FAIL (operator constraint, binding): a sweep failure (a
permission-denied stat, a race where a sibling is unlinked by something
else between listing and removal, a non-file directory entry) is caught,
logged to stderr, and never raises -- a cleanup miss must never turn a
successful stage or a successful read into a failed operation, and must
never change the caller's exit code. See `_sweep_stale_siblings`'s own
docstring for the exact per-file failure handling.

TTL, not an enumerated allowlist of "safe to delete" filenames: any
regular file directly inside the staging subdirectory (not a
subdirectory, not `acquire.scratch`'s own separate
`clagentic-loadout-acquire` namespace -- this reaper is scoped to exactly
the subdirectory `stage_caller_body`/`read_caller_body_bytes` write into)
whose mtime is older than `_ABANDONED_PAIR_TTL_SECONDS` is treated as
abandoned and removed, regardless of whether it looks like a body file, a
stamp file, or something else entirely -- an unrecognized filename is not
a reason to leave debris in place forever; it is more evidence something
here is stale, not less.

HONEST COVERAGE STATEMENT (required by the task; do not overclaim):
  - Consume-on-read (lr-becdef, already shipped) covers every COMPLETED
    stage-then-read cycle -- the common case, and the only case this
    reaper does NOT need to touch.
  - This reaper covers ABANDONMENT: a stage with no matching read (crash,
    kill, guard denial), and a stamp-mismatch read that correctly left a
    pair in place for a pending invocation that never arrives -- as long
    as SOME loadout verb that touches this staging subdirectory (a stage
    or a read, for ANY caller) runs again on the same TMPDIR after the
    TTL has elapsed.
  - NOTHING covers a host where no loadout verb in this staging path ever
    runs again -- there is no cron, no daemon, no process outside a
    verb invocation that ever sweeps this directory. That gap is
    accepted: the staging root is $TMPDIR, an already-inherently-volatile
    location by convention, not a durable store this package is
    responsible for reclaiming unconditionally.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class BodyEnvError(Exception):
    """Raised when --body-env is requested but the fixed body path is
    missing, empty, or unreadable. Carries no exit code of its own --
    each verb's own call site maps this to that verb's usage-error exit
    code, exactly like every other precondition failure in this package."""


#: Shared `--body-env` help-text fragment (lr-ae00e6), appended verbatim by
#: every verb that exposes this flag (transport.git_host_api, review.verb,
#: push.verb, transport.stage_body_verb's own epilog) so the discoverability
#: fix lands identically everywhere rather than drifting across four
#: hand-authored copies.
#:
#: WHY THIS TEXT EXISTS: a real incident (this task's own evidence) had two
#: separate crew agents misdiagnose a stamp/target mismatch as "the
#: architecture requires staging and posting in the same shell to preserve
#: ephemeral env vars, and the guard's ban on compound expressions breaks
#: that." Both diagnoses were false -- staging is a plain local filesystem
#: write (a body file plus an identity-stamp sidecar) that persists across
#: separate process invocations BY DESIGN; two separate Bash calls are the
#: CORRECT and INTENDED shape, not a workaround for a limitation. The flag
#: name `--body-env` itself invites the wrong inference (it sounds like an
#: environment variable, which by definition would NOT survive a second,
#: separate invocation) -- this text corrects that inference at the exact
#: place a caller reads before ever forming it.
BODY_ENV_NOT_EPHEMERAL_NOTE = (
    "IMPORTANT: despite the flag name, this is NOT an environment variable "
    "and nothing here is ephemeral. --body-env reads a body your harness "
    "staged via `loadout-stage-body` -- a plain local FILESYSTEM WRITE (a "
    "body file plus an identity-stamp sidecar under $TMPDIR/clagentic-"
    "loadout/) that persists on disk across separate process invocations. "
    "Staging and reading in TWO SEPARATE commands is the correct, intended "
    "shape, not a workaround -- the guard's ban on compound shell "
    "expressions is not an obstacle to this contract; it is fully "
    "compatible with it. See docs/integration.md's '--body-env: the "
    "harness-side staging process' section for the full two-call contract "
    "and copy-pasteable examples."
)


#: Shared JSON-contract-failure guidance (mirrors BODY_ENV_NOT_EPHEMERAL_NOTE's
#: "one text, every verb" pattern immediately above): appended to a
#: --body-stdin JSON-validation failure by every verb that exposes that flag
#: (transport.git_host_api, review.verb). Load-bearing, not cosmetic -- a
#: caller that hand-built this JSON inside a shell command line and lost
#: content to that shell's own quoting rules experiences the failure as "this
#: tool cannot post a multi-line/backtick-bearing body," and will conclude
#: the capability is absent rather than that it reached past the sanctioned
#: --body-env route. Naming that route, and stating explicitly that dense
#: content (backticks, fenced code, embedded JSON, quotes) is fully
#: supported through it, is the fix for that belief -- not tighter escaping.
BODY_STDIN_CONTRACT_GUIDANCE = (
    " A review/comment body of ANY shape is fully supported -- multi-line "
    "prose, backticks, fenced code blocks, embedded JSON, and quotes all "
    "post correctly. If this content was hand-built inside a shell command "
    "line and lost to that shell's own quoting rules, use "
    "loadout-stage-body to write the exact body bytes to a file first, "
    "then invoke this command with --body-env instead of --body-stdin (no "
    "shell quoting of the body content at all): "
    'echo \'{"body": "<the body, any content>"}\' | '
    "loadout-stage-body --caller <role> --target-pr <n>, then re-run this "
    "command with --body-env (same --caller, same PR)."
)


def augment_body_contract_error(message: str) -> str:
    """Append `BODY_STDIN_CONTRACT_GUIDANCE` to a JSON-contract failure
    message exactly once. Shared by every --body-stdin call site so the
    guidance text cannot drift between verbs or between a verb's own
    ordinary/verdict routes -- all of them read the same shape of bytes
    through the same JSON contract, and all of them are reachable by a
    caller whose hand-built shell producer mangled the JSON."""
    return f"{message}{BODY_STDIN_CONTRACT_GUIDANCE}"


def _recovery_stage_command(
    *, caller: str, target_pr: int | None = None, create_branch: str | None = None
) -> str:
    """Build the copy-pasteable `loadout-stage-body` invocation that
    correctly re-stages a body for *caller*, bound to whichever value is
    actually expected (lr-e1e2fb follow-up, error-message consistency
    review finding): every read-side mismatch/missing-stage `BodyEnvError`
    this module raises
    must name the EXACT recovery command, with the caller's own already-
    resolved values substituted in, not a generic placeholder -- an agent
    hitting this error must know exactly what to run next, the same
    standard `push.verb`'s `_BODY_STDIN_STAGING_POINTER` already holds
    --body-stdin failures to. This is the SINGLE constructor every mismatch
    message below calls, rather than each raise site hand-composing its own
    variant of "here is how you stage a body correctly".

    EXACTLY ONE of *target_pr* / *create_branch* is expected (mirroring the
    exactly-one-required contract every other function in this module
    already enforces) -- this is a pure string-building helper with no
    validation of its own; callers already know which mode they are in by
    construction, so this function trusts that rather than re-checking it.
    """
    if target_pr is not None:
        binding_flag = f"--target-pr {target_pr}"
    else:
        binding_flag = f"--create-branch {create_branch}"
    return (
        f"echo '{{\"body\": \"<your PR body text>\"}}' | "
        f"loadout-stage-body --caller {caller} {binding_flag}"
    )


#: Env var naming the per-spawn TMPDIR root a harness already provides
#: (CLAUDE.md rule 7 -- "per-spawn TMPDIR only", not a hardcoded machine
#: path). This module never invents its own tmp-root: it reads the SAME
#: TMPDIR every other per-spawn scratch convention in this deployment reads,
#: falling back to the platform default temp directory (via
#: `tempfile.gettempdir()`-equivalent resolution) only when TMPDIR itself is
#: unset -- e.g. local dev/test runs outside a harness spawn.
_TMPDIR_ENV_VAR = "TMPDIR"

#: Subdirectory name (under the resolved TMPDIR root) this module reads the
#: fixed body path from. A dedicated subdirectory -- not the bare TMPDIR
#: root -- so a harness populating this file can never collide with any
#: other per-spawn scratch content sharing the same TMPDIR.
_BODY_ENV_SUBDIR = "clagentic-loadout"

#: The fixed filename within `_BODY_ENV_SUBDIR`. Callers never choose this
#: name -- see the module docstring for why a fixed, non-caller-supplied
#: path is the entire point of this mechanism.
_BODY_ENV_FILENAME = "body.json"

#: Suffix appended to a caller-namespaced body filename to name its
#: identity-stamp sidecar (lr-becdef): `body.<caller>.json` stages the
#: raw bytes, `body.<caller>.stamp.json` stages the provenance JSON
#: (`target_pr`, optional `head_sha`, `staged_at`) `read_caller_body_bytes`
#: checks BEFORE trusting the body bytes belong to the current invocation.
_BODY_ENV_STAMP_SUFFIX = ".stamp.json"

#: Age threshold (lr-4c1646) past which a file directly inside the
#: `clagentic-loadout` staging subdirectory is treated as an abandoned
#: sibling and reaped -- see `sweep_abandoned_pairs`. One hour comfortably
#: exceeds any real stage-then-read latency (a caller's own harness stages
#: and reads back within the same short-lived spawn), while still being
#: short enough that a crashed/killed producer's debris does not survive
#: indefinitely on a persistent TMPDIR (lr-b3a7bf).
_ABANDONED_PAIR_TTL_SECONDS = 3600

#: Bare role/caller name pattern -- the SAME shape
#: `transport.git_host_api._SAFE_CALLER_RE` already validates a verb's
#: `--caller` value against. Duplicated here rather than imported: this
#: module is a low-level transport primitive other transport modules
#: (git_host_api, review.verb) import FROM, and importing git_host_api's
#: private regex back into body_env would invert that dependency direction
#: for a two-line constant. Both patterns must stay in lockstep by
#: construction -- see the "kept in lockstep" test in
#: tests/test_transport_body_env.py.
#:
#: Anchored with \A...\Z, not ^...$ (lr-3e3318, same sibling fix as
#: git_host_api._SAFE_CALLER_RE and credential_provider._SAFE_ROLE_RE/
#: _SAFE_REPO_RE): '$' without re.MULTILINE also matches just before a
#: trailing newline in Python.
_SAFE_CALLER_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


def _validate_caller_or_raise(caller: str) -> str:
    """Reject a *caller* value that is not a bare role/name token before it
    is ever composed into a filesystem path. A caller string reaching this
    function has ALREADY been validated by its own verb's `--caller`
    precondition in production (both `review.verb` and
    `transport.git_host_api` enforce this shape before resolving a token) --
    this is a second, defense-in-depth check at the point the value is
    actually used to build a path, so this module never trusts an upstream
    validator it does not control to have run.
    """
    if not _SAFE_CALLER_RE.match(caller):
        raise BodyEnvError(
            f"--body-env: caller {caller!r} is not a valid bare role/name "
            f"token (only alphanumeric, hyphen, underscore, 1-64 chars, no "
            f"path separators or traversal) -- refusing to compose it into "
            f"a staged-body path."
        )
    return caller


def resolve_body_path(*, env: dict[str, str] | None = None) -> Path:
    """Resolve the ONE fixed path `--body-env` reads from.

    `<TMPDIR>/clagentic-loadout/body.json`, where TMPDIR is read from the
    *env* mapping (defaults to the real process environment) via
    `_TMPDIR_ENV_VAR`, falling back to `tempfile.gettempdir()` when TMPDIR
    is unset/empty -- the same fallback Python's own `tempfile` module uses,
    so a bare local run (no harness-supplied TMPDIR) still resolves to a
    real, writable directory rather than raising.

    *env* is an injection point for tests; production callers never pass it
    (the real `os.environ` is used). No caller-supplied path of any kind is
    ever accepted here -- this function's entire contract is "compute the
    one fixed location," not "resolve a caller's choice."
    """
    import tempfile

    active_env = env if env is not None else os.environ
    tmp_root = active_env.get(_TMPDIR_ENV_VAR) or tempfile.gettempdir()
    return Path(tmp_root) / _BODY_ENV_SUBDIR / _BODY_ENV_FILENAME


def resolve_caller_body_path(*, caller: str, env: dict[str, str] | None = None) -> Path:
    """Resolve the PER-CALLER fixed path a *caller*-namespaced `--body-env`
    read uses: `<TMPDIR>/clagentic-loadout/body.<caller>.json`.

    This is the lr-3a7ae8 collision fix's structural half: two DIFFERENT
    *caller* values resolve to two DIFFERENT physical paths on the same
    TMPDIR, so two concurrent same-TMPDIR callers can never stage into (or
    read from) the same file -- there is no shared path for a race to land
    on. Still governed by the same "no caller-supplied path" contract as
    `resolve_body_path`: *caller* is a validated bare role/name TOKEN
    (`_validate_caller_or_raise`, the same shape `--caller` is already
    validated against at each verb's own precondition), never an arbitrary
    path or filename -- a caller cannot direct this function outside the
    fixed `clagentic-loadout` subdirectory by any value of *caller*.

    Raises BodyEnvError if *caller* is not a valid bare role/name token.
    """
    safe_caller = _validate_caller_or_raise(caller)
    import tempfile

    active_env = env if env is not None else os.environ
    tmp_root = active_env.get(_TMPDIR_ENV_VAR) or tempfile.gettempdir()
    filename = f"body.{safe_caller}.json"
    return Path(tmp_root) / _BODY_ENV_SUBDIR / filename


def _resolve_caller_stamp_path(*, caller: str, env: dict[str, str] | None = None) -> Path:
    """Resolve the identity-stamp sidecar path for *caller*'s staged body
    (lr-becdef): `<TMPDIR>/clagentic-loadout/body.<caller>.stamp.json`,
    sitting alongside `resolve_caller_body_path`'s own body file. Shares
    the same validated-caller contract -- never an arbitrary path."""
    safe_caller = _validate_caller_or_raise(caller)
    import tempfile

    active_env = env if env is not None else os.environ
    tmp_root = active_env.get(_TMPDIR_ENV_VAR) or tempfile.gettempdir()
    filename = f"body.{safe_caller}{_BODY_ENV_STAMP_SUFFIX}"
    return Path(tmp_root) / _BODY_ENV_SUBDIR / filename


def _resolve_staging_dir(*, env: dict[str, str] | None = None) -> Path:
    """Resolve the `clagentic-loadout` staging subdirectory itself (the
    parent of every path `resolve_body_path`/`resolve_caller_body_path`/
    `_resolve_caller_stamp_path` compute) -- the one directory
    `sweep_abandoned_pairs` (lr-4c1646) walks. Never a caller-supplied path:
    this is the exact same fixed subdirectory those functions already
    resolve to, computed the same way."""
    import tempfile

    active_env = env if env is not None else os.environ
    tmp_root = active_env.get(_TMPDIR_ENV_VAR) or tempfile.gettempdir()
    return Path(tmp_root) / _BODY_ENV_SUBDIR


def sweep_abandoned_pairs(
    *,
    env: dict[str, str] | None = None,
    ttl_seconds: float = _ABANDONED_PAIR_TTL_SECONDS,
    now: float | None = None,
) -> int:
    """Opportunistically reap abandoned siblings under the
    `clagentic-loadout` staging subdirectory (lr-4c1646) -- see this
    module's own "ABANDONED-PAIR REAPER" docstring section for the full
    design rationale and the honest coverage statement.

    Removes every REGULAR FILE directly inside the staging subdirectory
    whose mtime is older than *ttl_seconds* (default
    `_ABANDONED_PAIR_TTL_SECONDS`). Not recursive into subdirectories --
    this module never creates one, so a subdirectory found here would be
    foreign content this sweep has no business touching. *now* is an
    injection point for tests (defaults to `time.time()`); *env* overrides
    `os.environ` exactly like every other resolver in this module.

    WARN, NEVER FAIL (operator constraint, binding): this function NEVER
    raises. A missing staging directory is not an error (there is nothing
    to sweep -- the common case on a fresh TMPDIR). Any per-file failure
    (permission denied, a race where the file is removed by something else
    between listing and unlink, a stat failure) is caught and skipped --
    see `_sweep_stale_siblings` for the per-file handling this delegates
    to. A cleanup miss must never fail the caller's own stage/read
    operation or change its exit code.

    Returns the count of files actually removed (0 if the directory does
    not exist, is empty, or every candidate is either too young or failed
    to remove) -- callers (`stage_caller_body`, `read_caller_body_bytes`)
    do not currently act on this count, but a caller wiring in its own
    telemetry/logging can.
    """
    try:
        staging_dir = _resolve_staging_dir(env=env)
        current_time = now if now is not None else time.time()
        return _sweep_stale_siblings(staging_dir, ttl_seconds=ttl_seconds, now=current_time)
    except Exception:  # noqa: BLE001 -- warn, never fail (operator constraint)
        # A failure resolving the staging directory itself (e.g. TMPDIR
        # resolves to something unreadable) is exactly as much a cleanup
        # miss as a per-file failure -- never let it propagate into a
        # caller's stage/read outcome.
        print(
            "loadout: --body-env abandoned-pair sweep could not resolve "
            "the staging directory -- skipping this sweep (non-fatal).",
            file=sys.stderr,
        )
        return 0


def _sweep_stale_siblings(staging_dir: Path, *, ttl_seconds: float, now: float) -> int:
    """Remove every regular file directly inside *staging_dir* whose mtime
    is older than *ttl_seconds* relative to *now*. Never raises -- each
    directory-listing failure and each per-file stat/unlink failure is
    caught, logged to stderr, and skipped; this function always returns a
    (possibly zero) count rather than propagating an exception, so its
    caller (`sweep_abandoned_pairs`) never has to handle one either."""
    if not staging_dir.is_dir():
        return 0

    removed = 0
    try:
        entries = list(staging_dir.iterdir())
    except OSError as exc:
        print(
            f"loadout: --body-env abandoned-pair sweep could not list "
            f"{str(staging_dir)!r}: {exc} -- skipping this sweep (non-fatal).",
            file=sys.stderr,
        )
        return 0

    for entry in entries:
        try:
            if entry.is_symlink():
                # Checked BEFORE is_file()/stat() below, both of which
                # follow a symlink to its target by default (pathlib
                # semantics) -- without this check, a symlink whose target
                # happens to be a regular file would be judged by the
                # TARGET's mtime and unlinked, contradicting this
                # function's own contract (only a plain regular file
                # directly in the staging directory is ever a candidate).
                # entry.unlink() only ever removes the directory entry
                # itself, never the target it points at (POSIX semantics),
                # so this is a correctness fix, not a symlink-attack
                # mitigation -- but a symlink is still never something
                # this sweep was designed to reason about, so it is
                # skipped outright rather than followed.
                continue
            if not entry.is_file():
                # Never recurse into a subdirectory (this module creates
                # none) and never touch a special file (fifo, socket,
                # device) this sweep was not designed to reason about --
                # only a plain regular file is ever a candidate.
                continue
            age = now - entry.stat().st_mtime
            if age < ttl_seconds:
                continue
            entry.unlink()
            removed += 1
        except OSError as exc:
            # A single sibling's stat/unlink failure (permission denied, a
            # concurrent remover winning a race, the file vanishing between
            # iterdir() and stat()) must never abort the sweep for every
            # OTHER sibling, and must never propagate to the caller's own
            # stage/read outcome -- warn and continue.
            print(
                f"loadout: --body-env abandoned-pair sweep could not remove "
                f"{str(entry)!r}: {exc} -- leaving it in place (non-fatal).",
                file=sys.stderr,
            )
    return removed


@dataclass(frozen=True)
class _StagedStamp:
    """Parsed identity-stamp sidecar (lr-becdef, create-mode added
    lr-e1e2fb): the provenance a staged body is bound to. EXACTLY ONE of
    *target_pr* / *create_branch* is set -- an existing-PR binding
    (update/comment path) or a not-yet-created-PR binding (push's
    create path), never both, never neither (mirrors the same
    exactly-one-required contract `stage_caller_body` enforces at write
    time). *head_sha* is optional -- not every write-method call that
    stages a body has an evaluated head SHA to bind against (e.g. an
    ordinary, non-verdict review comment)."""

    target_pr: int | None
    create_branch: str | None
    head_sha: str | None
    staged_at: str


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write *data* to *path* durably and atomically (lr-765172, silent-
    producer-failure fix).

    Writes to a same-directory temp file, `os.fsync`s the file descriptor
    BEFORE `os.replace` swaps it into place (so a crash/interruption can
    never observe a half-written destination file -- a bare `write_bytes`
    can leave a short/partial file visible mid-write, and the write may be
    acknowledged by the OS without being durable yet), then `os.fsync`s the
    containing directory so the rename itself is durable too (POSIX does
    not guarantee a rename survives a crash without a directory fsync).
    The temp file lives in the SAME directory as *path* so `os.replace` is
    guaranteed atomic (a cross-filesystem rename is not); on any failure
    the temp file is removed rather than left as debris.
    """
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as tmp_file:
            tmp_file.write(data)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise

    dir_fd = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def stage_caller_body(
    *,
    caller: str,
    body_bytes: bytes,
    target_pr: int | None = None,
    create_branch: str | None = None,
    head_sha: str | None = None,
    env: dict[str, str] | None = None,
) -> None:
    """Stage *body_bytes* at the caller-namespaced fixed path, with an
    identity-stamp sidecar binding it to *target_pr* (and, when supplied,
    *head_sha*) (lr-becdef, Axis 1 defense-in-depth half). This is the
    WRITE side a harness's own staging step (its Write tool or equivalent)
    performs before invoking a verb with `--body-env`; production harnesses
    are not required to call this Python function directly (they write
    the two files themselves per docs/integration.md), but this is the
    single source of truth for the stamp's exact shape and is what this
    module's own tests use to stage a well-formed body+stamp pair.

    EXACTLY ONE of *target_pr* / *create_branch* is required (lr-e1e2fb,
    PR-CREATION staging): the ORIGINAL contract required a positive-int
    *target_pr* unconditionally, because the stamp exists to bind a staged
    body to an EXISTING PR (`--body-env`'s update/comment read side).
    PR-CREATION has no PR number yet at staging time by definition -- a
    caller opening a NEW PR passes *create_branch* (the git branch that
    will open the PR) instead, and the stamp records that binding under a
    `create_branch` key rather than `target_pr`. The platform still COMPUTES
    every stamp value in both modes -- `push.verb` resolves its own current
    branch via `git rev-parse --abbrev-ref HEAD` before staging, never
    accepting a caller-typed branch name as the binding key -- so this
    extension preserves the exact property the identity stamp exists for:
    a caller cannot hand-author what a staged body is bound to, in either
    mode. See `read_caller_body_bytes`'s `expect_create_branch` parameter
    for the symmetric read-side check.

    *staged_at* is captured here, at staging time, as the current UTC
    instant -- `read_caller_body_bytes` never has to trust a caller-typed
    timestamp for the "when did this land" half of provenance, because it
    only checks target_pr/create_branch/head_sha identity, not a freshness
    window (a freshness window would reintroduce the exact "genuinely fresh
    but wrong content" hole --verify-comment's own freshness anchor already
    has -- see the module docstring's SEQUENTIAL STALE-READ section).

    DURABILITY (lr-765172, silent-producer-failure fix): both files are
    written via `_atomic_write_bytes` (same-dir temp file + `os.fsync` +
    `os.replace` + directory fsync) rather than a bare `write_bytes`/
    `write_text` -- the pre-fix implementation returned successfully the
    instant the two independent, non-atomic, non-fsync'd writes RETURNED,
    with no guarantee either was durably on disk and no readback to
    confirm it. After both writes, this function reads back both files and
    raises `BodyEnvError` if either is absent or empty -- a caller of this
    function (in production, `stage_body_verb._run`) can therefore never
    observe a success return while the stamp (or body) is missing or
    partial; the contract the module's docstrings already claimed
    ("atomic") is now actually enforced, not just documented.

    ABANDONED-PAIR SWEEP (lr-4c1646): before staging, this function
    opportunistically sweeps stale siblings out of the SAME staging
    subdirectory it is about to write into (`sweep_abandoned_pairs`,
    warn-never-fail -- a sweep failure never blocks or fails this stage).
    This is one of the two hook points the reaper runs from (the other is
    `read_caller_body_bytes`); see this module's own "ABANDONED-PAIR
    REAPER" docstring section for the full design and honest coverage
    statement.
    """
    sweep_abandoned_pairs(env=env)

    if bool(target_pr) == bool(create_branch):
        raise BodyEnvError(
            "--body-env: stage_caller_body requires EXACTLY ONE of "
            f"target_pr / create_branch (got target_pr={target_pr!r}, "
            f"create_branch={create_branch!r}) -- a staged body is bound "
            f"either to an existing PR (update/comment path) or to the "
            f"branch that will open a new one (create path), never both "
            f"and never neither."
        )

    body_path = resolve_caller_body_path(caller=caller, env=env)
    stamp_path = _resolve_caller_stamp_path(caller=caller, env=env)
    body_path.parent.mkdir(parents=True, exist_ok=True)

    stamp = {
        "target_pr": target_pr,
        "create_branch": create_branch,
        "head_sha": head_sha,
        "staged_at": datetime.now(timezone.utc).isoformat(),
    }
    stamp_bytes = json.dumps(stamp).encode("utf-8")

    _atomic_write_bytes(body_path, body_bytes)
    _atomic_write_bytes(stamp_path, stamp_bytes)

    # Verify-after-write (lr-765172): a durable rename returning without
    # error is still not proof the destination is readable (e.g. a
    # filesystem quirk between the fsync'd write and this read) -- fail
    # closed here rather than let the verb layer report success on an
    # absent stamp/body. Existence + readability is checked for BOTH
    # files; a zero-byte-content check is applied ONLY to the stamp (this
    # module always writes a non-empty JSON object for it, so any
    # zero-byte stamp is unconditionally a write failure). The body is
    # deliberately NOT size-checked here: an empty *body_bytes* is a
    # content-validation concern the verb layer's own
    # `validate_body_stdin_content` already enforces before staging is
    # ever reached (see stage_body_verb._run) -- and a caller of this
    # function directly (e.g. a test staging a deliberately empty body to
    # exercise a downstream reader's own empty-content rejection) must
    # still be able to stage a durable, readable, zero-length body file
    # without that being confused for a failed write.
    for staged_path, label in ((body_path, "body"), (stamp_path, "identity stamp")):
        try:
            staged_path.stat()
        except OSError as exc:
            raise BodyEnvError(
                f"--body-env: staged {label} at {str(staged_path)!r} could "
                f"not be verified after write: {exc}."
            ) from exc

    if stamp_path.stat().st_size == 0:
        raise BodyEnvError(
            f"--body-env: staged identity stamp at {str(stamp_path)!r} is "
            f"empty immediately after write -- refusing to report a "
            f"successful stage."
        )


def _read_staged_bytes(path: Path) -> bytes:
    """Shared read-and-translate-errors body for both the single-fixed-path
    and the caller-namespaced entry points below -- the failure shape
    (missing / not-a-file / unreadable, each mapped to `BodyEnvError`) is
    identical regardless of which path convention resolved *path*."""
    if not path.exists():
        raise BodyEnvError(
            f"--body-env: no body staged at the fixed path {str(path)!r}. "
            f"The invoking harness must write the JSON body to this exact "
            f"path (its own Write tool or equivalent, per-spawn TMPDIR) "
            f"before invoking this verb with --body-env -- see "
            f"docs/integration.md's body-off-argv section."
        )
    if not path.is_file():
        raise BodyEnvError(
            f"--body-env: the fixed path {str(path)!r} exists but is not a "
            f"regular file (got {('directory' if path.is_dir() else 'other')!r})."
        )
    try:
        return path.read_bytes()
    except OSError as exc:
        raise BodyEnvError(
            f"--body-env: could not read the fixed body path {str(path)!r}: {exc}."
        ) from exc


def _read_and_validate_stamp(
    stamp_path: Path,
    *,
    caller: str,
    expect_target_pr: int | None = None,
    expect_create_branch: str | None = None,
) -> _StagedStamp:
    """Read and parse *stamp_path*, failing closed (BodyEnvError) on any
    shape the reader cannot trust: missing, unreadable, malformed JSON, or
    missing the mandatory `target_pr` field. Never partially trusts a
    stamp it cannot fully parse -- a malformed stamp is exactly as
    untrustworthy as a missing one.

    *expect_target_pr* / *expect_create_branch* are passed through ONLY to
    build the recovery-command pointer in the "no stamp staged" error
    (lr-e1e2fb follow-up, error-message consistency review finding) -- this
    function does not itself validate them against anything; the mismatch
    checks against a stamp that DID parse happen in the caller
    (`read_caller_body_bytes`).
    """
    if not stamp_path.exists():
        recovery = _recovery_stage_command(
            caller=caller, target_pr=expect_target_pr, create_branch=expect_create_branch
        )
        raise BodyEnvError(
            f"--body-env: no identity stamp staged at {str(stamp_path)!r} for "
            f"caller {caller!r}. A body staged without its stamp sidecar "
            f"cannot be provenance-checked -- stage both via: {recovery}"
        )
    try:
        raw = stamp_path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BodyEnvError(
            f"--body-env: could not read/parse the identity stamp at "
            f"{str(stamp_path)!r}: {exc}."
        ) from exc
    # lr-e1e2fb: a stamp binds to EXACTLY ONE of target_pr / create_branch --
    # both keys are always PRESENT in a stamp this module wrote (stage_
    # caller_body always writes both keys, one of them null), so "missing
    # target_pr" here means a genuinely malformed/foreign stamp (e.g. hand-
    # authored, or from a version of this module predating create_branch),
    # never a legitimate create-mode stamp -- those still carry an explicit
    # `"target_pr": null` key.
    if "target_pr" not in parsed:
        raise BodyEnvError(
            f"--body-env: identity stamp at {str(stamp_path)!r} is missing "
            f"its mandatory 'target_pr' field."
        )
    return _StagedStamp(
        target_pr=parsed["target_pr"],
        create_branch=parsed.get("create_branch"),
        head_sha=parsed.get("head_sha"),
        staged_at=parsed.get("staged_at", ""),
    )


def read_body_bytes(
    *,
    env: dict[str, str] | None = None,
    caller: str | None = None,
    expect_target_pr: int | None = None,
    expect_create_branch: str | None = None,
    expect_head_sha: str | None = None,
) -> bytes:
    """Read a staged body and return its raw bytes.

    This is the --body-env drop-in replacement for `sys.stdin.buffer.read()`
    at each verb's own body-ingestion call site -- same return shape (raw
    bytes, validated downstream by the SAME `validate_body_stdin_content` /
    `validate_review_body_stdin_content` functions --body-stdin already
    uses; this module does not duplicate that validation).

    *caller* selects WHICH staged path this reads (lr-3a7ae8): when
    provided, delegates to `read_caller_body_bytes` -- the per-caller
    path a concurrent same-TMPDIR caller can never collide with another
    caller's own staged body on. When omitted (the default), behavior is
    BYTE-FOR-BYTE UNCHANGED from before lr-3a7ae8: the original single
    fixed path (`resolve_body_path`), for a caller with no identity to
    namespace by (e.g. a standalone/local run with no concurrency concern)
    -- no stamp check, no consume.

    EXACTLY ONE of *expect_target_pr* / *expect_create_branch* is MANDATORY
    whenever *caller* is supplied (lr-becdef, Axis 1 fix; create-mode added
    lr-e1e2fb): every production write-method verb in this package always
    has EITHER a resolved PR number (update/comment path) OR a resolved
    current branch (push's create path) in scope by the time it reaches
    this call, and always passes exactly one, so a caller-namespaced read is
    always provenance-checked in production. Passing *caller* without
    exactly one of these raises BodyEnvError immediately -- there is no
    silent unchecked caller-namespaced read left in this API. *expect_head_sha*
    is optional (not every call site has an evaluated SHA to bind against).

    Raises BodyEnvError (never a bare FileNotFoundError/OSError a verb's own
    call site would have to know how to catch) when:
      - the resolved path does not exist -- the caller's harness has not
        staged a body file at all before invoking the verb with --body-env.
      - the path exists but is not a regular file (e.g. a directory).
      - reading the file raises any OSError (permission denied, etc).
      - *caller* is supplied but is not a valid bare role/name token.
      - *caller* is supplied without exactly one of *expect_target_pr* /
        *expect_create_branch*.
      - the staged file's identity stamp names a different PR/branch (or,
        when *expect_head_sha* is supplied, a different head SHA) than
        requested.

    See `read_caller_body_bytes` for the read-and-consume + stamp-check
    contract that applies whenever *caller* is supplied.
    """
    if caller is not None:
        if bool(expect_target_pr) == bool(expect_create_branch):
            raise BodyEnvError(
                "--body-env: read_body_bytes(caller=...) requires EXACTLY "
                "ONE of expect_target_pr / expect_create_branch -- a "
                "caller-namespaced read is always provenance-checked "
                "against either the PR it is being read for (update/comment "
                "path) or the branch that will open a new PR "
                "(create path); omitting both, or supplying "
                "both, would silently reopen the sequential-stale-read hole "
                "this check exists to close."
            )
        return read_caller_body_bytes(
            caller=caller,
            expect_target_pr=expect_target_pr,
            expect_create_branch=expect_create_branch,
            expect_head_sha=expect_head_sha,
            env=env,
        )
    path = resolve_body_path(env=env)
    return _read_staged_bytes(path)


def read_caller_body_bytes(
    *,
    caller: str,
    expect_target_pr: int | None = None,
    expect_create_branch: str | None = None,
    expect_head_sha: str | None = None,
    env: dict[str, str] | None = None,
) -> bytes:
    """Read the PER-CALLER staged body (`resolve_caller_body_path`), verify
    its identity stamp, consume both files, and return the body's raw bytes
    -- the lr-3a7ae8 collision-safe AND lr-becdef stale-read-safe entry
    point.

    EXACTLY ONE of *expect_target_pr* / *expect_create_branch* is required
    (lr-e1e2fb, create-mode extension): the update/comment path (an
    EXISTING PR) supplies *expect_target_pr*; push's create path (no PR
    number yet) supplies *expect_create_branch* -- the current git branch
    that will open the new PR, resolved by push.verb itself
    (`git rev-parse --abbrev-ref HEAD`) before staging AND before reading,
    never a caller-typed value. Supplying both, or neither, raises
    BodyEnvError immediately -- there is no ambiguous "which binding mode"
    state this function will silently resolve one way or the other.

    Provenance check (lr-becdef, Axis 1 defense-in-depth): the stamp
    sidecar staged alongside the body (`stage_caller_body`) is read FIRST.
    If the stamp's binding (`target_pr` or `create_branch`, whichever mode
    this read is checking) does not equal the expected value -- or, when
    *expect_head_sha* is supplied, its `head_sha` does not equal
    *expect_head_sha* -- this raises BodyEnvError and NEITHER file is
    touched: a caller reading a body staged for a DIFFERENT PR/branch or SHA
    must fail closed, never silently post foreign content, and the
    mismatched files are left in place rather than destroyed (they may
    belong to a different, still-pending invocation). A stamp staged in the
    OTHER mode than the one this read expects (e.g. staged with
    `create_branch` set, but this read supplies `expect_target_pr`) is
    exactly as much a mismatch as a wrong PR number/branch name -- it fails
    closed the same way, never falls back to checking the other field.

    Read-and-consume (lr-becdef, Axis 1 PRIMARY): once the stamp matches,
    the body is read and BOTH the body file and its stamp sidecar are
    unlinked before returning. A retried invocation of the SAME caller
    must therefore RE-STAGE a fresh body+stamp pair -- re-reading a
    leftover file from a completed invocation is no longer possible, since
    a completed read leaves nothing behind to re-read. A missing stage
    (because it was already consumed, or never staged at all) fails closed
    with the same `BodyEnvError` "no body staged" shape as before this fix.

    A caller reading THIS function's return value only ever sees bytes
    staged under its OWN *caller* namespace: there is no code path here
    that falls back to the shared single-fixed-path file, and no code path
    that reads a DIFFERENT caller's namespaced path. A harness that staged
    under the wrong caller name, or never staged at all, fails closed with
    the same `BodyEnvError` "no body staged" shape `read_body_bytes` always
    raised for a missing file -- never a silent read of some OTHER
    caller's content.

    ABANDONED-PAIR SWEEP (lr-4c1646): before doing anything else, this
    function opportunistically sweeps stale siblings out of the staging
    subdirectory (`sweep_abandoned_pairs`, warn-never-fail) -- this runs
    on EVERY read, including one that goes on to raise (missing stage,
    stamp mismatch), so an abandoned sibling from a DIFFERENT caller's
    aborted invocation is still reaped even when this particular read's
    own outcome is failure. See this module's own "ABANDONED-PAIR REAPER"
    docstring section for the full design and honest coverage statement.
    """
    sweep_abandoned_pairs(env=env)

    if bool(expect_target_pr) == bool(expect_create_branch):
        raise BodyEnvError(
            "--body-env: read_caller_body_bytes requires EXACTLY ONE of "
            f"expect_target_pr / expect_create_branch (got "
            f"expect_target_pr={expect_target_pr!r}, "
            f"expect_create_branch={expect_create_branch!r})."
        )

    path = resolve_caller_body_path(caller=caller, env=env)
    stamp_path = _resolve_caller_stamp_path(caller=caller, env=env)

    # The recovery command below always names THIS invocation's OWN correct
    # binding (lr-e1e2fb follow-up, error-message consistency review
    # finding) -- a caller hitting any of the mismatch errors below must be
    # told exactly what to run to fix it, not just what went wrong.
    recovery = _recovery_stage_command(
        caller=caller, target_pr=expect_target_pr, create_branch=expect_create_branch
    )

    # Stamp is checked BEFORE the body is even opened: a mismatch must
    # never consume anything (the mismatched files may belong to a
    # different, still-pending invocation), and a missing stamp is exactly
    # as untrustworthy as a missing body -- both fail closed identically.
    stamp = _read_and_validate_stamp(
        stamp_path,
        caller=caller,
        expect_target_pr=expect_target_pr,
        expect_create_branch=expect_create_branch,
    )
    if expect_target_pr is not None:
        if stamp.target_pr != expect_target_pr:
            raise BodyEnvError(
                f"--body-env: staged body for caller {caller!r} was staged "
                f"for PR {stamp.target_pr!r} (create_branch="
                f"{stamp.create_branch!r}), but this invocation is for PR "
                f"{expect_target_pr!r} -- refusing to read a body staged "
                f"for a different PR, or staged in create-branch mode "
                f"instead (stale-read guard). Fails closed: the "
                f"staged file is left in place, never posted. Re-stage the "
                f"correct body via: {recovery}"
            )
    else:
        if stamp.create_branch != expect_create_branch:
            raise BodyEnvError(
                f"--body-env: staged body for caller {caller!r} was staged "
                f"for branch {stamp.create_branch!r} (target_pr="
                f"{stamp.target_pr!r}), but this invocation is for branch "
                f"{expect_create_branch!r} -- refusing to read a body "
                f"staged for a different branch, or staged in target-pr "
                f"mode instead (create-mode stale-read guard). "
                f"Fails closed: the staged file is left in place, never "
                f"posted. Re-stage the correct body via: {recovery}"
            )
    if expect_head_sha is not None and stamp.head_sha is not None and stamp.head_sha != expect_head_sha:
        raise BodyEnvError(
            f"--body-env: staged body for caller {caller!r} was staged for "
            f"head_sha {stamp.head_sha!r}, but this invocation expects "
            f"{expect_head_sha!r} -- refusing to read a body staged for a "
            f"different SHA (stale-read guard). Fails closed: the "
            f"staged file is left in place, never posted. Re-stage the "
            f"correct body via: {recovery}"
        )

    body_bytes = _read_staged_bytes(path)

    # Consume ONLY after both the stamp check and the body read succeeded --
    # a read that raised above (missing/unreadable body) leaves the stamp in
    # place too, so a subsequent retry with a corrected body write still
    # finds a self-consistent pair rather than an orphaned stamp.
    path.unlink(missing_ok=True)
    stamp_path.unlink(missing_ok=True)

    return body_bytes


__all__ = [
    "BODY_ENV_NOT_EPHEMERAL_NOTE",
    "BODY_STDIN_CONTRACT_GUIDANCE",
    "BodyEnvError",
    "augment_body_contract_error",
    "read_body_bytes",
    "read_caller_body_bytes",
    "resolve_body_path",
    "resolve_caller_body_path",
    "stage_caller_body",
    "sweep_abandoned_pairs",
]
