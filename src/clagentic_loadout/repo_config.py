"""repo_config.py — the ONE shared repo-local config-path constant/loader
(lr-446c35, operator-ratified 2026-07-11).

Every loadout verb that reads a repo-local, sectioned config file (wait,
merge, push, provisioning.roles, provisioning.model_routing, release.detector)
previously hardcoded its own copy of the relative path literal
`.loadout/config.yaml` — six duplicated definitions, no shared constant. This
module is that shared constant/loader: every caller imports
`DEFAULT_CONFIG_RELATIVE_PATH` (and, where a caller needs the resolved
absolute path with legacy fallback applied, `resolve_repo_config_path`) from
here instead of re-declaring the literal.

PATH CHANGE (operator decision, 2026-07-11): the canonical location
moves from `.loadout/config.yaml` to `.clagentic/loadout/config.yaml` —
mirroring the user-tier `~/.config/clagentic/loadout/config.yaml` home
exactly, and giving other `clagentic` tools (e.g. gatekeeper) their own
`.clagentic/<tool>/` home without further repo-root dotdir proliferation.
`DEFAULT_CONFIG_RELATIVE_PATH` now points at the new path; the OLD path is
`LEGACY_CONFIG_RELATIVE_PATH`, kept only for the transitional back-compat
read below.

TRANSITIONAL BACK-COMPAT (remove after the fleet migration completes, tracked
in lr-a645aa VERIFY-DONE): a repo that has not yet migrated has ONLY
`.loadout/config.yaml`, not `.clagentic/loadout/config.yaml`. Rather than
have every one of the six section-owning loaders duplicate an
absent-new/present-old fallback check, `resolve_repo_config_path` centralizes
it: if the new path is absent but the legacy path exists, it returns the
LEGACY path and prints a single-line deprecation warning to stderr. A repo
with neither path, or with the new path already present, is unaffected (no
warning, no fallback). Every section-owning loader in this package (wait,
merge.post_merge_config, push.cleanliness_config, provisioning.roles,
provisioning.model_routing) now resolves its own config path through this
function rather than doing its own `Path(repo_root) / config_relative_path`
existence check directly against a single hardcoded literal.

BOUNDED WRAPPER-HOP DISCOVERY (lr-18f46a): a workspace-wrapper layout keeps a
project's `.clagentic/loadout/config.yaml` one directory ABOVE the inner git
repo (e.g. a wrapper directory holding the config, wrapping an inner git
checkout that has no config of its own). `resolve_repo_config_path`'s
starting `repo_root` argument previously supported only a single, direct
`repo_root / config_relative_path` join — invisible to that layout. Every
caller now gets ONE additional, git-anchored hop for free, via
`resolve_repo_config_root` below: (1) `repo_root` itself; (2) *repo_root*'s
own git top-level (if `repo_root` sits inside a git repo but isn't its own
top-level); (3) that git top-level's immediate parent (the wrapper) — each
step checked ONLY for presence of the specific config file (new or legacy),
stopping the instant one is found. This mirrors the reference bounded-hop
resolver's semantics (single git-anchored hop, never an unbounded ancestor
walk — that walk was deliberately killed as a landmine risk) WITHOUT
importing it: this module has zero dependencies beyond the stdlib and PyYAML
callers already pull in, and does not read `CLAUDE.md` or any other
project-marker file — keyed SOLELY on the specific config file's presence,
never on marker-file heuristics. A repo whose own git top-level already
carries its OWN config file is a dead end for the hop (steps 2/3 never run)
— the bound is exactly one hop past a self-contained repo, never further,
and never past a repo that already answers for itself.

BOUND DISCOVERY FOR THE MERGE VERBS: the hop above is right for settings, and
wrong for a config that supplies commands or gate keys, because a directory
above the git tree is not covered by the tree's origin binding.
`resolve_bound_config_root` / `resolve_bound_config_path` are the one resolver
those readers use. A config at the tree wins; a config in the tree's direct
parent counts only when it declares which single child directory is the tree
and that child resolves to exactly the tree; any other config above the tree
that holds one of the caller's guarded keys is refused, never read as empty.
The caller names its section, declaration key and guarded keys, so this module
still knows no verb's vocabulary.

Apart from that bound resolver this module owns ONLY the path/fallback/hop
concern — it does not validate section content, and it reads a section's keys
only to apply the bound rules above. Each verb's own loader keeps that
responsibility (mirrors this package's existing "caller fetches/validates,
this module is a pure policy/path check" split — see
provisioning.model_routing's own docstring for the same pattern applied to
scope-tier routing).
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import yaml

from clagentic_loadout.git_origin import OriginBindingError, read_origin_target

#: Canonical relative path (from a repo root) to the single sectioned
#: per-repo loadout config file. Every loadout verb that reads repo-local
#: config resolves its own path through `resolve_repo_config_path` (or, for
#: a caller that only needs the bare default literal — e.g. a CLI help
#: string, or a test asserting the constant's value — imports this name
#: directly) rather than re-declaring the literal.
DEFAULT_CONFIG_RELATIVE_PATH = ".clagentic/loadout/config.yaml"

#: Relative path (from a repo root) to the TRACKED, committed merge-gate
#: declaration. Unlike `DEFAULT_CONFIG_RELATIVE_PATH` (a per-deployment file a
#: repo may gitignore), this file is reviewed and landed through the PR flow,
#: and `loadout-merge` reads it only as it exists at the PR's base commit.
TRACKED_GATE_RELATIVE_PATH = ".clagentic/loadout/gate.yaml"

#: The PRE-lr-446c35 relative path. Read only as a transitional fallback by
#: `resolve_repo_config_path` when `DEFAULT_CONFIG_RELATIVE_PATH` is absent —
#: never used as a first choice, never returned when both paths are absent.
#: Remove once the fleet migration (lr-a645aa) completes and every enrolled
#: repo has been rewritten onto the new path.
LEGACY_CONFIG_RELATIVE_PATH = ".loadout/config.yaml"

#: Bare marker name (directory or file) for "this repo has a LEGACY,
#: not-yet-migrated loadout config home" — used by callers (e.g.
#: doctor.checks, release.detector's semantic-release-ownership marker) that
#: only need to test for the legacy directory's presence, not read/parse a
#: config file through it.
LEGACY_CONFIG_MARKER = ".loadout"


def find_git_top_level(start: str | Path) -> Path | None:
    """Locate the git top-level directory containing *start*, if any.

    Uses `git rev-parse --show-toplevel` (run with `cwd=start`) rather than a
    hand-rolled `.git`-marker ancestor walk — *start* is not necessarily an
    invocation cwd here (callers may pass an arbitrary `repo_root`), and
    shelling out to git is the same primitive `push.verb._resolve_repo_root`
    already uses for its own toplevel lookup, so both call sites agree with
    git's own notion of "top level" (worktrees, submodules, etc.) instead of
    each hand-rolling a slightly different answer.

    Returns None if *start* does not exist, is not a directory, or is not
    inside a git working tree (non-zero exit, or empty stdout).
    """
    start = Path(start)
    if not start.is_dir():
        return None
    probe = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return None
    stdout = probe.stdout.strip()
    if not stdout:
        return None
    return Path(stdout).resolve()


def find_git_top_level_down_hop(start: str | Path) -> Path | None:
    """Bounded DOWN-hop mirror of `find_git_top_level` (lr-c17040 SECONDARY,
    complementary to lr-18f46a's UP-hop): given a *start* directory that is
    itself NOT inside a git working tree (e.g. a workspace-wrapper directory
    holding a project's config one level above the actual checkout), descend
    EXACTLY ONE level to find the single git repo *start* directly contains,
    if there is exactly one.

    This exists for a caller that genuinely needs a LOCAL repo root resolved
    from a wrapper cwd (e.g. a sandbox whose only reachable git invocation is
    `git show|diff|log` with no `-C`/`--git-dir`/`cd`, so the process's own
    cwd must already BE the repo). It is deliberately the SECONDARY fix for
    the diff-acquisition gap this task addresses — the PRIMARY, API-based
    acquisition (see `clagentic_loadout.acquire`) never needs a local repo
    root at all, since it fetches everything from the host API. Use this
    function only when a local checkout is genuinely the only option.

    Resolution, bounded to exactly one level (never a recursive/unbounded
    descent — an unbounded walk down an arbitrary directory tree is exactly
    the landmine-risk class `resolve_repo_config_root`'s own docstring
    already rejected for the UP-hop direction):
      1. If *start* itself carries its OWN `.git` marker (directly, not via
         an ancestor), returns None — this function is for the "not in a
         repo at all" case only; a caller already inside a repo should use
         `find_git_top_level` directly, never this hop. This is
         deliberately a DIRECT marker check on *start* itself, never
         `find_git_top_level(start)` — that helper walks UP the ancestor
         chain (correct git behavior for "which repo am I in"), which would
         incorrectly treat *start* as "already in a repo" whenever some
         ANCESTOR of *start* happens to be a git repo, even though *start*
         itself is a perfectly ordinary wrapper directory with no `.git` of
         its own — exactly the wrapper-directory case this hop exists to
         handle.
      2. Otherwise, lists *start*'s immediate subdirectories (skipping
         dotdirs — `.git`, `.clagentic`, etc. are never candidates
         themselves) and checks each for its OWN `.git` marker (a directory
         or file, matching git's own submodule-vs-ordinary-repo shape)
         directly beneath it — never one more level down. Returns that
         subdirectory's resolved git top-level (via `find_git_top_level`,
         so worktrees/submodules resolve identically to the UP-hop path) IFF
         exactly ONE immediate subdirectory qualifies.
      3. Zero qualifying subdirectories, or MORE than one (an ambiguous
         wrapper containing multiple checkouts — this function refuses to
         guess which one the caller meant), both return None.

    Returns None if *start* does not exist or is not a directory.
    """
    start = Path(start)
    if not start.is_dir():
        return None

    if (start / ".git").exists():
        return None

    candidates: list[Path] = []
    for child in sorted(start.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if (child / ".git").exists():
            candidates.append(child)

    if len(candidates) != 1:
        return None

    return find_git_top_level(candidates[0])


def _default_exists_check(path: Path) -> bool:
    """Default per-candidate existence predicate: a FILE at *path*.

    Matches every section-owning loader's own contract (`resolve_repo_config_path`
    keys on the config file being a FILE, never a bare directory) — this is
    the correct default for the six config-reading consumers. A caller with
    a genuinely different presence contract (e.g. release.detector's
    per-marker check: config FILE for the current home, DIRECTORY for the
    legacy home) passes its own `exists_check`.
    """
    return path.is_file()


def resolve_repo_config_root(
    repo_root: str | Path,
    *relative_paths: str,
    exists_check: Callable[[Path], bool] = _default_exists_check,
) -> Path:
    """Resolve the directory that carries one of *relative_paths* (per
    *exists_check*), trying *repo_root* itself and then exactly ONE
    git-anchored hop above it (lr-18f46a — see module docstring, "BOUNDED
    WRAPPER-HOP DISCOVERY").

    Resolution order (bounded, never an unbounded ancestor walk):
      1. `repo_root` — if any of *relative_paths* satisfies *exists_check*
         directly under it, return `repo_root` unconditionally. A repo that
         already answers for itself is a dead end here; steps 2/3 never run.
      2. `repo_root`'s own git top-level, when `repo_root` sits inside a git
         repo and is NOT already that top-level itself — if any of
         *relative_paths* satisfies *exists_check* there, return the git
         top-level.
      3. That git top-level's immediate parent (the wrapper directory) — if
         any of *relative_paths* satisfies *exists_check* there, return the
         wrapper.
      4. Otherwise, return `repo_root` unconditionally (matches every
         section-owning loader's existing "missing file -> use my own
         default, relative to the root I was given" contract — this
         function only decides WHICH of the single-hop candidates counts as
         "the config-bearing root," never invents a new "absent" behavior).

    Keyed SOLELY on the presence of the named *relative_paths* — no
    CLAUDE.md or other project-marker awareness (explicit task requirement,
    lr-18f46a: this is about the config file only, unlike the reference
    resolver's separate marker-aware variant, which is deliberately NOT
    ported here).

    RESOLVED-PATH CONTRACT (lr-329d27, deferred review nit from lr-18f46a):
    every return path yields a RESOLVED `Path` (symlinks and `..` segments
    collapsed, absolute) — including the step-1/step-4 `repo_root`
    fallbacks, matching `find_git_top_level`'s own `.resolve()` on its
    result. Uniform across all four return paths; callers may rely on the
    result always being resolved, never conditionally so depending on
    whether `repo_root` happened to exist on disk at call time.
    """

    def _present(root: Path) -> bool:
        return any(exists_check(root / relative_path) for relative_path in relative_paths)

    repo_root = Path(repo_root).resolve()

    if _present(repo_root):
        return repo_root

    git_top = find_git_top_level(repo_root)
    if git_top is None:
        return repo_root

    if git_top != repo_root and _present(git_top):
        return git_top

    wrapper = git_top.parent
    if wrapper == git_top:
        # git_top is the filesystem root; no parent to hop to.
        return repo_root
    if _present(wrapper):
        return wrapper

    return repo_root


def resolve_repo_config_path(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
    warn: bool = True,
) -> Path:
    """Resolve the sectioned repo-local loadout config file's path for
    *repo_root*, applying the transitional legacy-path fallback (see module
    docstring) AND the bounded wrapper-hop discovery (lr-18f46a) — tried in
    that order, at each of the (at most three) candidate roots the hop
    considers.

    Resolution order:
      1. Resolve the config-bearing ROOT via `resolve_repo_config_root`:
         *repo_root* itself, or (only if *repo_root* itself carries neither
         path) exactly one git-anchored hop to the git top-level or its
         immediate parent (the wrapper) — whichever of those is the first to
         carry `config_relative_path` OR `legacy_relative_path` as a file.
         *repo_root* explicitly carrying its own config (new or legacy) is
         always a dead end for the hop — a self-contained repo never climbs.
      2. At the resolved root: `<root>/<config_relative_path>` (default: the
         NEW `.clagentic/loadout/config.yaml` path) — returned unconditionally
         when it exists, regardless of whether the legacy path also exists.
      3. `<root>/<legacy_relative_path>` (default: the OLD
         `.loadout/config.yaml` path) — returned, with a ONE-LINE deprecation
         warning to stderr (unless *warn* is False), only when the new path
         is ABSENT and the legacy path EXISTS.
      4. Neither exists anywhere in the single-hop chain: returns
         `<repo_root>/<config_relative_path>` (the ORIGINAL, un-hopped root)
         unconditionally (matches every section-owning loader's existing
         "missing file -> use my own default" contract — the caller decides
         what "absent" means for its own section).

    A caller that overrides `config_relative_path` for its own testing
    purposes (mirrors every section-owning loader's existing
    `config_relative_path=` test seam) gets the SAME legacy-fallback and hop
    behavior against `legacy_relative_path` — both paths are still resolved
    relative to whichever root the hop settles on.

    `--repo-path`-style CLI overrides still win: this function only ever
    reads FROM the *repo_root* a caller passes in (or hops exactly one level
    above it) — it never substitutes an unrelated root, and a caller that
    wants to disable the hop entirely can pass a *repo_root* that already
    carries its own config file (dead-ends the hop at step 1).
    """
    repo_root = Path(repo_root)
    config_root = resolve_repo_config_root(
        repo_root, config_relative_path, legacy_relative_path
    )
    return _config_file_at(
        config_root, repo_root, config_relative_path, legacy_relative_path, warn
    )


def _config_file_at(
    config_root: Path,
    repo_root: Path,
    config_relative_path: str,
    legacy_relative_path: str,
    warn: bool,
) -> Path:
    """The new-path-then-legacy-path choice at an already settled *config_root*;
    *repo_root*'s own new path when neither exists."""
    new_path = config_root / config_relative_path
    if new_path.exists():
        return new_path

    legacy_path = config_root / legacy_relative_path
    if legacy_path.exists():
        if warn:
            print(
                f"clagentic-loadout: {legacy_path} is deprecated — migrate to "
                f"{new_path} (legacy support will be removed after the "
                f"fleet migration).",
                file=sys.stderr,
            )
        return legacy_path

    return repo_root / config_relative_path


class ConfigRootRefusedError(ValueError):
    """A config file above the git tree cannot be accepted as that tree's config.
    The message names the resolved tree, the directory holding the file and the
    tree's origin as parsed host and owner/name only, never the remote's URL text."""


def _refusal(
    config_file: Path,
    directory: Path,
    tree: Path,
    why: str,
    section: str,
    declaration_key: str,
) -> ConfigRootRefusedError:
    try:
        target = read_origin_target(tree)
        remote = f"remote origin {target.slug!r} on host {target.host!r}"
    except OriginBindingError:
        remote = "remote origin is not a readable, parseable remote URL"
    return ConfigRootRefusedError(
        f"the config file {config_file} above the git tree is refused: {why}. "
        f"Resolved git tree {str(tree)!r}, config root {str(directory)!r}, "
        f"{remote}. A config in the directory holding the git "
        f"tree must declare {section}.{declaration_key} naming that tree; any other "
        f"config above the tree that holds merge settings is not used."
    )


def _section_of(config_file: Path, section: str) -> dict:
    """The *section* mapping of the YAML file *config_file*; ``{}`` when absent.

    Raises ValueError when the file cannot be read or is not a mapping."""
    try:
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise ValueError(f"{config_file} could not be read as YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError(f"{config_file}: top-level document must be a mapping")
    value = raw.get(section)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{config_file}: {section!r} section must be a mapping")
    return value


def _declared_tree_problem(
    declared: object, config_dir: Path, tree: Path, section: str, declaration_key: str
) -> str:
    """Why *declared* does not name *tree* as a direct child of *config_dir*;
    '' when it does."""
    label = f"{section}.{declaration_key}"
    if not isinstance(declared, str) or not declared or declared != declared.strip():
        return f"{label} must be a single path segment naming the git tree, got {declared!r}"
    if declared in (".", "..") or "/" in declared or "\\" in declared:
        return f"{label} must be a single path segment, got {declared!r}"
    candidate = config_dir / declared
    if candidate.is_symlink():
        return f"{label} {declared!r} is a symlink"
    if candidate.resolve() != tree:
        return f"{label} {declared!r} resolves to {candidate.resolve()}, not the git tree"
    return ""


def resolve_bound_config_root(
    repo_root: str | Path,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
    *,
    section: str,
    declaration_key: str,
    guarded_keys: Iterable[str],
) -> Path:
    """The directory whose config file a verb that executes or enforces what the
    file says may read for *repo_root*, tied to the git tree it names.

    T is the git top-level of *repo_root*. In order:

      1. A config file at *repo_root* or at T wins; nothing above T is consulted
         for it. A *repo_root* in no git tree is returned unchanged.
      2. A config file in T's direct parent X is accepted only when its
         *section* declares *declaration_key* as a single path segment W naming
         a real directory (not a symlink) that resolves to exactly T.
      3. Any config file anywhere above T whose *section* holds one of
         *guarded_keys* or the declaration, and which rule 2 does not accept, is
         refused with ConfigRootRefusedError naming T, its directory and T's
         parsed origin host and owner/name. A file above T that cannot be read is refused the same way,
         because what it would have declared cannot be known. Files above T that
         hold none of those keys are ignored.
      4. Otherwise *repo_root* (resolved), as when no config exists.

    This is the single resolver for the merge verbs' config readers; verbs that
    only read settings keep `resolve_repo_config_root`.

    Raises:
        ConfigRootRefusedError: rule 3.
    """
    root = Path(repo_root).resolve()
    names = (config_relative_path, legacy_relative_path)

    def _file_in(directory: Path) -> Path | None:
        for name in names:
            if (directory / name).is_file():
                return directory / name
        return None

    if _file_in(root) is not None:
        return root
    tree = find_git_top_level(root)
    if tree is None:
        return root
    if tree != root and _file_in(tree) is not None:
        return tree

    watched = (*guarded_keys, declaration_key)
    accepted: Path | None = None
    for directory in tree.parents:
        config_file = _file_in(directory)
        if config_file is None:
            continue

        try:
            held_section = _section_of(config_file, section)
        except ValueError as exc:
            raise _refusal(config_file, directory, tree, str(exc), section, declaration_key) from exc
        held = [key for key in watched if key in held_section]
        if not held:
            continue
        if directory != tree.parent:
            raise _refusal(
                config_file,
                directory,
                tree,
                f"it declares {held} but is not in the directory holding the git tree",
                section,
                declaration_key,
            )
        problem = _declared_tree_problem(
            held_section.get(declaration_key), directory, tree, section, declaration_key
        )
        if problem:
            raise _refusal(
                config_file, directory, tree, f"it declares {held} and {problem}", section, declaration_key
            )
        accepted = directory
    return accepted if accepted is not None else root


def resolve_bound_config_path(
    repo_root: str | Path,
    *,
    section: str,
    declaration_key: str,
    guarded_keys: Iterable[str],
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    legacy_relative_path: str = LEGACY_CONFIG_RELATIVE_PATH,
    warn: bool = True,
) -> Path:
    """The config file for *repo_root* under `resolve_bound_config_root`, with the
    same legacy-path fallback and deprecation notice as `resolve_repo_config_path`.

    Raises:
        ConfigRootRefusedError: as `resolve_bound_config_root`.
    """
    config_root = resolve_bound_config_root(
        repo_root,
        config_relative_path,
        legacy_relative_path,
        section=section,
        declaration_key=declaration_key,
        guarded_keys=guarded_keys,
    )
    return _config_file_at(
        config_root, Path(repo_root), config_relative_path, legacy_relative_path, warn
    )


__all__ = [
    "ConfigRootRefusedError",
    "DEFAULT_CONFIG_RELATIVE_PATH",
    "LEGACY_CONFIG_MARKER",
    "LEGACY_CONFIG_RELATIVE_PATH",
    "TRACKED_GATE_RELATIVE_PATH",
    "find_git_top_level",
    "find_git_top_level_down_hop",
    "resolve_bound_config_path",
    "resolve_bound_config_root",
    "resolve_repo_config_path",
    "resolve_repo_config_root",
]
