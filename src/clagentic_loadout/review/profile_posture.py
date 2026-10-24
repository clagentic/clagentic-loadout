"""review.profile_posture — the risk posture a repo profile declares, and what is missing from it.

The posture lives in ``profile.yaml`` beside the class (see review.repo_profile)
and is read only by gates, from the merge base (review.policy_source). It answers
the three questions a gate's stakes depend on: how reachable the repository is,
what data it holds, and whether merges are reviewed. Each key takes a small
closed vocabulary, mapped onto the evaluator's own vocabulary (review.stakes)::

    exposure       internet                    -> exposed
                   internal_authenticated      -> internal
                   local_or_ci_only            -> internal
    data           regulated_or_customer       -> regulated
                   internal                    -> internal
                   public_or_none              -> public
    merge_control  code_owner_review_required  -> controlled
                   review_required             -> controlled
                   unrestricted                -> uncontrolled
    visibility     public | private            (a claim; the host's record decides)
    confirmed_at   a date no more than 180 days old and not in the future

Anything not stated, not recognised, or not confirmed recently resolves to the
worst case (exposed, regulated, uncontrolled) AND is reported as a completeness
item, so a repository fixes its profile instead of silently living with the
stricter default. Nothing here ever holds a merge by itself.

Pure functions over explicit inputs; the clock is a parameter.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.review import stakes as stk

KEY_EXPOSURE = "exposure"
KEY_DATA = "data"
KEY_VISIBILITY = "visibility"
KEY_MERGE_CONTROL = "merge_control"
KEY_CONFIRMED_AT = "confirmed_at"

#: The posture keys in the order they are reported.
POSTURE_KEY_ORDER = (KEY_EXPOSURE, KEY_DATA, KEY_VISIBILITY, KEY_MERGE_CONTROL, KEY_CONFIRMED_AT)

EXPOSURE_MAP = {
    "internet": stk.EXPOSED,
    "internal_authenticated": stk.INTERNAL,
    "local_or_ci_only": stk.INTERNAL,
}
DATA_MAP = {
    "regulated_or_customer": "regulated",
    "internal": "internal",
    "public_or_none": "public",
}
MERGE_CONTROL_MAP = {
    "code_owner_review_required": stk.CONTROLLED,
    "review_required": stk.CONTROLLED,
    "unrestricted": stk.UNCONTROLLED,
}
VISIBILITY_VALUES = ("public", "private")

#: A confirmation older than this many days no longer counts.
MAX_AGE_DAYS = 180

#: Item key for the profile file itself and for a retired policy file.
ITEM_PROFILE_FILE = "profile"
ITEM_RETIRED_STAKES = "stakes.yaml"

_VOCABULARY = {
    KEY_EXPOSURE: tuple(EXPOSURE_MAP),
    KEY_DATA: tuple(DATA_MAP),
    KEY_VISIBILITY: VISIBILITY_VALUES,
    KEY_MERGE_CONTROL: tuple(MERGE_CONTROL_MAP),
}


@dataclass(frozen=True)
class CompletenessItem:
    """One thing a profile is missing or has wrong: the key, why, and how to fix it."""

    key: str
    reason: str
    fix: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "reason": self.reason, "fix": self.fix}

    def render(self) -> str:
        return f"{self.key}: {self.reason}. Fix: {self.fix}."


@dataclass(frozen=True)
class PostureClaims:
    """What the posture resolved to, in the evaluator's vocabulary. A claim is
    None when it is worst case (not stated, not recognised, or not confirmed)."""

    exposure: str | None = None
    data_class: str | None = None
    merge_control: str | None = None
    declared_visibility: str | None = None
    items: tuple[CompletenessItem, ...] = ()


def _fix_for(key: str, profile_path: str) -> str:
    if key == KEY_CONFIRMED_AT:
        return f"set `{KEY_CONFIRMED_AT}: <YYYY-MM-DD>` in {profile_path} to the date the posture was last checked"
    return f"set `{key}: <{' | '.join(_VOCABULARY[key])}>` in {profile_path}"


def parse_confirmed_at(value: object) -> dt.date | None:
    """The UTC calendar date *value* names, or None when it names none. A YAML
    date, a datetime or an ISO 8601 string are accepted; a boolean or number is
    not. An offset-aware moment is converted to UTC before the date is taken,
    so the same instant is the same date whatever offset it was written in; a
    bare date, or a moment with no offset, is already a UTC calendar date."""
    try:
        if isinstance(value, dt.datetime):
            return _utc_date(value)
        if isinstance(value, dt.date):
            return value
        if not isinstance(value, str):
            return None
        text = value.strip()
        return _utc_date(dt.datetime.fromisoformat(text.replace("Z", "+00:00")))
    except OverflowError:
        # An offset that carries the moment past the first or last representable day.
        return None
    except ValueError:
        pass
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _utc_date(moment: dt.datetime) -> dt.date:
    """The UTC calendar date of *moment*; a moment with no offset is read as UTC."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.timezone.utc)
    return moment.astimezone(dt.timezone.utc).date()


def today_utc(now: dt.datetime | None = None) -> dt.date:
    """The UTC calendar date of *now* (default: the current time)."""
    return _utc_date(now if now is not None else dt.datetime.now(dt.timezone.utc))


def _confirmation_item(
    raw: Mapping[str, Any], today: dt.date, profile_path: str
) -> CompletenessItem | None:
    """None when the posture is confirmed recently enough to be trusted."""
    fix = _fix_for(KEY_CONFIRMED_AT, profile_path)
    if KEY_CONFIRMED_AT not in raw or raw[KEY_CONFIRMED_AT] is None:
        return CompletenessItem(
            KEY_CONFIRMED_AT, "missing, so the posture is not trusted and resolves to the worst case", fix
        )
    confirmed = parse_confirmed_at(raw[KEY_CONFIRMED_AT])
    if confirmed is None:
        return CompletenessItem(
            KEY_CONFIRMED_AT, "not a date, so the posture resolves to the worst case", fix
        )
    if confirmed > today:
        return CompletenessItem(
            KEY_CONFIRMED_AT, "in the future, so the posture resolves to the worst case", fix
        )
    if (today - confirmed).days > MAX_AGE_DAYS:
        return CompletenessItem(
            KEY_CONFIRMED_AT,
            f"expired: older than the {MAX_AGE_DAYS}-day limit, so the posture resolves to the worst case",
            fix,
        )
    return None


def resolve_posture(
    raw: Mapping[str, Any] | None, *, now: dt.datetime | None = None, profile_path: str
) -> PostureClaims:
    """The posture claims in *raw* (the posture keys of a parsed profile; None
    when there is no readable profile), and an item for everything missing or
    wrong, in ``POSTURE_KEY_ORDER``. A key that is absent, unrecognised, or in a
    posture that is not confirmed within ``MAX_AGE_DAYS`` resolves to None."""
    values: Mapping[str, Any] = raw or {}
    today = today_utc(now)
    items: list[CompletenessItem] = []
    resolved: dict[str, str | None] = {}
    maps = {KEY_EXPOSURE: EXPOSURE_MAP, KEY_DATA: DATA_MAP, KEY_MERGE_CONTROL: MERGE_CONTROL_MAP}
    confirmation = _confirmation_item(values, today, profile_path)
    for key in POSTURE_KEY_ORDER:
        if key == KEY_CONFIRMED_AT:
            if confirmation is not None:
                items.append(confirmation)
            continue
        value = values.get(key)
        vocabulary = _VOCABULARY[key]
        if value is None:
            items.append(CompletenessItem(key, "missing", _fix_for(key, profile_path)))
            resolved[key] = None
        elif not isinstance(value, str) or value not in vocabulary:
            items.append(
                CompletenessItem(
                    key,
                    "unrecognised value, so it resolves to the worst case"
                    if key != KEY_VISIBILITY
                    else "unrecognised value, ignored",
                    _fix_for(key, profile_path),
                )
            )
            resolved[key] = None
        else:
            resolved[key] = maps[key][value] if key in maps else value
    trusted = confirmation is None
    return PostureClaims(
        exposure=resolved.get(KEY_EXPOSURE) if trusted else None,
        data_class=resolved.get(KEY_DATA) if trusted else None,
        merge_control=resolved.get(KEY_MERGE_CONTROL) if trusted else None,
        declared_visibility=resolved.get(KEY_VISIBILITY),
        items=tuple(items),
    )


def visibility_item(
    declared: str | None, host_visibility: str | None
) -> CompletenessItem | None:
    """An item when the declared visibility disagrees with the host's record.
    The host's record stays authoritative; this only reports the disagreement."""
    if declared is None or host_visibility not in VISIBILITY_VALUES or declared == host_visibility:
        return None
    return CompletenessItem(
        KEY_VISIBILITY,
        "disagrees with the visibility the host reports, and the host's record is used",
        f"set `{KEY_VISIBILITY}` to match the host's record, or correct the host setting",
    )


def raised_visibility(declared: str | None, host_visibility: str | None) -> str | None:
    """The visibility the stakes use: the host's, except that a declared public
    raises it. A declaration never lowers what the host reports."""
    if declared == "public":
        return "public"
    return host_visibility


def profile_file_item(state: str, problem: str | None, profile_path: str) -> CompletenessItem | None:
    """The item for a profile that is missing or invalid; None when it was read."""
    if state == "missing":
        return CompletenessItem(
            ITEM_PROFILE_FILE,
            f"no profile at {profile_path}, so the whole posture resolves to the worst case",
            "run `loadout-profile init` to draft one, then add the posture keys",
        )
    if state == "invalid":
        return CompletenessItem(
            ITEM_PROFILE_FILE,
            f"{profile_path} is invalid ({problem}), so the whole posture resolves to the worst case",
            f"correct {profile_path}",
        )
    return None


def retired_stakes_item(stakes_path: str, profile_path: str) -> CompletenessItem:
    """The item for a retired policy file still present in the tree."""
    return CompletenessItem(
        ITEM_RETIRED_STAKES,
        f"{stakes_path} is retired and no longer read; the stakes it declared are ignored",
        f"move exposure, data, merge_control and confirmed_at into {profile_path} using the "
        "posture vocabulary, then delete the retired file",
    )


__all__ = [
    "DATA_MAP",
    "EXPOSURE_MAP",
    "ITEM_PROFILE_FILE",
    "ITEM_RETIRED_STAKES",
    "KEY_CONFIRMED_AT",
    "KEY_DATA",
    "KEY_EXPOSURE",
    "KEY_MERGE_CONTROL",
    "KEY_VISIBILITY",
    "MAX_AGE_DAYS",
    "MERGE_CONTROL_MAP",
    "POSTURE_KEY_ORDER",
    "VISIBILITY_VALUES",
    "CompletenessItem",
    "PostureClaims",
    "parse_confirmed_at",
    "profile_file_item",
    "raised_visibility",
    "resolve_posture",
    "retired_stakes_item",
    "today_utc",
    "visibility_item",
]
