"""numeric_validation -- the one definition of "a usable positive number" for
config values and API arguments (timeouts, poll intervals).

YAML happily produces `.nan` and `.inf`. NaN compares false against every
bound (`nan <= 0` is False, so a naive `<= 0` guard admits it) and +inf is an
unbounded wait, so every validator that bounds a duration must reject
non-finite values explicitly. Validators call this helper rather than
re-deriving the check, so a new one cannot quietly omit the finiteness half.
"""

from __future__ import annotations

import math


def is_number(value: object) -> bool:
    """True for a real int/float. A bool is not a number here: `true` in YAML
    would otherwise pass as 1."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_finite_positive_number(value: object) -> bool:
    """True for a real int/float that is finite and strictly greater than 0."""
    return is_number(value) and math.isfinite(value) and value > 0  # type: ignore[arg-type]


__all__ = ["is_finite_positive_number", "is_number"]
