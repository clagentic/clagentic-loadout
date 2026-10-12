"""Policy is read from the base revision with git, never from the working tree."""

from __future__ import annotations

import textwrap

import pytest

from clagentic_loadout.review import policy_source as ps
from clagentic_loadout.review import stakes as stk

from _review_evaluate_support import commit_files, make_repo, run_git

DISPOSITIONS = textwrap.dedent(
    """\
    dispositions:
      - path: "src/**"
        rule_id: R1
        kind: by_design
        rationale: documented behaviour
        by: a-maintainer
        at: 2026-09-01
    """
)


def test_entries_at_the_base_apply(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.DISPOSITIONS_PATH: DISPOSITIONS, "src/a.py": "x = 1\n"})
    policy = ps.read_policy(repo, base)
    assert [d.rule_id for d in policy.dispositions] == ["R1"]
    assert policy.degraded == ()


def test_an_entry_only_in_the_head_does_not_apply(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    commit_files(repo, {ps.DISPOSITIONS_PATH: DISPOSITIONS})
    assert ps.read_policy(repo, base).dispositions == ()


def test_the_working_tree_is_never_read(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    target = repo / ps.DISPOSITIONS_PATH
    target.parent.mkdir(parents=True)
    target.write_text(DISPOSITIONS, encoding="utf-8")
    assert ps.read_policy(repo, base).dispositions == ()


def test_a_later_edit_to_the_file_does_not_change_the_base_reading(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.DISPOSITIONS_PATH: DISPOSITIONS})
    commit_files(repo, {ps.DISPOSITIONS_PATH: "dispositions: []\n"})
    assert len(ps.read_policy(repo, base).dispositions) == 1


def test_malformed_dispositions_resolve_to_no_policy_and_a_reason(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.DISPOSITIONS_PATH: "dispositions:\n  - path: x\n"})
    policy = ps.read_policy(repo, base)
    assert policy.dispositions == ()
    assert any("dispositions ignored" in reason for reason in policy.degraded)


def test_unparseable_yaml_resolves_to_no_policy_and_a_reason(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.STAKES_PATH: "profiles: [unclosed\n"})
    policy = ps.read_policy(repo, base)
    assert policy.stakes.profile is None
    assert any("stakes profile ignored" in reason for reason in policy.degraded)


def test_non_utf8_file_is_a_reason_not_a_crash(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".clagentic" / "loadout").mkdir(parents=True)
    (repo / ps.DISPOSITIONS_PATH).write_bytes(b"\xff\xfe\x00bad")
    run_git(repo, "add", "--", ps.DISPOSITIONS_PATH)
    run_git(repo, "commit", "-q", "-m", "chore: fixture")
    base = run_git(repo, "rev-parse", "HEAD")
    assert any("UTF-8" in reason for reason in ps.read_policy(repo, base).degraded)


def test_unreadable_base_is_a_reason(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a": "1\n"})
    policy = ps.read_policy(repo, "d" * 40)
    assert policy.dispositions == ()
    assert policy.degraded


def test_a_non_sha_base_is_refused_before_git_runs(tmp_path):
    policy = ps.read_policy(tmp_path / "missing", "--output=x")
    assert "full 40-character" in policy.degraded[0]


def test_absent_policy_files_are_not_a_degradation(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    policy = ps.read_policy(repo, base)
    assert policy.degraded == ()
    assert policy.stakes.profile is None


def test_requested_profile_that_is_not_defined_is_a_reason(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    assert ps.read_policy(repo, base, profile="svc").degraded


def test_profile_and_default_profile_come_from_the_base(tmp_path):
    repo = make_repo(tmp_path)
    doc = textwrap.dedent(
        """\
        profile: svc
        profiles:
          svc:
            exposure: internal
            data_class: confidential
          other:
            data_class: public
        """
    )
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    default = ps.read_policy(repo, base).stakes.for_path("src/a.py")
    chosen = ps.read_policy(repo, base, profile="other").stakes.for_path("src/a.py")
    assert default.data_class == "confidential" and default.exposure == "internal"
    assert chosen.data_class == "public"


def test_profile_claiming_internal_is_overridden_by_a_tree_marker(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n"
    base = commit_files(
        repo,
        {ps.STAKES_PATH: doc, "deploy/edge/ingress.yaml": "kind: Ingress\n", "tools/x.py": "y\n"},
    )
    stakes = ps.read_policy(repo, base).stakes
    assert stakes.for_path("deploy/edge/app.yaml").exposure == "exposed"
    assert stakes.for_path("deploy/edge/app.yaml").exposure_source == stk.SOURCE_TREE
    assert stakes.for_path("tools/x.py").exposure == "internal"


@pytest.mark.parametrize(
    "packaging", ["pyproject.toml", "package.json", "setup.py", "Cargo.toml", "Chart.yaml"]
)
def test_a_packaging_file_is_not_an_exposure_marker(tmp_path, packaging):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n"
    base = commit_files(repo, {packaging: "x\n", "src/a.py": "x\n", ps.STAKES_PATH: doc})
    policy = ps.read_policy(repo, base)
    assert policy.stakes.tree.exposed_dirs == ()
    assert policy.stakes.for_path("src/a.py").exposure == "internal"


def test_public_visibility_exposes_every_path_over_a_profile_claim(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n"
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    stakes = ps.read_policy(repo, base, visibility="public").stakes
    assert stakes.for_path("src/a.py").exposure_source == stk.SOURCE_VISIBILITY
    private = ps.read_policy(repo, base, visibility="private").stakes
    assert private.for_path("src/a.py").exposure == "internal"


def test_codeowners_is_recorded_as_context_and_never_controls_merges(tmp_path):
    repo = make_repo(tmp_path)
    owners = "# owners\n* @org/everyone\n/docs/\n/infra/** @org/infra\n"
    base = commit_files(repo, {".github/CODEOWNERS": owners})
    stakes = ps.read_policy(repo, base).stakes
    infra = stakes.for_path("infra/main.tf")
    assert infra.merge_control == "uncontrolled"
    assert infra.codeowners == stk.CODEOWNERS_COVERED
    assert stakes.for_path("docs/a.md").codeowners == stk.CODEOWNERS_UNCOVERED
    assert stakes.for_path("docs/a.md").merge_control == "uncontrolled"


def test_merge_control_is_controlled_only_by_the_base_profile(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    merge_control: controlled\n"
    base = commit_files(repo, {ps.STAKES_PATH: doc, ".github/CODEOWNERS": "* @org/everyone\n"})
    claimed = ps.read_policy(repo, base).stakes.for_path("src/a.py")
    assert (claimed.merge_control, claimed.merge_control_source) == ("controlled", stk.SOURCE_PROFILE)


def test_a_profile_without_a_merge_control_claim_reports_the_default_as_unstated(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    data_class: internal\n"
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    resolved = ps.read_policy(repo, base).stakes.for_path("src/a.py")
    assert (resolved.merge_control, resolved.merge_control_source) == (
        "uncontrolled",
        stk.SOURCE_UNSTATED,
    )


def test_a_nested_paths_key_in_a_path_entry_makes_the_file_malformed_naming_the_key_path(tmp_path):
    repo = make_repo(tmp_path)
    doc = textwrap.dedent(
        """\
        profile: svc
        profiles:
          svc:
            exposure: internal
            paths:
              "src/**":
                paths:
                  "src/a.py": {exposure: internal}
        """
    )
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    policy = ps.read_policy(repo, base)
    assert policy.stakes.profile is None
    assert any("profiles.svc.paths.src/**.paths" in reason for reason in policy.degraded)


def test_misplaced_and_unknown_keys_in_the_stakes_file_are_malformed(tmp_path):
    cases = (
        ("profiles:\n  svc:\n    owner: x\n", "profiles.svc.owner"),
        ("exposure: internal\n", "exposure"),
        ("profiles:\n  svc:\n    paths:\n      'a/**': {merge_control: maybe}\n", "merge_control"),
    )
    for number, (doc, key_path) in enumerate(cases):
        root = tmp_path / str(number)
        root.mkdir()
        repo = make_repo(root)
        base = commit_files(repo, {ps.STAKES_PATH: doc})
        reasons = ps.read_policy(repo, base).degraded
        assert reasons and all("stakes profile ignored" in r for r in reasons)
        assert key_path in reasons[0]


def test_a_nested_key_in_a_dispositions_entry_is_malformed_naming_the_key_path(tmp_path):
    repo = make_repo(tmp_path)
    doc = DISPOSITIONS + "    scope:\n      extra: 1\n"
    base = commit_files(repo, {ps.DISPOSITIONS_PATH: doc})
    policy = ps.read_policy(repo, base)
    assert policy.dispositions == ()
    assert any("dispositions[1].scope" in reason for reason in policy.degraded)


@pytest.mark.parametrize(
    "line, covered",
    [
        ("* @org/team", True),
        ("* @someone", True),
        ("* dev@example.org", True),
        ("* someone", False),
        ("* team/name", False),
        ("* @", False),
        ("*", False),
    ],
)
def test_only_a_host_principal_counts_as_a_codeowners_owner(line, covered):
    (rule,) = stk.parse_codeowners(line + "\n")
    assert rule.has_owner is covered


def test_no_codeowners_and_no_claim_is_uncontrolled(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x\n"})
    resolved = ps.read_policy(repo, base).stakes.for_path("src/a.py")
    assert resolved.merge_control == "uncontrolled"
    assert resolved.exposure == "exposed"
    assert resolved.data_class == "regulated"
