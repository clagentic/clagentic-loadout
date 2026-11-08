"""review.changeset_ratio — the net test:code ratio of a change, as advice.

Every file of a change is one of three things: a test (review.test_files, with
the repo profile's ``test.paths``), documentation, or code. Documentation is not
counted. The ratio is the net growth of tests against the net growth of code, so
a change that consolidates tests (more lines removed than added) reads as a
shrink and is never over budget.

The result object (every key always present)::

    {"test": {"added": 300, "removed": 0, "net": 300},
     "code": {"added": 100, "removed": 0, "net": 100},
     "ratio": "3.0:1" | "unbounded" | "n/a" | "unavailable",
     "budget": 2,
     "over_budget": true,
     "line": "<one sentence>"}

``over_budget`` is true only when the net test growth is at least the floor
(``test.ratio_floor_lines``, default 100) AND exceeds the budget
(``test.ratio_warn``, default 2) times the net code growth, counting a code
shrink as zero. ``ratio`` is ``unbounded`` when tests grew and code did not,
``n/a`` when neither grew, and ``unavailable`` (with ``reason``) when the
per-file counts could not be read; ``test`` and ``code`` are then null.

It is advice: nothing here refuses or blocks anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.repo_test.spec import KEY_RATIO_FLOOR_LINES, KEY_RATIO_WARN
from clagentic_loadout.review.churn_delta import FileDelta
from clagentic_loadout.review.repo_profile import RepoProfile
from clagentic_loadout.review.test_files import is_doc_file, is_test_file

DEFAULT_RATIO_WARN = 2
DEFAULT_RATIO_FLOOR_LINES = 100

RATIO_UNBOUNDED = "unbounded"
RATIO_NOT_APPLICABLE = "n/a"
RATIO_UNAVAILABLE = "unavailable"

_NO_BLOCK = "advisory only, it does not block"


@dataclass(frozen=True)
class RatioSettings:
    """The budget and floor in force, and a warning for each profile key that
    was set to something unusable and so fell back to its default."""

    warn: int = DEFAULT_RATIO_WARN
    floor_lines: int = DEFAULT_RATIO_FLOOR_LINES
    warnings: tuple[str, ...] = ()


def _positive_int(value: Any, key: str, default: int) -> tuple[int, str | None]:
    if value is None:
        return default, None
    # bool is an int subclass; ``true`` is not a count.
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value, None
    return default, f"test.{key} must be a positive integer, got {value!r}; using {default}"


def settings_from_profile(profile: RepoProfile | None) -> RatioSettings:
    """The settings the profile (read from the merge base) states."""
    declared = profile.test if profile is not None else None
    warn, warn_problem = _positive_int(
        declared.ratio_warn if declared else None, KEY_RATIO_WARN, DEFAULT_RATIO_WARN
    )
    floor, floor_problem = _positive_int(
        declared.ratio_floor_lines if declared else None,
        KEY_RATIO_FLOOR_LINES,
        DEFAULT_RATIO_FLOOR_LINES,
    )
    return RatioSettings(
        warn, floor, tuple(p for p in (warn_problem, floor_problem) if p is not None)
    )


def extra_test_globs(profile: RepoProfile | None) -> tuple[str, ...]:
    """The profile's extra test-file globs, or none."""
    return profile.test.paths if profile is not None and profile.test is not None else ()


def _bucket(added: int, removed: int) -> dict[str, int]:
    return {"added": added, "removed": removed, "net": added - removed}


def _ratio_text(test_net: int, code_net: int) -> str:
    if code_net > 0:
        return f"{test_net / code_net:.1f}:1"
    return RATIO_UNBOUNDED if test_net > 0 else RATIO_NOT_APPLICABLE


def _counts(side: dict[str, int]) -> str:
    return f"+{side['added']}/-{side['removed']}"


def compute(
    files: Iterable[FileDelta],
    settings: RatioSettings = RatioSettings(),
    test_globs: Sequence[str] = (),
) -> dict[str, Any]:
    """The ratio object for the per-file counts in *files*."""
    totals = {"test": [0, 0], "code": [0, 0]}
    for item in files:
        if is_test_file(item.path, test_globs):
            kind = "test"
        elif is_doc_file(item.path):
            continue
        else:
            kind = "code"
        totals[kind][0] += item.added
        totals[kind][1] += item.removed
    test = _bucket(*totals["test"])
    code = _bucket(*totals["code"])
    over = test["net"] >= settings.floor_lines and test["net"] > settings.warn * max(code["net"], 0)
    ratio = _ratio_text(test["net"], code["net"])
    verdict = (
        f"over the {settings.warn}:1 budget; {_NO_BLOCK}" if over else f"within the {settings.warn}:1 budget"
    )
    line = f"Net test:code ratio {ratio} (tests {_counts(test)}, code {_counts(code)}): {verdict}."
    result: dict[str, Any] = {
        "test": test,
        "code": code,
        "ratio": ratio,
        "budget": settings.warn,
        "over_budget": over,
        "line": line,
    }
    if settings.warnings:
        result["warnings"] = list(settings.warnings)
    return result


def unavailable(reason: str, settings: RatioSettings = RatioSettings()) -> dict[str, Any]:
    """The ratio object for a change whose per-file counts could not be read."""
    result: dict[str, Any] = {
        "test": None,
        "code": None,
        "ratio": RATIO_UNAVAILABLE,
        "budget": settings.warn,
        "over_budget": False,
        "line": f"Net test:code ratio unavailable: {reason}.",
        "reason": reason,
    }
    if settings.warnings:
        result["warnings"] = list(settings.warnings)
    return result


__all__ = [
    "DEFAULT_RATIO_FLOOR_LINES",
    "DEFAULT_RATIO_WARN",
    "RATIO_UNAVAILABLE",
    "RatioSettings",
    "compute",
    "extra_test_globs",
    "settings_from_profile",
    "unavailable",
]
