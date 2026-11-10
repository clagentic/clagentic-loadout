"""rules.applying -- the group rules that apply to one repository, resolved once.

Both `loadout-profile brief --role` and `loadout-spec new` need "the group rules
for repository R": the name comes from ``--repo`` or the origin of the working
tree, the groups file from the flag, the environment or the user config
(rules.groups), and the selection from rules.verb. This module is that one path.

A groups file that exists but cannot be used raises ``GroupsFileError`` rather
than being folded into a note, because the two callers treat it differently: the
brief ends with an I/O failure, the spec draft records the reason and goes on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clagentic_loadout.git_origin import OriginBindingError, read_origin_target
from clagentic_loadout.rules import groups as gr
from clagentic_loadout.rules.verb import RulesVerbError, build_result


@dataclass(frozen=True)
class ApplyingGroupRules:
    """The group rules that apply to a repository, in the verb-output shape of
    rules.verb. ``location`` is set when a groups file was read; ``note`` says why
    there are no rules when there are none."""

    rules: list[dict[str, Any]] = field(default_factory=list)
    location: gr.GroupsLocation | None = None
    note: str | None = None


def applying_group_rules(
    repo: str | None,
    repo_path: str,
    groups_file: str | None,
    config_root: str | Path | None,
    *,
    known_class: str | None = None,
) -> ApplyingGroupRules:
    """The group rules that apply to the repository named by *repo*, or by the
    origin of the tree at *repo_path* when *repo* is not given. *known_class* is
    a profile class the caller has already read. Raises gr.GroupsFileError when
    the groups file exists and cannot be used."""
    name = gr.bare_repo_name(repo) if repo else ""
    if not name:
        try:
            name = read_origin_target(repo_path).name
        except OriginBindingError:
            return ApplyingGroupRules(
                note="group rules not resolved: no --repo and no readable origin to name it"
            )
    location = gr.resolve_groups_location(groups_file, config_root=config_root)
    groups = gr.load_groups_file(location.path) if location.path else None
    try:
        result = build_result(name, location, groups, repo_path, known_class=known_class)
    except RulesVerbError as exc:
        return ApplyingGroupRules(note=f"group rules not resolved: {exc}")
    if result["result"] == "empty":
        return ApplyingGroupRules(location=None, note=f"no group rules: {result['reason']}")
    rules = [rule for group in result["groups"] for rule in group["rules"]]
    return ApplyingGroupRules(rules=rules, location=location)


def format_rule_item(rule: dict[str, Any]) -> str:
    """One rule as a spec item: ``<group id>/<rule id>: <statement>``, with
    `` [source: <source>]`` appended when the rule has a source."""
    item = f"{rule['group']}/{rule['id']}: {rule['statement']}"
    if rule.get("source"):
        item += f" [source: {rule['source']}]"
    return item


def source_label(location: gr.GroupsLocation) -> str:
    """``<path> (<origin>)``: the groups file and the setting that named it."""
    return f"{location.path} ({location.origin})"


__all__ = [
    "ApplyingGroupRules",
    "applying_group_rules",
    "format_rule_item",
    "source_label",
]
