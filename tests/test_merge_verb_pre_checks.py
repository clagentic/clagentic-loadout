"""test_merge_verb_pre_checks.py — merge.verb pre_checks gate tests.

The gate declares its checks in the TRACKED gate file read at the PR's BASE
commit; they execute in --repo-path as they always have:

  - a declared `on_failure: fail` pre_check that exits non-zero BLOCKS the
    merge through the REAL runner (a real subprocess, never a mock) --
    EXIT_PRE_CHECKS_FAILED, and the merge_pr call is never reached
  - a passing check lets the merge proceed; `warn` never blocks
  - pre_checks run BEFORE step 9 (the merge call) -- never after
  - `--skip-pre-checks` is an explicit, logged bypass
  - a working tree holding a relaxed gate cannot relax what base declares
  - a malformed or unreadable pre_checks declaration at base REFUSES the
    merge, as does a repo path with no git tree to read it from (a malformed
    `merge.git_working_tree`, or no git tree at all); only reviewer roles and
    scanners fall back, with a warning
  - absent --repo-path (--no-post-merge-tree) declares nothing

No real network call: the opener is a canned-response double.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from tests._gate_repo import GateRepo, commit_raw_gate_to_base, git, init_gate_repo, write_deployment_config

_PY = sys.executable
_FULL_SHA = "a" * 40
_FAIL_CHECK = {"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}


def _exists_check(name: str, *, must_exist: bool = True) -> dict:
    code = f"import os, sys; sys.exit(0 if os.path.exists({name!r}) == {must_exist!r} else 1)"
    return {"cmd": [_PY, "-c", code], "on_failure": "fail"}


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _make_opener(*, pr_info=None, merge_calls=None):
    """merge_calls, when given, is a mutable list this opener appends to
    every time the merge POST fires -- proving (or disproving) that step 9
    was ever reached, independent of the process exit code."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": _FULL_SHA},
        "title": "feat: a change",
        "base": {"ref": "main"},
    }
    posted_comments: list[dict] = []
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_calls is not None:
                merge_calls.append(url)
            _merge_landed[0] = True
            return _FakeResponse(200, b"{}")
        if method == "POST" and "/comments" in url:
            posted_body = json.loads(req.data.decode("utf-8"))["body"]
            posted_comments.append(
                {
                    "id": 9001 + len(posted_comments),
                    "user": {"login": "loadout-merger"},
                    "body": posted_body,
                    "html_url": "https://forgejo.example/comment/9001",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": "a.py"}])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, posted_comments)
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/actions/tasks"):
            return _json_resp(200, {"total_count": 0})
        if method == "GET" and "/compare/" in url:
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _base_args(**overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--role": "merger",
        "--authorized-role": "merger",
        "--repo": "some-owner/some-repo",
        "--pr": "1",
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    return argv


def _merge(repo, *, extra_args=None, merge_calls=None, pr_info=None):
    argv = _base_args(**{"--repo-path": str(repo.path)})
    argv += ["--skip-post-merge", *(extra_args or [])]
    return verb.main(
        argv,
        token_provider=_RecordingTokenProvider(),
        authority_provider=_AllowingAuthorityProvider(),
        opener=_make_opener(
            pr_info=pr_info if pr_info is not None else repo.pr_info(), merge_calls=merge_calls
        ),
    )


def _repo_with(tmp_path, steps, **kwargs):
    # A `merge:` section must say whether reviewers are required, so the
    # pre_checks-only fixtures opt out explicitly.
    gate = {"required_reviewer_roles": [], "pre_checks": steps}
    return init_gate_repo(tmp_path, tracked_gate=gate, **kwargs)


class TestPreChecksBlockTheMergeThroughTheRealRunner:
    """A mocked runner here would pass while the defect survives -- these
    tests exercise the REAL merge.post_merge.run_post_merge_steps executor,
    a real subprocess, never a stand-in."""

    def test_failing_fail_gated_check_blocks_merge_and_merge_pr_never_called(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == [], (
            "merge_pr (step 9) must never be reached when a fail-gated "
            "pre_check exits non-zero -- the merge must be refused BEFORE "
            "the merge call, not merely reported as failed after landing."
        )

    def test_passing_fail_gated_check_lets_merge_proceed(self, tmp_path):
        marker = tmp_path.parent / f"{tmp_path.name}-checked.txt"
        repo = _repo_with(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"], "on_failure": "fail"}],
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_OK
        assert marker.exists()
        assert len(merge_calls) == 1

    def test_warn_gated_failing_check_does_not_block_merge(self, tmp_path):
        repo = _repo_with(tmp_path, [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "warn"}])
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_default_on_failure_omitted_does_not_block_merge(self, tmp_path):
        repo = _repo_with(tmp_path, [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"]}])
        assert _merge(repo) == verb.EXIT_OK


class TestPreChecksRunBeforeTheMergeCall:
    def test_check_output_marker_exists_before_merge_post_fires(self, tmp_path):
        # If pre_checks ran AFTER merge_pr (wrong ordering), the marker would
        # not exist yet at merge-POST time.
        marker = tmp_path.parent / f"{tmp_path.name}-ran-first.txt"
        repo = _repo_with(tmp_path, [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}])
        ordering_violations: list[str] = []
        underlying_opener = _make_opener(pr_info=repo.pr_info())

        def _ordering_asserting_opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/merge"):
                if not marker.exists():
                    ordering_violations.append("merge POST fired before pre_check ran")
            return underlying_opener(req, timeout=timeout)

        argv = _base_args(**{"--repo-path": str(repo.path)}) + ["--skip-post-merge"]
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_ordering_asserting_opener,
        )
        assert code == verb.EXIT_OK
        assert ordering_violations == []


class TestSkipPreChecks:
    def test_skip_flag_bypasses_configured_fail_gated_check(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        assert _merge(repo, extra_args=["--skip-pre-checks"]) == verb.EXIT_OK

    def test_skip_flag_logs_to_stderr(self, tmp_path, capsys):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        _merge(repo, extra_args=["--skip-pre-checks"])
        assert "pre_checks gate BYPASSED" in capsys.readouterr().err


class TestChecksRunInTheRepoPath:
    def test_a_check_runs_in_the_repo_path_tree(self, tmp_path):
        repo = _repo_with(tmp_path, [_exists_check("README.md")])
        assert _merge(repo) == verb.EXIT_OK

    def test_no_worktree_is_created(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        assert git(tmp_path, "worktree", "list").count("\n") == 0


class TestTheGateIsReadFromBaseNotTheWorkingTree:
    def test_a_relaxed_working_tree_cannot_relax_what_base_declares(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        # The PR head's tree (or a stale checkout) holds a gate with no checks.
        (tmp_path / TRACKED_GATE_RELATIVE_PATH).write_text(
            yaml.safe_dump({"merge": {"required_reviewer_roles": []}}), encoding="utf-8"
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []

    def test_a_pr_that_deletes_the_gate_is_still_judged_by_base(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        git(tmp_path, "checkout", "-q", "pr")
        git(tmp_path, "rm", "-q", "--", TRACKED_GATE_RELATIVE_PATH)
        git(tmp_path, "commit", "-q", "-m", "drop the gate")
        repo.head_sha = git(tmp_path, "rev-parse", "HEAD")
        git(tmp_path, "checkout", "-q", "main")
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED

    def test_a_gate_key_in_the_deployment_file_is_ignored_with_a_warning(self, tmp_path, capsys):
        repo = init_gate_repo(tmp_path, tracked_gate=None)
        write_deployment_config(
            tmp_path, {"sync_tree_after_merge": False, "pre_checks": [_FAIL_CHECK]}
        )
        assert git(tmp_path, "status", "--porcelain") == "", "the deployment file is gitignored"
        assert _merge(repo) == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "merge.pre_checks in" in err and "IGNORED" in err
        assert TRACKED_GATE_RELATIVE_PATH in err

    def test_no_tracked_gate_at_base_declares_nothing_and_does_not_warn(self, tmp_path, capsys):
        repo = init_gate_repo(tmp_path, tracked_gate=None)
        assert _merge(repo) == verb.EXIT_OK
        assert "NOT ENFORCED" not in capsys.readouterr().err


class TestMalformedGateAtBase:
    """pre_checks keep their pre-existing failure rule: a declaration that
    cannot be read or validated REFUSES the merge. Only the reviewer-roles and
    scanners pair falls back."""

    def test_pre_checks_not_a_list_refuses_before_merge_pr_called(self, tmp_path):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": [], "pre_checks": "not-a-list"}
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []

    def test_invalid_step_shape_refuses_at_load_time(self, tmp_path):
        repo = init_gate_repo(
            tmp_path,
            tracked_gate={"required_reviewer_roles": [], "pre_checks": [{"description": "no cmd key"}]},
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []

    def test_the_refusal_names_the_commit_and_file(self, tmp_path, capsys):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": [], "pre_checks": "not-a-list"}
        )
        _merge(repo)
        err = capsys.readouterr().err
        assert "pre_checks config FAILED to load" in err
        assert TRACKED_GATE_RELATIVE_PATH in err

    def test_ignore_repo_gate_does_not_lift_a_pre_checks_refusal(self, tmp_path):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": [], "pre_checks": "not-a-list"}
        )
        assert _merge(repo, extra_args=["--ignore-repo-gate"]) == verb.EXIT_PRE_CHECKS_FAILED

    def test_skip_pre_checks_lets_the_fix_for_a_broken_pre_checks_land(self, tmp_path):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": [], "pre_checks": "not-a-list"}
        )
        assert _merge(repo, extra_args=["--skip-pre-checks"]) == verb.EXIT_OK

    def test_malformed_reviewer_roles_fall_back_with_a_warning_naming_only_that_pair(
        self, tmp_path, capsys
    ):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": "reviewer", "pre_checks": []}
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_OK
        assert len(merge_calls) == 1
        err = capsys.readouterr().err
        assert "merge.required_reviewer_roles NOT ENFORCED" in err
        assert "merge.required_scanners NOT ENFORCED" in err
        assert "merge.pre_checks NOT ENFORCED" not in err
        assert TRACKED_GATE_RELATIVE_PATH in err

    def test_malformed_reviewer_roles_do_not_disable_valid_pre_checks(self, tmp_path):
        repo = init_gate_repo(
            tmp_path, tracked_gate={"required_reviewer_roles": "reviewer", "pre_checks": [_FAIL_CHECK]}
        )
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []

    def test_a_pr_that_fixes_the_reviewer_roles_is_judged_by_the_broken_base(self, tmp_path, capsys):
        repo = init_gate_repo(
            tmp_path,
            tracked_gate={"required_reviewer_roles": "broken"},
            head_files={
                TRACKED_GATE_RELATIVE_PATH: yaml.safe_dump(
                    {"merge": {"required_reviewer_roles": [], "pre_checks": [_FAIL_CHECK]}}
                )
            },
        )
        # Judged by base (broken -> fallback), so the PR's own strict gate is
        # not applied to it; it takes effect from the next merge.
        assert _merge(repo) == verb.EXIT_OK
        assert "required_reviewer_roles NOT ENFORCED" in capsys.readouterr().err

    def test_a_whole_file_that_does_not_parse_refuses_pre_checks_and_warns_for_the_pair(
        self, tmp_path, capsys
    ):
        repo = init_gate_repo(tmp_path, tracked_gate=None)
        commit_raw_gate_to_base(repo, b"merge: [unclosed")
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []
        assert "required_reviewer_roles NOT ENFORCED" in capsys.readouterr().err

    def test_a_base_sha_that_cannot_be_read_refuses_pre_checks(self, tmp_path, capsys):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        pr_info = {**repo.pr_info(), "base": {"ref": "main", "sha": "f" * 40}}
        merge_calls: list[str] = []
        assert _merge(repo, pr_info=pr_info, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []
        assert "required_reviewer_roles NOT ENFORCED" in capsys.readouterr().err


class TestNoBaseShaInThePayload:
    def test_a_missing_base_sha_fails_closed_so_pre_checks_refuse_and_the_pair_falls_back(
        self, tmp_path, capsys
    ):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        pr_info = {**repo.pr_info(), "base": {"ref": "main"}}
        merge_calls: list[str] = []
        assert _merge(repo, pr_info=pr_info, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []
        err = capsys.readouterr().err
        assert "no base commit SHA" in err
        assert "required_reviewer_roles NOT ENFORCED" in err

    def test_a_missing_base_sha_refuses_even_when_no_check_is_declared_there(self, tmp_path):
        repo = _repo_with(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        pr_info = {**repo.pr_info(), "base": {"ref": "main"}}
        assert _merge(repo, pr_info=pr_info) == verb.EXIT_PRE_CHECKS_FAILED

    def test_skip_pre_checks_is_the_bypass_for_a_missing_base_sha(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        pr_info = {**repo.pr_info(), "base": {"ref": "main"}}
        assert _merge(repo, pr_info=pr_info, extra_args=["--skip-pre-checks"]) == verb.EXIT_OK


class TestNoGitTreeToReadTheGateFrom:
    """A gate that cannot be read from a git tree must refuse pre_checks before
    the merge lands, not be skipped and left to the post-merge tree sync."""

    def test_a_malformed_git_working_tree_with_pre_checks_refuses_before_merge_pr(self, tmp_path, capsys):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        write_deployment_config(tmp_path, {"sync_tree_after_merge": False, "git_working_tree": 42})
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []
        assert "git_working_tree" in capsys.readouterr().err

    def test_skip_pre_checks_is_the_bypass_for_a_malformed_git_working_tree(self, tmp_path):
        repo = _repo_with(tmp_path, [_FAIL_CHECK])
        write_deployment_config(tmp_path, {"sync_tree_after_merge": False, "git_working_tree": 42})
        assert _merge(repo, extra_args=["--skip-pre-checks"]) == verb.EXIT_OK

    def test_a_repo_path_that_is_not_a_git_tree_refuses_pre_checks(self, tmp_path):
        write_deployment_config(tmp_path, {"sync_tree_after_merge": False})
        repo = GateRepo(path=tmp_path, base_sha="a" * 40, head_sha="b" * 40)
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []


class TestInvalidUtf8GateAtBase:
    def test_follows_each_keys_rule_the_pair_falls_back_and_pre_checks_refuse(self, tmp_path, capsys):
        repo = init_gate_repo(tmp_path, tracked_gate=None)
        commit_raw_gate_to_base(repo, b"merge:\n  required_reviewer_roles: []\n  note: \xff\xfe\n")
        merge_calls: list[str] = []
        assert _merge(repo, merge_calls=merge_calls) == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []
        err = capsys.readouterr().err
        assert "not valid UTF-8" in err
        assert "required_reviewer_roles NOT ENFORCED" in err

    def test_with_pre_checks_skipped_the_merge_does_not_crash(self, tmp_path):
        repo = init_gate_repo(tmp_path, tracked_gate=None)
        commit_raw_gate_to_base(repo, b"\xff\xfe\x00 not text")
        assert _merge(repo, extra_args=["--skip-pre-checks"]) == verb.EXIT_OK


class TestAbsentRepoPathIsALegitimateNoOp:
    def test_no_repo_path_no_post_merge_tree_merges_cleanly(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        argv = _base_args()
        argv.append("--no-post-merge-tree")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestResolvedCwdAndPassFailRecordEmitted:
    """A gate silent on success is indistinguishable from a gate that never
    ran -- every step must emit an explicit PASS/FAIL line carrying the raw
    exit code and the RESOLVED cwd it executed in, on success as well as
    failure."""

    def test_passing_check_emits_pass_line_with_cwd_and_exit_code(self, tmp_path, capsys):
        repo = _repo_with(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        assert _merge(repo) == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "PASS (exit=0" in err
        assert str(tmp_path) in err
        assert "pre_checks gate -- all 1 check(s) PASSED" in err

    def test_failing_fail_gated_check_emits_fail_line_with_cwd_and_exit_code(self, tmp_path, capsys):
        repo = _repo_with(
            tmp_path, [{"cmd": [_PY, "-c", "import sys; sys.exit(3)"], "on_failure": "fail"}]
        )
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        err = capsys.readouterr().err
        assert "FAIL (exit=3" in err
        assert str(tmp_path) in err
