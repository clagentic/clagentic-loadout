"""review.docs_only_policy — which changed files may count as documentation.

A review may take a lighter path when the whole change is documentation. What
counts as documentation is decided here, deterministically, from two inputs and
nothing else: the changed paths and the docs allowlist in the repo profile as it
stands at the base revision (review.policy_source reads it there, so a pull
request cannot widen the list it is judged by). There is no second classifier:
the verb, `run` and `post` all call ``classify_changeset``.

The profile declares the list under ``review.docs_only``::

    review:
      docs_only:
        paths: ["docs/**/*.md", "README.md"]    # globs whose files may be docs
        depended_on: ["docs/README.md"]         # globs that are code although they look like docs

Absent means nothing is docs-only. A path is documentation only when it matches
``paths``, matches no ``depended_on`` glob, and falls in none of the built-in code
classes: configuration and data files, CI and host workflow directories, hook
files, scripts and other source, executable, symlink and submodule entries, and
the loadout configuration directory. Agent and instruction files are not named
here (a released tool names no deployment's files); a repository lists them in
``depended_on``. A change that touches no path, or any path that is not
documentation, is code. The answer errs toward code in every doubtful case.
"""

from __future__ import annotations

import posixpath
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.fence_state import CLASSIFICATION_CODE, CLASSIFICATION_DOCS_ONLY
from clagentic_loadout.review.path_glob import path_matches

KEY_REVIEW = "review"
KEY_DOCS_ONLY = "docs_only"
KEY_PATHS = "paths"
KEY_DEPENDED_ON = "depended_on"

PATH_DOCS = "docs"
PATH_CODE = "code"

#: Why the allowlist could not be used, or that it was.
STATE_OK = "ok"
STATE_ABSENT = "absent"

_MAX_GLOBS = 500
_MAX_GLOB_CHARS = 200

#: Git file modes that make an entry more than a plain file: executable, symlink,
#: submodule.
SPECIAL_MODES = frozenset({"100755", "120000", "160000"})

#: Configuration and data files. A change to one can change behaviour.
CONFIG_EXTENSIONS = frozenset({"yaml", "yml", "json", "toml", "ini", "cfg"})

#: Scripts and source files.
SCRIPT_EXTENSIONS = frozenset(
    {
        "sh", "bash", "zsh", "fish", "ksh", "csh", "py", "pyw", "rb", "pl", "pm", "php",
        "js", "mjs", "cjs", "ts", "tsx", "jsx", "go", "rs", "java", "kt", "kts", "scala",
        "c", "h", "cc", "cpp", "cxx", "hpp", "cs", "swift", "lua", "r", "ps1", "psm1",
        "bat", "cmd", "mk", "sql", "gradle", "groovy", "dart", "ex", "exs", "clj", "ml",
        "hs", "vb", "vbs", "tf", "nix",
    }
)

#: File names that are build or run definitions whatever their extension.
SCRIPT_NAMES = frozenset(
    {
        "makefile", "gnumakefile", "dockerfile", "jenkinsfile", "rakefile", "gemfile",
        "justfile", "vagrantfile", "procfile", "brewfile", "taskfile", "containerfile",
    }
)

#: Top-level directories that hold CI, host workflow or the loadout's own config.
CI_DIRS = frozenset(
    {".github", ".forgejo", ".gitea", ".gitlab", ".circleci", ".buildkite", ".drone", ".woodpecker"}
)
LOADOUT_CONFIG_DIR = ".clagentic"

#: Directory names, at any depth, that hold hook files, and hook file names.
HOOK_DIRS = frozenset({"hooks", ".husky", ".githooks"})
HOOK_NAMES = frozenset(
    {
        "pre-commit", "prepare-commit-msg", "commit-msg", "post-commit", "pre-push",
        "pre-rebase", "post-checkout", "post-merge", "post-rewrite", "pre-merge-commit",
    }
)


class DocsOnlyFormatError(ValueError):
    """The ``review`` block of a profile is not in the documented shape. The
    message names the offending key path."""


@dataclass(frozen=True)
class DocsOnlyPolicy:
    """The docs allowlist a profile declares."""

    paths: tuple[str, ...] = ()
    depended_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class ChangedPath:
    """One path a change touches, with the git file modes seen for it (old and
    new side; empty when the source does not state them)."""

    path: str
    modes: tuple[str, ...] = ()


@dataclass(frozen=True)
class AllowlistSource:
    """Where the allowlist came from: the base revision and the profile path,
    and what reading it came to (``ok``, ``absent``, or a profile state that
    gave no allowlist)."""

    base_sha: str | None
    path: str
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {"base_sha": self.base_sha, "path": self.path, "state": self.state}


@dataclass(frozen=True)
class PathClass:
    path: str
    kind: str
    reason: str

    def to_dict(self) -> dict[str, str]:
        return {"path": self.path, "class": self.kind, "reason": self.reason}


@dataclass(frozen=True)
class Classification:
    """The classification of one change: ``docs-only`` or ``code``, the class and
    reason of every path, and the allowlist it was judged by."""

    kind: str
    files: tuple[PathClass, ...]
    source: AllowlistSource

    @property
    def is_docs_only(self) -> bool:
        return self.kind == CLASSIFICATION_DOCS_ONLY

    def code_files(self) -> tuple[PathClass, ...]:
        return tuple(item for item in self.files if item.kind == PATH_CODE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.kind,
            "allowlist_source": self.source.to_dict(),
            "files": [item.to_dict() for item in self.files],
        }


def _globs(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise DocsOnlyFormatError(f"{where} must be a list of path globs")
    if len(value) > _MAX_GLOBS:
        raise DocsOnlyFormatError(f"{where} may hold at most {_MAX_GLOBS} entries")
    out: list[str] = []
    for position, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise DocsOnlyFormatError(f"{where}[{position}] must be a non-empty string")
        text = item.strip()
        if len(text) > _MAX_GLOB_CHARS or "\n" in text or "\r" in text:
            raise DocsOnlyFormatError(
                f"{where}[{position}] must be one line of at most {_MAX_GLOB_CHARS} characters"
            )
        if text.startswith("/") or ".." in text.split("/"):
            raise DocsOnlyFormatError(
                f"{where}[{position}] must be a repository-relative glob, got {text!r}"
            )
        out.append(text)
    return tuple(dict.fromkeys(out))


def parse_review_block(block: Any) -> DocsOnlyPolicy | None:
    """The docs allowlist in a profile's ``review`` block; None when the block
    is absent or declares none. Strict: an unknown or misplaced key makes the
    whole profile invalid, so a half-understood list is never half-applied."""
    if block is None:
        return None
    if not isinstance(block, dict):
        raise DocsOnlyFormatError(f"{KEY_REVIEW} must be a mapping")
    unknown = sorted(str(k) for k in block if k != KEY_DOCS_ONLY)
    if unknown:
        raise DocsOnlyFormatError(
            "unknown or misplaced key: " + ", ".join(f"{KEY_REVIEW}.{key}" for key in unknown)
        )
    docs = block.get(KEY_DOCS_ONLY)
    if docs is None:
        return None
    where = f"{KEY_REVIEW}.{KEY_DOCS_ONLY}"
    if not isinstance(docs, dict):
        raise DocsOnlyFormatError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in docs if k not in (KEY_PATHS, KEY_DEPENDED_ON))
    if unknown:
        raise DocsOnlyFormatError(
            "unknown or misplaced key: " + ", ".join(f"{where}.{key}" for key in unknown)
        )
    paths = _globs(docs[KEY_PATHS], f"{where}.{KEY_PATHS}") if docs.get(KEY_PATHS) is not None else ()
    depended = (
        _globs(docs[KEY_DEPENDED_ON], f"{where}.{KEY_DEPENDED_ON}")
        if docs.get(KEY_DEPENDED_ON) is not None
        else ()
    )
    return DocsOnlyPolicy(paths=paths, depended_on=depended)


def _normalize(path: str) -> str | None:
    """The repository-relative form of *path*, or None when it is not one."""
    text = path[2:] if path.startswith("./") else path
    if not text or text.startswith("/") or "\0" in text:
        return None
    parts = text.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return posixpath.join(*parts)


def builtin_code_reason(path: str) -> str | None:
    """Why *path* is code whatever the allowlist says, or None when it is not
    in a built-in code class."""
    parts = path.split("/")
    name = parts[-1]
    lowered = name.lower()
    extension = lowered.rsplit(".", 1)[1] if "." in lowered.lstrip(".") else ""
    if parts[0] == LOADOUT_CONFIG_DIR:
        return "in the loadout configuration directory"
    if parts[0] in CI_DIRS:
        return "in a CI or host workflow directory"
    if any(part in HOOK_DIRS for part in parts[:-1]) or name in HOOK_NAMES:
        return "a hook file"
    if extension in CONFIG_EXTENSIONS:
        return f"a configuration or data file (.{extension})"
    if extension in SCRIPT_EXTENSIONS or lowered in SCRIPT_NAMES or lowered.startswith("dockerfile"):
        return "a script or source file"
    if name.startswith("."):
        return "a dotfile, which configures a tool"
    return None


def _classify_path(entry: ChangedPath, policy: DocsOnlyPolicy | None) -> PathClass:
    normalized = _normalize(entry.path)
    if normalized is None:
        return PathClass(entry.path, PATH_CODE, "not a repository-relative path")
    special = sorted(set(entry.modes) & SPECIAL_MODES)
    if special:
        return PathClass(normalized, PATH_CODE, f"an executable, symlink or submodule entry ({special[0]})")
    if policy is None or not policy.paths:
        return PathClass(normalized, PATH_CODE, "the base revision declares no docs allowlist")
    reason = builtin_code_reason(normalized)
    if reason is not None:
        return PathClass(normalized, PATH_CODE, reason)
    for glob in policy.depended_on:
        if path_matches(glob, normalized):
            return PathClass(normalized, PATH_CODE, f"listed as depended on ({glob})")
    if not any(path_matches(glob, normalized) for glob in policy.paths):
        return PathClass(normalized, PATH_CODE, "not on the docs allowlist")
    return PathClass(normalized, PATH_DOCS, "on the docs allowlist")


def classify_changeset(
    changed: Iterable[ChangedPath],
    policy: DocsOnlyPolicy | None,
    source: AllowlistSource,
) -> Classification:
    """Classify a change from its paths and the base profile's allowlist.

    ``docs-only`` only when there is at least one path and every path is
    documentation; otherwise ``code``. Pure: the same inputs always give the
    same answer."""
    # Every entry is judged on its own and a path keeps its most restrictive
    # result: a later plain-file entry must never erase the executable, symlink
    # or submodule mode (or any other code reason) an earlier entry carried.
    by_path: dict[str, PathClass] = {}
    for entry in changed:
        result = _classify_path(entry, policy)
        known = by_path.get(result.path)
        if known is None or (known.kind == PATH_DOCS and result.kind == PATH_CODE):
            by_path[result.path] = result
    files = tuple(sorted(by_path.values(), key=lambda item: item.path))
    docs_only = bool(files) and all(item.kind == PATH_DOCS for item in files)
    return Classification(
        kind=CLASSIFICATION_DOCS_ONLY if docs_only else CLASSIFICATION_CODE,
        files=files,
        source=source,
    )


def merge_changed(*groups: Sequence[ChangedPath]) -> list[ChangedPath]:
    """The paths of every group, one entry per path, with the modes of all of
    its entries together."""
    merged: dict[str, set[str]] = {}
    for group in groups:
        for entry in group:
            merged.setdefault(entry.path, set()).update(entry.modes)
    return [ChangedPath(path, tuple(sorted(modes))) for path, modes in merged.items()]


__all__ = [
    "CLASSIFICATION_CODE",
    "CLASSIFICATION_DOCS_ONLY",
    "KEY_REVIEW",
    "PATH_CODE",
    "PATH_DOCS",
    "SPECIAL_MODES",
    "STATE_ABSENT",
    "STATE_OK",
    "AllowlistSource",
    "ChangedPath",
    "Classification",
    "DocsOnlyFormatError",
    "DocsOnlyPolicy",
    "PathClass",
    "builtin_code_reason",
    "classify_changeset",
    "merge_changed",
    "parse_review_block",
]
