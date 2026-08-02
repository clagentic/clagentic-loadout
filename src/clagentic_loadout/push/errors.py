"""push.errors — shared exception classes for the push verb.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference agent-push
transport; the source module stays primary until its separate CUT OVER +
RETIRE + VERIFY-GONE task per the migration plan.

Both platform paths (Forgejo, GitHub) behind the push verb raise these same
exception classes so main() never needs a transport-specific except clause.
"""

from __future__ import annotations

from clagentic_loadout.push.push_redaction import redact_push_secrets


class PushUsageError(Exception):
    """Raised for a caller-input-shape error (bad flags, bad owner/repo
    format, missing required argument combination). Fires before any git
    operation, credential resolution, or network call."""


class NamespaceDeniedError(Exception):
    """Raised when the target owner/namespace is not present in the
    caller-supplied allowed-namespace set (see push.namespace_guard). Fires
    BEFORE any credential is resolved or git operation attempted — a
    namespace refusal is deterministic and must never partially execute."""


class HostDeniedError(Exception):
    """Raised when the Forgejo API host derived from the live git remote
    (see push.git_coords.parse_forgejo_coords) is not present in the
    caller-supplied allowed-host set (see push.host_guard, lr-0e39f9).
    Fires BEFORE any credential is resolved or git operation attempted — a
    host refusal is deterministic and must never partially execute, mirroring
    NamespaceDeniedError's own posture for a different dimension of the same
    target."""


class AuthorMismatchError(Exception):
    """Raised when the HEAD commit author does not match the configured bot
    identity after re-authoring, or when re-authoring itself fails. A
    mis-attributed push is unrecoverable once it lands, so this fires before
    any push is attempted."""


class GitPushError(Exception):
    """Raised when the underlying `git push` subprocess exits non-zero.

    lr-f57f13 TYPED FIELDS: previously a bare marker class with no `__init__`
    and no fields — the raise-time locals (raw stderr, classification,
    per-ref reject reason, etc.) were destroyed at the raise and survived
    only inside a formatted f-string, with nothing to stop a future author
    from reformatting that string and silently dropping one. Precedent in
    this same module: `PrOpenError` carries `status_code` as a typed field
    "without parsing the status back out of the formatted message string" —
    this class was the odd one out, holding the richest diagnostic data with
    no typed access to any of it.

    `__str__` DERIVES the display string from these fields (never the other
    way around) — see `push.git_push` for the single formatter both the
    display string and any structured consumer share.

    STRUCTURAL REDACTION (pre-merge security review): every string-bearing
    field is redacted via `push.push_redaction.redact_push_secrets` INSIDE
    `__init__` itself, not merely by convention at the sole current call
    site (`push.git_push`). Before this fix, "redaction happens at
    construction" was true only because the one caller that existed
    happened to pre-redact every argument before calling this constructor —
    a SECOND construction site anywhere in this package would have been
    redacted by nothing, and nothing would have caught it (see
    `tests/test_push_git_push.py::test_git_push_error_direct_construction_still_redacts`,
    which constructs this class directly with unredacted sentinel values,
    bypassing `push.git_push` entirely, to enforce this structurally).
    *known_secrets*: literal values this call site holds (the minted token,
    verbatim) masked BY EXACT VALUE before the generic pattern-based passes
    — passed straight through to `redact_push_secrets`. A caller no longer
    needs to pre-redact any argument before constructing.
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: int,
        sub_cause: str,
        raw_stderr: str,
        reject_reason: str | None = None,
        remote_lines: tuple[str, ...] = (),
        local_hook_lines: tuple[str, ...] = (),
        reached_transport: bool = False,
        remote: str | None = None,
        refspec: str | None = None,
        lease_forced: bool = False,
        lease_origin: str = "",
        known_secrets: tuple[str, ...] = (),
    ) -> None:
        def _redact(value: str) -> str:
            return redact_push_secrets(value, known_secrets=known_secrets)

        super().__init__(_redact(message))
        self.exit_code = exit_code
        self.sub_cause = sub_cause
        self.raw_stderr = _redact(raw_stderr)
        self.reject_reason = _redact(reject_reason) if reject_reason is not None else None
        self.remote_lines = tuple(_redact(line) for line in remote_lines)
        self.local_hook_lines = tuple(_redact(line) for line in local_hook_lines)
        self.reached_transport = reached_transport
        self.remote = remote
        self.refspec = refspec
        self.lease_forced = lease_forced
        self.lease_origin = lease_origin


class PrOpenError(Exception):
    """Raised when PR creation (or update) fails at the platform API layer.

    `status_code` (lr-4e8a43, KNOWN TRAP fix): the raw HTTP status the
    platform returned, when the caller had one to supply (None for a
    network-level failure that never got a response at all). Lets
    `push.verb` distinguish a genuine PR-open failure from a REDUNDANT
    create — the platform returning 409/422 "PR already exists for this
    head/base pair" AFTER the git push itself already succeeded — without
    parsing the status back out of the formatted message string.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class MissingIssueLinkError(Exception):
    """Raised when the caller asserted a linked issue (--issue) but the PR
    body does not carry a matching 'Closes #NN' trailer after
    normalization."""


class BodyEmptyError(Exception):
    """Raised when the PR body is empty or whitespace-only on the create
    path, or --body-stdin content fails validation."""


class RemoteResolutionError(Exception):
    """Raised when the git remote URL cannot be read/parsed, or platform
    cannot be auto-detected and no explicit platform override was given."""


__all__ = [
    "AuthorMismatchError",
    "BodyEmptyError",
    "GitPushError",
    "HostDeniedError",
    "MissingIssueLinkError",
    "NamespaceDeniedError",
    "PrOpenError",
    "PushUsageError",
    "RemoteResolutionError",
]
