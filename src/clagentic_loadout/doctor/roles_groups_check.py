"""doctor.roles_groups_check -- the deployment's role classes and groups file.

Two optional USER-LEVEL settings feed read-only verbs, and a mistake in either
is silent until a planner or builder asks for a brief:

  * ``role_classes:`` maps a role word to a class (role_vocabulary). A value
    that is not a mapping of word to a known class is a FAILING finding.
  * the groups file named by ``--groups-file``-less resolution
    (``CLAGENTIC_LOADOUT_GROUPS_FILE`` or ``rules.groups_file``). One that exists
    and cannot be parsed is a FAILING finding. Not configured, or no file at the
    configured path, is an advisory only: the group rules are then empty.

With a repo, the class the groups file assigns that repo is compared to the
class of the repo's own profile. A disagreement is reported as an advisory and
never fails the check: a profile is reviewed at the merge base and the groups
file is out-of-repo configuration, so neither overrides the other here.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.doctor.checks import CheckResult
from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
from clagentic_loadout.review.policy_source import PROFILE_OK
from clagentic_loadout.review.profile_brief import ProfileReadError, read_profile
from clagentic_loadout.role_vocabulary import RoleVocabularyError, load_role_classes
from clagentic_loadout.rules import groups as gr


def _repo_name(repo_root: Path) -> str:
    try:
        return read_origin_target(repo_root).name
    except OriginBindingError:
        return repo_root.resolve().name


def _class_advisories(groups: gr.GroupsFile, repo_root: Path) -> list[str]:
    name = _repo_name(repo_root)
    assigned = groups.classes_of(name)
    if not assigned:
        return []
    try:
        profile, state, _problem = read_profile(repo_root)
    except ProfileReadError:
        return []
    out = []
    profile_class = profile.repo_class if state == PROFILE_OK and profile is not None else None
    for entry in assigned:
        if not gr.schema_admits(entry.name):
            out.append(
                f"{name!r} is assigned class {entry.name!r} by {groups.path}, which the profile "
                "schema does not admit"
            )
        if profile_class is not None and profile_class != entry.name:
            out.append(
                f"{name!r} has profile class {profile_class!r} but {groups.path} assigns "
                f"{entry.name!r} (advisory; neither overrides the other)"
            )
    return out


def check_roles_and_groups(
    repo_root: Path | None = None, *, config_root: str | Path | None = None
) -> CheckResult:
    """Validate the user-level ``role_classes`` and the groups file, and with
    *repo_root* report class disagreements as advisories."""
    resolved: dict = {"config_root": str(config_root) if config_root is not None else None}
    errors: list[str] = []
    advisories: list[str] = []

    try:
        classes = load_role_classes(config_root)
        resolved["role_classes"] = sorted(classes)
    except RoleVocabularyError as exc:
        errors.append(str(exc))

    location = gr.resolve_groups_location(config_root=config_root)
    resolved["groups_file"] = location.path
    resolved["groups_file_from"] = location.origin
    groups = None
    if location.path is None:
        advisories.append("no groups file configured, so group rules are empty")
    else:
        try:
            groups = gr.load_groups_file(location.path)
        except gr.GroupsFileError as exc:
            errors.append(str(exc))
        else:
            if groups is None:
                advisories.append(f"no groups file at {location.path!r}, so group rules are empty")
    if groups is not None and repo_root is not None:
        advisories.extend(_class_advisories(groups, repo_root))
    resolved["advisories"] = advisories
    resolved["errors"] = errors

    if errors:
        return CheckResult(
            name="roles_and_groups",
            ok=False,
            summary=f"{len(errors)} finding(s) -- " + "; ".join(errors),
            resolved=resolved,
        )
    summary = "role_classes and groups file OK"
    if advisories:
        summary += "; advisory: " + "; ".join(advisories)
    return CheckResult(name="roles_and_groups", ok=True, summary=summary, resolved=resolved)


__all__ = ["check_roles_and_groups"]
