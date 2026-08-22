"""test_transport_body_env.py — tests for clagentic_loadout.transport.body_env
(lr-10a996 BODY-TRANSPORT half: body-off-argv-and-pipe via a fixed,
statically-analyzable staged path).

Coverage:
  - resolve_body_path: TMPDIR-relative fixed path, TMPDIR-unset fallback to
    tempfile.gettempdir(), no caller-supplied path ever accepted (the
    function takes no path argument at all).
  - read_body_bytes: success (staged file present), missing path, path is a
    directory, unreadable file (permission denied) -- all raise
    BodyEnvError, never a bare OSError/FileNotFoundError a call site would
    have to special-case.
  - Fixed-path property: two resolve_body_path calls against the SAME env
    resolve to the byte-identical path (this is the whole point of the
    mechanism -- no per-invocation variation).
  - resolve_caller_body_path / read_caller_body_bytes / read_body_bytes(
    caller=...) (lr-3a7ae8): caller-namespaced staging that fixes the
    concurrent same-TMPDIR collision proven on console PR #332 --
    two DIFFERENT callers on the SAME TMPDIR resolve to two DIFFERENT
    physical paths (structural collision-proofing, not a race-prone
    ownership check), and a caller reading before its OWN write has landed
    fails closed rather than ever falling back to another caller's file.
  - create-mode staging (lr-e1e2fb): stage_caller_body / read_caller_body_
    bytes accept EITHER target_pr (existing-PR binding, unchanged) OR
    create_branch (push's PR-creation binding) -- exactly one, never both,
    never neither. This is the operator-directed replacement for a
    previously-attempted caller-supplied --body-file path (rejected after a
    security audit: a validated arbitrary path still accepts a location
    parameter). No filesystem path is ever caller-supplied anywhere in this
    module; loadout always computes the staging location.
"""

from __future__ import annotations

import os
import stat

import pytest

from clagentic_loadout.transport import body_env


class TestResolveBodyPath:
    def test_tmpdir_relative_fixed_path(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        path = body_env.resolve_body_path(env=env)
        assert path == tmp_path / "clagentic-loadout" / "body.json"

    def test_missing_tmpdir_falls_back_to_platform_tempdir(self):
        import tempfile

        path = body_env.resolve_body_path(env={})
        assert str(path).startswith(tempfile.gettempdir())
        assert path.name == "body.json"
        assert path.parent.name == "clagentic-loadout"

    def test_empty_tmpdir_value_falls_back_same_as_unset(self):
        import tempfile

        path = body_env.resolve_body_path(env={"TMPDIR": ""})
        assert str(path).startswith(tempfile.gettempdir())

    def test_two_resolutions_against_same_env_are_byte_identical(self, tmp_path):
        # The load-bearing property this whole module exists for: the
        # resolved path never varies across invocations sharing the same
        # TMPDIR, so the argv naming --body-env is a true constant.
        env = {"TMPDIR": str(tmp_path)}
        first = body_env.resolve_body_path(env=env)
        second = body_env.resolve_body_path(env=env)
        assert first == second

    def test_takes_no_caller_supplied_path_argument(self):
        # Signature-level lock-in: resolve_body_path has exactly one
        # parameter (env), never a path/filename a caller could vary --
        # that is the entire distinction from the previously-rejected
        # --body-file shape (see module docstring).
        import inspect

        sig = inspect.signature(body_env.resolve_body_path)
        assert list(sig.parameters) == ["env"]


class TestReadBodyBytes:
    def test_reads_staged_file(self, tmp_path):
        staged_dir = tmp_path / "clagentic-loadout"
        staged_dir.mkdir()
        staged_file = staged_dir / "body.json"
        staged_file.write_bytes(b'{"body": "hello"}')

        result = body_env.read_body_bytes(env={"TMPDIR": str(tmp_path)})
        assert result == b'{"body": "hello"}'

    def test_missing_path_raises_body_env_error(self, tmp_path):
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_body_bytes(env={"TMPDIR": str(tmp_path)})
        assert "no body staged" in str(exc_info.value)

    def test_path_is_a_directory_raises_body_env_error(self, tmp_path):
        staged_dir = tmp_path / "clagentic-loadout"
        # Create the fixed path itself AS a directory, not the parent.
        (staged_dir).mkdir()
        (staged_dir / "body.json").mkdir()

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_body_bytes(env={"TMPDIR": str(tmp_path)})
        assert "not a regular file" in str(exc_info.value)

    def test_unreadable_file_raises_body_env_error(self, tmp_path):
        staged_dir = tmp_path / "clagentic-loadout"
        staged_dir.mkdir()
        staged_file = staged_dir / "body.json"
        staged_file.write_bytes(b'{"body": "hello"}')
        os.chmod(staged_file, 0o000)
        try:
            if os.access(staged_file, os.R_OK):
                pytest.skip("running as a user that bypasses file permissions (e.g. root)")
            with pytest.raises(body_env.BodyEnvError) as exc_info:
                body_env.read_body_bytes(env={"TMPDIR": str(tmp_path)})
            assert "could not read" in str(exc_info.value)
        finally:
            os.chmod(staged_file, stat.S_IWRITE | stat.S_IREAD)

    def test_does_not_delete_or_truncate_after_read(self, tmp_path):
        # A retried invocation using the same staged body must still find
        # it -- cleanup lifecycle belongs to the harness/TMPDIR convention,
        # not to this module (see module docstring).
        staged_dir = tmp_path / "clagentic-loadout"
        staged_dir.mkdir()
        staged_file = staged_dir / "body.json"
        staged_file.write_bytes(b'{"body": "hello"}')

        body_env.read_body_bytes(env={"TMPDIR": str(tmp_path)})
        assert staged_file.exists()
        assert staged_file.read_bytes() == b'{"body": "hello"}'


class TestResolveCallerBodyPath:
    """lr-3a7ae8: the caller-namespaced sibling of resolve_body_path."""

    def test_tmpdir_and_caller_relative_fixed_path(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        path = body_env.resolve_caller_body_path(caller="reviewer-a", env=env)
        assert path == tmp_path / "clagentic-loadout" / "body.reviewer-a.json"

    def test_two_different_callers_resolve_to_two_different_paths(self, tmp_path):
        # The load-bearing structural property this whole fix exists for:
        # two callers sharing the SAME TMPDIR never resolve to the same
        # physical path, so there is no file for a write/write or
        # write/read race to land on.
        env = {"TMPDIR": str(tmp_path)}
        a_path = body_env.resolve_caller_body_path(caller="reviewer-a", env=env)
        b_path = body_env.resolve_caller_body_path(caller="reviewer-b", env=env)
        assert a_path != b_path
        assert a_path.parent == b_path.parent  # same staging dir

    def test_same_caller_two_resolutions_are_byte_identical(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        first = body_env.resolve_caller_body_path(caller="reviewer-a", env=env)
        second = body_env.resolve_caller_body_path(caller="reviewer-a", env=env)
        assert first == second

    def test_missing_tmpdir_falls_back_to_platform_tempdir(self):
        import tempfile

        path = body_env.resolve_caller_body_path(caller="reviewer-a", env={})
        assert str(path).startswith(tempfile.gettempdir())
        assert path.name == "body.reviewer-a.json"

    @pytest.mark.parametrize(
        "bad_caller",
        [
            "../../etc/passwd",
            "reviewer-a/../reviewer-b",
            "reviewer-a/reviewer-b",
            "",
            "reviewer-a reviewer-b",
            "reviewer-a\nreviewer-b",
            # lr-3e3318: a PURE TRAILING newline, distinct from the embedded
            # "reviewer-a\nreviewer-b" case above -- this is the exact shape
            # the prior '$'-anchored _SAFE_CALLER_RE let slip through (in
            # Python, without re.MULTILINE, '$' matches at end-of-string OR
            # just before a trailing newline), where the embedded-newline
            # case above was already rejected regardless of anchor style.
            "reviewer-a\n",
        ],
    )
    def test_invalid_caller_raises_body_env_error_before_touching_disk(self, tmp_path, bad_caller):
        # A caller value is a validated bare role/name TOKEN, never an
        # arbitrary path -- this is the same "no caller-supplied path"
        # contract resolve_body_path itself carries, extended to the new
        # namespacing input rather than weakened by it.
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.resolve_caller_body_path(caller=bad_caller, env={"TMPDIR": str(tmp_path)})
        assert "not a valid bare role/name token" in str(exc_info.value)


class TestConcurrentCallerCollisionRegression:
    """lr-3a7ae8 regression: the incident's clobber shape -- caller A
    stages its body, caller B's staging OVERWRITES a shared file before
    caller A's read, caller A silently posts caller B's content. Proves
    the fix's structural guarantee: caller A NEVER posts caller B's body,
    because the two callers' staged bytes never land on the same physical
    path in the first place."""

    def test_caller_a_never_reads_caller_b_body_same_tmpdir(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}

        # Caller A stages its own review body first.
        a_body = b'{"body": "code-craft review: 7 nits."}'
        body_env.stage_caller_body(caller="reviewer-a", body_bytes=a_body, target_pr=1, env=env)

        # Caller B stages its own, DIFFERENT security-audit body, on the
        # SAME TMPDIR -- this is the exact interleaving from the incident:
        # B's write happens BEFORE A's own verb invocation reads.
        b_body = b'{"body": "security audit: clean."}'
        body_env.stage_caller_body(caller="reviewer-b", body_bytes=b_body, target_pr=1, env=env)

        # Caller A's own invocation reads AFTER caller B's write landed.
        # The bug this regression guards against: A's read returning B's
        # bytes. The fix: A's read is scoped to A's own namespaced path,
        # which caller B's write never touched.
        read_by_a = body_env.read_caller_body_bytes(
            caller="reviewer-a", expect_target_pr=1, env=env
        )
        assert read_by_a == a_body
        assert read_by_a != b_body

        read_by_b = body_env.read_caller_body_bytes(
            caller="reviewer-b", expect_target_pr=1, env=env
        )
        assert read_by_b == b_body
        assert read_by_b != a_body

    def test_caller_b_staging_after_caller_a_read_still_never_clobbers_a(self, tmp_path):
        # Same scenario, opposite interleaving: caller B stages AFTER
        # caller A has already read. Order must not matter -- the two
        # paths are structurally distinct regardless of write/read timing.
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer-a", body_bytes=b'{"body": "reviewer-a first."}', target_pr=1, env=env
        )

        read_by_a = body_env.read_caller_body_bytes(
            caller="reviewer-a", expect_target_pr=1, env=env
        )
        assert read_by_a == b'{"body": "reviewer-a first."}'

        body_env.stage_caller_body(
            caller="reviewer-b", body_bytes=b'{"body": "reviewer-b second."}', target_pr=2, env=env
        )

        # A retried invocation (lr-becdef: A's own read already CONSUMED its
        # staged file) must RE-STAGE, not find a leftover -- re-staging the
        # same body simulates that retry.
        body_env.stage_caller_body(
            caller="reviewer-a", body_bytes=b'{"body": "reviewer-a first."}', target_pr=1, env=env
        )
        assert body_env.read_caller_body_bytes(
            caller="reviewer-a", expect_target_pr=1, env=env
        ) == b'{"body": "reviewer-a first."}'

    def test_read_body_bytes_caller_kwarg_is_collision_proof_end_to_end(self, tmp_path):
        # Same regression, exercised through the public read_body_bytes()
        # entry point (the one verb call sites actually use) rather than
        # read_caller_body_bytes directly -- proves the caller= kwarg wires
        # through to the same collision-proof path, not just the dedicated
        # function.
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer-a", body_bytes=b'{"body": "A body"}', target_pr=1, env=env
        )
        body_env.stage_caller_body(
            caller="reviewer-b", body_bytes=b'{"body": "B body"}', target_pr=1, env=env
        )

        assert body_env.read_body_bytes(
            caller="reviewer-a", expect_target_pr=1, env=env
        ) == b'{"body": "A body"}'
        assert body_env.read_body_bytes(
            caller="reviewer-b", expect_target_pr=1, env=env
        ) == b'{"body": "B body"}'

    def test_read_body_bytes_with_no_caller_keeps_legacy_single_path_behavior(self, tmp_path):
        # Back-compat lock-in: omitting caller entirely is BYTE-FOR-BYTE
        # the pre-fix single-fixed-path behavior -- this fix is additive,
        # not a breaking change to the original contract.
        env = {"TMPDIR": str(tmp_path)}
        legacy_path = body_env.resolve_body_path(env=env)
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        legacy_path.write_bytes(b'{"body": "legacy fixed path"}')

        assert body_env.read_body_bytes(env=env) == b'{"body": "legacy fixed path"}'


class TestOwnerMismatchFailsClosed:
    """lr-3a7ae8: a caller reading a body it did not stage -- either because
    its harness never staged one under this caller's OWN namespace, or
    because only a DIFFERENT caller's namespaced file exists -- must fail
    closed (BodyEnvError), never silently fall back to posting content it
    cannot attribute to its own identity."""

    def test_own_namespace_missing_fails_closed_even_when_a_foreign_caller_file_exists(
        self, tmp_path
    ):
        env = {"TMPDIR": str(tmp_path)}
        # Only "reviewer-b" staged a body -- "reviewer-a" never staged its own.
        body_env.stage_caller_body(
            caller="reviewer-b", body_bytes=b'{"body": "reviewer-b only."}', target_pr=1, env=env
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer-a", expect_target_pr=1, env=env)
        assert "no identity stamp staged" in str(exc_info.value)
        # The error names reviewer-a's own expected path, never
        # reviewer-b's -- confirms this is a fail-closed refusal, not a
        # silent fallback that happened not to fire in this scenario.
        assert "reviewer-a" in str(exc_info.value)
        assert "reviewer-b" not in str(exc_info.value)

    def test_owner_mismatch_never_returns_a_foreign_callers_bytes(self, tmp_path):
        # Belt-and-suspenders on the same scenario: assert directly that
        # NO exception path, and no successful-return path, ever yields
        # reviewer-b's bytes when reviewer-a is the caller asking.
        env = {"TMPDIR": str(tmp_path)}
        b_body = b'{"body": "reviewer-b only, must never reach reviewer-a."}'
        body_env.stage_caller_body(caller="reviewer-b", body_bytes=b_body, target_pr=1, env=env)

        try:
            result = body_env.read_caller_body_bytes(
                caller="reviewer-a", expect_target_pr=1, env=env
            )
        except body_env.BodyEnvError:
            result = None  # correct fail-closed outcome
        assert result != b_body
        assert result is None

    def test_read_body_bytes_caller_kwarg_also_fails_closed_on_missing_own_namespace(
        self, tmp_path
    ):
        # Same guarantee via the public read_body_bytes(caller=...) entry
        # point verb call sites actually use.
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer-b", body_bytes=b'{"body": "reviewer-b only."}', target_pr=1, env=env
        )

        with pytest.raises(body_env.BodyEnvError):
            body_env.read_body_bytes(caller="reviewer-a", expect_target_pr=1, env=env)


class TestReadAndConsumeRegression:
    """lr-becdef Axis 1 PRIMARY regression: PR #388 foreign-body incident.
    A staged body is now unlinked (consumed) on a successful read -- a
    retried/later invocation of the SAME caller must RE-STAGE, never
    silently re-read a leftover from a prior, unrelated invocation."""

    def test_read_twice_without_restaging_fails_closed(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "first read."}', target_pr=1, env=env
        )

        first = body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=1, env=env)
        assert first == b'{"body": "first read."}'

        # The second read (e.g. a retried invocation whose own staging step
        # never ran) finds nothing -- the first read consumed it -- and
        # fails closed rather than silently re-posting the first body under
        # a second, unrelated invocation's identity.
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=1, env=env)
        assert "no identity stamp staged" in str(exc_info.value)

    def test_consume_removes_both_body_and_stamp_files(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "x"}', target_pr=1, env=env
        )
        body_path = body_env.resolve_caller_body_path(caller="reviewer", env=env)
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer", env=env)
        assert body_path.exists()
        assert stamp_path.exists()

        body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=1, env=env)

        assert not body_path.exists()
        assert not stamp_path.exists()

    def test_stale_leftover_from_unrelated_pr_never_silently_reread(self, tmp_path):
        # Direct reproduction of the PR #388 incident shape: a body staged
        # for PR 100 (a prior, unrelated review) is left on disk (as it
        # always was pre-fix); a LATER invocation for a DIFFERENT PR (200)
        # must never read it as if it were its own.
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="peaches",
            body_bytes=b'{"body": "BOBBIE - clean: archivist_client.py review."}',
            target_pr=100,
            env=env,
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="peaches", expect_target_pr=200, env=env)
        assert "different PR" in str(exc_info.value)


class TestStagedForDifferentPrFailsClosed:
    """lr-becdef Axis 1 defense-in-depth (identity stamp): reading a body
    staged for a DIFFERENT PR than the current invocation must fail closed
    (BodyEnvError), never POST it -- the exact regression HOLDEN's scope
    comment pins verbatim."""

    def test_stage_for_pr_a_read_as_pr_b_fails_closed(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "review for PR-A"}', target_pr=111, env=env
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=222, env=env)
        assert "different PR" in str(exc_info.value)

        # Fails closed WITHOUT consuming -- the mismatched pair is left in
        # place (it may belong to a different, still-pending invocation),
        # never silently destroyed by the failed read attempt.
        body_path = body_env.resolve_caller_body_path(caller="reviewer", env=env)
        assert body_path.exists()

    def test_pr_mismatch_error_names_the_recovery_command(self, tmp_path):
        """PEACHES amos.code-craft.2: the message must name the concrete
        recovery invocation with the caller's own actual values substituted,
        not a generic placeholder -- this is what closes the gap where an
        agent knows WHAT went wrong but not what to run next."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "review for PR-A"}', target_pr=111, env=env
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=222, env=env)
        message = str(exc_info.value)
        assert "loadout-stage-body" in message
        assert "--caller reviewer" in message
        assert "--target-pr 222" in message

    def test_create_branch_mismatch_error_names_the_recovery_command(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"body for branch a", create_branch="feat/a", env=env
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(
                caller="builder", expect_create_branch="feat/b", env=env
            )
        message = str(exc_info.value)
        assert "loadout-stage-body" in message
        assert "--caller builder" in message
        assert "--create-branch feat/b" in message

    def test_wrong_binding_mode_error_names_the_recovery_command(self, tmp_path):
        """Staged in create-branch mode, read expects target_pr -- the
        recovery pointer must still name the CORRECT command for what this
        invocation actually expects (target_pr), not what was staged."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"body staged in create-branch mode",
            create_branch="feat/a", env=env,
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=42, env=env)
        message = str(exc_info.value)
        assert "loadout-stage-body" in message
        assert "--caller builder" in message
        assert "--target-pr 42" in message

    def test_head_sha_mismatch_error_names_the_recovery_command(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "verdict for sha-a"}',
            target_pr=1, head_sha="sha-a", env=env,
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(
                caller="reviewer", expect_target_pr=1, expect_head_sha="sha-b", env=env
            )
        message = str(exc_info.value)
        assert "loadout-stage-body" in message
        assert "--caller reviewer" in message
        assert "--target-pr 1" in message

    def test_no_stamp_staged_error_names_the_recovery_command(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="reviewer", expect_target_pr=7, env=env)
        message = str(exc_info.value)
        assert "loadout-stage-body" in message
        assert "--caller reviewer" in message
        assert "--target-pr 7" in message

    def test_read_body_bytes_caller_kwarg_also_fails_closed_on_pr_mismatch(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "review for PR-A"}', target_pr=111, env=env
        )

        with pytest.raises(body_env.BodyEnvError):
            body_env.read_body_bytes(caller="reviewer", expect_target_pr=222, env=env)

    def test_head_sha_mismatch_also_fails_closed(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer",
            body_bytes=b'{"body": "verdict for sha-a"}',
            target_pr=1,
            head_sha="sha-a",
            env=env,
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(
                caller="reviewer", expect_target_pr=1, expect_head_sha="sha-b", env=env
            )
        assert "different SHA" in str(exc_info.value)

    def test_matching_pr_and_head_sha_succeeds(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer",
            body_bytes=b'{"body": "verdict for sha-a"}',
            target_pr=1,
            head_sha="sha-a",
            env=env,
        )

        result = body_env.read_caller_body_bytes(
            caller="reviewer", expect_target_pr=1, expect_head_sha="sha-a", env=env
        )
        assert result == b'{"body": "verdict for sha-a"}'

    def test_unstamped_sha_fails_closed_when_reader_expects_a_sha(self, tmp_path):
        """lr-9ca25a hardening (MILLER comment #4/#7 residual): a stamp
        staged WITHOUT --head-sha must not silently satisfy ANY
        expect_head_sha the reader supplies -- this is the exact hole
        `expect_head_sha is not None and stamp.head_sha is not None`
        previously left open. Fails closed (BodyEnvError), and does NOT
        consume the staged pair (a mismatch is left in place, same as
        every other stale-read refusal in this class)."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer",
            body_bytes=b'{"body": "ordinary comment, staged with no SHA"}',
            target_pr=1,
            env=env,
        )

        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(
                caller="reviewer", expect_target_pr=1, expect_head_sha="sha-a", env=env
            )
        message = str(exc_info.value)
        assert "no head_sha" in message or "carries no head_sha" in message
        assert "loadout-stage-body" in message
        assert "--head-sha sha-a" in message

        # Not consumed -- the mismatched pair is left in place.
        body_path = body_env.resolve_caller_body_path(caller="reviewer", env=env)
        assert body_path.exists()

    def test_unstamped_sha_still_succeeds_when_reader_expects_no_sha(self, tmp_path):
        """Negative control: a reader that never supplies expect_head_sha at
        all (an ordinary, non-verdict comment post) is completely unaffected
        by this hardening -- the check only fires when the READER asks for a
        SHA match against a stamp that never recorded one."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer",
            body_bytes=b'{"body": "ordinary comment, staged with no SHA"}',
            target_pr=1,
            env=env,
        )

        result = body_env.read_caller_body_bytes(
            caller="reviewer", expect_target_pr=1, env=env
        )
        assert result == b'{"body": "ordinary comment, staged with no SHA"}'

    def test_read_caller_body_bytes_requires_exactly_one_of_target_pr_or_create_branch(
        self, tmp_path
    ):
        # lr-e1e2fb: a caller-namespaced read has no unchecked shape left --
        # omitting BOTH expect_target_pr and expect_create_branch raises,
        # exactly like the pre-existing "expect_target_pr is mandatory"
        # contract, now generalized to two mutually-exclusive bindings.
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError, match="EXACTLY ONE"):
            body_env.read_caller_body_bytes(caller="reviewer", env=env)

    def test_read_caller_body_bytes_rejects_both_target_pr_and_create_branch(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError, match="EXACTLY ONE"):
            body_env.read_caller_body_bytes(
                caller="reviewer", expect_target_pr=1, expect_create_branch="feat/x", env=env
            )

    def test_read_body_bytes_with_caller_but_no_binding_raises(self, tmp_path):
        # read_body_bytes(caller=...) must not offer an unchecked caller-
        # namespaced read path either -- omitting both bindings raises
        # immediately, before touching disk at all (no stage needed here).
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_body_bytes(caller="reviewer", env=env)
        assert "expect_target_pr" in str(exc_info.value)
        assert "expect_create_branch" in str(exc_info.value)

    def test_read_body_bytes_with_caller_and_both_bindings_raises(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError):
            body_env.read_body_bytes(
                caller="reviewer", expect_target_pr=1, expect_create_branch="feat/x", env=env
            )


class TestStageCallerBody:
    """lr-becdef: the WRITE side pairing with read_caller_body_bytes."""

    def test_stages_body_and_stamp_with_target_pr(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer", body_bytes=b'{"body": "x"}', target_pr=7, env=env
        )
        body_path = body_env.resolve_caller_body_path(caller="reviewer", env=env)
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer", env=env)
        assert body_path.read_bytes() == b'{"body": "x"}'

        import json as _json

        stamp = _json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["target_pr"] == 7
        assert stamp["head_sha"] is None
        assert "staged_at" in stamp

    def test_stages_optional_head_sha(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="reviewer",
            body_bytes=b'{"body": "x"}',
            target_pr=7,
            head_sha="deadbeef",
            env=env,
        )
        stamp_path = body_env._resolve_caller_stamp_path(caller="reviewer", env=env)

        import json as _json

        stamp = _json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["head_sha"] == "deadbeef"

    def test_stage_requires_exactly_one_of_target_pr_or_create_branch(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError, match="EXACTLY ONE"):
            body_env.stage_caller_body(caller="reviewer", body_bytes=b'{"body": "x"}', env=env)

    def test_stage_rejects_both_target_pr_and_create_branch(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError, match="EXACTLY ONE"):
            body_env.stage_caller_body(
                caller="reviewer",
                body_bytes=b'{"body": "x"}',
                target_pr=1,
                create_branch="feat/x",
                env=env,
            )


class TestCreateModeStaging:
    """lr-e1e2fb: the operator-directed fix for push's PR-creation path --
    extends the ALREADY-SANCTIONED --body-env mechanism to bind a staged
    body to the git branch that will open a new PR (create_branch), rather
    than an existing PR number (target_pr). No caller-supplied filesystem
    path is introduced anywhere; loadout still computes every path, and the
    branch binding is resolved by push.verb itself (git rev-parse), never
    caller-typed."""

    def test_stage_and_read_create_branch_round_trip(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder",
            body_bytes=b"plain text PR body",
            create_branch="feat/lr-e1e2fb-example",
            env=env,
        )
        result = body_env.read_caller_body_bytes(
            caller="builder", expect_create_branch="feat/lr-e1e2fb-example", env=env
        )
        assert result == b"plain text PR body"

    def test_stamp_records_create_branch_and_null_target_pr(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder",
            body_bytes=b"x",
            create_branch="feat/example",
            env=env,
        )
        stamp_path = body_env._resolve_caller_stamp_path(caller="builder", env=env)

        import json as _json

        stamp = _json.loads(stamp_path.read_text(encoding="utf-8"))
        assert stamp["create_branch"] == "feat/example"
        assert stamp["target_pr"] is None

    def test_read_rejects_wrong_branch_fails_closed_without_consuming(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"x", create_branch="feat/a", env=env
        )
        with pytest.raises(body_env.BodyEnvError, match="different branch"):
            body_env.read_caller_body_bytes(
                caller="builder", expect_create_branch="feat/b", env=env
            )
        # Fails closed WITHOUT consuming.
        body_path = body_env.resolve_caller_body_path(caller="builder", env=env)
        assert body_path.exists()

    def test_read_with_target_pr_against_a_create_branch_stamp_fails_closed(self, tmp_path):
        """A stamp staged in create-branch mode must never satisfy a read
        that expects a target_pr binding -- the two modes are not
        interchangeable, even when a caller mixes up which one to check."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"x", create_branch="feat/a", env=env
        )
        with pytest.raises(body_env.BodyEnvError, match="different PR"):
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=1, env=env)

    def test_read_with_create_branch_against_a_target_pr_stamp_fails_closed(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1, env=env)
        with pytest.raises(body_env.BodyEnvError, match="different branch"):
            body_env.read_caller_body_bytes(
                caller="builder", expect_create_branch="feat/a", env=env
            )

    def test_create_mode_read_and_consume_same_as_target_pr_mode(self, tmp_path):
        """The read-and-consume guarantee (lr-becdef) applies identically in
        create-branch mode -- a successful read unlinks both files, and a
        retry must re-stage."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"x", create_branch="feat/a", env=env
        )
        body_env.read_caller_body_bytes(caller="builder", expect_create_branch="feat/a", env=env)

        with pytest.raises(body_env.BodyEnvError, match="no identity stamp staged"):
            body_env.read_caller_body_bytes(
                caller="builder", expect_create_branch="feat/a", env=env
            )

    def test_no_caller_supplied_path_anywhere_in_create_mode(self, tmp_path):
        """The actual property this whole redesign exists for: neither
        stage_caller_body nor read_caller_body_bytes accepts a filesystem
        path argument of any kind in create-branch mode -- only caller
        identity, body content, and the branch name (a value push.verb
        resolves itself via git, never typed by an agent)."""
        import inspect

        stage_params = set(inspect.signature(body_env.stage_caller_body).parameters)
        read_params = set(inspect.signature(body_env.read_caller_body_bytes).parameters)
        path_shaped_names = {"path", "file", "filepath", "body_file"}
        assert not (stage_params & path_shaped_names)
        assert not (read_params & path_shaped_names)


class TestSafeCallerPatternKeptInLockstep:
    """body_env._SAFE_CALLER_RE is deliberately duplicated (not imported)
    from transport.git_host_api._SAFE_CALLER_RE, per the module docstring's
    dependency-direction rationale. This test locks the two patterns to the
    SAME literal regex source so a future edit to one that is not mirrored
    to the other is caught here rather than silently drifting into two
    subtly different "safe caller" definitions."""

    def test_patterns_are_byte_identical(self):
        from clagentic_loadout.transport import git_host_api

        assert body_env._SAFE_CALLER_RE.pattern == git_host_api._SAFE_CALLER_RE.pattern


class TestSweepAbandonedPairs:
    """sweep_abandoned_pairs (lr-4c1646) -- the abandoned-pair reaper.
    AVASARALA's task comment #1 narrowed this task to exactly this: an
    opportunistic, age-based sweep over the clagentic-loadout staging
    subdirectory, run from stage_caller_body and read_caller_body_bytes,
    warn-never-fail, never a caller-supplied path."""

    def test_removes_file_older_than_ttl(self, tmp_path):
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        stale = staging_dir / "body.orphan.json"
        stale.write_bytes(b"{}")

        env = {"TMPDIR": str(tmp_path)}
        # now is far enough past the file's mtime to exceed the TTL.
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=10, now=stale.stat().st_mtime + 20
        )
        assert removed == 1
        assert not stale.exists()

    def test_leaves_file_younger_than_ttl(self, tmp_path):
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        fresh = staging_dir / "body.builder.json"
        fresh.write_bytes(b"{}")

        env = {"TMPDIR": str(tmp_path)}
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=3600, now=fresh.stat().st_mtime + 5
        )
        assert removed == 0
        assert fresh.exists()

    def test_sweeps_both_body_and_stamp_of_an_abandoned_pair(self, tmp_path):
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        body_file = staging_dir / "body.builder.json"
        stamp_file = staging_dir / "body.builder.stamp.json"
        body_file.write_bytes(b"{}")
        stamp_file.write_bytes(b"{}")

        env = {"TMPDIR": str(tmp_path)}
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=10, now=body_file.stat().st_mtime + 20
        )
        assert removed == 2
        assert not body_file.exists()
        assert not stamp_file.exists()

    def test_missing_staging_directory_is_not_an_error(self, tmp_path):
        # No clagentic-loadout subdirectory ever created under this TMPDIR.
        env = {"TMPDIR": str(tmp_path)}
        assert body_env.sweep_abandoned_pairs(env=env, ttl_seconds=10) == 0

    def test_does_not_recurse_into_a_subdirectory(self, tmp_path):
        """This module never creates a subdirectory under its own staging
        dir -- a foreign subdirectory found there is left alone, never
        descended into or removed."""
        staging_dir = tmp_path / "clagentic-loadout"
        nested = staging_dir / "some-subdir"
        nested.mkdir(parents=True)
        (nested / "leftover.txt").write_bytes(b"x")

        env = {"TMPDIR": str(tmp_path)}
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=0, now=nested.stat().st_mtime + 100
        )
        assert removed == 0
        assert nested.exists()
        assert (nested / "leftover.txt").exists()

    def test_does_not_follow_or_sweep_a_symlink(self, tmp_path):
        """BOBBIE audit finding (PR #147 comment 16519): entry.is_file() and
        entry.stat() both follow a symlink to its target by default -- a
        symlink in the staging directory pointing at a stale regular file
        (even one OUTSIDE the staging directory entirely) must not be
        treated as a sweep candidate, and its target must never be
        touched. entry.unlink() would only ever remove the symlink itself,
        never the target (POSIX semantics), but this sweep is not designed
        to reason about symlinks at all -- it must skip them outright."""
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()

        target_dir = tmp_path / "outside-staging"
        target_dir.mkdir()
        stale_target = target_dir / "stale-target.json"
        stale_target.write_bytes(b"{}")
        old_time = stale_target.stat().st_mtime - (body_env._ABANDONED_PAIR_TTL_SECONDS + 60)
        os.utime(stale_target, (old_time, old_time))

        symlink = staging_dir / "body.symlinked.json"
        symlink.symlink_to(stale_target)

        env = {"TMPDIR": str(tmp_path)}
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=10, now=stale_target.stat().st_mtime + 1000
        )

        assert removed == 0
        assert symlink.is_symlink()
        assert stale_target.exists()

    def test_per_file_failure_is_warned_not_raised(self, tmp_path, monkeypatch, capsys):
        """A single sibling's unlink failure must never raise out of the
        sweep (operator constraint: warn, never fail on cleanup)."""
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        stale = staging_dir / "body.orphan.json"
        stale.write_bytes(b"{}")

        real_unlink = type(stale).unlink

        def _boom(self, *args, **kwargs):
            if self.name == "body.orphan.json":
                raise OSError("permission denied (synthetic, for test)")
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(type(stale), "unlink", _boom)

        env = {"TMPDIR": str(tmp_path)}
        removed = body_env.sweep_abandoned_pairs(
            env=env, ttl_seconds=10, now=stale.stat().st_mtime + 20
        )
        assert removed == 0
        assert stale.exists()
        assert "could not remove" in capsys.readouterr().err

    def test_stage_caller_body_sweeps_a_stale_sibling_from_a_different_caller(self, tmp_path):
        """The reaper hook wired into stage_caller_body (lr-4c1646): staging
        a NEW body for one caller sweeps an abandoned sibling left behind by
        a DIFFERENT caller's aborted invocation."""
        env = {"TMPDIR": str(tmp_path)}
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        orphan = staging_dir / "body.dead-caller.json"
        orphan.write_bytes(b"{}")
        old_time = orphan.stat().st_mtime - (body_env._ABANDONED_PAIR_TTL_SECONDS + 60)
        os.utime(orphan, (old_time, old_time))

        body_env.stage_caller_body(caller="builder", body_bytes=b"x", target_pr=1, env=env)

        assert not orphan.exists()
        assert body_env.resolve_caller_body_path(caller="builder", env=env).exists()

    def test_read_caller_body_bytes_sweeps_a_stale_sibling_even_on_its_own_failure(
        self, tmp_path
    ):
        """The reaper hook wired into read_caller_body_bytes (lr-4c1646)
        runs even when THIS invocation's own read fails closed (no body
        staged for the requesting caller) -- an abandoned sibling from a
        different caller is still reaped."""
        env = {"TMPDIR": str(tmp_path)}
        staging_dir = tmp_path / "clagentic-loadout"
        staging_dir.mkdir()
        orphan = staging_dir / "body.dead-caller.json"
        orphan.write_bytes(b"{}")
        old_time = orphan.stat().st_mtime - (body_env._ABANDONED_PAIR_TTL_SECONDS + 60)
        os.utime(orphan, (old_time, old_time))

        with pytest.raises(body_env.BodyEnvError, match="no identity stamp staged"):
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=1, env=env)

        assert not orphan.exists()

    def test_sweep_failure_resolving_staging_dir_never_raises(self, tmp_path, monkeypatch, capsys):
        """A failure resolving the staging directory itself must never
        propagate -- warn-never-fail applies at the top level of
        sweep_abandoned_pairs, not just per-file."""

        def _boom(*args, **kwargs):
            raise RuntimeError("synthetic resolution failure")

        monkeypatch.setattr(body_env, "_resolve_staging_dir", _boom)

        removed = body_env.sweep_abandoned_pairs(env={"TMPDIR": str(tmp_path)})
        assert removed == 0
        assert "could not resolve the staging directory" in capsys.readouterr().err
