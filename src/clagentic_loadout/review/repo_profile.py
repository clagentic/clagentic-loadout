"""review.repo_profile — a repository's own statement of the shape it must keep.

One file, ``.clagentic/loadout/profile.yaml``, read from the trusted base
revision exactly like ``dispositions.yaml`` (review.policy_source does the reading), so
a pull request cannot loosen the rules it is judged by. This module is the pure
part: the schema, the strict parser, the per-path class lookup, the class rules,
and the draft a repository without a profile can start from.

Classes:

  released-tool  The tree ships to people outside the deployment that built it.
                 No agent or deployment names, hostnames, domains, usernames or
                 absolute host paths in it; other tools are optional and
                 pluggable, never a hard requirement.
  internal       The tree stays inside a deployment. It consumes other tools
                 only through their documented interfaces.

Schema (strict: an unknown or misplaced key makes the whole file invalid, and so
does ``deny`` in a profile with no released-tool path or ``tool_packages`` in one
with no internal path)::

    class: released-tool            # required: released-tool | internal
    paths:                          # optional; applied in order, a later match wins
      "tests/fixtures/**": {class: internal}
    deny:                           # optional; released-tool only
      names: [some-agent-name]      # whole-token, case-insensitive
      hosts: [host.example.test]
      users: [some-username]
    tool_packages: [some_tool]      # optional; internal only: stated to the
                                    # reviewer as the packages to consume only
                                    # through their top level; no check flags it
    test: {full: [...]}             # optional; the commands ``clagentic-loadout
                                    # test run`` uses (see repo_test.spec)
    exposure: local_or_ci_only      # optional posture keys, read only by gates from
    data: public_or_none            # the merge base (see review.profile_posture).
    visibility: public              # A missing, unrecognised or unconfirmed value
    merge_control: unrestricted     # is the worst case and a completeness item,
    confirmed_at: 2026-01-31        # never an invalid profile.

The denial lists named here ADD to the deployment's own list (see
review.profile_deny); nothing in a profile can remove an entry from it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from clagentic_loadout.repo_test.spec import DeclaredTests, TestBlockError, parse_test_block
from clagentic_loadout.review.path_glob import path_matches
from clagentic_loadout.review.profile_posture import POSTURE_KEY_ORDER

CLASS_RELEASED_TOOL = "released-tool"
CLASS_INTERNAL = "internal"
CLASSES = (CLASS_RELEASED_TOOL, CLASS_INTERNAL)

#: The rules each class carries, as the statements a reader of a verdict or of
#: ``loadout-profile check`` sees. The mechanically checkable ones are applied
#: by review.profile_rules; the rest are stated for the reviewer.
CLASS_RULES: dict[str, tuple[str, ...]] = {
    CLASS_RELEASED_TOOL: (
        "No agent or deployment names, hostnames, domains or usernames in the shipped tree.",
        "No absolute host paths in the shipped tree.",
        "Other tools are optional and pluggable, never a hard requirement.",
        "A released tool never adds, removes or changes the tools or permissions of a "
        "particular agent or consumer based on its identity, and never names or special-cases "
        "a consumer. Behaviour that affects permissions for all sessions is a documented "
        "setting the user controls.",
    ),
    CLASS_INTERNAL: (
        "Other tools are consumed only through their documented interfaces: an installed "
        "command, or the top level of a package, never one of its internal modules.",
    ),
}

DENY_NAMES = "names"
DENY_HOSTS = "hosts"
DENY_USERS = "users"
DENY_KINDS = (DENY_NAMES, DENY_HOSTS, DENY_USERS)

_MAX_ENTRIES = 500
_MAX_ENTRY_CHARS = 200
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")

#: Hidden top-level directories that are repository or host configuration and
#: ship with the product. Any other hidden directory is harness or tool state.
HOST_CONFIG_DIRS = frozenset({".github", ".forgejo", ".gitea", ".gitlab", ".clagentic"})

#: Files at the repository root whose presence says the tree is published as a
#: package.
PACKAGING_MANIFESTS = frozenset(
    {
        "pyproject.toml",
        "setup.py",
        "setup.cfg",
        "package.json",
        "Cargo.toml",
        "go.mod",
        "pom.xml",
        "build.gradle",
        "composer.json",
    }
)


class ProfileFormatError(ValueError):
    """The profile (or a denial list) is not in the documented shape. The
    message names the offending key path."""


@dataclass(frozen=True)
class ProfileDeny:
    """Literal values a released-tool tree must not contain, by kind."""

    names: tuple[str, ...] = ()
    hosts: tuple[str, ...] = ()
    users: tuple[str, ...] = ()

    def merged(self, other: ProfileDeny) -> ProfileDeny:
        """Both lists together, order kept, duplicates dropped."""
        return ProfileDeny(
            names=tuple(dict.fromkeys((*self.names, *other.names))),
            hosts=tuple(dict.fromkeys((*self.hosts, *other.hosts))),
            users=tuple(dict.fromkeys((*self.users, *other.users))),
        )

    def entries(self) -> tuple[tuple[str, str], ...]:
        """``(kind, value)`` pairs, in a fixed order."""
        return (
            *((DENY_NAMES, v) for v in self.names),
            *((DENY_HOSTS, v) for v in self.hosts),
            *((DENY_USERS, v) for v in self.users),
        )


@dataclass(frozen=True)
class RepoProfile:
    """A parsed profile. ``paths`` entries apply in order, a later match
    overriding an earlier one."""

    repo_class: str
    paths: tuple[tuple[str, str], ...] = ()
    deny: ProfileDeny = field(default_factory=ProfileDeny)
    tool_packages: tuple[str, ...] = ()
    test: DeclaredTests | None = None
    #: The posture keys as written, unvalidated: an unrecognised value is a
    #: completeness item (review.profile_posture), not an invalid profile.
    posture: tuple[tuple[str, Any], ...] = ()

    def posture_values(self) -> dict[str, Any]:
        return dict(self.posture)

    def class_for(self, path: str) -> str:
        """The class that governs *path*."""
        found = self.repo_class
        for pattern, override in self.paths:
            if path_matches(pattern, path):
                found = override
        return found


def _entry_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProfileFormatError(f"{where} must be a list of strings")
    if len(value) > _MAX_ENTRIES:
        raise ProfileFormatError(f"{where} may hold at most {_MAX_ENTRIES} entries")
    out: list[str] = []
    for position, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ProfileFormatError(f"{where}[{position}] must be a non-empty string")
        text = item.strip()
        if len(text) > _MAX_ENTRY_CHARS or "\n" in text or "\r" in text:
            raise ProfileFormatError(
                f"{where}[{position}] must be one line of at most {_MAX_ENTRY_CHARS} characters"
            )
        out.append(text)
    return tuple(dict.fromkeys(out))


def parse_deny(block: Any, where: str = "deny") -> ProfileDeny:
    """A denial block (``names``, ``hosts``, ``users``). ``None`` is no list."""
    if block is None:
        return ProfileDeny()
    if not isinstance(block, dict):
        raise ProfileFormatError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in block if k not in DENY_KINDS)
    if unknown:
        raise ProfileFormatError(
            "unknown or misplaced key: " + ", ".join(f"{where}.{key}" for key in unknown)
        )
    lists = {
        kind: _entry_list(block[kind], f"{where}.{kind}") if block.get(kind) is not None else ()
        for kind in DENY_KINDS
    }
    return ProfileDeny(**lists)


def _class_value(value: Any, where: str) -> str:
    if isinstance(value, str) and value in CLASSES:
        return value
    raise ProfileFormatError(f"{where} must be one of {', '.join(CLASSES)}, got {value!r}")


def _path_overrides(block: Any) -> tuple[tuple[str, str], ...]:
    if block is None:
        return ()
    if not isinstance(block, dict):
        raise ProfileFormatError("paths must be a mapping of path glob to {class: ...}")
    out: list[tuple[str, str]] = []
    for glob, entry in block.items():
        where = f"paths.{glob}"
        if not isinstance(entry, dict):
            raise ProfileFormatError(f"{where} must be a mapping with a class")
        unknown = sorted(str(k) for k in entry if k != "class")
        if unknown:
            raise ProfileFormatError(
                "unknown or misplaced key: " + ", ".join(f"{where}.{key}" for key in unknown)
            )
        if "class" not in entry:
            raise ProfileFormatError(f"{where}.class is required")
        out.append((str(glob), _class_value(entry["class"], f"{where}.class")))
    return tuple(out)


def _tool_packages(block: Any) -> tuple[str, ...]:
    if block is None:
        return ()
    names = _entry_list(block, "tool_packages")
    for position, name in enumerate(names):
        if not _IDENTIFIER_RE.match(name):
            raise ProfileFormatError(
                f"tool_packages[{position}] must be a top-level package name, got {name!r}"
            )
    return names


def parse_profile_document(doc: Any) -> RepoProfile:
    """The profile in a parsed ``profile.yaml``. Raises ProfileFormatError on
    anything outside the documented shape, so a half-understood file is never
    half-applied."""
    if not isinstance(doc, dict):
        raise ProfileFormatError("the profile must be a mapping with a class")
    allowed = ("class", "paths", "deny", "tool_packages", "test", *POSTURE_KEY_ORDER)
    unknown = sorted(str(k) for k in doc if k not in allowed)
    if unknown:
        raise ProfileFormatError(f"unknown or misplaced key: {', '.join(unknown)}")
    if "class" not in doc:
        raise ProfileFormatError("class is required")
    repo_class = _class_value(doc["class"], "class")
    paths = _path_overrides(doc.get("paths"))
    deny = parse_deny(doc.get("deny"))
    tool_packages = _tool_packages(doc.get("tool_packages"))
    try:
        declared_tests = parse_test_block(doc.get("test"))
    except TestBlockError as exc:
        raise ProfileFormatError(str(exc)) from exc
    # A field only means something where its class is in effect: the top-level
    # class or a path override. Anywhere else it would be silently ignored.
    classes_in_effect = {repo_class, *(value for _, value in paths)}
    if deny.entries() and CLASS_RELEASED_TOOL not in classes_in_effect:
        raise ProfileFormatError(
            f"deny is for {CLASS_RELEASED_TOOL} paths only, and no path of this profile has that class"
        )
    if tool_packages and CLASS_INTERNAL not in classes_in_effect:
        raise ProfileFormatError(
            f"tool_packages is for {CLASS_INTERNAL} paths only, and no path of this profile has that class"
        )
    return RepoProfile(
        repo_class=repo_class,
        paths=paths,
        deny=deny,
        tool_packages=tool_packages,
        test=declared_tests if declared_tests.declared_keys() else None,
        posture=tuple((key, doc[key]) for key in POSTURE_KEY_ORDER if key in doc),
    )


@dataclass(frozen=True)
class InferredProfile:
    """A draft profile and the observations it was drafted from."""

    profile: RepoProfile
    reasons: tuple[str, ...]


def infer_profile(paths: Iterable[str]) -> InferredProfile:
    """A draft profile for a tree with none, from the tree's file list alone.

    A root packaging manifest or a license file says the tree is published, so
    the draft is released-tool; with neither it is internal. For a released-tool
    draft, every top-level hidden directory that is not repository or host
    configuration is proposed as an internal override, because tool and harness
    state is not part of what ships. It is a starting point for a person to
    review, never a ruling."""
    listing = sorted(set(paths))
    root_files = {p for p in listing if "/" not in p}
    manifests = sorted(root_files & PACKAGING_MANIFESTS)
    licenses = sorted(p for p in root_files if p.upper().startswith(("LICENSE", "LICENCE", "COPYING")))
    reasons: list[str] = []
    if manifests:
        repo_class = CLASS_RELEASED_TOOL
        reasons.append(f"root packaging manifest: {', '.join(manifests)}")
    elif licenses:
        repo_class = CLASS_RELEASED_TOOL
        reasons.append(f"root license file: {', '.join(licenses)}")
    else:
        repo_class = CLASS_INTERNAL
        reasons.append("no root packaging manifest or license file")
    overrides: list[tuple[str, str]] = []
    if repo_class == CLASS_RELEASED_TOOL:
        hidden = sorted(
            {
                p.split("/", 1)[0]
                for p in listing
                if "/" in p and p.startswith(".") and p.split("/", 1)[0] not in HOST_CONFIG_DIRS
            }
        )
        for directory in hidden:
            overrides.append((f"{directory}/**", CLASS_INTERNAL))
            reasons.append(f"hidden directory {directory}/ looks like tool state, not product")
    return InferredProfile(RepoProfile(repo_class=repo_class, paths=tuple(overrides)), tuple(reasons))


def render_profile_yaml(inferred: InferredProfile) -> str:
    """YAML text for a draft, led by a comment saying it is a draft and why.
    Every string is JSON-quoted, which is valid YAML."""
    profile = inferred.profile
    lines = [
        "# DRAFT repo profile, inferred from the file tree. Review every line before committing.",
        "# Observations:",
        *(f"#   - {reason}" for reason in inferred.reasons),
        f"class: {profile.repo_class}",
    ]
    if profile.paths:
        lines.append("paths:")
        lines.extend(f"  {json.dumps(glob)}: {{class: {value}}}" for glob, value in profile.paths)
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "CLASSES",
    "CLASS_INTERNAL",
    "CLASS_RELEASED_TOOL",
    "CLASS_RULES",
    "DENY_HOSTS",
    "DENY_KINDS",
    "DENY_NAMES",
    "DENY_USERS",
    "HOST_CONFIG_DIRS",
    "PACKAGING_MANIFESTS",
    "InferredProfile",
    "ProfileDeny",
    "ProfileFormatError",
    "RepoProfile",
    "infer_profile",
    "parse_deny",
    "parse_profile_document",
    "render_profile_yaml",
]
