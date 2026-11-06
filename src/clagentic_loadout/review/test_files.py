"""review.test_files -- the one definition of "this path is a test file".

Shared by every part of the product that has to tell a test from the rest of a
change (a changeset ratio reads it too), so none carries its own list.

A path is a test file when any of these holds, and none of them can hold for a
documentation path:

  - a directory component is ``test``, ``tests`` or ``__tests__``;
  - the base name starts with ``test_`` or ends with ``_test.<ext>``;
  - the base name contains ``.test.`` or ``.spec.``;
  - the base name is ``conftest.py``;
  - it matches a glob the repo profile lists under ``test.paths``.

Documentation (``.md``, ``.txt``, ``.rst`` or anything under ``docs/``) is never
a test, whatever else it matches. Profile globs extend the rule and never
replace it; the profile is read from the merge base by the caller.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import PurePosixPath

from clagentic_loadout.review.path_glob import path_matches

TEST_DIR_NAMES = frozenset({"test", "tests", "__tests__"})
DOC_SUFFIXES = frozenset({".md", ".txt", ".rst"})
DOC_DIR = "docs"


def _is_doc(path: PurePosixPath) -> bool:
    return path.suffix.lower() in DOC_SUFFIXES or DOC_DIR in path.parts[:-1]


def _builtin_test(path: PurePosixPath) -> bool:
    name = path.name
    stem = path.stem
    return (
        any(part in TEST_DIR_NAMES for part in path.parts[:-1])
        or name.startswith("test_")
        or (stem.endswith("_test") and path.suffix != "")
        or ".test." in name
        or ".spec." in name
        or name == "conftest.py"
    )


def is_test_file(path: str, extra_globs: Iterable[str] = ()) -> bool:
    """True when the repo-relative *path* is a test file under the built-in rule
    or one of *extra_globs* (the profile's ``test.paths``)."""
    normalized = path[2:] if path.startswith("./") else path
    posix = PurePosixPath(normalized)
    if _is_doc(posix):
        return False
    return _builtin_test(posix) or any(path_matches(glob, normalized) for glob in extra_globs)


__all__ = ["is_test_file"]
