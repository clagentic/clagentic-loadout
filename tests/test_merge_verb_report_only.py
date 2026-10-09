"""test_merge_verb_report_only.py — the merge verb's --report-only mode.

--report-only runs the whole gate chain as a real merge would and stops before
the merge step. These tests prove, with a recording opener and a real git
repo:

  - on a mergeable PR it makes no merge API call, posts no comment, and runs
    no post_merge_steps entry (while the same repo without the flag does run
    the step and merge, so the repo shape really would have triggered them)
  - on a refusing PR it returns exactly the exit code a real run returns
  - pre_checks still execute and still refuse
"""

from __future__ import annotations

import sys

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.transport import provider_config
from tests._gate_repo import git, init_gate_repo
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

_PY = sys.executable


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Keep the user-level config lookup off the real home directory."""
    monkeypatch.setattr(
        provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "isolated-user-config-root"
    )


def _recording(opener):
    """Wrap *opener* and record (method, path-suffix) for every call."""
    calls: list[tuple[str, str]] = []

    def wrapper(req, timeout=15):
        calls.append((req.get_method(), req.full_url))
        return opener(req, timeout=timeout)

    return wrapper, calls


def _writes(calls):
    return [(m, u) for m, u in calls if m != "GET"]


def _marker_step(marker) -> dict:
    code = f"open({str(marker)!r}, 'w').close()"
    return {"cmd": [_PY, "-c", code], "on_failure": "fail"}


def _repo_with_post_merge_step(tmp_path, marker):
    """A gate repo with a real origin and tree sync on, so a real merge would
    fetch the merged commit and run the configured post_merge_step."""
    repo = init_gate_repo(
        tmp_path / "repo",
        tracked_gate={},
        deployment_merge={
            "sync_tree_after_merge": True,
            "post_merge_steps": [_marker_step(marker)],
        },
    )
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    git(repo.path, "remote", "add", "origin", str(origin))
    git(repo.path, "push", "-q", "origin", "main")
    return repo


def _run(argv, opener):
    return verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )


class TestReportOnlyMergeable:
    def test_no_merge_call_no_comment_no_post_merge_steps(self, tmp_path):
        marker = tmp_path / "post-merge-ran"
        repo = _repo_with_post_merge_step(tmp_path, marker)
        opener, calls = _recording(make_opener(pr_info=repo.pr_info()))
        argv = base_args(**{"--repo-path": repo.path}) + ["--report-only"]

        assert _run(argv, opener) == verb.EXIT_OK

        assert _writes(calls) == []
        assert not marker.exists()

    def test_control_without_flag_merges_posts_and_runs_steps(self, tmp_path):
        marker = tmp_path / "post-merge-ran"
        repo = _repo_with_post_merge_step(tmp_path, marker)
        opener, calls = _recording(make_opener(pr_info=repo.pr_info()))
        argv = base_args(**{"--repo-path": repo.path})

        assert _run(argv, opener) == verb.EXIT_OK

        assert any(m == "POST" and u.endswith("/merge") for m, u in calls)
        assert any(m == "POST" and "/comments" in u for m, u in calls)
        assert marker.exists()

    def test_prints_the_verdict_it_would_reach(self, capsys):
        argv = base_args() + ["--report-only"]
        assert _run(argv, make_opener()) == verb.EXIT_OK
        out = capsys.readouterr().out
        assert "report-only" in out
        assert "would be merged" in out
        assert "PR #1 in some-owner/some-repo merged" not in out


class TestReportOnlyRefusals:
    @pytest.mark.parametrize(
        "pr_title,expected",
        [("not conventional", verb.EXIT_PR_TITLE_INVALID)],
    )
    def test_title_refusal_matches_real_run(self, pr_title, expected):
        pr_info = {"head": {"sha": "a" * 40}, "title": pr_title}
        real = _run(base_args(), make_opener(pr_info=pr_info))
        report = _run(base_args() + ["--report-only"], make_opener(pr_info=pr_info))
        assert real == expected
        assert report == real

    def test_stale_sha_refusal_matches_real_run(self):
        argv = base_args(**{"--expected-head-sha": "b" * 40})
        real = _run(argv, make_opener())
        report = _run(argv + ["--report-only"], make_opener())
        assert real == verb.EXIT_STALE_HEAD_SHA
        assert report == real

    def test_missing_verdict_refusal_matches_real_run(self):
        argv = base_args(**{"--required-reviewer": "some-reviewer:reviewer-login"})
        real = _run(argv, make_opener())
        report = _run(argv + ["--report-only"], make_opener())
        assert real == verb.EXIT_GATE_RESULT_BLOCKED
        assert report == real

    def test_authority_refusal_happens_before_any_credential(self):
        argv = [
            "--platform", "forgejo", "--role", "merger", "--repo", "some-owner/some-repo",
            "--pr", "1", "--no-post-merge-tree", "--report-only",
        ]
        code = verb.main(argv, token_provider=RecordingTokenProvider())
        assert code == verb.EXIT_AUTHORITY_DENIED

    def test_failing_pre_check_still_refuses_and_makes_no_write(self, tmp_path):
        failing = {"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"pre_checks": [failing]})
        opener, calls = _recording(make_opener(pr_info=repo.pr_info()))
        argv = base_args(**{"--repo-path": repo.path}) + ["--report-only"]

        assert _run(argv, opener) == verb.EXIT_PRE_CHECKS_FAILED
        assert _writes(calls) == []

    def test_passing_pre_check_runs_in_report_only(self, tmp_path):
        marker = tmp_path / "pre-check-ran"
        repo = init_gate_repo(
            tmp_path / "repo", tracked_gate={"pre_checks": [_marker_step(marker)]}
        )
        opener, calls = _recording(make_opener(pr_info=repo.pr_info()))
        argv = base_args(**{"--repo-path": repo.path}) + ["--report-only"]

        assert _run(argv, opener) == verb.EXIT_OK
        assert marker.exists()
        assert _writes(calls) == []
