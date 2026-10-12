"""merge.ci_payload — the only place host CI JSON is interpreted.

One strict parser per endpoint: the GitHub combined status, a GitHub check-runs
page and the Forgejo combined status. Each returns fully validated data or raises
GateFactUnavailableError naming the head SHA, so the caller refuses the merge
instead of judging evidence it could not read. Every field the gate reads is
required and type-checked; nothing is defaulted, so an unreadable response can
never turn into an empty pass. Backends do transport and pagination only and
never index these payloads themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.ci_status import CheckRun, StatusEntry
from clagentic_loadout.merge.errors import GateFactUnavailableError

_COMPLETED = "completed"


@dataclass(frozen=True)
class CombinedStatus:
    """A combined-status response: the platform's rollup `state` (lowercased,
    possibly empty) and every status entry behind it."""

    state: str
    statuses: tuple[StatusEntry, ...]


@dataclass(frozen=True)
class CheckRunsPage:
    """One page of a check-runs response and the total the host reports."""

    total_count: int
    check_runs: tuple[CheckRun, ...]


def _unreadable(endpoint: str, where: str, head_sha: str, why: str) -> GateFactUnavailableError:
    return GateFactUnavailableError(
        f"cannot read {endpoint} for {head_sha!r} in {where}: {why}; "
        f"refusing to judge CI from evidence that cannot be read"
    )


def _field(raw: dict, key: str, kind: type, label: str, fail) -> Any:
    """A required key of *raw* holding *kind* (a bool is never an int)."""
    if key not in raw:
        raise fail(f"{label} is missing")
    value = raw[key]
    wrong_type = not isinstance(value, kind) or (kind is int and isinstance(value, bool))
    if wrong_type:
        raise fail(f"{label} must be {kind.__name__}, got {type(value).__name__}")
    return value


def _status_entry(raw: Any, state_keys: tuple[str, ...], index: int, fail) -> StatusEntry:
    label = f"statuses[{index}]"
    if not isinstance(raw, dict):
        raise fail(f"{label} must be an object, got {type(raw).__name__}")
    present = [key for key in state_keys if key in raw]
    if not present:
        raise fail(f"{label} has none of {list(state_keys)!r}")
    state = _field(raw, present[0], str, f"{label}.{present[0]}", fail)
    context = _field(raw, "context", str, f"{label}.context", fail)
    return StatusEntry(context=context, state=state.lower())


def _combined_status(
    body: Any, *, state_keys: tuple[str, ...], endpoint: str, where: str, head_sha: str
) -> CombinedStatus:
    def fail(why: str) -> GateFactUnavailableError:
        return _unreadable(endpoint, where, head_sha, why)

    if not isinstance(body, dict):
        raise fail(f"the response must be an object, got {type(body).__name__}")
    state = _field(body, "state", str, "state", fail)
    entries = _field(body, "statuses", list, "statuses", fail)
    return CombinedStatus(
        state=state.lower(),
        statuses=tuple(_status_entry(e, state_keys, i, fail) for i, e in enumerate(entries)),
    )


def parse_github_combined_status(body: Any, *, where: str, head_sha: str) -> CombinedStatus:
    """GitHub `GET /commits/{sha}/status`."""
    return _combined_status(
        body, state_keys=("state",), endpoint="commit statuses", where=where, head_sha=head_sha
    )


def parse_forgejo_combined_status(body: Any, *, where: str, head_sha: str) -> CombinedStatus:
    """Forgejo `GET /commits/{sha}/status`. An entry spells its result `state`
    or `status`; whichever key is present must hold a string."""
    return _combined_status(
        body,
        state_keys=("state", "status"),
        endpoint="commit statuses",
        where=where,
        head_sha=head_sha,
    )


def _check_run(raw: Any, index: int, fail) -> CheckRun:
    label = f"check_runs[{index}]"
    if not isinstance(raw, dict):
        raise fail(f"{label} must be an object, got {type(raw).__name__}")
    name = _field(raw, "name", str, f"{label}.name", fail)
    if not name:
        raise fail(f"{label}.name is empty")
    status = _field(raw, "status", str, f"{label}.status", fail).lower()
    run_id = _field(raw, "id", int, f"{label}.id", fail)
    suite = _field(raw, "check_suite", dict, f"{label}.check_suite", fail)
    suite_id = _field(suite, "id", int, f"{label}.check_suite.id", fail)
    if "conclusion" not in raw:
        raise fail(f"{label}.conclusion is missing")
    conclusion = raw["conclusion"]
    if conclusion is not None and not isinstance(conclusion, str):
        raise fail(f"{label}.conclusion must be str or null, got {type(conclusion).__name__}")
    if status == _COMPLETED and not conclusion:
        raise fail(f"{label} ({name!r}) completed with no conclusion")
    return CheckRun(
        name=name,
        status=status,
        conclusion=(conclusion or "").lower(),
        suite_id=suite_id,
        run_id=run_id,
    )


def parse_github_check_runs_page(body: Any, *, where: str, head_sha: str) -> CheckRunsPage:
    """GitHub `GET /commits/{sha}/check-runs`. A page reporting `total_count`
    0 has an empty `check_runs` list; the list itself is always required."""

    def fail(why: str) -> GateFactUnavailableError:
        return _unreadable("check-runs", where, head_sha, why)

    if not isinstance(body, dict):
        raise fail(f"the response must be an object, got {type(body).__name__}")
    total = _field(body, "total_count", int, "total_count", fail)
    if total < 0:
        raise fail(f"total_count must not be negative, got {total}")
    entries = _field(body, "check_runs", list, "check_runs", fail)
    if total == 0 and entries:
        raise fail("total_count is 0 but check_runs is not empty")
    return CheckRunsPage(
        total_count=total,
        check_runs=tuple(_check_run(e, i, fail) for i, e in enumerate(entries)),
    )


__all__ = [
    "CheckRunsPage",
    "CombinedStatus",
    "parse_forgejo_combined_status",
    "parse_github_check_runs_page",
    "parse_github_combined_status",
]
