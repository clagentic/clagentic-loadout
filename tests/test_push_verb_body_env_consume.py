"""Tests for when loadout-push consumes a staged --body-env pair: validated at
read time, deleted only immediately before the first remote write, so a
refusal that never reached the remote leaves the pair in place for a retry."""

from __future__ import annotations

import json
import sys
import urllib.error

import pytest
import yaml

from clagentic_loadout.push import verb
from clagentic_loadout.transport import body_env
from tests._support.push_verb import (  # noqa: F401  (isolate_user_config_root is an autouse fixture)
    RecordingTokenProvider as _RecordingTokenProvider,
    RefusingTokenProvider as _RefusingTokenProvider,
    git as _git,
    isolate_user_config_root,
    json_resp as _json_resp,
    repo_with_remote,
    run_main as _run_main,
)

_CALLER = verb.DEFAULT_ROLE
_BODY = "a body staged ahead of time"
_SYNTHETIC_GUARD_PATTERN = r"\bWIDGET-\d+\b"


def _stage_create(repo) -> None:
    body_env.stage_caller_body(
        caller=_CALLER,
        body_bytes=json.dumps({"body": _BODY}).encode("utf-8"),
        create_branch=verb.git_coords.current_branch(repo),
    )


def _stage_update(pr: int = 42) -> None:
    body_env.stage_caller_body(
        caller=_CALLER,
        body_bytes=json.dumps({"body": _BODY}).encode("utf-8"),
        target_pr=pr,
    )


def _still_staged(*, create_branch=None, target_pr=None) -> bool:
    try:
        body_env.read_caller_body_bytes(
            caller=_CALLER, expect_create_branch=create_branch,
            expect_target_pr=target_pr, consume=False,
        )
    except body_env.BodyEnvError:
        return False
    return True


def _capturing_opener(sent: list, *, pr_number=7):
    def opener(req, timeout=15):
        if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
            sent.append(json.loads(req.data.decode("utf-8")))
            return _json_resp(201, {"number": pr_number})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


def _write_config(repo, push_section: dict) -> None:
    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"push": push_section}), encoding="utf-8"
    )
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".clagentic/\n", encoding="utf-8")


def _failing_verify() -> dict:
    return {
        "verify": [
            {
                "name": "unit",
                "argv": [sys.executable, "-c", "import sys; sys.exit(2)"],
                "timeout_seconds": 30,
            }
        ]
    }


@pytest.fixture(autouse=True)
def _isolated_tmpdir(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))


class TestPreRemoteRefusalKeepsStagedBody:
    def test_github_without_repo_then_corrected_retry_needs_no_restage(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)
        argv = ["--repo-path", str(repo), "--platform", "github", "--title", "feat: t", "--body-env"]

        code = _run_main(argv, token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_REMOTE_ERROR
        assert _still_staged(create_branch=branch)
        assert "nothing was consumed" in capsys.readouterr().err

        sent: list = []

        def github_opener(req, timeout=30):
            if req.get_method() == "POST" and req.full_url.endswith("/repos/some-owner/some-repo/pulls"):
                sent.append(json.loads(req.data.decode("utf-8")))
                return _json_resp(201, {"number": 7})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        code = _run_main(
            [*argv, "--repo", "some-owner/some-repo"],
            token_provider=_RecordingTokenProvider(),
            opener=github_opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == _BODY
        assert not _still_staged(create_branch=branch)

    def test_task_id_in_title_then_corrected_retry_needs_no_restage(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _write_config(repo, {"task_id_guard_pattern": _SYNTHETIC_GUARD_PATTERN})
        _stage_create(repo)
        base = ["--repo-path", str(repo), "--platform", "forgejo", "--body-env"]

        code = _run_main(
            [*base, "--title", "feat: fix WIDGET-42 leak"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION
        assert _still_staged(create_branch=branch)
        assert "nothing was consumed" in capsys.readouterr().err

        sent: list = []
        code = _run_main(
            [*base, "--title", "feat: fix the leak"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == _BODY

    def test_create_verification_failure_keeps_staged_body(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _write_config(repo, _failing_verify())
        _stage_create(repo)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener([]),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert _still_staged(create_branch=branch)

    def test_update_verification_failure_keeps_staged_body(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        _write_config(repo, _failing_verify())
        head = _git(["rev-parse", "HEAD"], repo).stdout.strip()
        _stage_update()

        def opener(req, timeout=15):
            if req.get_method() == "GET":
                return _json_resp(200, {"body": "existing", "head": {"sha": head}})
            raise AssertionError("no remote write may happen after a failed verification")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--update-pr", "--pr", "42",
             "--body-env", "--replace-body"],
            token_provider=_RecordingTokenProvider(), opener=opener, monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_VERIFY_FAILED
        assert _still_staged(target_pr=42)

    def test_update_title_gate_refusal_keeps_staged_body(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _stage_update()
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--update-pr", "--pr", "42",
             "--title", "not a conventional title", "--body-env", "--replace-body"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        assert _still_staged(target_pr=42)


class TestConsumedOnceARemoteWriteIsAttempted:
    def test_success_consumes_on_create(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener([]),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert not _still_staged(create_branch=branch)

    def test_remote_push_failure_still_consumes(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _git(["config", "remote.origin.pushurl", str(repo.parent / "does-not-exist.git")], repo)
        _stage_create(repo)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener([]),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED
        assert not _still_staged(create_branch=branch)

    def test_dry_run_keeps_staged_body(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t",
             "--body-env", "--dry-run"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener([]),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert _still_staged(create_branch=branch)

    def test_update_skipped_not_pr_head_posts_staged_body_and_consumes(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_config(repo, _failing_verify())
        _stage_update()
        sent: list = []

        def opener(req, timeout=15):
            if req.get_method() == "GET":
                return _json_resp(200, {"body": "existing", "head": {"sha": "b" * 40}})
            sent.append(json.loads(req.data.decode("utf-8")))
            return _json_resp(200, {})

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--update-pr", "--pr", "42",
             "--body-env", "--replace-body"],
            token_provider=_RecordingTokenProvider(), opener=opener, monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == _BODY
        result = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert result["verification"] == "skipped_not_pr_head"
        assert not _still_staged(target_pr=42)

    def test_update_remote_failure_still_consumes(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _stage_update()

        def opener(req, timeout=15):
            raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--update-pr", "--pr", "42",
             "--body-env", "--replace-body"],
            token_provider=_RecordingTokenProvider(), opener=opener, monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_FAILED
        assert not _still_staged(target_pr=42)

    def test_second_invocation_sharing_one_staged_body_gets_exit_28(
        self, repo_with_remote, monkeypatch
    ):
        """The other invocation wins the single-use race between this one's
        read and its first remote write: this one must not write."""
        repo, remote = repo_with_remote
        _stage_create(repo)

        class RacingProvider(_RecordingTokenProvider):
            def resolve_token(self, role):
                body_env.consume_caller_body(caller=_CALLER)
                return super().resolve_token(role)

        sent: list = []
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=RacingProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE
        assert sent == []
        listing = _git(["ls-remote", str(remote), "refs/heads/feature"], repo).stdout
        assert listing.strip() == ""


class TestConsumeCallerBody:
    def test_second_consume_raises(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        body_env.consume_caller_body(caller="builder")
        with pytest.raises(body_env.BodyEnvError, match="already consumed"):
            body_env.consume_caller_body(caller="builder")

    def test_read_without_consume_leaves_pair_and_validates_stamp(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        assert body_env.read_caller_body_bytes(
            caller="builder", expect_target_pr=1, consume=False
        ) == b"x"
        assert body_env.read_caller_body_bytes(caller="builder", expect_target_pr=1) == b"x"
        with pytest.raises(body_env.BodyEnvError):
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=1)

    def test_read_without_consume_still_rejects_wrong_binding(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        with pytest.raises(body_env.BodyEnvError):
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=2, consume=False)
