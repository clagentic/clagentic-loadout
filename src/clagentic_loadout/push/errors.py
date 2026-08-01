"""push.errors — shared exception classes for the push verb.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference agent-push
transport; the source module stays primary until its separate CUT OVER +
RETIRE + VERIFY-GONE task per the migration plan.

Both platform paths (Forgejo, GitHub) behind the push verb raise these same
exception classes so main() never needs a transport-specific except clause.
"""

from __future__ import annotations


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
    """Raised when the underlying `git push` subprocess exits non-zero."""


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
