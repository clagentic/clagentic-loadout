"""spec.impact -- the impact section of a draft spec.

For each path a change names, the files of the same repository that import it
and the test files that import it, computed from the tree at one commit with the
file-import graph in spec.import_graph. The tree is read through git objects
(``git ls-tree`` and ``git show`` at the resolved commit), never the working
tree, and nothing leaves the machine: no network call, no stored index.

Python is the only language covered. A path in another language is listed as not
computed, naming its language; nothing is guessed. The section is a report: it
never refuses an action.
"""

from __future__ import annotations

import fnmatch
import posixpath
from collections.abc import Sequence
from typing import Any

from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.spec.import_graph import PYTHON_SUFFIX, ImportGraph, build_import_graph

#: Most importers listed per path, separately for source files and for test
#: files; the count of the rest is stated beside the list.
IMPORTER_CAP = 25

LANGUAGES_COVERED = ("python",)

_TEST_DIRS = frozenset({"test", "tests"})

#: Names for the extensions that are recognised but not covered. Anything else is
#: reported as an unrecognised extension, never as a guessed language.
_OTHER_LANGUAGES = {
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript",
    ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".rb": "ruby", ".php": "php",
    ".c": "c", ".h": "c", ".cc": "c++", ".cpp": "c++", ".hpp": "c++", ".cs": "c#",
    ".swift": "swift", ".sh": "shell", ".bash": "shell", ".lua": "lua", ".pl": "perl",
    ".md": "markdown", ".json": "json", ".yaml": "yaml", ".yml": "yaml", ".toml": "toml",
    ".html": "html", ".css": "css", ".sql": "sql",
}


def is_test_path(path: str) -> bool:
    """Whether *path* is a test file: under a ``test`` or ``tests`` directory, a
    ``test_*`` or ``*_test`` module, or a ``conftest.py``."""
    directory, name = posixpath.split(path)
    stem = name[: -len(PYTHON_SUFFIX)] if name.endswith(PYTHON_SUFFIX) else name
    in_test_dir = any(part in _TEST_DIRS for part in directory.split("/"))
    return in_test_dir or stem.startswith("test_") or stem.endswith("_test") or name == "conftest.py"


def language_of(path: str) -> str:
    """The language name for *path*, or a statement that its extension is not
    recognised."""
    suffix = posixpath.splitext(path)[1].lower()
    if suffix == PYTHON_SUFFIX:
        return "python"
    if not suffix:
        return "unrecognised (no extension)"
    return _OTHER_LANGUAGES.get(suffix, f"unrecognised ({suffix})")


def _capped(paths: Sequence[str]) -> dict[str, Any]:
    ordered = sorted(paths)
    return {"files": ordered[:IMPORTER_CAP], "more": max(0, len(ordered) - IMPORTER_CAP)}


def _entry(path: str, language: str, status: str, note: str | None, graph: ImportGraph | None) -> dict[str, Any]:
    importers: dict[str, Any] | None = None
    tests: dict[str, Any] | None = None
    if status == "computed" and graph is not None:
        found = graph.importers.get(path, frozenset())
        importers = _capped([p for p in found if not is_test_path(p)])
        tests = _capped([p for p in found if is_test_path(p)])
        if not found:
            note = "none found: no file in the tree at this head imports it"
    return {
        "path": path,
        "language": language,
        "status": status,
        "importers": importers,
        "importing_tests": tests,
        "note": note,
    }


def _unavailable(reason: str, head: str | None) -> dict[str, Any]:
    return {
        "source": None,
        "note": f"impact not computed: {reason}",
        "head_sha": head,
        "languages_covered": list(LANGUAGES_COVERED),
        "cap": IMPORTER_CAP,
        "skipped_files": 0,
        "items": [],
    }


def compute_impact(repo_path: str, ref: str | None, globs: Sequence[str]) -> dict[str, Any]:
    """The impact section for the paths matching *globs* in the tree of the
    repository at *repo_path*, at *ref* (default HEAD). The ref is resolved once
    and every read is made at the commit it names. A read that fails gives an
    empty section that says why."""
    reader = GitPolicyReader(repo_path)
    head: str | None = None
    try:
        head = reader.resolve_commit(ref or "HEAD")
        tree = reader.list_paths(head)
    except PolicyReadError as exc:
        return _unavailable(str(exc), head)

    targets: list[str] = []
    unmatched: list[str] = []
    for glob in dict.fromkeys(globs):
        hits = [p for p in tree if fnmatch.fnmatchcase(p, glob)]
        if not hits:
            unmatched.append(glob)
        held = set(targets)
        targets.extend(p for p in hits if p not in held)

    graph: ImportGraph | None = None
    skipped = 0
    if any(language_of(p) == "python" for p in targets):
        sources: dict[str, str] = {}
        for path in tree:
            if not path.endswith(PYTHON_SUFFIX):
                continue
            try:
                sources[path] = reader.read_text(head, path)
            except PolicyReadError:
                skipped += 1
        graph = build_import_graph(sources, tree)
        skipped += len(graph.unparsed)

    items: list[dict[str, Any]] = []
    for path in targets:
        language = language_of(path)
        if language == "python":
            items.append(_entry(path, language, "computed", None, graph))
        else:
            covered = ", ".join(LANGUAGES_COVERED)
            items.append(
                _entry(path, language, "not_computed", f"not computed: {language} is not covered (covered: {covered})", None)
            )
    for glob in unmatched:
        items.append(_entry(glob, "unknown", "no_match", "no file in the tree at this head matches", None))
    return {
        "source": f"import graph of the tree at {head}, read from git objects",
        "note": None if items else "no path was named",
        "head_sha": head,
        "languages_covered": list(LANGUAGES_COVERED),
        "cap": IMPORTER_CAP,
        "skipped_files": skipped,
        "items": items,
    }


__all__ = ["IMPORTER_CAP", "LANGUAGES_COVERED", "compute_impact", "is_test_path", "language_of"]
