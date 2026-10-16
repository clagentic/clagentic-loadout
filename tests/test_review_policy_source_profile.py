"""The repo profile is read from the base revision, like the other policy files,
and a problem with it is state, never a degraded input."""

from __future__ import annotations

from clagentic_loadout.review import policy_source as ps
from clagentic_loadout.review import repo_profile as rp
from tests._review_evaluate_support import commit_files, make_repo, run_git


def test_a_profile_at_the_base_is_read(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo,
        {ps.PROFILE_PATH: "class: released-tool\npaths:\n  'x/**': {class: internal}\n"},
    )

    policy = ps.read_policy(repo, base)

    assert policy.profile_state == ps.PROFILE_OK
    assert policy.repo_profile.repo_class == rp.CLASS_RELEASED_TOOL
    assert policy.repo_profile.class_for("x/a") == rp.CLASS_INTERNAL
    assert policy.degraded == () and policy.profile_problem is None


def test_no_profile_is_missing_and_is_not_a_degraded_input(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})

    policy = ps.read_policy(repo, base)

    assert (policy.profile_state, policy.repo_profile) == (ps.PROFILE_MISSING, None)
    assert policy.degraded == ()


def test_a_profile_only_in_the_head_does_not_apply(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    commit_files(repo, {ps.PROFILE_PATH: "class: internal\n"})

    assert ps.read_policy(repo, base).profile_state == ps.PROFILE_MISSING


def test_the_working_tree_is_never_read(tmp_path):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"src/a.py": "x = 1\n"})
    (repo / ps.PROFILE_PATH).parent.mkdir(parents=True)
    (repo / ps.PROFILE_PATH).write_text("class: internal\n", encoding="utf-8")

    assert ps.read_policy(repo, base).profile_state == ps.PROFILE_MISSING


def test_an_invalid_profile_is_state_with_the_problem_and_no_degraded_reason(tmp_path):
    for number, (text, key) in enumerate(
        (
            ("class: shipped\n", "class must be one of"),
            ("class: internal\nowner: x\n", "owner"),
            ("class: [unclosed\n", "not valid YAML"),
            ("", "must be a mapping"),
        )
    ):
        root = tmp_path / str(number)
        root.mkdir()
        repo = make_repo(root)
        base = commit_files(repo, {ps.PROFILE_PATH: text})

        policy = ps.read_policy(repo, base)

        assert policy.profile_state == ps.PROFILE_INVALID, text
        assert policy.repo_profile is None
        assert key in policy.profile_problem
        assert policy.degraded == ()


def test_a_non_utf8_profile_is_invalid_not_a_crash(tmp_path):
    repo = make_repo(tmp_path)
    (repo / ".clagentic" / "loadout").mkdir(parents=True)
    (repo / ps.PROFILE_PATH).write_bytes(b"\xff\xfe\x00bad")
    commit_files(repo, {"a": "1\n"})
    run_git(repo, "add", "--", ps.PROFILE_PATH)
    run_git(repo, "commit", "-q", "-m", "chore: fixture")
    base = run_git(repo, "rev-parse", "HEAD")

    policy = ps.read_policy(repo, base)

    assert policy.profile_state == ps.PROFILE_INVALID and "UTF-8" in policy.profile_problem


def test_an_unreadable_base_leaves_the_profile_unread_and_degrades_for_its_own_reason(tmp_path):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a": "1\n"})

    policy = ps.read_policy(repo, "d" * 40)

    assert policy.profile_state == ps.PROFILE_UNREAD
    assert policy.degraded
