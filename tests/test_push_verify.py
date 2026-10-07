"""Tests for push.verify (repo-declared verification commands run by
loadout-push): config loading, runner, and the verb integration on the
create-PR and --update-pr paths."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.push import verb
from clagentic_loadout.push.verify_config import (
    InvalidVerifyConfigError,
    VerifyEntry,
    load_verify_entries,
)
from clagentic_loadout.push.verify_run import (
    VerificationFailedError,
    render_verification_section,
    run_verifications,
)
from tests.test_push_verb import (  # noqa: F401  (fixtures reused, not redefined)
    _RecordingTokenProvider,
    _RefusingTokenProvider,
    _forgejo_create_opener,
    _isolate_user_config_root,
    _json_resp,
    _run_main,
    repo_with_remote,
)

PY = sys.executable


def _write_verify(repo, entries) -> None:
    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"push": {"verify": entries}}), encoding="utf-8"
    )


def _py(name, code, timeout=30):
    return {"name": name, "argv": [PY, "-c", code], "timeout_seconds": timeout}


def _capturing_create_opener(captured: list):
    def opener(req, timeout=15):
        if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
            captured.append(json.loads(req.data.decode("utf-8")))
            return _json_resp(201, {"number": 42})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


def _create_argv(repo, *extra):
    return [
        "--repo-path", str(repo), "--platform", "forgejo",
        "--title", "feat: t", "--body-stdin", *extra,
    ]


def _remote_has_branch(remote, branch="feature") -> bool:
    out = subprocess.run(
        ["git", "ls-remote", str(remote), f"refs/heads/{branch}"],
        capture_output=True, text=True, check=True,
    ).stdout
    return bool(out.strip())


class TestConfig:
    def test_absent_config_is_empty(self, tmp_path):
        assert load_verify_entries(tmp_path) == ()

    def test_loads_entries_with_default_timeout(self, tmp_path):
        _write_verify(tmp_path, [{"name": "unit", "argv": ["make", "test"]}])
        (entry,) = load_verify_entries(tmp_path)
        assert entry.name == "unit"
        assert entry.argv == ("make", "test")
        assert entry.timeout_seconds > 0

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-list",
            [{"name": "x", "argv": "make test"}],
            [{"name": "x", "argv": []}],
            [{"name": "", "argv": ["a"]}],
            [{"name": "x", "argv": ["a"], "timeout_seconds": 0}],
            [{"name": "x", "argv": ["a"], "timeout_seconds": True}],
            [{"name": "x", "argv": ["a"], "timeout_seconds": float("nan")}],
            [{"name": "x", "argv": ["a"], "timeout_seconds": float("inf")}],
            [{"name": "x", "argv": ["a"], "timeout_seconds": float("-inf")}],
            [{"name": "x", "argv": ["a"]}, {"name": "x", "argv": ["b"]}],
            ["bare-string"],
        ],
    )
    def test_malformed_config_raises(self, tmp_path, bad):
        _write_verify(tmp_path, bad)
        with pytest.raises(InvalidVerifyConfigError):
            load_verify_entries(tmp_path)


class TestRunner:
    def test_pass_records_exit_and_output(self, tmp_path):
        entry = VerifyEntry("ok", (PY, "-c", "print('hello')"), 30)
        (result,) = run_verifications((entry,), tmp_path)
        assert result.passed and result.exit_code == 0
        section = render_verification_section((result,))
        assert "ok" in section and "PASS" in section and "hello" in section

    def test_failure_raises_with_named_check_and_tail(self, tmp_path):
        entry = VerifyEntry(
            "lint", (PY, "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"), 30
        )
        with pytest.raises(VerificationFailedError) as info:
            run_verifications((entry,), tmp_path)
        assert info.value.failed.exit_code == 3
        assert "lint" in str(info.value) and "boom" in str(info.value)

    def test_timeout_is_a_failure(self, tmp_path):
        entry = VerifyEntry("slow", (PY, "-c", "import time; time.sleep(30)"), 0.5)
        with pytest.raises(VerificationFailedError) as info:
            run_verifications((entry,), tmp_path)
        assert info.value.failed.timed_out
        assert "TIMEOUT" in str(info.value)

    def test_missing_executable_is_a_failure(self, tmp_path):
        entry = VerifyEntry("ghost", ("definitely-not-a-real-binary-xyz",), 5)
        with pytest.raises(VerificationFailedError) as info:
            run_verifications((entry,), tmp_path)
        assert "COULD NOT START" in str(info.value)

    def test_stops_at_first_failure(self, tmp_path):
        marker = tmp_path / "ran-second"
        entries = (
            VerifyEntry("first", (PY, "-c", "raise SystemExit(1)"), 30),
            VerifyEntry("second", (PY, "-c", f"open({str(marker)!r}, 'w')"), 30),
        )
        with pytest.raises(VerificationFailedError):
            run_verifications(entries, tmp_path)
        assert not marker.exists()

    def test_output_is_bounded_and_fence_safe(self, tmp_path):
        entry = VerifyEntry(
            "noisy", (PY, "-c", "print('x' * 10000 + '```')"), 30
        )
        (result,) = run_verifications((entry,), tmp_path)
        assert len(result.stdout_tail) < 2100
        section = render_verification_section((result,))
        assert "````text" in section

    def test_runs_in_given_cwd_without_a_shell(self, tmp_path):
        entry = VerifyEntry(
            "cwd", (PY, "-c", "import os; print(os.getcwd()); print('$HOME;echo hi')"), 30
        )
        (result,) = run_verifications((entry,), tmp_path)
        assert str(tmp_path.resolve()) in result.stdout_tail
        assert "$HOME;echo hi" in result.stdout_tail


class TestVerbCreatePath:
    def test_absent_config_body_is_unchanged(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == "some body"

    def test_pass_appends_verification_section_to_body(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_py("unit", "print('all green')")])
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        body = sent[0]["body"]
        assert body.startswith("some body")
        assert "## Verification" in body and "unit" in body and "all green" in body

    def test_failure_refuses_before_push_and_pr(self, repo_with_remote, monkeypatch, capsys):
        repo, remote = repo_with_remote
        _write_verify(repo, [_py("unit", "import sys; print('red'); sys.exit(2)")])
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        err = capsys.readouterr().err
        assert "unit" in err and "red" in err
        assert not _remote_has_branch(remote)
        assert sent == []

    def test_timeout_refuses(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        _write_verify(repo, [_py("slow", "import time; time.sleep(30)", timeout=0.5)])
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert not _remote_has_branch(remote)
        assert sent == []

    def test_skip_verify_is_logged_and_recorded_in_body(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        marker = repo.parent / "should-not-exist"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        sent: list = []
        code = _run_main(
            _create_argv(repo, "--skip-verify"),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert not marker.exists()
        assert "BYPASSED via --skip-verify" in capsys.readouterr().err
        assert "SKIPPED" in sent[0]["body"] and "unit" in sent[0]["body"]

    def test_skip_verify_without_config_leaves_body_unchanged(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        sent: list = []
        code = _run_main(
            _create_argv(repo, "--skip-verify"),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == "some body"

    def test_malformed_config_exits_config_invalid(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        _write_verify(repo, "not-a-list")
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_CONFIG_INVALID
        assert not _remote_has_branch(remote)
        assert sent == []

    @pytest.mark.parametrize("bad", [float("nan"), float("inf")])
    def test_non_finite_timeout_exits_config_invalid(self, repo_with_remote, monkeypatch, bad):
        repo, remote = repo_with_remote
        _write_verify(repo, [_py("unit", "pass", timeout=bad)])
        sent: list = []
        code = _run_main(
            _create_argv(repo),
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_create_opener(sent),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_CONFIG_INVALID
        assert not _remote_has_branch(remote)
        assert sent == []

    def test_dry_run_does_not_run_verification(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        marker = repo.parent / "dry-run-marker"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        code = _run_main(
            _create_argv(repo, "--dry-run"),
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert not marker.exists()


class TestVerbUpdatePath:
    def _update_opener(self, captured: list):
        def opener(req, timeout=15):
            if req.get_method() == "GET":
                return _json_resp(200, {"body": "existing body"})
            if req.get_method() == "PATCH":
                captured.append(json.loads(req.data.decode("utf-8")))
                return _json_resp(200, {})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        return opener

    def test_body_update_runs_verification_and_records_it(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_py("unit", "print('fine')")])
        sent: list = []
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--replace-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=self._update_opener(sent),
            stdin_text=json.dumps({"body": "new body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "## Verification" in sent[0]["body"]

    def test_body_update_failure_refuses_before_token(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_py("unit", "raise SystemExit(1)")])
        sent: list = []
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--replace-body",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=self._update_opener(sent),
            stdin_text=json.dumps({"body": "new body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert sent == []

    @staticmethod
    def _track_upstream_at_head(repo) -> None:
        subprocess.run(
            ["git", "update-ref", "refs/remotes/origin/feature", "HEAD"], cwd=repo, check=True
        )
        subprocess.run(
            ["git", "branch", "--set-upstream-to=origin/feature"],
            cwd=repo, check=True, capture_output=True,
        )

    def _title_update(self, repo, monkeypatch, sent=None):
        return _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: new title",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=self._update_opener([] if sent is None else sent),
            monkeypatch=monkeypatch,
        )

    def test_metadata_only_update_without_new_commits_skips_and_says_so(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        self._track_upstream_at_head(repo)
        marker = repo.parent / "update-marker"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        sent: list = []
        code = self._title_update(repo, monkeypatch, sent)
        assert code == verb.EXIT_OK
        assert not marker.exists()
        assert "SKIPPED" in capsys.readouterr().err
        assert len(sent) == 1 and sent[0]["title"] == "feat: new title"

    def test_title_only_update_with_new_commits_runs_verification(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        self._track_upstream_at_head(repo)
        (repo / "more.txt").write_text("more\n")
        subprocess.run(["git", "add", "more.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: more work"], cwd=repo, check=True, capture_output=True
        )
        marker = repo.parent / "update-marker"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        assert self._title_update(repo, monkeypatch) == verb.EXIT_OK
        assert marker.exists()

    def test_title_only_update_with_new_commits_and_failing_check_refuses(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        self._track_upstream_at_head(repo)
        (repo / "more.txt").write_text("more\n")
        subprocess.run(["git", "add", "more.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: more work"], cwd=repo, check=True, capture_output=True
        )
        _write_verify(repo, [_py("unit", "raise SystemExit(1)")])
        sent: list = []
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: new title",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=self._update_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert sent == []

    def _commit_ahead(self, repo) -> None:
        self._track_upstream_at_head(repo)
        (repo / "more.txt").write_text("more\n")
        subprocess.run(["git", "add", "more.txt"], cwd=repo, check=True)
        subprocess.run(
            ["git", "commit", "-m", "feat: more work"], cwd=repo, check=True, capture_output=True
        )

    def test_no_body_pass_appends_record_to_existing_body(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        self._commit_ahead(repo)
        _write_verify(repo, [_py("unit", "print('fine')")])
        sent: list = []
        assert self._title_update(repo, monkeypatch, sent) == verb.EXIT_OK
        body = sent[0]["body"]
        assert body.startswith("existing body")
        assert "## Verification" in body and "unit" in body and "PASS" in body

    def test_no_body_fail_refuses_and_updates_nothing(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        self._commit_ahead(repo)
        _write_verify(repo, [_py("unit", "raise SystemExit(1)")])
        sent: list = []
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: new title",
            ],
            token_provider=_RefusingTokenProvider(),
            opener=self._update_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert sent == []

    def test_no_body_skip_verify_records_skip_in_body(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        self._commit_ahead(repo)
        marker = repo.parent / "skip-marker"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        sent: list = []
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: new title", "--skip-verify",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=self._update_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert not marker.exists()
        assert sent[0]["body"].startswith("existing body")
        assert "SKIPPED" in sent[0]["body"] and "unit" in sent[0]["body"]

    def test_title_only_update_with_unknown_upstream_runs_verification(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        marker = repo.parent / "update-marker"
        _write_verify(repo, [_py("unit", f"open({str(marker)!r}, 'w')")])
        assert self._title_update(repo, monkeypatch) == verb.EXIT_OK
        assert marker.exists()


class TestRunVerificationWithoutBody:
    def test_pass_returns_section_alone(self, tmp_path):
        _write_verify(tmp_path, [_py("unit", "print('ok')")])
        out = verb._run_verification(tmp_path, body=None, skip=False)
        assert out is not None and out.startswith("## Verification") and "PASS" in out

    def test_skip_returns_skip_notice(self, tmp_path):
        _write_verify(tmp_path, [_py("unit", "pass")])
        out = verb._run_verification(tmp_path, body=None, skip=True)
        assert out is not None and "SKIPPED" in out

    def test_fail_raises(self, tmp_path):
        _write_verify(tmp_path, [_py("unit", "raise SystemExit(1)")])
        with pytest.raises(VerificationFailedError):
            verb._run_verification(tmp_path, body=None, skip=False)

    def test_absent_config_stays_none(self, tmp_path):
        assert verb._run_verification(tmp_path, body=None, skip=False) is None


def test_help_documents_skip_verify(monkeypatch, capsys):
    code = _run_main(["--help"], token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
    assert code == verb.EXIT_OK
    assert "--skip-verify" in " ".join(capsys.readouterr().out.split())
