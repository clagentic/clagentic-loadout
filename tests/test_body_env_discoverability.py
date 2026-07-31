"""test_body_env_discoverability.py — tests for lr-ae00e6: the seam did not
have a mechanism defect (--body-env's stage-then-read contract already works,
and the read side already emits a self-diagnosing error naming caller,
target, found-stamp binding, and the exact recovery command — see
tests/test_transport_body_env.py's TestStagedForDifferentPrFailsClosed for
that pre-existing coverage of failure shapes (a) no stamp, (b) stamp scoped
to a different target, (c) stamp for a different caller). The defect this
task actually closes is DISCOVERABILITY: the flag name --body-env invites a
false "ephemeral environment variable" inference, and nothing in the
--help/description text of any of the four verbs exposing this flag
preempted that inference before this fix.

Coverage:
  - BODY_ENV_NOT_EPHEMERAL_NOTE (transport.body_env): the single shared
    fragment every verb appends, asserting it explicitly states this is a
    FILESYSTEM mechanism, that it PERSISTS across separate invocations, and
    that separate stage-then-read calls are the CORRECT and INTENDED shape
    (not a workaround for a guard limitation) — the exact three claims whose
    absence produced this task's two real incidents.
  - Every verb that exposes --body-env (loadout-git-host-api, loadout-review-
    post, loadout-push) renders this note in its own --help output.
  - loadout-stage-body (the write side) also renders it, since a caller
    reading ITS --help first (before ever reaching a read-side verb) should
    see the same correction.
  - Case (b) re-affirmed as data: reading transport.body_env.BodyEnvError's
    message for a stamp scoped to a different target already names the
    recovery command (locked pre-existing behavior, re-asserted here so this
    test file stands as the one place documenting all three failure shapes
    for this task's own verification record, per the task's explicit
    instruction to prove (a)/(b)/(c) and call out (b) by name).
"""

from __future__ import annotations

import io

import pytest

from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import body_env, git_host_api, stage_body_verb


class TestSharedNoteContent:
    """The note itself must make the three corrective claims explicit -- this
    is what closes the "ephemeral env var" inference at the source, per the
    task's item 2."""

    def test_states_this_is_a_filesystem_mechanism(self):
        note = body_env.BODY_ENV_NOT_EPHEMERAL_NOTE.lower()
        assert "not" in note and "environment variable" in note
        assert "filesystem" in note

    def test_states_it_persists_across_invocations(self):
        note = body_env.BODY_ENV_NOT_EPHEMERAL_NOTE.lower()
        assert "persist" in note
        assert "separate process invocations" in note or "separate invocations" in note

    def test_states_separate_calls_are_correct_not_a_workaround(self):
        note = body_env.BODY_ENV_NOT_EPHEMERAL_NOTE.lower()
        assert "correct" in note
        assert "not a workaround" in note

    def test_points_at_the_documented_two_call_contract(self):
        note = body_env.BODY_ENV_NOT_EPHEMERAL_NOTE
        assert "docs/integration.md" in note


def _normalize_whitespace(text: str) -> str:
    """argparse's HelpFormatter wraps long help strings across lines at
    arbitrary word boundaries (terminal-width-dependent) -- collapsing all
    whitespace runs to a single space lets a substring assertion survive
    wherever a wrap happens to land, without asserting anything about the
    formatter's own line-wrapping choices."""
    return " ".join(text.split())


class TestEveryBodyEnvVerbRendersTheNote:
    """Each of the four verbs exposing --body-env (or, for loadout-stage-
    body, the write side of the same contract) must surface the SAME shared
    note in --help -- proving this is one source of truth wired into every
    call site, not four hand-authored (and driftable) copies."""

    def test_git_host_api_help_renders_the_note(self, capsys):
        assert git_host_api.main(["--help"]) == git_host_api.EXIT_OK
        captured = _normalize_whitespace(capsys.readouterr().out)
        assert "NOT an environment variable" in captured

    def test_review_post_help_renders_the_note(self, capsys):
        assert review_verb.main(["--help"]) == review_verb.EXIT_OK
        captured = _normalize_whitespace(capsys.readouterr().out)
        assert "NOT an environment variable" in captured

    def test_push_help_renders_the_note(self, capsys):
        assert push_verb.main(["--help"]) == push_verb.EXIT_OK
        captured = _normalize_whitespace(capsys.readouterr().out)
        assert "NOT an environment variable" in captured

    def test_stage_body_help_renders_the_note(self, capsys):
        assert stage_body_verb.main(["--help"]) == stage_body_verb.EXIT_OK
        captured = _normalize_whitespace(capsys.readouterr().out)
        assert "NOT an environment variable" in captured


class TestThreeFailureShapesReaffirmed:
    """Re-affirms, as data, that the read side already self-diagnoses all
    three real failure shapes this task's verification section requires:
    (a) no stamp at all, (b) stamp present but scoped to a DIFFERENT target
    (the shape that actually burned this session -- NOT the same code path
    as (a)), and (c) stamp present for a different caller. Each assertion
    below exercises transport.body_env.read_caller_body_bytes directly (the
    single call site every read-side verb -- git_host_api, review.verb,
    push.verb -- delegates to), so this is genuine coverage of the shared
    mechanism, not a verb-specific reimplementation."""

    def test_shape_a_no_stamp_at_all(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=27, env=env)
        message = str(exc_info.value)
        assert "no identity stamp staged" in message
        assert "builder" in message
        assert "loadout-stage-body" in message
        assert "--target-pr 27" in message

    def test_shape_b_stamp_scoped_to_a_different_target(self, tmp_path):
        """THE SHAPE THAT BURNED THIS SESSION (lr-ae00e6): a stamp staged for
        --create-branch is silently useless for --target-pr 27 -- this is a
        DIFFERENT code path than shape (a) (the stamp IS found and parsed,
        then rejected on binding mismatch), so covering only (a) would leave
        this defect live."""
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="builder", body_bytes=b"some PR body", create_branch="feat/lr-ae00e6", env=env
        )
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=27, env=env)
        message = str(exc_info.value)
        # Names BOTH what was found (create_branch binding) and what was
        # expected (target_pr 27) -- never a generic "mismatch" with no
        # detail, which is exactly what forced both incident agents to
        # invent an architecture theory instead of reading the error.
        assert "different PR" in message or "create-branch mode" in message
        assert "feat/lr-ae00e6" in message
        assert "--target-pr 27" in message
        assert "loadout-stage-body" in message
        assert "builder" in message

    def test_shape_c_stamp_present_for_a_different_caller(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        body_env.stage_caller_body(
            caller="other-caller", body_bytes=b"body", target_pr=27, env=env
        )
        with pytest.raises(body_env.BodyEnvError) as exc_info:
            body_env.read_caller_body_bytes(caller="builder", expect_target_pr=27, env=env)
        message = str(exc_info.value)
        assert "no identity stamp staged" in message
        assert "builder" in message
        assert "other-caller" not in message
        assert "loadout-stage-body" in message
        assert "--target-pr 27" in message
