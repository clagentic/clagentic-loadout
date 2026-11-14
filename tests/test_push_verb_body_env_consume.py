"""Tests for when loadout-push consumes a staged --body-env pair: validated at
read time, deleted only immediately before the first remote write, so a
refusal that never reached the remote leaves the pair in place for a retry."""

from __future__ import annotations

import json
import os
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
    use_local_fetch_url as _use_local_fetch_url,
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
        repo, remote = repo_with_remote
        _use_local_fetch_url(repo, remote)
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)
        argv = ["--repo-path", str(repo), "--platform", "github", "--title", "feat: t", "--body-env"]

        code = _run_main(argv, token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_REMOTE_ERROR
        assert _still_staged(create_branch=branch)
        assert "no remote write happened" in capsys.readouterr().err

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
        assert "no remote write happened" in capsys.readouterr().err

        sent: list = []
        code = _run_main(
            [*base, "--title", "feat: fix the leak"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == _BODY

    def test_title_gate_refusal_then_corrected_retry_needs_no_restage(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)
        base = ["--repo-path", str(repo), "--platform", "forgejo", "--body-env"]

        code = _run_main(
            [*base, "--title", "not a conventional title"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        assert _still_staged(create_branch=branch)
        err = capsys.readouterr().err
        assert "no remote write happened" in err
        assert str(body_env.resolve_caller_body_path(caller=_CALLER).parent) in err
        assert "until" in err

        sent: list = []
        code = _run_main(
            [*base, "--title", "feat: t"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert sent[0]["body"] == _BODY
        assert not _still_staged(create_branch=branch)

    def test_refusal_leaves_the_pair_byte_identical_and_unrenamed(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        _stage_create(repo)
        body_path = body_env.resolve_caller_body_path(caller=_CALLER)
        before = sorted(
            (p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in body_path.parent.iterdir()
        )
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--body-env",
             "--title", "not a conventional title"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        after = sorted(
            (p.name, p.read_bytes(), p.stat().st_mtime_ns) for p in body_path.parent.iterdir()
        )
        assert after == before

    def test_refusal_then_ttl_aged_pair_gets_the_accurate_exit_28_message(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _stage_create(repo)
        argv = ["--repo-path", str(repo), "--platform", "forgejo", "--body-env"]
        code = _run_main(
            [*argv, "--title", "not a conventional title"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        capsys.readouterr()

        staged_dir = body_env.resolve_caller_body_path(caller=_CALLER).parent
        aged = staged_dir.stat().st_mtime - (body_env._ABANDONED_PAIR_TTL_SECONDS + 60)
        for path in staged_dir.iterdir():
            os.utime(path, (aged, aged))

        sent: list = []
        code = _run_main(
            [*argv, "--title", "feat: t"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE
        assert sent == []
        err = capsys.readouterr().err
        assert "no staged pair" in err
        assert str(staged_dir) in err
        assert "no identity stamp staged" not in err

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

    def test_dry_run_keeps_staged_body(self, repo_with_remote, monkeypatch, capsys):
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
        assert "no remote write happened" not in capsys.readouterr().err

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

    def test_consume_claims_under_the_root_the_peek_used(self, repo_with_remote, tmp_path):
        """A staging env whose TMPDIR differs from the process TMPDIR: the
        claim resolves the peek's root, not the default one."""
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        identity_root = tmp_path / "identity" / "tmp"
        identity_root.mkdir(parents=True)
        env = {"TMPDIR": str(identity_root)}
        body_env.stage_caller_body(
            caller=_CALLER,
            body_bytes=json.dumps({"body": _BODY}).encode("utf-8"),
            create_branch=branch,
            env=env,
        )
        staged = verb._StagedBody()
        body = verb._read_body_env(
            staged=staged, caller=_CALLER, create_branch=branch, stage_kwargs={"env": env}
        )
        assert body == _BODY
        staged.consume()
        assert not body_env.resolve_caller_body_path(caller=_CALLER, env=env).exists()
        assert not staged.pending

    def test_second_invocation_cannot_claim_a_pair_already_claimed(
        self, repo_with_remote, monkeypatch
    ):
        """Another invocation claims the pair after this one read it: this
        one reaches its first remote write, finds the pair gone, exits 28
        and writes nothing."""
        repo, _remote = repo_with_remote
        _stage_create(repo)
        branch = verb.git_coords.current_branch(repo)

        class RacingProvider(_RecordingTokenProvider):
            def resolve_token(self, role):
                pair = body_env.peek_caller_body(caller=_CALLER, expect_create_branch=branch)
                body_env.claim_caller_body(caller=_CALLER, expected=pair).consume()
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

    def test_second_verb_invocation_without_a_stage_exits_28_and_pushes_nothing(
        self, repo_with_remote, monkeypatch
    ):
        repo, remote = repo_with_remote
        sent: list = []
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE
        assert sent == []
        branch = verb.git_coords.current_branch(repo)
        listing = _git(["ls-remote", str(remote), f"refs/heads/{branch}"], repo).stdout
        assert listing.strip() == ""


class TestRefusalNoteNamesRootAndExpiry:
    def test_pre_remote_refusal_names_the_resolved_root_and_the_expiry_time(
        self, repo_with_remote, monkeypatch, capsys
    ):
        from datetime import datetime, timezone

        repo, _remote = repo_with_remote
        _stage_create(repo)
        body_path = body_env.resolve_caller_body_path(caller=_CALLER)
        stamp_path = body_env._resolve_caller_stamp_path(caller=_CALLER)
        oldest = min(body_path.stat().st_mtime, stamp_path.stat().st_mtime)
        expected_expiry = datetime.fromtimestamp(
            oldest + body_env._ABANDONED_PAIR_TTL_SECONDS, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "not a conventional title",
             "--body-env"],
            token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        err = capsys.readouterr().err
        assert repr(str(body_path.parent)) in err
        assert f"until {expected_expiry}" in err

    def test_successful_dry_run_prints_no_refusal_note(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _stage_create(repo)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t",
             "--body-env", "--dry-run"],
            token_provider=_RecordingTokenProvider(),
            opener=_capturing_opener([]),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "stamp age limit" not in err
        assert "was left as staged" not in err


class TestRestageDuringAnInvocation:
    def test_restage_between_the_read_and_the_claim_exits_28_and_posts_nothing(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        branch = verb.git_coords.current_branch(repo)
        _stage_create(repo)

        class RestagingProvider(_RecordingTokenProvider):
            def resolve_token(self, role):
                body_env.stage_caller_body(
                    caller=_CALLER,
                    body_bytes=json.dumps({"body": "the newer body"}).encode("utf-8"),
                    create_branch=branch,
                )
                return super().resolve_token(role)

        sent: list = []
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-env"],
            token_provider=RestagingProvider(),
            opener=_capturing_opener(sent),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE
        assert sent == []
        assert "changed since this push started" in capsys.readouterr().err


class TestPeekAndClaimCallerBody:
    def test_peek_renames_and_deletes_nothing(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        staging_dir = body_env.resolve_caller_body_path(caller="builder").parent
        before = sorted(p.name for p in staging_dir.iterdir())
        pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        assert pair.data == b"x"
        assert pair.staging_root == staging_dir
        assert sorted(p.name for p in staging_dir.iterdir()) == before

    def test_peek_still_rejects_wrong_binding_and_leaves_the_pair(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        with pytest.raises(body_env.BodyEnvError):
            body_env.peek_caller_body(caller="builder", expect_target_pr=2)
        assert body_env.read_caller_body_bytes(caller="builder", expect_target_pr=1) == b"x"

    def test_claim_frees_the_public_path(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        claim = body_env.claim_caller_body(caller="builder", expected=pair)
        assert claim.data == b"x"
        assert not body_env.resolve_caller_body_path(caller="builder").exists()
        claim.consume()

    def test_restage_after_the_peek_is_refused_and_the_claimed_pair_deleted(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"old", target_pr=1)
        pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        body_env.stage_caller_body(caller="builder", body_bytes=b"new", target_pr=1)
        with pytest.raises(body_env.BodyEnvError, match="changed since this push started"):
            body_env.claim_caller_body(caller="builder", expected=pair)
        staging_dir = body_env.resolve_caller_body_path(caller="builder").parent
        assert not [p.name for p in staging_dir.iterdir() if ".claim-" in p.name]

    def test_restage_between_the_stamp_validation_and_the_snapshot_is_refused(self, monkeypatch):
        """A re-stage that lands after the stamp was read and validated: the
        peek keeps the validated stamp bytes, so the claim sees the newer
        stamp as a mismatch and refuses."""
        body_env.stage_caller_body(caller="builder", body_bytes=b"old", target_pr=1)
        real_read = body_env._read_staged_bytes

        def restaging_read(path):
            body_env.stage_caller_body(caller="builder", body_bytes=b"new", target_pr=1)
            return real_read(path)

        with monkeypatch.context() as patch:
            patch.setattr(body_env, "_read_staged_bytes", restaging_read)
            pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        with pytest.raises(body_env.BodyEnvError, match="changed since this push started"):
            body_env.claim_caller_body(caller="builder", expected=pair)
        staging_dir = body_env.resolve_caller_body_path(caller="builder").parent
        assert not [p.name for p in staging_dir.iterdir() if ".claim-" in p.name]

    def test_restage_between_the_stamp_read_and_its_validation_is_refused(self, monkeypatch):
        """A re-stage that lands between the stamp read and its validation:
        the stored stamp bytes are the ones that were read, not a re-read."""
        body_env.stage_caller_body(caller="builder", body_bytes=b"old", target_pr=1)
        real_validate = body_env._read_and_validate_stamp

        def restaging_validate(*args, **kwargs):
            body_env.stage_caller_body(caller="builder", body_bytes=b"new", target_pr=1)
            return real_validate(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(body_env, "_read_and_validate_stamp", restaging_validate)
            pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        with pytest.raises(body_env.BodyEnvError, match="changed since this push started"):
            body_env.claim_caller_body(caller="builder", expected=pair)
        public = body_env.resolve_caller_body_path(caller="builder")
        assert not public.exists()
        assert not [p.name for p in public.parent.iterdir() if ".claim-" in p.name]

    def test_claim_of_a_missing_pair_says_no_staged_pair_and_names_the_root(self):
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        body_env.claim_caller_body(caller="builder", expected=pair).consume()
        with pytest.raises(body_env.BodyEnvError, match="no staged pair") as exc_info:
            body_env.claim_caller_body(caller="builder", expected=pair)
        assert str(pair.staging_root) in str(exc_info.value)

    def test_peek_with_nothing_staged_says_no_staged_pair_and_names_the_root(self):
        with pytest.raises(body_env.BodyEnvError, match="no staged pair") as exc_info:
            body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        assert str(body_env.resolve_caller_body_path(caller="builder").parent) in str(exc_info.value)

    def test_concurrent_claims_have_exactly_one_winner(self):
        import threading

        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1)
        pair = body_env.peek_caller_body(caller="builder", expect_target_pr=1)
        barrier = threading.Barrier(8)
        results: list = []

        def attempt():
            barrier.wait()
            try:
                results.append(body_env.claim_caller_body(caller="builder", expected=pair))
            except body_env.BodyEnvError as exc:
                results.append(exc)

        threads = [threading.Thread(target=attempt) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        winners = [r for r in results if isinstance(r, body_env.ClaimedBody)]
        assert len(winners) == 1
        assert len(results) == 8

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
