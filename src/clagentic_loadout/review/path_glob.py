"""review.path_glob — repo-relative path globbing with ``**``.

``fnmatch`` lets ``*`` cross a ``/`` and has no ``**``, so a glob like
``src/*.py`` would match ``src/a/b.py``. Policy files (dispositions, stakes,
exposure markers) need the usual meaning: ``*`` and ``?`` stay inside one path
segment, ``**`` spans segments, and a leading ``**/`` also matches no directory
at all (so ``**/Caddyfile`` matches a root-level ``Caddyfile``).
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_regex(pattern: str) -> re.Pattern[str]:
    """The compiled regular expression for *pattern*, anchored at both ends."""
    out: list[str] = []
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif char == "*":
            out.append("[^/]*")
            i += 1
        elif char == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(char))
            i += 1
    return re.compile("".join(out) + r"\Z")


def path_matches(pattern: str, path: str) -> bool:
    """True when the repo-relative *path* matches the glob *pattern*."""
    normalized = path[2:] if path.startswith("./") else path
    return glob_regex(pattern).match(normalized) is not None


__all__ = ["glob_regex", "path_matches"]
