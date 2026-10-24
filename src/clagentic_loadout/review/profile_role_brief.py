"""review.profile_role_brief -- the repo profile shaped for the role reading it.

``loadout-profile brief --role ROLE`` hands an author the rules it writes under
before it writes, instead of leaving them to be found by a reviewer afterwards.
What each role is shown depends on the role's class (role_vocabulary):

  scope    (planner, project orchestrator) the class rules, the group rules and
           the declared checks, as acceptance-criteria candidates to put in a
           task. Denied values are counted, never listed.
  build    (builder) what the build envelope carries: the class, its rules, the
           path overrides, the denied values, the tool packages and the declared
           test commands.
  gate     (reviewers, release manager) nothing new: a gate reads the repo's
           policy from the merge base through its own path, and this brief is
           not an input to it.
  others   the class, its rules and the path overrides, for context.

Risk posture is read only by gates, from the merge base. No brief carries it;
``without_posture`` removes any such key from what is emitted, so a field added
to the profile later cannot reach an author through this verb by accident.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from clagentic_loadout.review.repo_profile import DENY_KINDS, RepoProfile
from clagentic_loadout.role_vocabulary import (
    CLASS_BUILD,
    CLASS_GATE,
    CLASS_SCOPE,
    RoleResolution,
)

#: Keys that belong to the gate's view of a repository. Matched on the key
#: itself and on a ``<key>_source`` companion.
POSTURE_KEYS = frozenset(
    {
        "exposure",
        "data",
        "data_class",
        "visibility",
        "merge_control",
        "confirmed_at",
        "stakes",
        "posture",
    }
)

GATE_READ_NOTE = (
    "unchanged: a gate reads the repository policy from the merge base through its own path; "
    "this brief is not an input to any gate"
)


def _is_posture_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    return key in POSTURE_KEYS or (
        key.endswith("_source") and key[: -len("_source")] in POSTURE_KEYS
    )


def without_posture(value: Any) -> Any:
    """*value* with every posture key removed at any depth."""
    if isinstance(value, dict):
        return {k: without_posture(v) for k, v in value.items() if not _is_posture_key(k)}
    if isinstance(value, list):
        return [without_posture(v) for v in value]
    return value


def _deny_lists(profile: RepoProfile) -> dict[str, list[str]]:
    return {kind: list(getattr(profile.deny, kind)) for kind in DENY_KINDS}


def _candidates(
    profile: RepoProfile, brief: dict[str, Any], group_rules: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [
        {"source": f"class:{name}", "statement": statement}
        for name, statements in brief["class_rules"].items()
        for statement in statements
    ]
    for rule in group_rules:
        out.append(
            {
                "source": f"group:{rule['group']}",
                "file": rule["file"],
                "rule": rule["id"],
                "statement": rule["statement"],
            }
        )
    counts = {kind: len(values) for kind, values in _deny_lists(profile).items() if values}
    if counts:
        listed = ", ".join(f"{count} {kind}" for kind, count in counts.items())
        out.append(
            {
                "source": "profile:deny",
                "statement": f"The change adds none of the denied values the profile lists "
                f"({listed}) to released-tool paths.",
            }
        )
    if brief["tests"]:
        out.append(
            {
                "source": "profile:test",
                "statement": "The change passes the test commands the profile declares.",
            }
        )
    return out


def role_brief(
    profile: RepoProfile | None,
    resolution: RoleResolution,
    *,
    group_rules: Sequence[dict[str, Any]] = (),
    group_note: str | None = None,
) -> dict[str, Any]:
    """The brief for the role *resolution* names. *profile* may be None only
    for a gate role, which is not shown the profile. *group_rules* are the
    group rules that apply to the repository, in the verb-output shape of
    rules.verb."""
    out: dict[str, Any] = {
        "role": resolution.role,
        "role_word": resolution.word,
        "role_class": resolution.role_class,
        "role_source": resolution.source,
    }
    if resolution.role_class == CLASS_GATE:
        out.update({"shaped_for": "gate", "gate_read": GATE_READ_NOTE})
        return without_posture(out)
    if profile is None:
        raise ValueError("a non-gate role needs the repo profile")
    # Imported here because profile_brief imports profile_verb, which imports this module.
    from clagentic_loadout.review.profile_brief import profile_brief

    brief = profile_brief(profile)
    context = {
        "class": brief["class"],
        "class_rules": brief["class_rules"],
        "paths": brief["paths"],
    }
    if resolution.role_class == CLASS_SCOPE:
        out.update(
            {
                "shaped_for": "acceptance-criteria",
                **context,
                "tests": brief["tests"],
                "acceptance_criteria_candidates": _candidates(profile, brief, group_rules),
            }
        )
        if group_note:
            out["group_rules_note"] = group_note
    elif resolution.role_class == CLASS_BUILD:
        out.update(
            {
                "shaped_for": "envelope",
                **context,
                "deny": _deny_lists(profile),
                "tool_packages": list(profile.tool_packages),
                "tests": brief["tests"],
            }
        )
    else:
        out.update({"shaped_for": "context", **context})
    return without_posture(out)


__all__ = [
    "GATE_READ_NOTE",
    "POSTURE_KEYS",
    "role_brief",
    "without_posture",
]
