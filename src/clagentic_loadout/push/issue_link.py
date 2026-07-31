"""push.issue_link — 'Closes #NN' + 'Task: <id>' trailer normalization and
enforcement.

Wave B slice 3 (lr-09ca, tome #688). normalize_closes_trailer/
enforce_issue_link ported unchanged from the reference push transport
(lr-b493). Load-bearing behavior the task requires preserved: when a caller
asserts a PR closes a specific issue number, the body must carry a matching
GitHub/Forgejo keyword-linking trailer, or the push fails closed before any
push/PR-open call.

SCOPE BOUNDARY (unchanged from the source module): this does not resolve an
opaque task ID to an issue number — the caller supplies the resolved issue
number explicitly. That resolution mechanism lives outside this package.

TASK-ID TRAILER (lr-eb22f3): normalize_task_trailer grows the SAME PR body
with a `Task: <id>` footer line whenever the caller's dispatch envelope
carried an opaque task_id -- the write-side counterpart to
release.dispatch.parse_trailers, which already reads this exact `Task: <id>`
grammar back out of a merged PR body downstream (release/dispatch.py's
_TASK_TRAILER_RE). Kept in THIS module (not a new one) because both
trailers are the same "PR body footer trailer" concern and normalize
together at the same push.verb call site. task_id stays a fully opaque
string here -- no shape validation, no lore coupling, no tracker assumption.
"""

from __future__ import annotations

import re

from clagentic_loadout.push.errors import MissingIssueLinkError

#: Matches the keyword-linking grammar: bare `Closes #<int>` on its own
#: line, case-insensitive on the keyword (GitHub/Forgejo both accept
#: Closes/Fixes/Resolves; this module only ever emits "Closes").
_CLOSES_TRAILER_RE = re.compile(r"(?im)^\s*closes\s+#(\d+)\b")

#: Matches the `Task: <id>` trailer grammar -- kept BYTE-IDENTICAL to
#: release.dispatch._TASK_TRAILER_RE (that module is the downstream reader
#: of the same trailer this module writes; both sides must agree on the
#: exact grammar or a written trailer could go unread).
_TASK_TRAILER_RE = re.compile(r"(?im)^\s*task:\s*(\S+)\s*$")


def normalize_closes_trailer(body: str, issue_number: int) -> str:
    """Ensure *body* carries a `Closes #<issue_number>` trailer, appending
    one if absent. Does not rewrite an existing trailer for a DIFFERENT
    issue number — enforce_issue_link is what fails the push on a mismatch,
    so this never silently overwrites caller intent."""
    if _CLOSES_TRAILER_RE.search(body):
        return body
    separator = "\n" if body.endswith("\n") else "\n\n"
    return f"{body}{separator}Closes #{issue_number}\n"


def normalize_task_trailer(body: str, task_id: str) -> str:
    """Ensure *body* carries a `Task: <task_id>` trailer, appending one if
    absent. *task_id* is an opaque work-item ref -- no shape validation, no
    lore coupling (CLAUDE.md rule 6a). Does not rewrite an existing trailer
    for a DIFFERENT task_id -- mirrors normalize_closes_trailer's own
    never-silently-overwrite discipline; a caller who wants a different
    value edits the body itself before calling this."""
    if _TASK_TRAILER_RE.search(body):
        return body
    separator = "\n" if body.endswith("\n") else "\n\n"
    return f"{body}{separator}Task: {task_id}\n"


def parse_closes_issue_number(body: str) -> int | None:
    """Extract the issue number from a `Closes #NN` trailer in *body*, if
    present. Returns None when no trailer matches -- the git-host-native,
    lore-blind source of truth for "which issue does this PR close" (lr-eb22f3):
    a caller (e.g. merge.attestation) that wants to surface the linked issue
    reads it back out of the PR body via this function rather than tracking
    it as separate state, so the attestation can never drift from what the
    PR body actually says. When multiple trailers are present (a case
    normalize_closes_trailer/enforce_issue_link never produce, but a body
    could still be hand-edited to contain), the FIRST match wins, mirroring
    _CLOSES_TRAILER_RE's own single-match search semantics."""
    match = _CLOSES_TRAILER_RE.search(body)
    return int(match.group(1)) if match else None


def enforce_issue_link(body: str, issue_number: int | None) -> None:
    """Fail-closed check: when *issue_number* is supplied, *body* (after
    normalize_closes_trailer has run) MUST carry a matching trailer.

    *issue_number* is None in the genuine no-linked-issue case — always
    allowed, no trailer required.

    Raises MissingIssueLinkError on mismatch/absence.
    """
    if issue_number is None:
        return
    match = _CLOSES_TRAILER_RE.search(body)
    if not match or int(match.group(1)) != issue_number:
        raise MissingIssueLinkError(
            f"issue link {issue_number} was supplied but the PR body does "
            f"not carry a matching 'Closes #{issue_number}' trailer. This "
            f"push resolves to a known issue; the link is required. If the "
            f"body already contains a conflicting 'Closes #NN' trailer for "
            f"a different issue number, fix it to reference "
            f"#{issue_number}, or omit the issue link if there is "
            f"genuinely no linked issue."
        )


__all__ = [
    "enforce_issue_link",
    "normalize_closes_trailer",
    "normalize_task_trailer",
    "parse_closes_issue_number",
]
