"""An internal-class path with tool_packages yields no mechanical finding; the
packages reach the reviewer in the rule statements instead."""

from __future__ import annotations

from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.policy_source import PROFILE_OK, PolicySource

PROFILE = rp.RepoProfile(repo_class=rp.CLASS_INTERNAL, tool_packages=("tool_one", "tool_two"))
LINES = ["import tool_one.guard", "from tool_two.deep import y", "s='#'; import tool_one.guard"]


def _diff() -> str:
    return "\n".join(
        [
            "diff --git a/x/a.py b/x/a.py",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/x/a.py",
            f"@@ -0,0 +1,{len(LINES)} @@",
            *(f"+{line}" for line in LINES),
        ]
    ) + "\n"


def test_internal_imports_produce_no_mechanical_finding():
    policy = PolicySource((), stk.Stakes(), (), PROFILE, PROFILE_OK, None)
    assert pr.profile_findings(policy, plan_chunks(_diff())) == []


def test_the_rule_statements_carry_the_tool_packages_list():
    statements = pr.rule_statements(PROFILE)
    assert statements[: len(rp.CLASS_RULES[rp.CLASS_INTERNAL])] == list(rp.CLASS_RULES[rp.CLASS_INTERNAL])
    assert any("tool_one, tool_two" in item for item in statements)


def test_without_tool_packages_the_statements_are_the_class_rules():
    bare = rp.RepoProfile(repo_class=rp.CLASS_INTERNAL)
    assert pr.rule_statements(bare) == list(rp.CLASS_RULES[rp.CLASS_INTERNAL])
