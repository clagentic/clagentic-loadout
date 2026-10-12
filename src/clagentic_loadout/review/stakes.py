"""review.stakes — how much a path matters, resolved per path.

Three inputs, in decreasing order of trust for the question they answer:

  * the TREE at the trusted base revision: exposure markers (public ingress
    manifests and listener configs only, see EXPOSURE_MARKER_GLOBS; a packaging
    file is a supply-chain property, not an attack vector, so it marks nothing)
    and CODEOWNERS presence and coverage, recorded as context only;
  * the HOST: repository visibility, a single field the caller passes in;
  * the PROFILE: a deployment's own claim in ``stakes.yaml``.

Rules that make the result safe to gate on:

  * A tree marker (or a public repository) that says EXPOSED overrides a
    profile claim of INTERNAL. A profile can raise stakes, never hide them.
  * Anything not stated resolves to the worst case: exposed, regulated data,
    uncontrolled merges.
  * ``merge_control`` is ``controlled`` ONLY when the base-committed profile
    declares it; otherwise ``uncontrolled``. CODEOWNERS never changes it: a
    file that names owners does not prove review is enforced. Branch
    protection is not read (its API is admin-only on one host).

Pure functions over explicit inputs; reading the base revision is
review.policy_source's job.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from clagentic_loadout.review.path_glob import glob_regex, path_matches

EXPOSED = "exposed"
INTERNAL = "internal"
EXPOSURES = (EXPOSED, INTERNAL)

DATA_CLASSES = ("public", "internal", "confidential", "regulated")
WORST_DATA_CLASS = "regulated"

CONTROLLED = "controlled"
UNCONTROLLED = "uncontrolled"
MERGE_CONTROLS = (CONTROLLED, UNCONTROLLED)

VISIBILITY_PUBLIC = "public"
VISIBILITIES = ("public", "private", "internal")

_VOCABULARY = {"exposure": EXPOSURES, "data_class": DATA_CLASSES, "merge_control": MERGE_CONTROLS}

SOURCE_TREE = "tree marker"
SOURCE_VISIBILITY = "public repository"
SOURCE_PROFILE = "profile"
SOURCE_UNSTATED = "unstated (worst case)"

CODEOWNERS_ABSENT = "no CODEOWNERS file"
CODEOWNERS_COVERED = "covered by an owner"
CODEOWNERS_UNCOVERED = "not covered by an owner"

#: Files whose presence at the base revision marks the directory holding them
#: (and everything under it) as internet-exposed. Matched against every path in
#: the base tree. A marker at the repository root exposes the whole repository.
EXPOSURE_MARKER_GLOBS = (
    # public ingress manifests
    "**/ingress*.yaml",
    "**/ingress*.yml",
    "**/*-ingress.yaml",
    "**/*-ingress.yml",
    "**/ingress/**",
    # listener configs
    "**/nginx.conf",
    "**/Caddyfile",
    "**/haproxy.cfg",
    "**/traefik*.yaml",
    "**/traefik*.yml",
    "**/*listener*.yaml",
    "**/*listener*.yml",
    "**/*listener*.conf",
)

#: Where CODEOWNERS may live, first existing wins (the order the major hosts use).
CODEOWNERS_LOCATIONS = (
    ".github/CODEOWNERS",
    "CODEOWNERS",
    "docs/CODEOWNERS",
    ".forgejo/CODEOWNERS",
)


@dataclass(frozen=True)
class PathStakes:
    """The resolved stakes of one path, with where each value came from."""

    exposure: str
    exposure_source: str
    data_class: str
    data_class_source: str
    merge_control: str
    merge_control_source: str
    codeowners: str = CODEOWNERS_ABSENT

    def as_dict(self) -> dict[str, str]:
        return {
            "exposure": self.exposure,
            "exposure_source": self.exposure_source,
            "data_class": self.data_class,
            "data_class_source": self.data_class_source,
            "merge_control": self.merge_control,
            "merge_control_source": self.merge_control_source,
            "codeowners": self.codeowners,
        }


@dataclass(frozen=True)
class StakesProfile:
    """One named profile from ``stakes.yaml``. ``paths`` entries apply in
    order, a later match overriding an earlier one field by field."""

    name: str
    exposure: str | None = None
    data_class: str | None = None
    merge_control: str | None = None
    paths: tuple[tuple[str, dict[str, str]], ...] = ()


#: A host principal: ``@user``, ``@org/team`` or an email address. Any other
#: token after the pattern is not an owner.
_OWNER_RE = re.compile(r"^(?:@[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?|[^@\s]+@[^@\s]+\.[^@\s]+)\Z")


@dataclass(frozen=True)
class CodeownersRule:
    pattern: str
    has_owner: bool


@dataclass(frozen=True)
class TreeStakes:
    """What the base tree says. ``exposed_dirs`` are directories ('' is the
    repository root); ``codeowners`` is None when the file is absent."""

    exposed_dirs: tuple[str, ...] = ()
    codeowners: tuple[CodeownersRule, ...] | None = None


@dataclass(frozen=True)
class Stakes:
    """Everything needed to resolve the stakes of any path."""

    profile: StakesProfile | None = None
    tree: TreeStakes = field(default_factory=TreeStakes)
    visibility: str | None = None

    def for_path(self, path: str) -> PathStakes:
        exposure, exposure_source = self._exposure(path)
        data_class, data_class_source = self._profile_value(path, "data_class", WORST_DATA_CLASS)
        merge_control, merge_source = self._merge_control(path)
        return PathStakes(
            exposure,
            exposure_source,
            data_class,
            data_class_source,
            merge_control,
            merge_source,
            self._codeowners_context(path),
        )

    def _profile_value(self, path: str, key: str, default: str) -> tuple[str, str]:
        """The profile's claim for *key* on *path*, or *default* (the worst case)
        when it makes none. A claim outside the key's vocabulary is no claim."""
        if self.profile is None:
            return default, SOURCE_UNSTATED
        value: str | None = getattr(self.profile, key)
        for pattern, override in self.profile.paths:
            if key in override and path_matches(pattern, path):
                value = override[key]
        if value is None or value not in _VOCABULARY[key]:
            return default, SOURCE_UNSTATED
        return value, SOURCE_PROFILE

    def _exposure(self, path: str) -> tuple[str, str]:
        if any(_under(directory, path) for directory in self.tree.exposed_dirs):
            return EXPOSED, SOURCE_TREE
        if self.visibility == VISIBILITY_PUBLIC:
            return EXPOSED, SOURCE_VISIBILITY
        return self._profile_value(path, "exposure", EXPOSED)

    def _merge_control(self, path: str) -> tuple[str, str]:
        return self._profile_value(path, "merge_control", UNCONTROLLED)

    def _codeowners_context(self, path: str) -> str:
        """Whether CODEOWNERS names an owner for *path*. Context only: it feeds
        no rank, because naming an owner does not prove review is enforced."""
        if self.tree.codeowners is None:
            return CODEOWNERS_ABSENT
        owned = False
        for rule in self.tree.codeowners:
            if codeowners_pattern_matches(rule.pattern, path):
                owned = rule.has_owner
        return CODEOWNERS_COVERED if owned else CODEOWNERS_UNCOVERED


def _under(directory: str, path: str) -> bool:
    return directory == "" or path == directory or path.startswith(directory + "/")


def exposed_dirs_from_paths(paths: list[str]) -> tuple[str, ...]:
    """The directories holding an exposure marker among the base tree's *paths*."""
    found: set[str] = set()
    for candidate in paths:
        if any(path_matches(marker, candidate) for marker in EXPOSURE_MARKER_GLOBS):
            found.add(candidate.rpartition("/")[0])
    return tuple(sorted(found))


def codeowners_pattern_matches(pattern: str, path: str) -> bool:
    """CODEOWNERS pattern semantics, in the common subset: a leading or inner
    ``/`` anchors the pattern to the root, otherwise it matches at any depth;
    a pattern naming a directory covers everything under it."""
    anchored = pattern.startswith("/") or "/" in pattern.rstrip("/")
    body = pattern.lstrip("/").rstrip("/")
    if not body:
        return False
    if body.endswith("/**"):
        body = body[:-3]
    regex = glob_regex(body).pattern.removesuffix(r"\Z")
    prefix = "" if anchored else "(?:.*/)?"
    return re.match(prefix + regex + r"(?:/.*)?\Z", path) is not None


def parse_codeowners(text: str) -> tuple[CodeownersRule, ...]:
    """The rules of a CODEOWNERS file, in file order (the last match wins)."""
    rules: list[CodeownersRule] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("["):
            continue
        parts = line.split()
        rules.append(
            CodeownersRule(
                pattern=parts[0], has_owner=any(_OWNER_RE.fullmatch(token) for token in parts[1:])
            )
        )
    return tuple(rules)


class StakesFormatError(ValueError):
    """``stakes.yaml`` is not in the documented shape."""


def _checked(value: Any, allowed: tuple[str, ...], where: str) -> str | None:
    if value is None:
        return None
    if isinstance(value, str) and value in allowed:
        return value
    raise StakesFormatError(f"{where} must be one of {', '.join(allowed)}, got {value!r}")


_STAKE_KEYS = ("exposure", "data_class", "merge_control")


def _fields(block: Any, where: str, *, allow_paths: bool = False) -> dict[str, str]:
    """The stake claims in *block*. Only a profile may carry ``paths``; a
    ``paths`` key anywhere else is rejected by its full key path, never ignored."""
    if not isinstance(block, dict):
        raise StakesFormatError(f"{where} must be a mapping")
    allowed = (*_STAKE_KEYS, "paths") if allow_paths else _STAKE_KEYS
    unknown = sorted(str(k) for k in block if k not in allowed)
    if unknown:
        paths = ", ".join(f"{where}.{key}" for key in unknown)
        raise StakesFormatError(f"unknown or misplaced key: {paths}")
    out: dict[str, str] = {}
    for key, allowed in (
        ("exposure", EXPOSURES),
        ("data_class", DATA_CLASSES),
        ("merge_control", MERGE_CONTROLS),
    ):
        value = _checked(block.get(key), allowed, f"{where}.{key}")
        if value is not None:
            out[key] = value
    return out


def parse_stakes_document(doc: Any) -> tuple[dict[str, StakesProfile], str | None]:
    """The profiles of a parsed ``stakes.yaml`` and its default profile name.
    Raises StakesFormatError on anything outside the documented shape, so a
    half-understood file is never half-applied."""
    if doc is None:
        return {}, None
    if not isinstance(doc, dict):
        raise StakesFormatError("stakes file must be a mapping")
    unknown = sorted(str(k) for k in doc if k not in ("profile", "profiles"))
    if unknown:
        raise StakesFormatError(f"unknown or misplaced key: {', '.join(unknown)}")
    default = doc.get("profile")
    if default is not None and not isinstance(default, str):
        raise StakesFormatError("profile must be a profile name")
    raw_profiles = doc.get("profiles", {})
    if not isinstance(raw_profiles, dict):
        raise StakesFormatError("profiles must be a mapping of profile name to profile")
    profiles: dict[str, StakesProfile] = {}
    for name, block in raw_profiles.items():
        where = f"profiles.{name}"
        base = _fields(block, where, allow_paths=True)
        raw_paths = block.get("paths", {})
        if not isinstance(raw_paths, dict):
            raise StakesFormatError(f"{where}.paths must be a mapping of path glob to stakes")
        entries = tuple(
            (str(glob), _fields(entry, f"{where}.paths.{glob}")) for glob, entry in raw_paths.items()
        )
        profiles[str(name)] = StakesProfile(
            name=str(name),
            exposure=base.get("exposure"),
            data_class=base.get("data_class"),
            merge_control=base.get("merge_control"),
            paths=entries,
        )
    return profiles, default


__all__ = [
    "CODEOWNERS_LOCATIONS",
    "CONTROLLED",
    "DATA_CLASSES",
    "EXPOSED",
    "EXPOSURES",
    "EXPOSURE_MARKER_GLOBS",
    "INTERNAL",
    "MERGE_CONTROLS",
    "UNCONTROLLED",
    "VISIBILITIES",
    "VISIBILITY_PUBLIC",
    "CodeownersRule",
    "PathStakes",
    "Stakes",
    "StakesFormatError",
    "StakesProfile",
    "TreeStakes",
    "codeowners_pattern_matches",
    "exposed_dirs_from_paths",
    "parse_codeowners",
    "parse_stakes_document",
]
