"""spec.import_graph -- the file-import graph of a set of Python sources.

The one primitive that answers "which files import this file". It is pure: it
takes the text of the sources and the list of paths in the tree and returns the
graph; it reads no file, runs no command and makes no network call. The caller
decides where the text comes from (spec.impact reads it from git objects).

Only the standard library ``ast`` is used. ``import a.b``, ``from a import b``
(``b`` may be a name in ``a`` or the submodule ``a.b``) and relative imports are
resolved against the package layout of the tree: a directory is a package when
it holds ``__init__.py``, and the name of a file inside a package is its path
below the highest contiguous package. A relative import is resolved from the
importing file's own directory, with or without ``__init__.py``. Importing ``a.b``
also records an edge to the ``__init__.py`` of every package on its dotted path.
Every module is also known by its dotted path from the tree root, which is the
only name a file in a directory without ``__init__.py`` has (a tree-root file keeps
its stem). An absolute import made by a file in such a directory also tries the
name as a sibling in that directory, since the running script's directory is on
the import path. An import that names nothing in the tree (a third-party or
standard-library module) is not an edge. Nothing is guessed: an import that
cannot be resolved against the tree adds no edge.
"""

from __future__ import annotations

import ast
from collections import defaultdict
from collections.abc import Collection, Iterator, Mapping
from dataclasses import dataclass

PYTHON_SUFFIX = ".py"
_INIT = "__init__"


@dataclass(frozen=True)
class ImportGraph:
    """``importers`` maps a path to the paths that import it. ``unparsed`` lists
    the sources that are not valid Python and ``unread`` the Python paths in the
    tree with no source supplied; neither contributes an edge, so a list that
    could have held them is incomplete."""

    importers: Mapping[str, frozenset[str]]
    unparsed: tuple[str, ...]
    unread: tuple[str, ...] = ()


def _split(path: str) -> tuple[list[str], str]:
    parts = path.split("/")
    return parts[:-1], parts[-1][: -len(PYTHON_SUFFIX)]


def _is_package_dir(path: str, tree_paths: Collection[str]) -> bool:
    return "/".join([*_split(path)[0], _INIT + PYTHON_SUFFIX]) in tree_paths


def _layout_name(path: str, tree_paths: Collection[str]) -> str:
    """The dotted name *path* has below its highest contiguous package. A file whose
    own directory is not a package has none: it is only known by its path from the
    tree root, so a bare stem is never guessed for it."""
    dirs, stem = _split(path)
    if dirs and not _is_package_dir(path, tree_paths):
        return ""
    top = len(dirs)
    while top > 0 and "/".join(dirs[:top]) + "/" + _INIT + PYTHON_SUFFIX in tree_paths:
        top -= 1
    parts = dirs[top:] + ([] if stem == _INIT else [stem])
    return ".".join(parts)


def _root_name(path: str) -> str:
    dirs, stem = _split(path)
    return ".".join(dirs + ([] if stem == _INIT else [stem]))


def _module_index(tree_paths: Collection[str]) -> dict[str, set[str]]:
    """Every Python path in the tree under the names it can be imported by. It is
    built from the tree, not from the sources that could be read, so a file whose
    text is unavailable is still a known target."""
    index: dict[str, set[str]] = defaultdict(set)
    for path in tree_paths:
        if not path.endswith(PYTHON_SUFFIX):
            continue
        for name in (_layout_name(path, tree_paths), _root_name(path)):
            if name:
                index[name].add(path)
    return index


def _package_of(path: str) -> list[str]:
    """The package a relative import in *path* is resolved against: the file's own
    directory as name parts. It does not depend on ``__init__.py``, so a namespace
    package resolves the same way a regular one does."""
    return _split(path)[0]


def _name_prefixes(dotted: str) -> Iterator[str]:
    parts = dotted.split(".")
    for end in range(1, len(parts)):
        yield ".".join(parts[:end])


def _imported_names(node: ast.AST, package: list[str]) -> Iterator[tuple[str, bool]]:
    """The dotted module names a single import statement may refer to, each with
    whether it names a package only. Importing ``a.b.c`` also runs the ``__init__``
    of ``a`` and ``a.b``, so those come back flagged package-only: they match a
    package's ``__init__.py`` and never a plain module of the same name."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name, False
            for prefix in _name_prefixes(alias.name):
                yield prefix, True
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level:
        if node.level - 1 > len(package):
            return
        base_parts = package[: len(package) - (node.level - 1)]
        base = ".".join(base_parts + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    if base:
        yield base, False
        for prefix in _name_prefixes(base):
            yield prefix, True
    for alias in node.names:
        if alias.name != "*":
            yield (f"{base}.{alias.name}" if base else alias.name), False


def _candidate_names(
    node: ast.AST, package: list[str], in_package: bool
) -> Iterator[tuple[str, bool]]:
    """The names of ``_imported_names`` plus, for an absolute import made by a file
    whose own directory is not a package, the same names as siblings in that
    directory: a running script's directory is on the import path."""
    for name, packages_only in _imported_names(node, package):
        yield name, packages_only
        if package and not in_package and not getattr(node, "level", 0):
            yield ".".join([*package, name]), packages_only


def build_import_graph(
    sources: Mapping[str, str], tree_paths: Collection[str]
) -> ImportGraph:
    """The import graph of *sources* (path to Python text), resolved against
    *tree_paths*, every path in the tree. *sources* must be a subset of
    *tree_paths*."""
    tree = frozenset(tree_paths)
    index = _module_index(tree)
    edges: dict[str, set[str]] = defaultdict(set)
    unparsed: list[str] = []
    for path in sorted(sources):
        try:
            module = ast.parse(sources[path])
        except (SyntaxError, ValueError):
            unparsed.append(path)
            continue
        package = _package_of(path)
        in_package = _is_package_dir(path, tree)
        for node in ast.walk(module):
            for name, packages_only in _candidate_names(node, package, in_package):
                for target in index.get(name, ()):
                    if target == path:
                        continue
                    if packages_only and _split(target)[1] != _INIT:
                        continue
                    edges[target].add(path)
    unread = sorted(p for p in tree if p.endswith(PYTHON_SUFFIX) and p not in sources)
    return ImportGraph(
        importers={target: frozenset(found) for target, found in edges.items()},
        unparsed=tuple(unparsed),
        unread=tuple(unread),
    )


__all__ = ["PYTHON_SUFFIX", "ImportGraph", "build_import_graph"]
