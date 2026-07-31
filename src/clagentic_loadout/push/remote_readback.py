"""push.remote_readback — authoritative post-push remote state (lr-4e8a43).

THE DEFECT THIS CLOSES: prior to this module, `push.verb`'s success envelope
carried `head_sha` from `_resolve_head_sha` — a LOCAL `git rev-parse HEAD`
read in the project's own working tree, taken on faith to equal what the
remote actually now holds. Nothing between the `git push` call and that read
verifies the remote accepted the exact ref state the local read reports. A
caller reporting a remote fact (`git push happened, and the remote now has
SHA X`) was structurally indistinguishable from a caller that skipped the
push entirely and read its own stale local HEAD — this is the exact defect
class a documented incident (lr-60fac5) diagnosed one layer up, in a CALLER
of this verb: a build agent reported `head_sha` for a push that was never
invoked, and nothing in the substrate made that structurally detectable.

A code-verification pass (lr-4e8a43 comment #2) found the push VERB ITSELF
is an instance of the same defect: `_resolve_head_sha` in `push.verb` is a
local read, not a remote readback, despite being the tool that is supposed
to be the authority the caller reaches for instead of its own local read.

THE FIX — READ THE REMOTE, DON'T TRUST THE LOCAL COPY OF IT:
`read_remote_head` runs `git ls-remote <remote> <branch>` AFTER `git push`
has returned success. `git ls-remote` is a genuine round-trip to the remote
— it is not a cached/local value, and it works IDENTICALLY on both Forgejo
and GitHub (both are plain git-over-HTTP(S) remotes for this purpose; no
platform-specific API call is needed for the ref-advance half of the
readback — this is the same primitive callers previously had to run by hand
as an out-of-band verification step, per this task's own acceptance
criterion, folded into the tool that should have done it in the first
place).

PROVENANCE, NOT JUST A NEW FIELD (task requirement 4 / ADDITIVE HALF ONLY):
the returned `RemoteReadback` is a distinct, structurally-tagged shape (a
dataclass with a `source` field fixed to `"git_ls_remote"`) rather than a
bare string — a caller cannot construct one by accident via a local
`git rev-parse`, and a downstream consumer (e.g. an integrator's own
lr-0c07d9-tracked schema, BLOCKED on this task) can key validation off the
presence of that shape
rather than trying to guess whether a given `head_sha`-shaped string came
from a push verb's own readback or a caller's local git call. Threat model,
stated per the task: this defends against a caller that SKIPS the read and
reports optimistically, NOT a hostile forger constructing a fake provenance
object — no cryptographic signing, no HMAC. Over-engineering that would be
solving a problem this task's evidence (lr-60fac5: an omission, not an
attack) does not present.

AUTHORSHIP, NOT MERELY REF-ADVANCE (task ADDITION 1):
a readback that only confirms the ref moved passes cleanly even when the
landed commit carries the WRONG author — `push.verb`'s own re-authoring
(`push.identity.pin_commits_to_bot_identity`) is flag-contingent
(`--bot-name`/`--bot-email`), and when omitted the ambient git config is
inherited by documented design (`push.identity`'s own module docstring).
`verify_remote_authorship` closes that gap: given the SHA the remote just
confirmed it has (not a locally-assumed one), it reads that commit's author
email via `git log -1 --format=%ae <sha>` — safe to do LOCALLY because the
object in question is one this process itself just pushed, so it is
necessarily already present in the local object database; no extra fetch is
needed to inspect a commit this same process just created and transmitted.
This is a no-op (returns True, "not checked") when no expected email is
supplied — a caller that never configured a bot identity gets no new
enforcement (see push.identity_config's own opt-in framing, and this
module's ADDITIVE-ONLY posture below).

ADDITIVE HALF ONLY (task scope, explicit): this module NEVER raises to
signal a readback mismatch — every function here returns a result object the
CALLER inspects (`RemoteReadback.matches`, `AuthorshipCheck.matches`).
`push.verb` (this task's PR) only ADDS these fields to its success envelope;
it does not fail the push on a mismatch.
Enforcement (a verb refusing on readback mismatch) is an explicit
follow-up, config-gated, per the task's own sequencing directive — shipping
it here would impose a new failure mode on every existing external
consumer of this verb, which the task's non-negotiable constraints forbid
doing unilaterally in the additive half.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.sha import InvalidShaError, validate_sha

#: Provenance tag stamped on every RemoteReadback this module produces. A
#: caller (or a downstream schema, e.g. an integrator's own lr-0c07d9-tracked
#: schema) checks this field to distinguish a verb-supplied remote fact from
#: a value it read itself (e.g. a local `git rev-parse`) — see module
#: docstring, "PROVENANCE, NOT JUST A NEW FIELD".
REMOTE_READBACK_SOURCE_GIT_LS_REMOTE = "git_ls_remote"


class RemoteReadbackError(Exception):
    """Raised when `git ls-remote` itself cannot be run or returns no
    parseable ref line — a check-EXECUTION failure (network/transport/auth),
    distinct from a successful readback that simply does not match an
    expected value. Callers translate this to their own exit code; this
    module never silently substitutes a local read when the remote read
    fails, since a fallback to a local value is exactly the defect this
    module exists to close."""


@dataclass(frozen=True)
class RemoteReadback:
    """Authoritative post-push remote ref state, read back from the remote
    itself via `git ls-remote` — never a local `git rev-parse`.

    `source` is always REMOTE_READBACK_SOURCE_GIT_LS_REMOTE: a fixed literal,
    not a caller-suppliable field, so a caller cannot construct a
    "readback claims to be verb-supplied but isn't" value through this
    dataclass's own constructor without deliberately overriding a
    frozen-dataclass field (out of scope for the stated threat model — see
    module docstring).
    """

    remote_head_sha: str
    remote: str
    ref: str
    source: str = REMOTE_READBACK_SOURCE_GIT_LS_REMOTE

    def matches(self, expected_sha: str) -> bool:
        """True iff *expected_sha* equals this readback's `remote_head_sha`
        (exact match — both are full 40-char SHAs by construction; see
        `read_remote_head`)."""
        return self.remote_head_sha == expected_sha


@dataclass(frozen=True)
class AuthorshipCheck:
    """Result of verifying the author of the commit a RemoteReadback just
    confirmed the remote holds, against an expected email.

    `checked=False` means no expected email was supplied — this is a no-op
    pass, not a claim that authorship was verified (see
    `verify_remote_authorship`'s own docstring for why this is opt-in).
    """

    checked: bool
    matches: bool
    actual_email: str
    expected_email: str


def _run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=str(cwd),
    )


def read_remote_head(
    remote: str,
    ref: str,
    project_root: Path,
) -> RemoteReadback:
    """Read *ref*'s current SHA on *remote* via `git ls-remote` — an actual
    round-trip to the remote, never a local/cached value.

    Works identically for a Forgejo or a GitHub remote: both are plain
    git-over-HTTP(S) for this purpose, and `git ls-remote` needs no
    platform-specific API call for the ref-advance half of the readback
    (see module docstring). Credentials are whatever the calling process's
    git/credential-helper state already has configured for *remote* at call
    time — this function performs no separate auth of its own.

    Raises RemoteReadbackError if the `git ls-remote` call itself fails
    (non-zero exit — network/transport/auth failure) or returns no line
    matching *ref* (the ref does not exist on the remote from this read's
    point of view — e.g. the push never actually landed). Never falls back
    to a local `git rev-parse` on any failure path — see module docstring,
    "ADDITIVE HALF ONLY."
    """
    result = _run_git(
        ["ls-remote", "--exit-code", remote, f"refs/heads/{ref}"], cwd=project_root
    )
    if result.returncode != 0:
        raise RemoteReadbackError(
            f"post-push remote readback FAILED -- `git ls-remote {remote} "
            f"refs/heads/{ref}` exited {result.returncode}: "
            f"{result.stderr.strip()[:400]}. This is NOT a local read "
            f"fallback -- the caller cannot report a remote fact this "
            f"process did not itself confirm."
        )

    line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
    if not line:
        raise RemoteReadbackError(
            f"post-push remote readback FAILED -- `git ls-remote {remote} "
            f"refs/heads/{ref}` returned no matching ref line. The branch "
            f"does not appear to exist on the remote from this read."
        )

    raw_sha = line.split()[0] if line.split() else ""
    try:
        sha = validate_sha(raw_sha, allow_abbreviated=False)
    except InvalidShaError as exc:
        raise RemoteReadbackError(
            f"post-push remote readback produced a malformed SHA from "
            f"`git ls-remote {remote} refs/heads/{ref}` output {line!r} -- {exc}"
        ) from exc

    return RemoteReadback(remote_head_sha=sha, remote=remote, ref=ref)


def verify_remote_authorship(
    sha: str,
    expected_email: str | None,
    project_root: Path,
) -> AuthorshipCheck:
    """Verify the author email of commit *sha* against *expected_email*.

    *sha* is expected to be a value a RemoteReadback already confirmed the
    remote holds (not a value assumed locally) -- reading its author is safe
    to do against the LOCAL object database because this process itself just
    pushed that exact object, so it is guaranteed to already be present
    locally without a further fetch.

    A no-op (`checked=False`, `matches=True`) when *expected_email* is
    None/empty -- a deployment that has not configured a bot identity
    (`push.identity_config.load_builder_identity` returned `(None, None)`,
    or the caller never passed `--bot-email`) gets no new enforcement; see
    module docstring "ADDITIVE HALF ONLY" and push.identity_config's own
    opt-in framing.
    """
    if not expected_email:
        return AuthorshipCheck(checked=False, matches=True, actual_email="", expected_email="")

    result = _run_git(["log", "-1", "--format=%ae", sha], cwd=project_root)
    actual_email = result.stdout.strip() if result.returncode == 0 else ""
    return AuthorshipCheck(
        checked=True,
        matches=(actual_email == expected_email),
        actual_email=actual_email,
        expected_email=expected_email,
    )


__all__ = [
    "REMOTE_READBACK_SOURCE_GIT_LS_REMOTE",
    "AuthorshipCheck",
    "RemoteReadback",
    "RemoteReadbackError",
    "read_remote_head",
    "verify_remote_authorship",
]
