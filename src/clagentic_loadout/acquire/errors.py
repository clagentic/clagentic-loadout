"""acquire.errors — shared exception classes for PR content acquisition.

lr-c17040. Both transports behind the acquire contract (see
clagentic_loadout.acquire.contract) raise these same classes, so a caller
never needs a transport-specific except clause — the same shape
review.errors already established for the review-post contract.
"""

from __future__ import annotations


class PlatformMismatchError(Exception):
    """Raised when the caller's own explicit platform selection does not
    match the transport a backend implements. Fires BEFORE any credential is
    minted or API call is made, so a wrong-platform call fails fast and
    locally instead of reaching the wrong host's API."""


class AcquireFetchError(Exception):
    """Raised when a PR's metadata, diff, or changed-file list cannot be
    read from the host API — a non-2xx response, a network failure, or a
    response body that does not match the expected shape. FAIL-CLOSED: an
    unreadable PR is never reported as an empty diff/file list — the two are
    not distinguishable from the outside, so this exception exists precisely
    to keep them distinguishable to the caller."""


class ScratchWriteError(Exception):
    """Raised when the fetched diff/content cannot be staged to the
    caller-usable scratch location (e.g. the target directory is
    unwritable, or a filesystem error occurs mid-write). A scanner-backed
    caller that receives this must fall back to whatever degraded posture
    its own contract defines — this module never silently returns a partial
    file."""


__all__ = [
    "AcquireFetchError",
    "PlatformMismatchError",
    "ScratchWriteError",
]
