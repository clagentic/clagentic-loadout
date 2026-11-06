"""The shared test-file classifier, the profile's test.paths key, the builder
brief's guidance block and the reviewer prompt's test text."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.guidance_section import TEST_RULE, guidance_text
from clagentic_loadout.repo_test.spec import TestBlockError, parse_test_block
from clagentic_loadout.review.chunk_review import build_prompt
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.findings_contract import OUTPUT_CONTRACT
from clagentic_loadout.review.profile_role_brief import GATE_READ_NOTE, role_brief
from clagentic_loadout.review.repo_profile import RepoProfile
from clagentic_loadout.review.test_files import is_test_file
from clagentic_loadout.role_vocabulary import CLASS_BUILD, CLASS_GATE, RoleResolution
from tests._review_cli_support import make_diff

# path, extra globs, is a test
CLASSIFY_ROWS = [
    ("tests/a.py", (), True),
    ("pkg/test/a.go", (), True),
    ("web/__tests__/a.js", (), True),
    ("pkg/test_a.py", (), True),
    ("pkg/a_test.go", (), True),
    ("web/a.test.ts", (), True),
    ("web/a.spec.ts", (), True),
    ("pkg/conftest.py", (), True),
    ("./tests/a.py", (), True),
    ("src/a.py", (), False),
    ("src/contest.py", (), False),
    ("tests/README.md", (), False),
    ("docs/tests/a.py", (), False),
    ("tests/notes.txt", (), False),
    ("spec/x.rb", (), False),
    ("spec/x.rb", ("spec/**",), True),
    ("docs/x.md", ("spec/**",), False),
    ("docs/x.md", ("docs/**",), False),
    ("src/a.py", ("spec/**",), False),
]


@pytest.mark.parametrize("path,extra,expected", CLASSIFY_ROWS)
def test_classification(path, extra, expected):
    assert is_test_file(path, extra) is expected


def test_test_paths_parse_and_bad_values_are_refused_naming_the_key():
    assert parse_test_block({"paths": ["spec/**", "spec/**"]}).paths == ("spec/**",)
    assert parse_test_block({"paths": ["spec/**"]}).declared_keys() == ["paths"]
    for bad in ("spec/**", [3], [""], ["a\nb"]):
        with pytest.raises(TestBlockError, match=r"test\.paths"):
            parse_test_block({"paths": bad})


def _resolution(role_class):
    return RoleResolution(role="r", word="r", role_class=role_class, source="test")


def test_builder_brief_and_reviewer_prompt_read_one_rule():
    brief = role_brief(RepoProfile(repo_class="internal"), _resolution(CLASS_BUILD))
    chunk = plan_chunks(make_diff({"a.py": 3}), 600)[0]
    prompt = build_prompt(chunk, 1, "rule text", class_rules="## class rules\nx")

    assert brief["guidance"] == {"tests": TEST_RULE, "classes": guidance_text(repeated=False)}
    assert TEST_RULE in prompt
    assert prompt.index("## class rules") < prompt.index(TEST_RULE) < prompt.index("## Diff chunk")
    assert prompt.endswith(OUTPUT_CONTRACT)


def test_the_gate_brief_is_unchanged():
    brief = role_brief(None, _resolution(CLASS_GATE))

    assert brief == {
        "role": "r", "role_word": "r", "role_class": CLASS_GATE, "role_source": "test",
        "shaped_for": "gate", "gate_read": GATE_READ_NOTE,
    }
