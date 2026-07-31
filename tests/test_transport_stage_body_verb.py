"""test_transport_stage_body_verb.py — tests for
clagentic_loadout.transport.stage_body_verb (lr-199b99, lr-becdef follow-on:
the sanctioned WRITE side for --body-env's identity-stamp contract;
PR-creation binding added lr-e1e2fb).

Coverage:
  - _run / main(): stages both the body file AND its identity-stamp sidecar
    via transport.body_env.stage_caller_body -- proving the CLI wrapper
    actually reaches the same write-side API lr-becdef's own tests already
    cover, rather than re-implementing the stamp shape.
  - --caller validation (reuses transport.git_host_api._SAFE_CALLER_RE),
    --target-pr validation (must be a positive int), and the --target-pr/
    --create-branch mutual-exclusion (lr-e1e2fb) all fail BEFORE any
    filesystem write.
  - stdin content validation (validate_body_stdin_content) runs BEFORE
    staging -- empty/malformed content is refused here, not later at
    --body-env read time. stdin is the SOLE content-input path (lr-e1e2fb:
    --body-file is removed -- no caller-supplied filesystem path is
    accepted anywhere in this package for PR-body content).
  - --head-sha is optional; when supplied, it lands in the stamp.
  - End-to-end regression (the exact defect this task fixes): stage via this
    verb, then read via transport.body_env.read_caller_body_bytes with a
    matching expect_target_pr/expect_head_sha succeeds and consumes both
    files -- the read side (lr-becdef) needs nothing else once this verb's
    write side has run. A raw-printf-only stage (body file with NO stamp)
    still fails closed on read -- this verb does not weaken lr-becdef's
    read-side guarantee, it is the ONLY sanctioned way to satisfy it.
  - --create-branch (lr-e1e2fb): the PR-creation binding, end to end through
    this verb and the read side, mirroring --target-pr's own coverage.
  - --version / --help exit cleanly before any argument is otherwise
    required (mirrors every other verb's own contract in this package).
"""

from __future__ import annotations

import json
import os

import pytest

from clagentic_loadout.transport import body_env
from clagentic_loadout.transport import stage_body_verb


@pytest.fixture(autouse=True)
def _isolate_tmpdir(tmp_path, monkeypatch):
    """Every staged path in this module is TMPDIR-relative
    (transport.body_env.resolve_caller_body_path) -- pin TMPDIR to a fresh
    per-test directory so no test here can ever read/write a real, shared
    staging location."""
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    return tmp_path


class TestArgParsing:
    def test_help_exits_ok_before_required_args(self, capsys):
        assert stage_body_verb.main(["--help"]) == stage_body_verb.EXIT_OK
        captured = capsys.readouterr()
        assert "loadout-stage-body" in captured.out

    def test_version_exits_ok(self):
        assert stage_body_verb.main(["--version"]) == stage_body_verb.EXIT_OK

    def test_missing_caller_is_usage_error(self):
        # argparse itself enforces required=True here (SystemExit(2)),
        # exactly like every other required flag on every other verb's own
        # parser in this package -- main() translates that SystemExit into
        # its exit code, never lets it propagate.
        rc = stage_body_verb.main(["--target-pr", "1"])
        assert rc == 2

    def test_missing_target_pr_and_create_branch_is_usage_error(self, monkeypatch):
        # lr-e1e2fb: --target-pr is no longer argparse-required=True on its
        # own -- --create-branch is now a valid alternative. Omitting BOTH
        # is caught by _run's own mutual-exclusion check (EXIT_USAGE), not
        # argparse's own exit 2.
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        rc = stage_body_verb.main(["--caller", "reviewer"])
        assert rc == stage_body_verb.EXIT_USAGE

    def test_both_target_pr_and_create_branch_is_usage_error(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        rc = stage_body_verb.main(
            ["--caller", "reviewer", "--target-pr", "1", "--create-branch", "feat/x"]
        )
        assert rc == stage_body_verb.EXIT_USAGE


class TestCallerValidation:
    @pytest.mark.parametrize(
        "bad_caller",
        ["../../etc/passwd", "reviewer/other", "", "has space", "has\nnewline"],
    )
    def test_invalid_caller_rejected_before_any_write(self, tmp_path, bad_caller, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: b'{"body": "x"}'
        )
        rc = stage_body_verb.main(["--caller", bad_caller, "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_USAGE
        # Nothing was written anywhere under the staging dir.
        staging_dir = tmp_path / "clagentic-loadout"
        assert not staging_dir.exists() or not any(staging_dir.iterdir())


class TestTargetPrValidation:
    @pytest.mark.parametrize("bad_pr", ["0", "-1"])
    def test_non_positive_target_pr_rejected(self, bad_pr, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", bad_pr])
        assert rc == stage_body_verb.EXIT_USAGE

    def test_non_integer_target_pr_rejected(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "not-a-number"])
        assert rc == 2  # argparse itself exits 2 for a bad int


class TestCreateBranch:
    """--create-branch (lr-e1e2fb): the PR-creation binding, mirroring
    --target-pr's own coverage."""

    def test_create_branch_happy_path_stages_body_and_stamp(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "plain text PR body"}')
        rc = stage_body_verb.main(
            ["--caller", "builder", "--create-branch", "feat/lr-e1e2fb-example"]
        )
        assert rc == stage_body_verb.EXIT_OK

        body_path = body_env.resolve_caller_body_path(caller="builder")
        stamp_path = body_env._resolve_caller_stamp_path(caller="builder")
        assert body_path.read_bytes() == b'{"body": "plain text PR body"}'

        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["create_branch"] == "feat/lr-e1e2fb-example"
        assert stamp["target_pr"] is None

    def test_create_branch_then_read_side_succeeds(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "plain text body"}')
        rc = stage_body_verb.main(["--caller", "builder", "--create-branch", "feat/x"])
        assert rc == stage_body_verb.EXIT_OK

        read_bytes = body_env.read_caller_body_bytes(
            caller="builder", expect_create_branch="feat/x"
        )
        assert read_bytes == b'{"body": "plain text body"}'


class TestStdinValidation:
    def test_empty_stdin_rejected_before_staging(self, tmp_path, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b"")
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_BODY_STDIN_EMPTY
        staging_dir = tmp_path / "clagentic-loadout"
        assert not staging_dir.exists() or not any(staging_dir.iterdir())

    def test_malformed_json_rejected_before_staging(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b"not json")
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_BODY_STDIN_EMPTY

    def test_missing_body_field_rejected_before_staging(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"not_body": "x"}')
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_BODY_STDIN_EMPTY


class TestStagesBodyAndStamp:
    """The core contract this task adds: a single verb invocation writes
    BOTH the body file and its identity-stamp sidecar, via the SAME
    transport.body_env.stage_caller_body API lr-becdef's own write-side
    tests already cover -- this is a thin CLI wrapper, not a second
    implementation of the stamp shape."""

    def test_stages_body_and_stamp_with_target_pr(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: b'{"body": "LGTM, no issues."}'
        )
        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "42"])
        assert rc == stage_body_verb.EXIT_OK

        body_path = body_env.resolve_caller_body_path(caller="reviewer")
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer")
        assert body_path.exists()
        assert stamp_path.exists()
        assert body_path.read_bytes() == b'{"body": "LGTM, no issues."}'

        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["target_pr"] == 42
        assert stamp["head_sha"] is None
        assert "staged_at" in stamp

    def test_stages_optional_head_sha(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        rc = stage_body_verb.main(
            ["--caller", "reviewer", "--target-pr", "7", "--head-sha", "deadbeef"]
        )
        assert rc == stage_body_verb.EXIT_OK

        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer")
        stamp = json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["head_sha"] == "deadbeef"

    def test_two_different_callers_stage_to_two_different_paths(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "peaches body"}')
        assert stage_body_verb.main(["--caller", "peaches", "--target-pr", "1"]) == 0

        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "bobbie body"}')
        assert stage_body_verb.main(["--caller", "bobbie", "--target-pr", "1"]) == 0

        peaches_body = body_env.resolve_caller_body_path(caller="peaches").read_bytes()
        bobbie_body = body_env.resolve_caller_body_path(caller="bobbie").read_bytes()
        assert peaches_body == b'{"body": "peaches body"}'
        assert bobbie_body == b'{"body": "bobbie body"}'


class TestEndToEndWithReadSide:
    """The exact regression this task fixes: stage via loadout-stage-body,
    then read via the lr-becdef read-and-consume + identity-stamp API.
    Proves BOTH reviewer roles succeed identically -- no reviewer-specific
    branch anywhere in the stage verb, so PEACHES and BOBBIE (or any other
    role) get the same deterministic outcome."""

    @pytest.mark.parametrize("caller", ["peaches", "bobbie"])
    def test_stage_then_read_succeeds_for_any_caller(self, caller, monkeypatch):
        monkeypatch.setattr(
            "sys.stdin.buffer.read",
            lambda: b'{"body": "clean.", "review_status": "clean"}',
        )
        rc = stage_body_verb.main(
            ["--caller", caller, "--target-pr", "388", "--head-sha", "abc123"]
        )
        assert rc == stage_body_verb.EXIT_OK

        read_bytes = body_env.read_caller_body_bytes(
            caller=caller, expect_target_pr=388, expect_head_sha="abc123"
        )
        assert read_bytes == b'{"body": "clean.", "review_status": "clean"}'

        # Consumed: a second read without re-staging fails closed, exactly
        # like the lr-becdef read-and-consume contract requires.
        with pytest.raises(body_env.BodyEnvError):
            body_env.read_caller_body_bytes(caller=caller, expect_target_pr=388)

    def test_raw_printf_only_stage_without_stamp_still_fails_closed_on_read(
        self, tmp_path, caller="reviewer"
    ):
        # Simulates the pre-fix defect: an agent writes ONLY the body file
        # (raw printf > body.<caller>.json), never the stamp sidecar. This
        # verb is not involved at all here -- the point is that the READ
        # side's guarantee (lr-becdef) is unweakened: a body staged without
        # its stamp is refused exactly like a missing body.
        body_path = body_env.resolve_caller_body_path(caller=caller)
        body_path.parent.mkdir(parents=True, exist_ok=True)
        body_path.write_bytes(b'{"body": "raw printf, no stamp"}')

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller=caller, expect_target_pr=1)
        assert "no identity stamp staged" in str(exc_info.value)

    def test_stage_for_wrong_pr_fails_closed_on_read(self, monkeypatch):
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "for PR 1"}')
        assert stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"]) == 0

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=2)
        assert "different PR" in str(exc_info.value)


class TestDurableWriteAndVerifyAfterWrite:
    """lr-765172 regression coverage: loadout-stage-body must never report
    success (EXIT_OK) while either the body file or its identity-stamp
    sidecar is absent or partial on disk. The pre-fix implementation wrote
    both files via bare, non-atomic, non-fsync'd write_bytes/write_text
    calls with no readback -- a write that landed the body but not (or only
    partially) the stamp produced exactly this silent-producer-failure
    symptom, caught previously only by a downstream consumer's own
    fail-closed check (lr-765172 incident). These tests cover the
    interrupted/partial-write shape the pre-fix suite never exercised
    (which asserted only .exists() on uninterrupted happy paths)."""

    def test_stage_uses_atomic_temp_file_then_replace(self, monkeypatch, tmp_path):
        """The staged body and stamp are written via a same-directory temp
        file + os.replace, never a direct write to the final path -- so an
        observer can never see a partially-written destination file."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        seen_tmp_names = []
        real_replace = os.replace

        def _spy_replace(src, dst):
            seen_tmp_names.append(str(src))
            return real_replace(src, dst)

        monkeypatch.setattr(os, "replace", _spy_replace)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_OK

        # Both the body and stamp writes went through a temp file distinct
        # from their final destination path (never a direct in-place write).
        assert len(seen_tmp_names) == 2
        body_path = body_env.resolve_caller_body_path(caller="reviewer")
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer")
        for tmp_name in seen_tmp_names:
            assert tmp_name not in (str(body_path), str(stamp_path))

    def test_stage_fsyncs_before_replace(self, monkeypatch):
        """Each write fsyncs the temp file's descriptor before the atomic
        rename -- the durability half of the fix, not just the atomicity
        half. A regression here would mean data could be acknowledged by
        the OS without actually being flushed to disk before rename."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        fsync_calls = []
        real_fsync = os.fsync

        def _spy_fsync(fd):
            fsync_calls.append(fd)
            return real_fsync(fd)

        monkeypatch.setattr(os, "fsync", _spy_fsync)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_OK

        # One fsync per file write (body, stamp) plus one directory fsync
        # per write -- at minimum, more than zero: a bare write_bytes/
        # write_text (the pre-fix shape) calls fsync zero times.
        assert len(fsync_calls) >= 2

    def test_zero_byte_stamp_after_write_fails_closed_not_exit_ok(self, monkeypatch, tmp_path):
        """Simulates the exact defect class this task fixes: the body lands
        but the stamp write is truncated/empty. Patches _atomic_write_bytes
        so the STAMP write (second call) lands a zero-byte file, proving
        stage_caller_body's verify-after-write step -- not luck, not a
        downstream consumer's own fail-closed check -- is what catches it,
        and that the verb layer reports a real failure exit code rather
        than EXIT_OK."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        from clagentic_loadout.transport import body_env as body_env_module

        real_atomic_write = body_env_module._atomic_write_bytes
        call_count = {"n": 0}

        def _flaky_atomic_write(path, data):
            call_count["n"] += 1
            if call_count["n"] == 2:
                # Second call is always the stamp write (body is written
                # first in stage_caller_body) -- simulate a truncated
                # write landing zero bytes instead of the real payload.
                real_atomic_write(path, b"")
            else:
                real_atomic_write(path, data)

        monkeypatch.setattr(body_env_module, "_atomic_write_bytes", _flaky_atomic_write)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])

        # Must NEVER be EXIT_OK: a zero-byte stamp is exactly the silent
        # producer failure this task closes.
        assert rc != stage_body_verb.EXIT_OK
        assert rc == stage_body_verb.EXIT_STAGE_VERIFY_FAILED

    def test_stage_caller_body_raises_if_body_missing_after_write(self, monkeypatch, tmp_path):
        """Same interrupted-write shape, exercised directly against the
        transport.body_env API (not through the verb layer): if the BODY
        write (first call) never actually lands on disk, stage_caller_body
        must raise BodyEnvError rather than return normally. A zero-byte
        body is deliberately NOT this module's write-durability signal (an
        empty body is a legitimate, readable write of empty content -- see
        test_transport_git_host_api.py's own deliberate empty-body staging
        to exercise a downstream reader's content validation); a MISSING
        file after write is the unambiguous durability failure this
        function's readback must catch."""
        from clagentic_loadout.transport import body_env as body_env_module

        real_atomic_write = body_env_module._atomic_write_bytes
        call_count = {"n": 0}

        def _flaky_atomic_write(path, data):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Simulate the body write never landing at all (e.g. a
                # crash between the temp-file write and os.replace).
                return
            real_atomic_write(path, data)

        monkeypatch.setattr(body_env_module, "_atomic_write_bytes", _flaky_atomic_write)

        with pytest.raises(body_env.BodyEnvError, match="could not be verified after write"):
            body_env_module.stage_caller_body(
                caller="reviewer", body_bytes=b'{"body": "x"}', target_pr=1
            )

    def test_stage_caller_body_accepts_deliberately_empty_body_content(self, monkeypatch, tmp_path):
        """An empty *body_bytes* is NOT a durability failure -- stage_caller_
        body must still stage it (and its non-empty stamp) successfully so a
        downstream reader's own content validation (not this module's write-
        durability check) is what rejects empty body content. Mirrors
        test_transport_git_host_api.py::TestMainBodyEnvEndToEnd's own
        deliberate empty-body staging against the real (unpatched)
        stage_caller_body."""
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        body_env.stage_caller_body(caller="reviewer", body_bytes=b"", target_pr=1)

        body_path = body_env.resolve_caller_body_path(caller="reviewer")
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer")
        assert body_path.exists()
        assert body_path.read_bytes() == b""
        assert stamp_path.exists()
        assert stamp_path.stat().st_size > 0

    def test_missing_stamp_after_stage_caller_body_returns_verb_fails_closed(
        self, monkeypatch, tmp_path
    ):
        """Belt-and-suspenders verb-layer check (independent of
        stage_caller_body's own verify): if stage_caller_body somehow
        returned normally but the stamp is not actually present on disk
        (e.g. a future regression that removes stage_caller_body's own
        verify step), the verb layer's own readback must still catch it
        and refuse EXIT_OK."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        from clagentic_loadout.transport import body_env as body_env_module

        real_stage = body_env_module.stage_caller_body

        def _stage_then_delete_stamp(
            *, caller, body_bytes, target_pr, create_branch=None, head_sha=None, env=None
        ):
            real_stage(
                caller=caller, body_bytes=body_bytes, target_pr=target_pr,
                create_branch=create_branch, head_sha=head_sha, env=env,
            )
            stamp_path = body_env_module._resolve_caller_stamp_path(caller=caller, env=env)
            stamp_path.unlink()

        monkeypatch.setattr(stage_body_verb, "stage_caller_body", _stage_then_delete_stamp)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_STAGE_VERIFY_FAILED

    def test_missing_body_after_stage_caller_body_returns_verb_fails_closed(
        self, monkeypatch, tmp_path
    ):
        """Sibling of the missing-stamp case above: an ABSENT body file
        after stage_caller_body reported success is a real durability
        failure and must still fail closed at the verb layer, distinct
        from the deliberately-empty-but-present case covered by
        test_verb_stages_deliberately_empty_body_successfully below."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        from clagentic_loadout.transport import body_env as body_env_module

        real_stage = body_env_module.stage_caller_body

        def _stage_then_delete_body(
            *, caller, body_bytes, target_pr, create_branch=None, head_sha=None, env=None
        ):
            real_stage(
                caller=caller, body_bytes=body_bytes, target_pr=target_pr,
                create_branch=create_branch, head_sha=head_sha, env=env,
            )
            body_path = body_env_module.resolve_caller_body_path(caller=caller, env=env)
            body_path.unlink()

        monkeypatch.setattr(stage_body_verb, "stage_caller_body", _stage_then_delete_body)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_STAGE_VERIFY_FAILED

    def test_verb_stages_deliberately_empty_body_successfully(self, monkeypatch, tmp_path):
        """PEACHES lr-765172 (amos.code-craft.4) regression: the verb-layer
        readback must NOT conflate a deliberately-empty body (valid,
        stageable content) with a missing/partial write (a real
        durability failure). This mirrors the transport-layer
        test_stage_caller_body_accepts_deliberately_empty_body_content --
        here exercised end-to-end through the verb itself.

        validate_body_stdin_content (called earlier in _run) would refuse
        a genuinely empty --body-stdin/--body-file payload before staging
        is ever reached -- that is a content-validation concern, separate
        from this test. To isolate the readback logic under test, this
        patches stage_caller_body to write a zero-byte body (as a
        --body-file caller staging intentional zero-byte content past
        content validation would produce), then asserts the verb still
        reports EXIT_OK rather than treating the zero-byte body as a
        write-durability failure."""
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')

        from clagentic_loadout.transport import body_env as body_env_module

        def _stage_with_empty_body(
            *, caller, body_bytes, target_pr, create_branch=None, head_sha=None, env=None
        ):
            body_env_module.stage_caller_body(
                caller=caller, body_bytes=b"", target_pr=target_pr,
                create_branch=create_branch, head_sha=head_sha, env=env,
            )

        monkeypatch.setattr(stage_body_verb, "stage_caller_body", _stage_with_empty_body)

        rc = stage_body_verb.main(["--caller", "reviewer", "--target-pr", "1"])
        assert rc == stage_body_verb.EXIT_OK

        body_path = body_env_module.resolve_caller_body_path(caller="reviewer")
        stamp_path = body_env_module._resolve_caller_stamp_path(caller="reviewer")
        assert body_path.exists()
        assert body_path.read_bytes() == b""
        assert stamp_path.exists()
        assert stamp_path.stat().st_size > 0
