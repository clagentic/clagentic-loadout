"""Policy is read from the base revision with git, never from the working tree."""

from __future__ import annotations

import textwrap

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


def test_a_root_packaging_file_exposes_the_whole_repository(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"pyproject.toml": "[project]\n", "src/a.py": "x\n"})
    assert ps.read_policy(repo, base).stakes.for_path("src/a.py").exposure == "exposed"


def test_public_visibility_exposes_every_path_over_a_profile_claim(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n"
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    stakes = ps.read_policy(repo, base, visibility="public").stakes
    assert stakes.for_path("src/a.py").exposure_source == stk.SOURCE_VISIBILITY
    private = ps.read_policy(repo, base, visibility="private").stakes
    assert private.for_path("src/a.py").exposure == "internal"


def test_codeowners_coverage_sets_merge_control(tmp_path):
    repo = make_repo(tmp_path)
    owners = "# owners\n* @org/everyone\n/docs/\n/infra/** @org/infra\n"
    base = commit_files(repo, {".github/CODEOWNERS": owners})
    stakes = ps.read_policy(repo, base).stakes
    assert stakes.for_path("infra/main.tf").merge_control == "controlled"
    assert stakes.for_path("src/a.py").merge_control == "controlled"
    assert stakes.for_path("docs/a.md").merge_control == "uncontrolled"


def test_no_codeowners_and_no_claim_is_uncontrolled(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x\n"})
    resolved = ps.read_policy(repo, base).stakes.for_path("src/a.py")
    assert resolved.merge_control == "uncontrolled"
    assert resolved.exposure == "exposed"
    assert resolved.data_class == "regulated"
