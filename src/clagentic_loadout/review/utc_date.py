"""review.utc_date — the one place a moment becomes a UTC calendar date.

Policy dates (a disposition's expiry, a ruling's dates, a profile's
``confirmed_at``) are judged against the UTC date. Reducing an offset-aware
moment to its own local date first misjudges past and future around midnight
UTC, so every reader of such a date goes through ``parse_utc_date``.

Pure functions over explicit inputs; the clock is a parameter.
"""

from __future__ import annotations

import datetime as dt


def moment_utc_date(moment: dt.datetime) -> dt.date:
    """The UTC calendar date of *moment*; a moment with no offset is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).date()


def today_utc(now: dt.datetime | None = None) -> dt.date:
    """The UTC calendar date of *now* (default: the current time)."""
    return moment_utc_date(now if now is not None else dt.datetime.now(dt.timezone.utc))


def parse_utc_date(value: object) -> dt.date | None:
    """The UTC calendar date *value* names, or None when it names none. A YAML
    date, a datetime or an ISO 8601 string are accepted; a boolean or number is
    not. An offset-aware moment is converted to UTC before the date is taken,
    so the same instant is the same date whatever offset it was written in; a
    bare date, or a moment with no offset, is already a UTC calendar date."""
    try:
        if isinstance(value, dt.datetime):
            return moment_utc_date(value)
        if isinstance(value, dt.date):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip()
        return moment_utc_date(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))
    except OverflowError:
        # An offset that carries the moment past the first or last representable day.
        return None
    except ValueError:
        pass
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


__all__ = ["moment_utc_date", "parse_utc_date", "today_utc"]
