"""test_merge_verb_pre_checks.py — merge.verb <-> merge.pre_checks_config
wiring tests (lr-843900).

Covers the defect this task closes: `merge.pre_checks_config.load_pre_checks`
existed as a fully-implemented, fully-tested module (see
test_merge_pre_checks_config.py) that `merge.verb` never imported or called —
a repo could declare `merge: pre_checks:` with `on_failure: fail` and get
zero signal the check never actually ran; the merge proceeded silently.

  - a declared `on_failure: fail` pre_check that exits non-zero BLOCKS the
    merge through the REAL runner (a real subprocess, never a mock) --
    EXIT_PRE_CHECKS_FAILED, and the merge_pr call is never reached
  - a declared `on_failure: fail` pre_check that exits 0 lets the merge
    proceed
  - a declared `on_failure: warn` (default) pre_check that fails does NOT
    block the merge
  - pre_checks run BEFORE step 9 (the merge call) -- never after
  - `--skip-pre-checks` is an explicit, logged bypass
  - a malformed pre_checks config (unreadable YAML, invalid step shape)
    refuses the merge at load time, before merge_pr is ever called
  - absent --repo-path (--no-post-merge-tree) is a legitimate no-op, mirroring
    load_pre_checks(None) == [] -- there is no repo-local config file to read
    without a local tree
  - every step, success or failure, emits a resolved-cwd PASS/FAIL record

No real network call: mirrors test_merge_verb_post_merge.py's own minimal
local opener/token/authority test-double set (tests/ is not a package in
this project, so these are kept self-contained rather than cross-imported).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

import yaml

from clagentic_loadout.merge import verb

_PY = sys.executable
_FULL_SHA = "a" * 40


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


def _make_opener(*, pr_info=None, files=None, comments=None, merge_calls=None):
    """merge_calls, when given, is a mutable list this opener appends to
    every time the merge POST fires -- proving (or disproving) that step 9
    was ever reached, independent of the process exit code."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": _FULL_SHA},
        "title": "feat: a change",
        "base": {"ref": "main"},
    }
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
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
            return _json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, comments + posted_comments)
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


def _write_pre_checks_config(repo_root, steps: list[dict]) -> None:
    # sync_tree_after_merge: false -- this file exercises the PRE-merge
    # pre_checks gate (step 8b, runs BEFORE merge_pr), never step 10's
    # post-merge tree sync (a real `git fetch origin` against a real remote,
    # covered by test_merge_verb_post_merge.py's own _init_repo_with_origin
    # fixture); tmp_path here is deliberately never a real git working tree.
    _write_pre_checks_config_raw(
        repo_root, {"pre_checks": steps, "sync_tree_after_merge": False}
    )


def _write_pre_checks_config_raw(repo_root, merge_section: dict) -> None:
    config_dir = repo_root / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"merge": merge_section}), encoding="utf-8"
    )


class TestPreChecksBlockTheMergeThroughTheRealRunner:
    """A mocked runner here would pass while the defect survives -- these
    tests exercise the REAL merge.post_merge.run_post_merge_steps executor,
    a real subprocess, never a stand-in."""

    def test_failing_fail_gated_check_blocks_merge_and_merge_pr_never_called(
        self, tmp_path
    ):
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}],
        )
        merge_calls: list[str] = []
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_calls=merge_calls),
        )
        assert code == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == [], (
            "merge_pr (step 9) must never be reached when a fail-gated "
            "pre_check exits non-zero -- the merge must be refused BEFORE "
            "the merge call, not merely reported as failed after landing."
        )

    def test_passing_fail_gated_check_lets_merge_proceed(self, tmp_path):
        marker = tmp_path / "checked.txt"
        _write_pre_checks_config(
            tmp_path,
            [
                {
                    "cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"],
                    "on_failure": "fail",
                }
            ],
        )
        merge_calls: list[str] = []
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_calls=merge_calls),
        )
        assert code == verb.EXIT_OK
        assert marker.exists()
        assert len(merge_calls) == 1

    def test_warn_gated_failing_check_does_not_block_merge(self, tmp_path):
        # on_failure: warn (or the default, when omitted) is a diagnostic,
        # never a refusal -- mirrors post_merge_steps' own on_failure
        # contract exactly (merge.post_merge.run_post_merge_steps).
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "warn"}],
        )
        merge_calls: list[str] = []
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_calls=merge_calls),
        )
        assert code == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_default_on_failure_omitted_does_not_block_merge(self, tmp_path):
        _write_pre_checks_config(
            tmp_path, [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"]}]
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestPreChecksRunBeforeTheMergeCall:
    def test_check_output_marker_exists_before_merge_post_fires(self, tmp_path):
        # The pre_check writes a marker file; the fixture opener's merge POST
        # handler checks for it. If pre_checks ran AFTER merge_pr (wrong
        # ordering), the marker would not exist yet at merge-POST time.
        marker = tmp_path / "pre-check-ran-first.txt"
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
        )
        ordering_violations: list[str] = []
        underlying_opener = _make_opener()

        def _ordering_asserting_opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/merge"):
                if not marker.exists():
                    ordering_violations.append("merge POST fired before pre_check ran")
            return underlying_opener(req, timeout=timeout)

        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
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
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        argv.append("--skip-pre-checks")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK

    def test_skip_flag_logs_to_stderr(self, tmp_path, capsys):
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        argv.append("--skip-pre-checks")
        verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert "pre_checks gate BYPASSED" in capsys.readouterr().err


class TestMalformedPreChecksConfig:
    def test_pre_checks_not_a_list_refuses_before_merge_pr_called(self, tmp_path):
        # A malformed merge.pre_checks VALUE (not top-level unparseable YAML
        # -- that shape is caught earlier, by step 7b's own task_id_guard
        # config load, an existing and separate concern out of this task's
        # scope) still refuses at THIS gate, before merge_pr is ever called.
        _write_pre_checks_config_raw(tmp_path, {"pre_checks": "not-a-list"})
        merge_calls: list[str] = []
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_calls=merge_calls),
        )
        assert code == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []

    def test_invalid_step_shape_refuses_at_load_time(self, tmp_path):
        _write_pre_checks_config(tmp_path, [{"description": "no cmd key"}])
        merge_calls: list[str] = []
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_calls=merge_calls),
        )
        assert code == verb.EXIT_PRE_CHECKS_FAILED
        assert merge_calls == []


class TestAbsentRepoPathIsALegitimateNoOp:
    """Mirrors load_pre_checks(None) == [] (test_merge_pre_checks_config.py):
    a repo-local config file cannot be read without a local tree at all --
    this is a pre-existing boundary every other repo-tier `merge:` key in
    this gate chain already has, not a new gap this gate introduces."""

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
    """lr-843900: a gate silent on success is indistinguishable from a gate
    that never ran -- every step must emit an explicit PASS/FAIL line
    carrying the raw exit code and the RESOLVED cwd it executed in, on
    success as well as failure."""

    def test_passing_check_emits_pass_line_with_cwd_and_exit_code(self, tmp_path, capsys):
        _write_pre_checks_config(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "PASS (exit=0" in err
        assert str(tmp_path) in err
        assert "pre_checks gate -- all 1 check(s) PASSED" in err

    def test_failing_fail_gated_check_emits_fail_line_with_cwd_and_exit_code(
        self, tmp_path, capsys
    ):
        _write_pre_checks_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(3)"], "on_failure": "fail"}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_PRE_CHECKS_FAILED
        err = capsys.readouterr().err
        assert "FAIL (exit=3" in err
        assert str(tmp_path) in err
