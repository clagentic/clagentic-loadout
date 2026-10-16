"""The internal-class import rule inspects every name an import statement brings in,
not only the first, and reaches a statement that follows a semicolon."""

from __future__ import annotations

import pytest

from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.policy_source import PROFILE_OK, PolicySource

PROFILE = rp.RepoProfile(repo_class=rp.CLASS_INTERNAL, tool_packages=("tool_one", "tool_two"))


def _diff(lines: list[str]) -> str:
    return "\n".join(
        [
            "diff --git a/x/a.py b/x/a.py",
            "new file mode 100644",
            "--- /dev/null",
            "+++ b/x/a.py",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    ) + "\n"


def _found(*lines: str) -> list[dict]:
    policy = PolicySource((), stk.Stakes(), (), PROFILE, PROFILE_OK, None)
    return pr.profile_findings(policy, plan_chunks(_diff(list(lines))))


@pytest.mark.parametrize(
    "line, modules",
    [
        ("import os, tool_one.guard", ["guard"]),
        ("import tool_one.guard, os", ["guard"]),
        ("import os,tool_one.guard", ["guard"]),
        ("import os, sys, tool_one.guard as g", ["guard"]),
        ("import tool_one.a, tool_one.b", ["a", "b"]),
        ("import tool_one.a, tool_two.b", ["a", "b"]),
        ("import tool_one.guard as g, os", ["guard"]),
        ("x = 1; import tool_one.guard", ["guard"]),
        ("import os; from tool_two.deep.mod import y", ["deep.mod"]),
        ("import os, tool_one.guard  # keep", ["guard"]),
    ],
)
def test_every_imported_name_is_inspected(line, modules):
    found = _found(line)

    assert [f["rule_id"] for f in found] == [pr.RULE_TOOL_INTERNALS] * len(modules)
    for finding, module in zip(found, modules):
        assert repr(module) in finding["message"]


@pytest.mark.parametrize(
    "line",
    [
        "import os, tool_one",
        "import tool_one as t, os",
        "import os, tool_oneextra.guard",
        "import os  # tool_one.guard",
        "from tool_one import guard, other",
        "from tool_one import (guard,",
        "x = 'import os, tool_one.guard'",
    ],
)
def test_top_level_imports_and_look_alikes_are_not_reported(line):
    assert _found(line) == []
