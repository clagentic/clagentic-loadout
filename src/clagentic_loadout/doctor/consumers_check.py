"""doctor.consumers_check -- the age of the consumer map, as an advisory.

The consumer map (interface.consumers) lists each consumer of a producer's
interface with the date its use was confirmed. A map whose confirmations are old
may no longer describe who consumes what. This check reports the age and never
fails: a map that is not configured, not there, or not valid is advisory too,
since every verb that reads it carries on without one.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from clagentic_loadout.doctor.checks import CheckResult
from clagentic_loadout.interface.consumers import DEFAULT_WINDOW_DAYS, locate_consumers, map_age


def check_consumers_map(
    *,
    config_root: str | Path | None = None,
    today: dt.date | None = None,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> CheckResult:
    """Report where the consumer map is and how old its confirmations are. Always
    ``ok``: every finding is an advisory."""
    status = locate_consumers(config_root=config_root)
    resolved: dict = {
        "consumers_file": status.location.path,
        "consumers_file_from": status.location.origin,
        "window_days": window_days,
    }
    if status.consumers is None:
        resolved["advisories"] = [status.unavailable]
        return CheckResult(
            name="consumers_map",
            ok=True,
            summary=f"advisory: {status.unavailable}; consumers are not listed",
            resolved=resolved,
        )
    age = map_age(status.consumers, today or dt.date.today(), window_days=window_days)
    resolved.update(
        entries=age.entries,
        oldest_confirmed_at=age.oldest,
        newest_confirmed_at=age.newest,
        stale_entries=age.stale_entries,
        unconfirmed=status.consumers.unconfirmed,
    )
    if age.entries == 0:
        resolved["advisories"] = ["the consumer map lists no confirmed consumer"]
        return CheckResult(
            name="consumers_map",
            ok=True,
            summary="advisory: the consumer map lists no confirmed consumer",
            resolved=resolved,
        )
    summary = (
        f"consumer map {status.consumers.path}: {age.entries} confirmation(s), "
        f"confirmed_at {age.oldest} to {age.newest} "
        f"(newest {age.newest_age_days} day(s) old, oldest {age.oldest_age_days})"
    )
    advisories = []
    if age.stale:
        advisories.append(
            f"every confirmation is older than the {age.window_days}-day window, so the map may no "
            "longer describe who consumes what"
        )
    elif age.stale_entries:
        advisories.append(
            f"{age.stale_entries} of {age.entries} confirmation(s) are older than the "
            f"{age.window_days}-day window"
        )
    resolved["advisories"] = advisories
    if advisories:
        summary += "; advisory: " + "; ".join(advisories)
    return CheckResult(name="consumers_map", ok=True, summary=summary, resolved=resolved)


__all__ = ["check_consumers_map"]
