"""merge.repo_gate_runtime — the runtime consumer of a repo's declared merge
gate keys.

`merge.gate_config` parses and validates the repo's `merge:` gate declarations.
This module is the ONLY place the gate rules live, and it owns two policies
that make consuming them safe.

WHERE THE GATE COMES FROM. Gate keys are read from the repo's TRACKED gate file
(`repo_config.TRACKED_GATE_RELATIVE_PATH`) as it exists at the PR's BASE
commit, with `git show <base_sha>:<path>`. Never from the working tree: a tree
holding the PR head (or anything but base) would let the PR under review
delete its own required scanners, corrupt the file to force the fallback below,
or drop its pre_checks. The base commit is not something the PR controls.

  - `required_reviewer_roles` is a FLOOR beneath `--required-reviewer`: the
    roles actually required are the union of the two.
  - `required_scanners` maps a reviewer role to scanner names a clean verdict
    from that role must record, and must not report as failed.
  - `pre_checks` are the commands run against the merge result before merging.

Keys that are machine-local (`post_merge_steps`, host paths, the sync knobs)
stay in the gitignored deployment file. A gate key found in that file is
IGNORED, with a warning naming the tracked location, so a deployment that has
not migrated is told instead of silently running with a weaker gate than it
believes it has.

ONE RULE SET, TWO CONSUMERS. `gate_from_tracked_text` is a pure function from a
gate file's text to a `RepoGate`; every per-key rule below lives in it and in
`with_resolvable_reviewer_roles`. `loadout-merge` feeds it the text at the PR
base commit (`load_repo_gate_at_base`); `loadout-doctor` feeds it the text at
HEAD (`load_gate_at_commit`) and prints the result's own warnings. A diagnostic
therefore consumes these functions rather than re-deriving what merge would do.

BOOTSTRAP. The PR that introduces the tracked file is judged by a base that has
none, so it runs flags-only; the file is enforced from the next merge on. A
fix-the-config PR is likewise judged by base's (broken) file, which falls back,
and its corrected file takes effect from the next merge.

FAILURE RULES DIFFER BY KEY, and this module changes none of them: it only
changes where the text comes from.

  - `required_reviewer_roles` and `required_scanners` fall back AS A PAIR. If
    the tracked file at base cannot be read, or either key cannot be loaded,
    neither is enforced: the merge degrades to the flags-only behaviour with a
    warning naming the commit, the file and the error, so the merge that lands
    the corrected config is always possible. A pair that is half-trusted is
    harder to reason about than one that is either enforced or visibly not.
    A declared role the deployment cannot resolve to a platform login is NOT an
    unloadable key: it degrades per role (`with_resolvable_reviewer_roles`).
    That role drops from the reviewer floor and its own `required_scanners`
    entry is skipped, each with a warning naming it; every other resolvable
    role and scanner requirement stays enforced. THIS IS DELIBERATE, not a
    gate relaxation: an unresolvable deployment mapping must never block
    merges, so a repo's declaration cannot demand deployment config it was
    never given. The role is skipped with a loud warning; resolvable roles
    and explicit `--required-reviewer` flags stay enforced (an unresolvable
    flag role is still a usage error). Locked by
    `TestRepoReviewerFloor.test_an_unresolvable_role_is_dropped_with_a_warning_and_the_rest_still_merges`
    in tests/test_merge_verb_findings_state.py.
  - `pre_checks` NEVER falls back. A `pre_checks` declaration that cannot be
    read or validated at base (including a file that is not valid UTF-8, or a
    whole-file parse failure, or a base commit that cannot be fetched or shown,
    or a PR payload that carries no base commit SHA (alone or beside a malformed
    `merge.git_working_tree`), or a repo path that is not a git tree) is reported in `RepoGate.pre_checks_error`, and the merge verb REFUSES the merge, as it
    did before the gate moved to base. No local tree at all (`repo_path` None)
    declares nothing. `--skip-pre-checks` is its bypass; `--ignore-repo-gate`
    does not lift it.

A tracked file that is simply absent at base declares nothing and is neither a
warning nor an error.

A config that loads cleanly but cannot be satisfied is NOT handled here: that
is a real refusal, overridable only by the verb's explicit, logged
`--ignore-repo-gate`, which covers the reviewer roles and required scanners.
This includes `required_scanners` declared for a role that is not a required
reviewer: `RepoGate.unreachable_scanner_roles` names such roles so the verb can
refuse.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

from clagentic_loadout.merge.commit_files import CommitFileReadError, read_file_at_commit
from clagentic_loadout.merge.gate_config import (
    CONFIG_KEY_REQUIRED_REVIEWER_ROLES,
    CONFIG_KEY_REQUIRED_SCANNERS,
    InvalidMergeGateConfigError,
    parse_tracked_merge_section,
    read_merge_section,
    reviewer_gate_from_section,
)
from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.post_merge_config import resolve_git_working_tree
from clagentic_loadout.merge.pre_checks_config import CONFIG_KEY_PRE_CHECKS, pre_checks_from_section
from clagentic_loadout.merge.reviewer_login import ReviewerLoginNotConfiguredError, resolve_reviewer_login
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH, resolve_repo_config_path

#: Keys that belong to the tracked gate file and are never honoured from the
#: per-deployment working-tree file.
GATE_KEYS = (CONFIG_KEY_REQUIRED_REVIEWER_ROLES, CONFIG_KEY_REQUIRED_SCANNERS, CONFIG_KEY_PRE_CHECKS)


@dataclass(frozen=True)
class RepoGate:
    """What the repo's config requires of a merge, plus any load warnings."""

    reviewer_roles: tuple[str, ...] = ()
    required_scanners: dict[str, tuple[str, ...]] | None = None
    pre_checks: tuple[dict, ...] = ()
    warnings: tuple[str, ...] = ()
    #: Why `pre_checks` could not be determined. Non-empty means the merge must
    #: be refused (unless pre_checks are explicitly skipped); it never means
    #: "no checks".
    pre_checks_error: str = ""

    def scanners_for(self, reviewer_name: str) -> tuple[str, ...]:
        return (self.required_scanners or {}).get(reviewer_name, ())

    def unreachable_scanner_roles(self, required_roles: Iterable[str]) -> list[str]:
        """Roles with declared required scanners that no required reviewer covers.

        Scanners are only ever checked on a role's verdict, so a declaration
        for a role outside the effective required-reviewer set could never
        gate anything. That is reported, never silently ignored.
        """
        covered = set(required_roles)
        return [role for role, names in (self.required_scanners or {}).items() if names and role not in covered]


def ignored_deployment_gate_warnings(repo_path: str | Path) -> tuple[str, ...]:
    """Warn for each gate key sitting in the working-tree deployment file.

    An unreadable deployment file yields nothing here: the loaders that own it
    report that failure themselves, and it carries no gate to warn about.
    """
    config_path = resolve_repo_config_path(repo_path, warn=False)
    try:
        merge_section = read_merge_section(config_path)
    except InvalidMergeGateConfigError:
        return ()
    return tuple(
        f"merge.{key} in {config_path} is IGNORED -- repo gate keys are read only from "
        f"{TRACKED_GATE_RELATIVE_PATH} at the PR base commit; declare it there"
        for key in GATE_KEYS
        if key in merge_section
    )


def _reviewer_pair_warnings(warnings: tuple[str, ...], reason: str) -> tuple[str, ...]:
    return (
        *warnings,
        f"merge.required_reviewer_roles NOT ENFORCED and merge.required_scanners NOT "
        f"ENFORCED -- the repo gate config could not be loaded, so only "
        f"--required-reviewer applies: {reason}",
    )


def _nothing_readable(warnings: tuple[str, ...], reason: str) -> RepoGate:
    """No gate declaration could be obtained (no base commit SHA, or a tracked
    file at the base commit that cannot be read or parsed): the reviewer pair
    falls back, and pre_checks cannot be determined, which refuses. A missing
    trusted base is never read as "nothing declared"."""
    return RepoGate(warnings=_reviewer_pair_warnings(warnings, reason), pre_checks_error=reason)


def gate_from_tracked_text(
    text: str | None, *, source: str, ignored_warnings: tuple[str, ...] = ()
) -> RepoGate:
    """The gate a tracked gate file's *text* declares: the one home of every
    per-key rule in the module docstring, with no I/O.

    *text* None means the file is absent, which declares nothing. *source* is
    the label error messages carry. *ignored_warnings* lead the result's
    warnings.
    """
    if text is None:
        return RepoGate(warnings=ignored_warnings)
    try:
        merge_section, section_present = parse_tracked_merge_section(text, source=source)
    except InvalidMergeGateConfigError as exc:
        return _nothing_readable(ignored_warnings, str(exc))

    warnings = ignored_warnings
    roles: tuple[str, ...] = ()
    scanners: dict[str, tuple[str, ...]] | None = None
    try:
        roles, scanners = reviewer_gate_from_section(source, merge_section, section_present)
    except InvalidMergeGateConfigError as exc:
        warnings = _reviewer_pair_warnings(ignored_warnings, str(exc))

    pre_checks: tuple[dict, ...] = ()
    pre_checks_error = ""
    try:
        pre_checks = tuple(pre_checks_from_section(merge_section))
    except PostMergeConfigError as exc:
        pre_checks_error = f"{source}: pre_checks: {exc}"
    return RepoGate(
        reviewer_roles=roles,
        required_scanners=scanners,
        pre_checks=pre_checks,
        warnings=warnings,
        pre_checks_error=pre_checks_error,
    )


def resolve_gate_git_tree(repo_path: str | Path) -> Path:
    """The git tree the tracked gate file is read from: the deployment's
    `merge.git_working_tree` when it declares one, else *repo_path* itself.

    Raises:
        PostMergeConfigError: `merge.git_working_tree` is malformed.
    """
    declared_tree = resolve_git_working_tree(repo_path)
    return declared_tree if declared_tree is not None else Path(repo_path)


def load_gate_at_commit(
    git_tree: str | Path,
    sha: str,
    *,
    base_branch: str = "",
    ignored_warnings: tuple[str, ...] = (),
) -> RepoGate:
    """Read the tracked gate file at *sha* in *git_tree* and apply
    `gate_from_tracked_text`. A commit or file that cannot be read is the same
    "nothing readable" gate a malformed file is."""
    source = f"{Path(git_tree).resolve()}@{sha[:12]}:{TRACKED_GATE_RELATIVE_PATH}"
    try:
        text = read_file_at_commit(git_tree, sha, TRACKED_GATE_RELATIVE_PATH, base_branch=base_branch)
    except CommitFileReadError as exc:
        return _nothing_readable(ignored_warnings, f"{source}: {exc}")
    return gate_from_tracked_text(text, source=source, ignored_warnings=ignored_warnings)


def load_repo_gate_at_base(
    repo_path: str | Path | None, *, base_sha: str, base_branch: str = ""
) -> RepoGate:
    """Load the repo's declared gate from the PR base commit.

    *repo_path* None (no local tree) declares nothing, matching every other
    repo-tier key in the merge verb. An empty *base_sha* (a PR payload that did
    not carry one) has no trusted base to read: the reviewer pair falls back
    with a warning and pre_checks refuse (fail closed).

    A malformed `merge.git_working_tree` is the post-merge tree sync's own
    config error, reported only after the merge has landed, so it must not let
    the gate be skipped. The reviewer pair falls back with a warning, and with
    a base commit known pre_checks are read from *repo_path* itself instead: a
    declared check still runs, and a *repo_path* that is not a git tree refuses
    before anything merges. With no base commit either, pre_checks refuse as
    well, never skipped. A tracked file absent at base declares nothing. The
    per-key failure rules are in the module docstring.
    """
    if repo_path is None:
        return RepoGate()

    ignored = ignored_deployment_gate_warnings(repo_path)
    try:
        git_tree = resolve_gate_git_tree(repo_path)
    except PostMergeConfigError as exc:
        pair_warnings = _reviewer_pair_warnings(ignored, str(exc))
        if not base_sha:
            # No tree to name and no base commit to read from: nothing can
            # supply pre_checks, so they refuse rather than being skipped.
            return RepoGate(
                warnings=pair_warnings,
                pre_checks_error=(
                    f"{exc}; and the PR payload carried no base commit SHA to read the gate at"
                ),
            )
        # The key names no tree, so the reviewer pair falls back like any gate
        # that cannot be loaded. pre_checks must not be skipped with it: they
        # are read from *repo_path*'s own tree at the base commit, so declared
        # checks still run and a tree that cannot supply them refuses.
        from_repo_path = load_gate_at_commit(
            Path(repo_path), base_sha, base_branch=base_branch, ignored_warnings=ignored
        )
        return replace(
            from_repo_path, reviewer_roles=(), required_scanners=None, warnings=pair_warnings
        )
    if not base_sha:
        return _nothing_readable(ignored, "the PR payload carried no base commit SHA to read it at")
    return load_gate_at_commit(git_tree, base_sha, base_branch=base_branch, ignored_warnings=ignored)


def with_resolvable_reviewer_roles(
    gate: RepoGate, platform: str, *, flagged_roles: Iterable[str] = ()
) -> RepoGate:
    """Degrade, per role, a gate naming roles the deployment cannot resolve to a
    *platform* login.

    A declared role is resolved through `merge.reviewer_login.resolve_reviewer_login`,
    the single role -> login path (the bare role on Forgejo; the role's entry
    under `github_app.slugs` plus the bot suffix on GitHub). A role that does
    not resolve can never be checked, so ONLY that role is dropped from the
    reviewer floor and its own `required_scanners` entry is skipped, each with a
    warning naming the role, the platform and the missing mapping. Every other
    resolvable role, and its scanner requirements, stays enforced. This is not
    the whole-pair fallback, which is reserved for a gate file that cannot be
    parsed at all. Never a refusal: a repo's declaration must not demand
    deployment config it was never told about. A role already named by a
    `--required-reviewer` flag keeps the flag's login and is not resolved here.

    DELIBERATE POLICY, do not "fix" it into a refusal: an unresolvable
    deployment mapping must never block merges (no project may need new manual
    config just to keep merging). The role is skipped with a loud warning;
    resolvable roles and explicit `--required-reviewer` flags stay enforced.
    Locked by `TestRepoReviewerFloor.test_an_unresolvable_role_is_dropped_with_a_warning_and_the_rest_still_merges`
    in tests/test_merge_verb_findings_state.py.
    """
    flagged = set(flagged_roles)
    kept: list[str] = []
    dropped: list[str] = []
    warnings = list(gate.warnings)
    for role in gate.reviewer_roles:
        if role in flagged:
            kept.append(role)
            continue
        try:
            resolve_reviewer_login(role, platform)
        except ReviewerLoginNotConfiguredError as exc:
            dropped.append(role)
            warnings.append(
                f"declared reviewer role {role!r} cannot be resolved to a {platform} login "
                f"({exc}); it is DROPPED from the reviewer floor and its required_scanners "
                f"entry is skipped, every other declared role and scanner stays enforced"
            )
        else:
            kept.append(role)
    if not dropped:
        return gate
    scanners = gate.required_scanners
    if scanners is not None:
        scanners = {role: names for role, names in scanners.items() if role not in dropped}
    return replace(
        gate, reviewer_roles=tuple(kept), required_scanners=scanners, warnings=tuple(warnings)
    )


__all__ = [
    "GATE_KEYS",
    "RepoGate",
    "gate_from_tracked_text",
    "ignored_deployment_gate_warnings",
    "load_gate_at_commit",
    "load_repo_gate_at_base",
    "resolve_gate_git_tree",
    "with_resolvable_reviewer_roles",
]
