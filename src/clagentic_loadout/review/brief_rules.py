"""review.brief_rules -- the standing rules a brief reports for one role and step.

Every brief, whatever the role's class, carries a ``rules`` section: the group
rules (rules.groups) that apply to the repository, narrowed to the role and the
step asked. The section reports only. It never refuses, gates or changes the exit
code on a rule, a role or a step: a role or step the deployment has not declared
is named in ``notes`` and the records that do not name a role or step are still
listed.

Each listed rule carries its id, its statement as the parser stored it, its
source, its kind (null when the record has none), its group and the file it came
from, plus any of the optional record keys it holds and ``not_understood``.
"""

from __future__ import annotations

from typing import Any

from clagentic_loadout.role_vocabulary import RoleResolution
from clagentic_loadout.rules.applying import ApplyingGroupRules, select_rules


def _role_words(resolution: RoleResolution, declared: bool) -> tuple[str, ...] | None:
    """The words a record's ``roles`` may name for this role: the word asked and
    the role it resolves to. None when the role is not declared."""
    if not declared:
        return None
    return tuple(dict.fromkeys((resolution.word, resolution.role)))


def rules_section(
    found: ApplyingGroupRules,
    resolution: RoleResolution,
    *,
    role_declared: bool,
    role_note: str | None,
    step: str | None,
    declared_steps: tuple[str, ...],
    steps_problem: str | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """``(section, items)``: the ``rules`` section of a brief and the rules it
    lists, for the role *resolution* and the optional *step*."""
    step_declared = step is None or step in declared_steps
    notes: list[str] = []
    if role_note:
        notes.append(role_note)
    if steps_problem:
        notes.append(steps_problem)
    if step is not None and not step_declared:
        listed = ", ".join(declared_steps) if declared_steps else "none"
        notes.append(f"step {step!r} is not declared; declared steps: {listed}")
    items = [
        {**rule, "kind": rule.get("kind")}
        for rule in select_rules(
            found.rules,
            role_words=_role_words(resolution, role_declared),
            step=step,
            step_declared=step_declared,
        )
    ]
    section: dict[str, Any] = {
        "applies_to": {
            "role": resolution.word,
            "role_declared": role_declared,
            "step": step,
            "step_declared": step_declared if step is not None else None,
        },
        "groups_file": found.location.path if found.location else None,
        "groups_file_from": found.location.origin if found.location else None,
        "note": found.note,
        "notes": notes,
        "items": items,
    }
    return section, items


__all__ = ["rules_section"]
