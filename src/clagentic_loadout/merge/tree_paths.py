"""merge.tree_paths -- name a working-tree file by its LEXICAL path inside a git tree.

A base-commit or content-addressed read (`git show <sha>:<path>`) must be keyed by
the path a file is CONFIGURED at, never by where the working tree's filesystem
sends it. A pull request controls the working tree it is judged from: replacing a
tracked config file with a symlink (to `/dev/null`, to a path outside the tree, or
to an in-tree path that does not exist at base) changes what `Path.resolve()`
returns, and a lookup keyed by that answer misses the tracked blob and falls back
to reading the PR's own content. This one helper is the shared primitive every
such lookup in the merge package uses, so the choice is made in one place.

Only the git tree itself (a directory the operator names, never PR content) is
resolved; the file path below it is normalised lexically and never followed.
"""

from __future__ import annotations

import os
from pathlib import Path


def lexical_tree_relative_path(path: str | Path, git_tree: str | Path) -> str | None:
    """*path* relative to *git_tree* as a git (POSIX) path, or None when *path* is
    not inside that tree (a wrapper config above it).

    `..` segments are collapsed textually and no symlink below *git_tree* is
    followed. *git_tree* is matched as written and, failing that, in its resolved
    spelling, because the tree root is operator-chosen and a caller may hold the
    file's path under either spelling of it.
    """
    candidate = os.path.normpath(os.path.abspath(path))
    tree = Path(git_tree)
    for spelling in (os.path.normpath(os.path.abspath(tree)), os.path.normpath(str(tree.resolve()))):
        try:
            relative = Path(candidate).relative_to(spelling)
        except ValueError:
            continue
        return relative.as_posix()
    return None


__all__ = ["lexical_tree_relative_path"]
