"""Policy is read from the base revision with git, never from the working tree."""

from __future__ import annotations

import datetime as dt
import textwrap

import pytest

from clagentic_loadout.review import policy_source as ps
from clagentic_loadout.review import stakes as stk

from _review_evaluate_support import commit_files, make_repo, run_git

NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)


def posture(drop: tuple[str, ...] = (), **overrides: str) -> str:
    """A profile whose posture claims an internal, reviewed, private repository
    confirmed on NOW's date."""
    values = {
        "exposure": "internal_authenticated",
        "data": "internal",
        "visibility": "private",
        "merge_control": "review_required",
        "confirmed_at": NOW.date().isoformat(),
    }
    values.update(overrides)
    return "class: internal\n" + "".join(f"{k}: {v}\n" for k, v in values.items() if k not in drop)


def _worst(policy: ps.PolicySource) -> bool:
    resolved = policy.stakes.for_path("src/a.py")
    return (resolved.exposure, resolved.data_class, resolved.merge_control) == (
        "exposed",
        "regulated",
        "uncontrolled",
    )

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


def test_an_unparseable_dispositions_file_resolves_to_no_policy_and_a_reason(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.DISPOSITIONS_PATH: "dispositions: [unclosed\n"})
    policy = ps.read_policy(repo, base)
    assert policy.dispositions == ()
    assert any("dispositions ignored" in reason for reason in policy.degraded)


def test_an_unparseable_profile_is_state_and_worst_case_stakes_never_a_degraded_reason(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: "class: [unclosed\n"})
    policy = ps.read_policy(repo, base, now=NOW)
    assert policy.profile_state == ps.PROFILE_INVALID
    assert policy.degraded == ()
    assert _worst(policy)
    assert [item.key for item in policy.completeness] == ["profile"]


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
    policy = ps.read_policy(repo, base, now=NOW)
    assert policy.degraded == ()
    assert _worst(policy)


def test_posture_comes_from_the_profile_at_the_base(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(data="regulated_or_customer")})
    resolved = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert (resolved.exposure, resolved.data_class, resolved.merge_control) == (
        "internal",
        "regulated",
        "controlled",
    )
    assert resolved.data_class_source == stk.SOURCE_PROFILE


@pytest.mark.parametrize(
    "exposure, data, control, expected",
    [
        ("internet", "regulated_or_customer", "unrestricted", ("exposed", "regulated", "uncontrolled")),
        ("internal_authenticated", "internal", "review_required", ("internal", "internal", "controlled")),
        ("local_or_ci_only", "public_or_none", "code_owner_review_required", ("internal", "public", "controlled")),
    ],
)
def test_each_posture_value_maps_onto_the_evaluator_vocabulary(
    tmp_path, exposure, data, control, expected
):
    repo = make_repo(tmp_path)
    doc = posture(exposure=exposure, data=data, merge_control=control)
    base = commit_files(repo, {ps.PROFILE_PATH: doc})
    resolved = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert (resolved.exposure, resolved.data_class, resolved.merge_control) == expected


def test_a_head_only_posture_edit_is_not_applied_the_base_is_read(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(exposure="internal_authenticated")})
    commit_files(repo, {ps.PROFILE_PATH: posture(exposure="local_or_ci_only", data="public_or_none")})
    resolved = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert resolved.data_class == "internal"


def test_a_posture_first_introduced_in_the_head_does_not_apply(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x\n"})
    commit_files(repo, {ps.PROFILE_PATH: posture()})
    assert _worst(ps.read_policy(repo, base, now=NOW))


def test_a_retired_stakes_file_is_ignored_reported_and_the_stakes_are_the_worst_case(tmp_path):
    repo = make_repo(tmp_path)
    doc = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n    data_class: internal\n    merge_control: controlled\n"
    base = commit_files(repo, {ps.STAKES_PATH: doc})
    policy = ps.read_policy(repo, base, now=NOW)
    assert policy.degraded == ()
    assert _worst(policy)
    retired = [item for item in policy.completeness if item.key == "stakes.yaml"]
    assert len(retired) == 1
    assert "retired" in retired[0].reason
    assert "profile.yaml" in retired[0].fix


def test_a_retired_stakes_file_does_not_weaken_a_profile_posture(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo,
        {ps.PROFILE_PATH: posture(exposure="internet"), ps.STAKES_PATH: "profile: x\nprofiles:\n  x:\n    exposure: internal\n"},
    )
    resolved = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert resolved.exposure == "exposed"


def test_profile_claiming_internal_is_overridden_by_a_tree_marker(tmp_path):
    repo = make_repo(tmp_path)
    doc = posture(exposure="local_or_ci_only")
    base = commit_files(
        repo,
        {ps.PROFILE_PATH: doc, "deploy/edge/ingress.yaml": "kind: Ingress\n", "tools/x.py": "y\n"},
    )
    stakes = ps.read_policy(repo, base, now=NOW).stakes
    assert stakes.for_path("deploy/edge/app.yaml").exposure == "exposed"
    assert stakes.for_path("deploy/edge/app.yaml").exposure_source == stk.SOURCE_TREE
    assert stakes.for_path("tools/x.py").exposure == "internal"


@pytest.mark.parametrize(
    "packaging", ["pyproject.toml", "package.json", "setup.py", "Cargo.toml", "Chart.yaml"]
)
def test_a_packaging_file_is_not_an_exposure_marker(tmp_path, packaging):
    repo = make_repo(tmp_path)
    doc = posture(exposure="local_or_ci_only")
    base = commit_files(repo, {packaging: "x\n", "src/a.py": "x\n", ps.PROFILE_PATH: doc})
    policy = ps.read_policy(repo, base, now=NOW)
    assert policy.stakes.tree.exposed_dirs == ()
    assert policy.stakes.for_path("src/a.py").exposure == "internal"


def test_public_visibility_exposes_every_path_over_a_profile_claim(tmp_path):
    repo = make_repo(tmp_path)
    doc = posture(exposure="local_or_ci_only")
    base = commit_files(repo, {ps.PROFILE_PATH: doc})
    stakes = ps.read_policy(repo, base, visibility="public", now=NOW).stakes
    assert stakes.for_path("src/a.py").exposure_source == stk.SOURCE_VISIBILITY
    private = ps.read_policy(repo, base, visibility="private", now=NOW).stakes
    assert private.for_path("src/a.py").exposure == "internal"


def test_a_declared_public_visibility_raises_the_exposure_and_a_disagreement_is_an_item(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(visibility="public")})
    policy = ps.read_policy(repo, base, visibility="private", now=NOW)
    assert policy.stakes.for_path("src/a.py").exposure_source == stk.SOURCE_VISIBILITY
    assert [item.key for item in policy.completeness] == ["visibility"]


def test_a_declared_private_never_lowers_what_the_host_reports(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(visibility="private")})
    policy = ps.read_policy(repo, base, visibility="public", now=NOW)
    assert policy.stakes.for_path("src/a.py").exposure_source == stk.SOURCE_VISIBILITY
    assert [item.key for item in policy.completeness] == ["visibility"]


def test_a_declared_visibility_that_matches_the_host_is_complete(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(visibility="private")})
    assert ps.read_policy(repo, base, visibility="private", now=NOW).completeness == ()


def test_codeowners_is_recorded_as_context_and_never_controls_merges(tmp_path):
    repo = make_repo(tmp_path)
    owners = "# owners\n* @org/everyone\n/docs/\n/infra/** @org/infra\n"
    base = commit_files(repo, {".github/CODEOWNERS": owners})
    stakes = ps.read_policy(repo, base, now=NOW).stakes
    infra = stakes.for_path("infra/main.tf")
    assert infra.merge_control == "uncontrolled"
    assert infra.codeowners == stk.CODEOWNERS_COVERED
    assert stakes.for_path("docs/a.md").codeowners == stk.CODEOWNERS_UNCOVERED
    assert stakes.for_path("docs/a.md").merge_control == "uncontrolled"


def test_merge_control_is_controlled_only_by_the_base_profile(tmp_path):
    repo = make_repo(tmp_path)
    doc = posture(merge_control="code_owner_review_required")
    base = commit_files(repo, {ps.PROFILE_PATH: doc, ".github/CODEOWNERS": "* @org/everyone\n"})
    claimed = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert (claimed.merge_control, claimed.merge_control_source) == ("controlled", stk.SOURCE_PROFILE)


def test_a_profile_without_a_merge_control_claim_reports_the_default_as_unstated(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(drop=("merge_control",))})
    policy = ps.read_policy(repo, base, now=NOW)
    resolved = policy.stakes.for_path("src/a.py")
    assert (resolved.merge_control, resolved.merge_control_source) == (
        "uncontrolled",
        stk.SOURCE_UNSTATED,
    )
    assert [item.key for item in policy.completeness] == ["merge_control"]


def test_an_unrecognised_posture_value_is_worst_case_and_an_item_not_an_invalid_profile(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(exposure="sealed")})
    policy = ps.read_policy(repo, base, now=NOW)
    assert policy.profile_state == ps.PROFILE_OK
    assert policy.degraded == ()
    assert policy.stakes.for_path("src/a.py").exposure == "exposed"
    (item,) = policy.completeness
    assert item.key == "exposure" and "unrecognised" in item.reason
    assert "sealed" not in item.render()


@pytest.mark.parametrize(
    "confirmed, fragment",
    [
        ("2026-04-24", "expired"),
        ("2027-01-01", "future"),
        ("yesterday", "not a date"),
    ],
)
def test_a_posture_confirmed_wrongly_is_the_worst_case_and_the_item_names_why(
    tmp_path, confirmed, fragment
):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: posture(confirmed_at=confirmed)})
    policy = ps.read_policy(repo, base, now=NOW)
    assert _worst(policy)
    (item,) = policy.completeness
    assert item.key == "confirmed_at" and fragment in item.reason


def test_the_confirmation_is_good_through_its_180th_day_and_expired_after(tmp_path):
    repo = make_repo(tmp_path)
    last_good = (NOW - dt.timedelta(days=180)).date().isoformat()
    first_bad = (NOW - dt.timedelta(days=181)).date().isoformat()
    base = commit_files(repo, {ps.PROFILE_PATH: posture(confirmed_at=last_good)})
    assert ps.read_policy(repo, base, now=NOW).completeness == ()
    base = commit_files(repo, {ps.PROFILE_PATH: posture(confirmed_at=first_bad)})
    assert [i.key for i in ps.read_policy(repo, base, now=NOW).completeness] == ["confirmed_at"]


def test_a_repo_with_no_posture_keys_lists_each_missing_key_in_a_fixed_order(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {ps.PROFILE_PATH: "class: internal\n"})
    policy = ps.read_policy(repo, base, now=NOW)
    assert [item.key for item in policy.completeness] == [
        "exposure",
        "data",
        "visibility",
        "merge_control",
        "confirmed_at",
    ]
    assert _worst(policy)
    assert policy.degraded == ()


def test_a_missing_profile_is_a_file_item_then_every_key(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x\n"})
    keys = [item.key for item in ps.read_policy(repo, base, now=NOW).completeness]
    assert keys[0] == "profile" and len(keys) == 6


def test_every_item_has_a_fix(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x\n", ps.STAKES_PATH: "profiles: {}\n"})
    for item in ps.read_policy(repo, base, now=NOW).completeness:
        assert item.fix and item.reason


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
    resolved = ps.read_policy(repo, base, now=NOW).stakes.for_path("src/a.py")
    assert resolved.merge_control == "uncontrolled"
    assert resolved.exposure == "exposed"
    assert resolved.data_class == "regulated"
