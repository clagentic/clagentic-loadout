"""ci.backend -- the one interface both git-host backends of `ci` implement.

A backend knows which operations its host offers (``supported``), reads the
commit a run was started for, and sends one operation. It does not decide
anything: it never retries, never refuses on the state of the run, and never
returns a credential in anything it hands back.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

#: The operations the verb names. ``rerun`` runs the whole CI run again,
#: ``rerun-failed-jobs`` only its failed jobs, ``cancel`` stops a queued or
#: running one.
OPERATION_RERUN = "rerun"
OPERATION_RERUN_FAILED_JOBS = "rerun-failed-jobs"
OPERATION_CANCEL = "cancel"
OPERATIONS = (OPERATION_RERUN, OPERATION_RERUN_FAILED_JOBS, OPERATION_CANCEL)

#: Longest host message kept, so a verbose error page cannot flood a log.
MAX_HOST_MESSAGE_CHARS = 300


@dataclass(frozen=True)
class HostAnswer:
    """What the host answered to one operation.

    ``status`` is the HTTP status, or 0 when there was no answer (network
    failure, refused redirect). ``accepted`` is True only for the one success
    status the host documents for the operation. ``message`` is the host's own
    words, truncated, with the credential removed.
    """

    status: int
    accepted: bool
    message: str


class CiBackend(Protocol):
    platform: str
    supported: frozenset[str]

    def read_head_sha(self, owner: str, repo: str, run_id: int) -> str | None:
        """The commit the run was started for, or None when it cannot be read."""

    def send(self, operation: str, owner: str, repo: str, run_id: int) -> HostAnswer:
        """Send one operation. One request, no retry."""


def clean_message(text: str, token: str) -> str:
    """The host's message, without the credential, cut to MAX_HOST_MESSAGE_CHARS."""
    if token:
        text = text.replace(token, "[redacted]")
    return text.strip()[:MAX_HOST_MESSAGE_CHARS]
