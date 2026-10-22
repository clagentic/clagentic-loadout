"""An internal-class path with tool_packages yields no mechanical finding; the
packages reach the reviewer in the rule statements and the chunk prompt instead."""

from __future__ import annotations

import re

from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.chunk_review import build_prompt
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.findings_contract import OUTPUT_CONTRACT
from clagentic_loadout.review.policy_source import (
    PROFILE_INVALID,
    PROFILE_MISSING,
    PROFILE_OK,
    PolicySource,
)

PROFILE = rp.RepoProfile(repo_class=rp.CLASS_INTERNAL, tool_packages=("tool_one", "tool_two"))
MIXED = rp.RepoProfile(
    repo_class=rp.CLASS_RELEASED_TOOL,
    paths=(("internal_dir/**", rp.CLASS_INTERNAL),),
    tool_packages=("tool_one", "tool_two"),
)
LINES = ["import tool_one.guard", "from tool_two.deep import y", "s='#'; import tool_one.guard"]


def _file_diff(name: str, lines=("x = 1",)) -> str:
    return "\n".join(
        [
            f"diff --git a/{name} b/{name}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{name}",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    ) + "\n"


def _diff() -> str:
    return _file_diff("x/a.py", LINES)


def _policy(profile, state=PROFILE_OK) -> PolicySource:
    return PolicySource((), stk.Stakes(), (), profile, state, None)


def test_internal_imports_produce_no_mechanical_finding():
    assert pr.profile_findings(_policy(PROFILE), plan_chunks(_diff())) == []


def test_the_rule_statements_carry_the_tool_packages_list():
    statements = pr.rule_statements(PROFILE, rp.CLASS_INTERNAL)
    assert statements[: len(rp.CLASS_RULES[rp.CLASS_INTERNAL])] == list(rp.CLASS_RULES[rp.CLASS_INTERNAL])
    assert any("tool_one, tool_two" in item for item in statements)


def test_without_tool_packages_the_statements_are_the_class_rules():
    bare = rp.RepoProfile(repo_class=rp.CLASS_INTERNAL)
    assert pr.rule_statements(bare, rp.CLASS_INTERNAL) == list(rp.CLASS_RULES[rp.CLASS_INTERNAL])


def test_the_released_tool_statements_are_its_class_rules_without_the_package_list():
    assert pr.rule_statements(MIXED, rp.CLASS_RELEASED_TOOL) == list(rp.CLASS_RULES[rp.CLASS_RELEASED_TOOL])


def test_the_released_tool_rules_state_the_agent_neutrality_rule():
    assert rp.CLASS_RULES[rp.CLASS_RELEASED_TOOL][-1] == (
        "A released tool never adds, removes or changes the tools or permissions of a "
        "particular agent or consumer based on its identity, and never names or special-cases "
        "a consumer. Behaviour that affects permissions for all sessions is a documented "
        "setting the user controls."
    )


def test_classes_in_effect_follow_the_fixed_class_order():
    assert pr.classes_in_effect(MIXED) == [rp.CLASS_RELEASED_TOOL, rp.CLASS_INTERNAL]
    assert pr.classes_in_effect(PROFILE) == [rp.CLASS_INTERNAL]


def test_an_internal_chunk_gets_its_rules_the_package_list_and_the_files():
    chunk = plan_chunks(_diff())[0]

    section = pr.chunk_class_rules(_policy(PROFILE), chunk)

    assert section.startswith("## Repository class rules")
    assert "### internal paths" in section and "x/a.py" in section
    assert "tool_one, tool_two" in section
    assert "released-tool" not in section


def test_a_released_tool_chunk_gets_the_released_tool_rules_only():
    chunk = plan_chunks(_file_diff("src/mod.py"))[0]

    section = pr.chunk_class_rules(_policy(MIXED), chunk)

    assert "### released-tool paths" in section and "src/mod.py" in section
    for statement in rp.CLASS_RULES[rp.CLASS_RELEASED_TOOL]:
        assert statement in section
    assert "### internal paths" not in section and "tool_one" not in section


def test_a_chunk_touching_both_classes_lists_each_class_with_its_own_files():
    chunk = plan_chunks(_file_diff("src/mod.py") + _file_diff("internal_dir/a.py"))[0]

    section = pr.chunk_class_rules(_policy(MIXED), chunk)

    released = section.index("### released-tool paths")
    internal = section.index("### internal paths")
    assert released < internal
    assert "src/mod.py" in section[released:internal]
    assert "internal_dir/a.py" in section[internal:]
    assert "tool_one, tool_two" in section[internal:]


def test_the_section_names_no_severity():
    chunk = plan_chunks(_file_diff("src/mod.py"))[0]
    section = pr.chunk_class_rules(_policy(MIXED), chunk).lower()
    for word in ("severity", "blocking", "critical", "high", "low", "nit"):
        assert not re.search(rf"\b{word}\b", section)


def test_a_missing_or_invalid_or_unread_profile_gives_no_section():
    chunk = plan_chunks(_diff())[0]
    for state in (PROFILE_MISSING, PROFILE_INVALID, "unread"):
        assert pr.chunk_class_rules(_policy(None, state), chunk) == ""
    assert pr.chunk_class_rules(_policy(PROFILE, PROFILE_INVALID), chunk) == ""


def test_a_long_file_list_is_bounded():
    names = [f"d/f{i}.py" for i in range(30)]

    section = pr.class_rules_section(PROFILE, names)

    assert "d/f19.py" in section and "d/f20.py" not in section
    assert "(and 10 more)" in section


def test_the_section_sits_after_the_rulebook_before_the_diff_and_the_contract_stays_last():
    chunk = plan_chunks(_diff())[0]
    section = pr.chunk_class_rules(_policy(PROFILE), chunk)

    prompt = build_prompt(chunk, 1, "RULEBOOK TEXT", "", section)

    assert prompt.index("RULEBOOK TEXT") < prompt.index("## Repository class rules")
    assert prompt.index("## Repository class rules") < prompt.index("## Diff chunk 1 of 1")
    assert prompt.endswith(OUTPUT_CONTRACT)


def test_without_a_section_the_prompt_is_unchanged():
    chunk = plan_chunks(_diff())[0]
    assert "Repository class rules" not in build_prompt(chunk, 1, "RULEBOOK TEXT")
