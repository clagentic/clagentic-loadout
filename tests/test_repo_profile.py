"""The repo profile schema: strict parsing, per-path classes, and the draft."""

from __future__ import annotations

import textwrap

import pytest
import yaml

from clagentic_loadout.review import repo_profile as rp


def _parse(text: str) -> rp.RepoProfile:
    return rp.parse_profile_document(yaml.safe_load(textwrap.dedent(text)))


def test_a_class_alone_is_a_profile():
    profile = _parse("class: released-tool\n")
    assert profile.repo_class == rp.CLASS_RELEASED_TOOL
    assert profile.paths == () and profile.deny == rp.ProfileDeny() and profile.tool_packages == ()


def test_every_field_is_read():
    profile = _parse(
        """\
        class: released-tool
        paths:
          "fixtures/**": {class: internal}
        deny:
          names: [agent-alpha]
          hosts: [host-one.example.test]
          users: [someone-local]
        tool_packages: [tool_one]
        """
    )
    assert profile.paths == (("fixtures/**", rp.CLASS_INTERNAL),)
    assert profile.deny.entries() == (
        ("names", "agent-alpha"),
        ("hosts", "host-one.example.test"),
        ("users", "someone-local"),
    )
    assert profile.tool_packages == ("tool_one",)


def test_the_later_matching_override_wins_and_unmatched_paths_keep_the_class():
    profile = _parse(
        """\
        class: released-tool
        paths:
          "tools/**": {class: internal}
          "tools/public/**": {class: released-tool}
        """
    )
    assert profile.class_for("src/a.py") == rp.CLASS_RELEASED_TOOL
    assert profile.class_for("tools/x.py") == rp.CLASS_INTERNAL
    assert profile.class_for("tools/public/x.py") == rp.CLASS_RELEASED_TOOL


@pytest.mark.parametrize(
    "document, key_path",
    [
        ("{}", "class is required"),
        ("class: shipped\n", "class must be one of released-tool, internal"),
        ("class: internal\nowner: x\n", "owner"),
        ("class: internal\npaths: [a]\n", "paths must be a mapping"),
        ("class: internal\npaths:\n  a/**: released-tool\n", "paths.a/** must be a mapping"),
        ("class: internal\npaths:\n  a/**: {}\n", "paths.a/**.class is required"),
        ("class: internal\npaths:\n  a/**: {class: nope}\n", "paths.a/**.class"),
        ("class: internal\npaths:\n  a/**: {class: internal, paths: {}}\n", "paths.a/**.paths"),
        ("class: internal\ndeny: [x]\n", "deny must be a mapping"),
        ("class: internal\ndeny: {colour: [x]}\n", "deny.colour"),
        ("class: internal\ndeny: {names: x}\n", "deny.names must be a list"),
        ("class: internal\ndeny: {names: ['']}\n", "deny.names[0]"),
        ("class: internal\ndeny: {hosts: [3]}\n", "deny.hosts[0]"),
        ("class: internal\ntool_packages: [a.b]\n", "tool_packages[0]"),
        ("- class\n", "must be a mapping"),
        ("null\n", "must be a mapping"),
    ],
)
def test_anything_outside_the_documented_shape_is_refused_naming_the_key_path(document, key_path):
    with pytest.raises(rp.ProfileFormatError) as caught:
        rp.parse_profile_document(yaml.safe_load(document))
    assert key_path in str(caught.value)


def test_a_denial_entry_may_not_span_lines_or_run_long():
    for bad in ("a\nb", "x" * 201):
        with pytest.raises(rp.ProfileFormatError):
            rp.parse_deny({"names": [bad]})


def test_merging_denial_lists_keeps_order_and_drops_duplicates():
    left = rp.ProfileDeny(names=("a", "b"), hosts=("h",))
    right = rp.ProfileDeny(names=("b", "c"), users=("u",))
    assert left.merged(right) == rp.ProfileDeny(names=("a", "b", "c"), hosts=("h",), users=("u",))


def test_every_class_states_its_rules():
    assert set(rp.CLASS_RULES) == set(rp.CLASSES)
    assert all(rp.CLASS_RULES[name] for name in rp.CLASSES)


class TestInference:
    def test_a_root_packaging_manifest_drafts_a_released_tool(self):
        drafted = rp.infer_profile(["pyproject.toml", "src/a.py"])
        assert drafted.profile.repo_class == rp.CLASS_RELEASED_TOOL
        assert any("pyproject.toml" in reason for reason in drafted.reasons)

    def test_a_license_file_alone_drafts_a_released_tool(self):
        assert rp.infer_profile(["LICENSE", "a.sh"]).profile.repo_class == rp.CLASS_RELEASED_TOOL

    def test_a_nested_manifest_is_not_a_root_signal(self):
        assert rp.infer_profile(["sub/package.json"]).profile.repo_class == rp.CLASS_INTERNAL

    def test_no_signal_drafts_internal(self):
        drafted = rp.infer_profile(["scripts/run.sh", "notes.md"])
        assert drafted.profile.repo_class == rp.CLASS_INTERNAL
        assert drafted.profile.paths == ()

    def test_tool_state_directories_are_proposed_as_internal_overrides(self):
        drafted = rp.infer_profile(
            ["package.json", ".github/ci.yml", ".clagentic/x.yaml", ".agentstate/a.yaml", "src/a.js"]
        )
        assert drafted.profile.paths == ((".agentstate/**", rp.CLASS_INTERNAL),)

    def test_an_internal_draft_proposes_no_overrides(self):
        assert rp.infer_profile([".agentstate/a.yaml"]).profile.paths == ()

    @pytest.mark.parametrize(
        "listing",
        [["pyproject.toml"], ["notes.md"], ["package.json", ".agentstate/a", ".hidden/b"]],
    )
    def test_a_draft_is_valid_by_the_same_parser_the_review_uses(self, listing):
        drafted = rp.infer_profile(listing)
        text = rp.render_profile_yaml(drafted)
        assert text.startswith("# DRAFT")
        assert rp.parse_profile_document(yaml.safe_load(text)) == drafted.profile

    def test_inference_does_not_depend_on_input_order(self):
        listing = ["z.py", "package.json", ".b/x", ".a/y"]
        assert rp.infer_profile(listing) == rp.infer_profile(list(reversed(listing)))
