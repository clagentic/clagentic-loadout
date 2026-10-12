"""merge.ci_payload — strict parsing of the CI entries a host returns for a head.

The CI gate decides from the entries it parsed, so an entry it cannot read must
never be counted toward "the list is complete" and then dropped: an unreadable
failing run would turn into an empty pass. Every parser here either returns a
fully-read entry or raises GateFactUnavailableError naming the head SHA, so the
caller refuses the merge instead of judging a partial list.
"""

from __future__ import annotations

from typing import Any

from clagentic_loadout.merge.ci_status import CheckRun, StatusEntry
from clagentic_loadout.merge.errors import GateFactUnavailableError


def _unreadable(kind: str, where: str, head_sha: str, why: str) -> GateFactUnavailableError:
    return GateFactUnavailableError(
        f"cannot read {kind} for {head_sha!r} in {where}: malformed entry ({why}); "
        f"refusing to judge CI from a list with an unreadable entry"
    )


def _required_str(raw: dict, key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None


def _required_int(raw: dict, key: str) -> int | None:
    value = raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def parse_check_run(raw: Any, *, where: str, head_sha: str) -> CheckRun:
    """One check-run entry. The gate reads `name`, `status`, `id`,
    `check_suite.id` and, once the run completed, `conclusion`."""
    if not isinstance(raw, dict):
        raise _unreadable("check-runs", where, head_sha, f"expected an object, got {type(raw).__name__}")
    name = _required_str(raw, "name")
    status = _required_str(raw, "status")
    run_id = _required_int(raw, "id")
    suite = raw.get("check_suite")
    suite_id = _required_int(suite, "id") if isinstance(suite, dict) else None
    if not name:
        raise _unreadable("check-runs", where, head_sha, "missing name")
    if not status:
        raise _unreadable("check-runs", where, head_sha, f"{name!r} has no status")
    if run_id is None:
        raise _unreadable("check-runs", where, head_sha, f"{name!r} has no integer id")
    if suite_id is None:
        raise _unreadable("check-runs", where, head_sha, f"{name!r} has no check_suite id")
    conclusion = raw.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise _unreadable("check-runs", where, head_sha, f"{name!r} has a non-string conclusion")
    if status.lower() == "completed" and not conclusion:
        raise _unreadable("check-runs", where, head_sha, f"{name!r} completed with no conclusion")
    return CheckRun(
        name=name,
        status=status.lower(),
        conclusion=(conclusion or "").lower(),
        suite_id=suite_id,
        run_id=run_id,
    )


def parse_status_entry(raw: Any, *, where: str, head_sha: str) -> StatusEntry:
    """One commit-status entry. The gate reads `state` (Forgejo also spells it
    `status`); `context` is read only to match a required check name, so an entry
    without one still gates the combined state but can satisfy no required name."""
    if not isinstance(raw, dict):
        raise _unreadable("commit statuses", where, head_sha, f"expected an object, got {type(raw).__name__}")
    state = _required_str(raw, "state")
    if state is None:
        state = _required_str(raw, "status")
    if state is None:
        raise _unreadable("commit statuses", where, head_sha, "missing state")
    context = raw.get("context", "")
    if context is None:
        context = ""
    if not isinstance(context, str):
        raise _unreadable("commit statuses", where, head_sha, "non-string context")
    return StatusEntry(context=context, state=state.lower())


def parse_status_entries(raw: Any, *, where: str, head_sha: str) -> tuple[StatusEntry, ...]:
    """Every entry of a combined-status `statuses` field. An absent or null
    field is an empty list; any other non-list value is unreadable."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise _unreadable("commit statuses", where, head_sha, f"statuses is a {type(raw).__name__}, not a list")
    return tuple(parse_status_entry(entry, where=where, head_sha=head_sha) for entry in raw)


__all__ = ["parse_check_run", "parse_status_entries", "parse_status_entry"]
