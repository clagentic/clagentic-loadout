"""spec.import_graph -- the file-import graph of a set of Python sources.

The one primitive that answers "which files import this file". It is pure: it
takes the text of the sources and the list of paths in the tree and returns the
graph; it reads no file, runs no command and makes no network call. The caller
decides where the text comes from (spec.impact reads it from git objects).

Only the standard library ``ast`` is used. ``import a.b``, ``from a import b``
(``b`` may be a name in ``a`` or the submodule ``a.b``) and relative imports are
resolved against the package layout of the tree: a directory is a package when
it holds ``__init__.py``, and a module's name is its path below the highest
contiguous package. A module is also known by its dotted path from the tree root,
which is how a directory without ``__init__.py`` is imported when the root is on
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
    """``importers`` maps a path to the paths that import it; ``unparsed`` lists
    the sources that are not valid Python and so contribute no edge."""

    importers: Mapping[str, frozenset[str]]
    unparsed: tuple[str, ...]


def _split(path: str) -> tuple[list[str], str]:
    parts = path.split("/")
    return parts[:-1], parts[-1][: -len(PYTHON_SUFFIX)]


def _layout_name(path: str, tree_paths: Collection[str]) -> str:
    """The dotted name *path* has below its highest contiguous package."""
    dirs, stem = _split(path)
    top = len(dirs)
    while top > 0 and "/".join(dirs[:top]) + "/" + _INIT + PYTHON_SUFFIX in tree_paths:
        top -= 1
    parts = dirs[top:] + ([] if stem == _INIT else [stem])
    return ".".join(parts)


def _root_name(path: str) -> str:
    dirs, stem = _split(path)
    return ".".join(dirs + ([] if stem == _INIT else [stem]))


def _module_index(sources: Collection[str], tree_paths: Collection[str]) -> dict[str, set[str]]:
    index: dict[str, set[str]] = defaultdict(set)
    for path in sources:
        for name in (_layout_name(path, tree_paths), _root_name(path)):
            if name:
                index[name].add(path)
    return index


def _imported_names(node: ast.AST, package: list[str] | None) -> Iterator[str]:
    """The dotted module names a single import statement may refer to. *package*
    is the importing module's package as name parts, or None when it has none."""
    if isinstance(node, ast.Import):
        for alias in node.names:
            yield alias.name
        return
    if not isinstance(node, ast.ImportFrom):
        return
    if node.level:
        if package is None or node.level - 1 > len(package):
            return
        base_parts = package[: len(package) - (node.level - 1)]
        base = ".".join(base_parts + ([node.module] if node.module else []))
    else:
        base = node.module or ""
    if base:
        yield base
    for alias in node.names:
        if alias.name != "*":
            yield f"{base}.{alias.name}" if base else alias.name


def build_import_graph(
    sources: Mapping[str, str], tree_paths: Collection[str]
) -> ImportGraph:
    """The import graph of *sources* (path to Python text), resolved against
    *tree_paths*, every path in the tree. *sources* must be a subset of
    *tree_paths*."""
    tree = frozenset(tree_paths)
    index = _module_index(list(sources), tree)
    edges: dict[str, set[str]] = defaultdict(set)
    unparsed: list[str] = []
    for path in sorted(sources):
        try:
            module = ast.parse(sources[path])
        except (SyntaxError, ValueError):
            unparsed.append(path)
            continue
        layout = _layout_name(path, tree)
        is_package = _split(path)[1] == _INIT
        parts = layout.split(".") if layout else []
        package: list[str] | None = parts if is_package else (parts[:-1] if len(parts) > 1 else None)
        for node in ast.walk(module):
            for name in _imported_names(node, package):
                for target in index.get(name, ()):
                    if target != path:
                        edges[target].add(path)
    return ImportGraph(
        importers={target: frozenset(found) for target, found in edges.items()},
        unparsed=tuple(unparsed),
    )


__all__ = ["PYTHON_SUFFIX", "ImportGraph", "build_import_graph"]
