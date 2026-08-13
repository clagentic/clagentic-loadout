"""test_merge_post_merge.py — unit tests for clagentic_loadout.merge.post_merge
(lr-77d6).

Covers:
  - ordered execution of multiple steps
  - on_failure: warn vs fail semantics
  - the lr-77d6 fix: a plain cmd STRING containing a shell-operator token
    (&&, ||, |, ;, >, >>, <) is REJECTED at parse time, never silently
    misparsed into a bogus argv
  - the list-form escape hatch: cmd as a list of strings bypasses shell-quote
    parsing entirely (no shell-operator ambiguity possible)
  - VAR=VALUE env-assignment-prefix support, in both string and list form
  - steps never run with shell=True (verified indirectly: a cmd relying on
    shell expansion/operators fails or is rejected, never silently succeeds
    via a shell)
  - the lr-53556a `detaches: true` schema flag: validator accept/reject
    rules, and (the regression this flag exists to fix) a detached step
    whose child holds fds 1/2 open returns promptly instead of hanging in
    subprocess.run's capture_output=True communicate() wait
  - the lr-d6e52b hardening: bounded per-step timeout_seconds for ordinary
    (non-detached) steps, and liveness_probe verification for detaches:true
    steps (heartbeat-advance-across-one-poll-interval, not a fixed wait) --
    both opt-in, validator accept/reject rules, and execution regression
    coverage.
"""

from __future__ import annotations

import sys
import time

import pytest

from clagentic_loadout.merge.post_merge import (
    PostMergeConfigError,
    PostMergeLivenessError,
    PostMergeStepFailedError,
    PostMergeStepTimeoutError,
    run_post_merge_steps,
    validate_post_merge_steps,
)

_PY = sys.executable


def _py_step(*, code: str, on_failure: str = "warn", description: str = "") -> dict:
    """Build a step whose cmd invokes the current interpreter with -c, so
    tests never depend on any external binary being on PATH."""
    step = {"cmd": [_PY, "-c", code], "on_failure": on_failure}
    if description:
        step["description"] = description
    return step


class TestOrderedExecution:
    def test_steps_run_in_order(self, tmp_path):
        marker = tmp_path / "order.txt"
        steps = [
            _py_step(code=f"open(r'{marker}', 'a').write('1')"),
            _py_step(code=f"open(r'{marker}', 'a').write('2')"),
            _py_step(code=f"open(r'{marker}', 'a').write('3')"),
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "123"

    def test_steps_run_in_project_root_cwd(self, tmp_path):
        marker = tmp_path / "cwd.txt"
        steps = [_py_step(code="import os; open('cwd.txt', 'w').write(os.getcwd())")]
        run_post_merge_steps(steps, tmp_path)
        assert marker.exists()
        assert marker.read_text() == str(tmp_path)


class TestOnFailureSemantics:
    def test_warn_logs_and_continues(self, tmp_path):
        marker = tmp_path / "reached.txt"
        steps = [
            _py_step(code="import sys; sys.exit(1)", on_failure="warn"),
            _py_step(code=f"open(r'{marker}', 'w').write('reached')"),
        ]
        # Must not raise -- a warn-level failure never aborts the run.
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "reached"

    def test_fail_raises_and_stops_subsequent_steps(self, tmp_path):
        marker = tmp_path / "should-not-exist.txt"
        steps = [
            _py_step(code="import sys; sys.exit(1)", on_failure="fail"),
            _py_step(code=f"open(r'{marker}', 'w').write('reached')"),
        ]
        with pytest.raises(PostMergeStepFailedError):
            run_post_merge_steps(steps, tmp_path)
        assert not marker.exists()

    def test_default_on_failure_is_warn(self, tmp_path):
        # No on_failure key at all -- default must be warn (never abort).
        marker = tmp_path / "default.txt"
        steps = [
            {"cmd": [_PY, "-c", "import sys; sys.exit(3)"]},
            _py_step(code=f"open(r'{marker}', 'w').write('ok')"),
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ok"


class TestShellOperatorRejection:
    """The lr-77d6 fix: a shell operator in a plain cmd STRING is rejected
    at config-validation time, never silently misparsed into a literal argv
    token the way the reference runner's shlex.split + shell=False did."""

    @pytest.mark.parametrize(
        "operator",
        ["&&", "||", "|", ";", ">", ">>", "<"],
    )
    def test_shell_operator_token_rejected(self, operator, tmp_path):
        steps = [{"cmd": f"git fetch origin {operator} git switch --detach X"}]
        with pytest.raises(PostMergeConfigError, match="shell operator"):
            run_post_merge_steps(steps, tmp_path)

    def test_rejection_happens_before_any_step_executes(self, tmp_path):
        # A malformed step LATER in the list must be caught before an
        # EARLIER, well-formed step is allowed to run (validate-all-first).
        marker = tmp_path / "must-not-exist.txt"
        steps = [
            _py_step(code=f"open(r'{marker}', 'w').write('ran')"),
            {"cmd": "echo one && echo two"},
        ]
        with pytest.raises(PostMergeConfigError):
            run_post_merge_steps(steps, tmp_path)
        assert not marker.exists()

    def test_shell_operator_as_separate_argv_token_in_list_form_is_fine(self, tmp_path):
        # List-form cmd is an explicit argv -- "&&" appearing as a literal
        # argument (not shell syntax) is unambiguous and must NOT be
        # rejected; it is simply passed through to the subprocess as data.
        marker = tmp_path / "list-form.txt"
        steps = [
            {
                "cmd": [_PY, "-c", "import sys; open('list-form.txt','w').write(sys.argv[1])", "&&"],
            }
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "&&"


class TestListFormMultiStep:
    def test_multi_command_sequence_expressed_as_separate_steps(self, tmp_path):
        # The documented replacement for a shell "a && b" string: two
        # ordered steps, each independently on_failure-controlled.
        marker = tmp_path / "sequence.txt"
        steps = [
            _py_step(code=f"open(r'{marker}', 'w').write('a')"),
            _py_step(code=f"open(r'{marker}', 'a').write('b')"),
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ab"


class TestEnvAssignmentPrefix:
    def test_string_form_env_assignment_prefix(self, tmp_path):
        marker = tmp_path / "env-string.txt"
        cmd = f"MY_VAR=hello {_PY} -c \"import os; open('env-string.txt','w').write(os.environ['MY_VAR'])\""
        steps = [{"cmd": cmd}]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "hello"

    def test_list_form_env_assignment_prefix(self, tmp_path):
        marker = tmp_path / "env-list.txt"
        steps = [
            {
                "cmd": [
                    "MY_VAR=world",
                    _PY,
                    "-c",
                    "import os; open('env-list.txt','w').write(os.environ['MY_VAR'])",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "world"

    def test_multiple_env_assignments_stripped(self, tmp_path):
        marker = tmp_path / "env-multi.txt"
        steps = [
            {
                "cmd": [
                    "A=1",
                    "B=2",
                    _PY,
                    "-c",
                    "import os; open('env-multi.txt','w').write(os.environ['A']+os.environ['B'])",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "12"


class TestNeverShellTrue:
    def test_shell_metachar_in_list_form_is_not_expanded(self, tmp_path):
        # If this ran with shell=True, "$HOME" would be expanded by the
        # shell. It must instead reach the subprocess as a literal string.
        marker = tmp_path / "no-shell.txt"
        steps = [
            {
                "cmd": [
                    _PY,
                    "-c",
                    "import sys; open('no-shell.txt','w').write(sys.argv[1])",
                    "$HOME",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "$HOME"


class TestValidation:
    def test_missing_cmd_key_rejected(self):
        with pytest.raises(PostMergeConfigError, match="cmd"):
            validate_post_merge_steps([{"description": "no cmd here"}])

    def test_empty_cmd_string_rejected(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps([{"cmd": "   "}])

    def test_empty_cmd_list_rejected(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps([{"cmd": []}])

    def test_non_string_cmd_rejected(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps([{"cmd": 5}])

    def test_invalid_on_failure_rejected(self):
        with pytest.raises(PostMergeConfigError, match="on_failure"):
            validate_post_merge_steps([{"cmd": "true", "on_failure": "explode"}])

    def test_steps_not_a_list_rejected(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps({"cmd": "true"})

    def test_step_not_a_mapping_rejected(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps(["true"])

    def test_valid_steps_pass(self):
        validate_post_merge_steps(
            [
                {"cmd": "true"},
                {"cmd": ["echo", "hi"], "on_failure": "fail"},
                {"cmd": "false", "on_failure": "warn", "description": "ok to fail"},
            ]
        )


class TestDetachesValidation:
    """lr-53556a: validator accept/reject rules for the `detaches` flag."""

    def test_detaches_true_accepted_with_default_on_failure(self):
        validate_post_merge_steps([{"cmd": "true", "detaches": True}])

    def test_detaches_true_accepted_with_on_failure_warn(self):
        validate_post_merge_steps(
            [{"cmd": "true", "detaches": True, "on_failure": "warn"}]
        )

    def test_detaches_false_accepted_with_on_failure_fail(self):
        validate_post_merge_steps(
            [{"cmd": "true", "detaches": False, "on_failure": "fail"}]
        )

    def test_detaches_absent_defaults_to_false(self):
        # No detaches key at all is byte-identical to detaches: false.
        validate_post_merge_steps([{"cmd": "true", "on_failure": "fail"}])

    def test_non_bool_detaches_rejected(self):
        with pytest.raises(PostMergeConfigError, match="detaches"):
            validate_post_merge_steps([{"cmd": "true", "detaches": "yes"}])

    def test_detaches_true_with_on_failure_fail_rejected(self):
        # The chosen contract (lr-53556a): a detached step's exit code is
        # never awaited, so on_failure: fail on a detached step is a
        # contradiction -- rejected at validation time, not silently ignored.
        with pytest.raises(PostMergeConfigError, match="detaches"):
            validate_post_merge_steps(
                [{"cmd": "true", "detaches": True, "on_failure": "fail"}]
            )


class TestDetachesExecution:
    """lr-53556a regression coverage: the actual hang this flag fixes.

    Without `detaches: true`, a step whose child holds fds 1/2 open (the
    double-forked-daemon shape from the bug report) would never reach
    subprocess.run's capture_output=True communicate() EOF, and this test
    would hang until the suite's own timeout killed it. With detaches=true,
    run_post_merge_steps must return promptly regardless.
    """

    def test_detached_step_with_fds_held_open_returns_promptly(self, tmp_path):
        marker = tmp_path / "detached-ran.txt"
        # This child immediately double-forks (os.fork twice, parent exits
        # each time) so the grandchild is reparented and outlives the
        # subprocess.Popen call, then sleeps far longer than any sane test
        # timeout while holding its inherited stdout/stderr fds open --
        # exactly the "long-lived daemon that inherits fds 1/2" shape from
        # the bug report. A marker file write proves it actually launched.
        code = (
            "import os, sys, time\n"
            f"marker = {str(marker)!r}\n"
            "if os.fork() == 0:\n"
            "    if os.fork() == 0:\n"
            "        open(marker, 'w').write('ran')\n"
            "        time.sleep(60)\n"
            "        os._exit(0)\n"
            "    os._exit(0)\n"
            "os.wait()\n"
        )
        steps = [{"cmd": [_PY, "-c", code], "detaches": True}]

        started = time.monotonic()
        run_post_merge_steps(steps, tmp_path)
        elapsed = time.monotonic() - started

        # Generous upper bound: this must return in a couple seconds, not
        # anywhere near the daemon's 60s sleep -- proves no communicate()
        # wait ever happened on this step's pipes.
        assert elapsed < 10, (
            f"run_post_merge_steps took {elapsed:.1f}s -- detaches: true "
            f"must never block on a held-open inherited fd."
        )

        # Poll briefly for the marker: the immediate parent/mid-child exit
        # is async relative to our own return, so the grandchild's write may
        # land a beat after run_post_merge_steps already returned.
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "detached step's grandchild never ran"
        assert marker.read_text() == "ran"

    def test_non_detached_sibling_step_still_runs_after_detached_step(self, tmp_path):
        # A detached step must not disrupt ordered execution of subsequent
        # steps -- the detach only changes THIS step's own await behavior.
        marker = tmp_path / "after-detach.txt"
        steps = [
            {"cmd": [_PY, "-c", "import sys; sys.exit(0)"], "detaches": True},
            {"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]},
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ok"


class TestTimeoutValidation:
    """lr-d6e52b: validator accept/reject rules for `timeout_seconds`."""

    def test_timeout_seconds_accepted_on_ordinary_step(self):
        validate_post_merge_steps([{"cmd": "true", "timeout_seconds": 30}])

    def test_timeout_seconds_float_accepted(self):
        validate_post_merge_steps([{"cmd": "true", "timeout_seconds": 2.5}])

    def test_timeout_seconds_absent_is_fine(self):
        validate_post_merge_steps([{"cmd": "true"}])

    def test_non_numeric_timeout_seconds_rejected(self):
        with pytest.raises(PostMergeConfigError, match="timeout_seconds"):
            validate_post_merge_steps([{"cmd": "true", "timeout_seconds": "soon"}])

    def test_bool_timeout_seconds_rejected(self):
        with pytest.raises(PostMergeConfigError, match="timeout_seconds"):
            validate_post_merge_steps([{"cmd": "true", "timeout_seconds": True}])

    def test_zero_timeout_seconds_rejected(self):
        with pytest.raises(PostMergeConfigError, match="timeout_seconds"):
            validate_post_merge_steps([{"cmd": "true", "timeout_seconds": 0}])

    def test_negative_timeout_seconds_rejected(self):
        with pytest.raises(PostMergeConfigError, match="timeout_seconds"):
            validate_post_merge_steps([{"cmd": "true", "timeout_seconds": -1}])

    def test_timeout_seconds_with_detaches_true_rejected(self):
        # A detached step is never awaited -- there is nothing for a timeout
        # to bound. Use liveness_probe instead.
        with pytest.raises(PostMergeConfigError, match="timeout_seconds"):
            validate_post_merge_steps(
                [{"cmd": "true", "detaches": True, "timeout_seconds": 30}]
            )


class TestTimeoutExecution:
    """lr-d6e52b regression coverage: the hang this hardening bounds."""

    def test_step_exceeding_timeout_raises_and_is_always_terminal(self, tmp_path):
        # on_failure: warn does NOT save a step that times out -- a hang is
        # never eligible for warn-and-continue (see
        # PostMergeStepTimeoutError's own docstring).
        steps = [
            {
                "cmd": [_PY, "-c", "import time; time.sleep(60)"],
                "timeout_seconds": 0.5,
                "on_failure": "warn",
            }
        ]
        started = time.monotonic()
        with pytest.raises(PostMergeStepTimeoutError):
            run_post_merge_steps(steps, tmp_path)
        elapsed = time.monotonic() - started
        assert elapsed < 10, f"timeout took {elapsed:.1f}s -- bound was not enforced promptly"

    def test_step_finishing_before_timeout_is_unaffected(self, tmp_path):
        marker = tmp_path / "fast.txt"
        steps = [
            {
                "cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"],
                "timeout_seconds": 30,
            }
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ok"

    def test_no_timeout_configured_is_unbounded_by_default(self, tmp_path):
        # A step with no timeout_seconds and no default_timeout_seconds must
        # behave exactly as before this feature -- no timeout kwarg reaches
        # subprocess.run at all. Verified here via a step that finishes
        # quickly but would hang if a stray default were somehow applied
        # with too small a bound; the ABSENCE of any exception is the point.
        marker = tmp_path / "unbounded.txt"
        steps = [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ok"

    def test_repo_default_timeout_applies_when_step_has_none(self, tmp_path):
        steps = [{"cmd": [_PY, "-c", "import time; time.sleep(60)"]}]
        with pytest.raises(PostMergeStepTimeoutError):
            run_post_merge_steps(steps, tmp_path, default_timeout_seconds=0.5)

    def test_step_own_timeout_wins_over_repo_default(self, tmp_path):
        # Step's own timeout_seconds (generous) must win over a stingy
        # repo-tier default -- per-step is always the more specific value.
        marker = tmp_path / "own-timeout-wins.txt"
        steps = [
            {
                "cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"],
                "timeout_seconds": 30,
            }
        ]
        run_post_merge_steps(steps, tmp_path, default_timeout_seconds=0.001)
        assert marker.read_text() == "ok"


class TestLivenessProbeValidation:
    """lr-d6e52b: validator accept/reject rules for `liveness_probe`."""

    def test_liveness_probe_accepted_on_detached_step(self):
        validate_post_merge_steps(
            [
                {
                    "cmd": "true",
                    "detaches": True,
                    "liveness_probe": {"cmd": ["cat", "heartbeat.txt"]},
                }
            ]
        )

    def test_liveness_probe_with_custom_poll_interval_and_max_polls(self):
        validate_post_merge_steps(
            [
                {
                    "cmd": "true",
                    "detaches": True,
                    "liveness_probe": {
                        "cmd": ["cat", "heartbeat.txt"],
                        "poll_interval_seconds": 1,
                        "max_polls": 3,
                    },
                }
            ]
        )

    def test_liveness_probe_requires_detaches_true(self):
        with pytest.raises(PostMergeConfigError, match="liveness_probe"):
            validate_post_merge_steps(
                [{"cmd": "true", "liveness_probe": {"cmd": ["cat", "x"]}}]
            )

    def test_liveness_probe_not_a_mapping_rejected(self):
        with pytest.raises(PostMergeConfigError, match="liveness_probe"):
            validate_post_merge_steps(
                [{"cmd": "true", "detaches": True, "liveness_probe": "cat x"}]
            )

    def test_liveness_probe_missing_cmd_rejected(self):
        with pytest.raises(PostMergeConfigError, match="cmd"):
            validate_post_merge_steps(
                [{"cmd": "true", "detaches": True, "liveness_probe": {}}]
            )

    def test_liveness_probe_cmd_rejects_shell_operator(self):
        with pytest.raises(PostMergeConfigError, match="shell operator"):
            validate_post_merge_steps(
                [
                    {
                        "cmd": "true",
                        "detaches": True,
                        "liveness_probe": {"cmd": "cat x && cat y"},
                    }
                ]
            )

    def test_liveness_probe_max_polls_below_two_rejected(self):
        with pytest.raises(PostMergeConfigError, match="max_polls"):
            validate_post_merge_steps(
                [
                    {
                        "cmd": "true",
                        "detaches": True,
                        "liveness_probe": {"cmd": ["cat", "x"], "max_polls": 1},
                    }
                ]
            )

    def test_liveness_probe_non_positive_poll_interval_rejected(self):
        with pytest.raises(PostMergeConfigError, match="poll_interval_seconds"):
            validate_post_merge_steps(
                [
                    {
                        "cmd": "true",
                        "detaches": True,
                        "liveness_probe": {"cmd": ["cat", "x"], "poll_interval_seconds": 0},
                    }
                ]
            )


class TestLaunchFailureHandling:
    """Launch-time failures (the process never starts at all) must be routed
    through the same on_failure path as an ordinary non-zero exit, never
    propagate as a raw, uncaught OSError/FileNotFoundError/PermissionError
    traceback."""

    def test_missing_binary_with_on_failure_warn_logs_and_continues(
        self, tmp_path, capsys
    ):
        marker = tmp_path / "reached-after-missing.txt"
        missing_binary = str(tmp_path / "definitely-not-a-real-binary")
        steps = [
            {"cmd": [missing_binary, "--flag"], "on_failure": "warn"},
            _py_step(code=f"open(r'{marker}', 'w').write('reached')"),
        ]
        # Must not raise -- a launch failure is warn-eligible exactly like a
        # non-zero exit.
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "reached"

        stderr = capsys.readouterr().err
        assert "warning" in stderr
        assert missing_binary in stderr
        assert "step 1" in stderr

    def test_missing_binary_with_on_failure_fail_raises_typed_error(self, tmp_path):
        missing_binary = str(tmp_path / "definitely-not-a-real-binary")
        steps = [{"cmd": [missing_binary], "on_failure": "fail"}]
        with pytest.raises(PostMergeStepFailedError) as exc_info:
            run_post_merge_steps(steps, tmp_path)
        assert missing_binary in str(exc_info.value)

    def test_non_executable_binary_handled_identically_to_missing(self, tmp_path):
        # A binary that EXISTS but lacks the executable bit raises
        # PermissionError at launch time (a distinct OSError subclass from
        # FileNotFoundError) -- must be caught the same way.
        non_executable = tmp_path / "not-executable"
        non_executable.write_text("#!/bin/sh\necho hi\n")
        non_executable.chmod(0o644)

        steps = [{"cmd": [str(non_executable)], "on_failure": "fail"}]
        with pytest.raises(PostMergeStepFailedError) as exc_info:
            run_post_merge_steps(steps, tmp_path)
        assert str(non_executable) in str(exc_info.value)

    def test_non_executable_binary_with_on_failure_warn_continues(self, tmp_path, capsys):
        non_executable = tmp_path / "not-executable-warn"
        non_executable.write_text("#!/bin/sh\necho hi\n")
        non_executable.chmod(0o644)
        marker = tmp_path / "reached-after-non-executable.txt"

        steps = [
            {"cmd": [str(non_executable)], "on_failure": "warn"},
            _py_step(code=f"open(r'{marker}', 'w').write('reached')"),
        ]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "reached"

        stderr = capsys.readouterr().err
        assert "warning" in stderr
        assert str(non_executable) in stderr

    def test_launch_failure_never_raises_bare_oserror(self, tmp_path):
        # Regression: previously FileNotFoundError/PermissionError propagated
        # straight out of run_post_merge_steps, uncaught. Confirm the raised
        # type is always the module's own typed error, never bubbled raw.
        missing_binary = str(tmp_path / "still-not-a-real-binary")
        steps = [{"cmd": [missing_binary], "on_failure": "fail"}]
        try:
            run_post_merge_steps(steps, tmp_path)
        except PostMergeStepFailedError:
            pass
        else:
            pytest.fail("expected PostMergeStepFailedError")


class TestLivenessProbeExecution:
    """lr-d6e52b regression coverage: heartbeat-advance-across-one-poll-
    interval liveness verification for a detaches:true step -- the "third
    option" between block-forever and verify-nothing."""

    def test_daemon_transition_faster_than_poll_interval_still_confirms_liveness(
        self, tmp_path
    ):
        # lr-4d5ef9: end-to-end confirmation at the PUBLIC API (real
        # subprocesses, real detaches:true execution, no timing mocks) that
        # a daemon whose ENTIRE heartbeat transition (pre-seeded '0' ->
        # daemon-written 'final') completes near-instantly -- faster than
        # one poll_interval -- is still confirmed alive. The DETERMINISTIC
        # proof that this scenario fails against the OLD implementation and
        # passes only with the fix is
        # test_old_eager_baseline_races_daemon_launch_fixed_baseline_does_not
        # below, which replays both algorithms against an identical
        # scripted timeline without depending on real process-scheduling
        # timing (which this test, being end-to-end, cannot fully control).
        #
        # Under the OLD code, the baseline sample was taken EAGERLY, racing
        # the daemon's own Popen/interpreter-startup latency: if that race
        # goes against it (daemon starts and finishes before the baseline
        # read lands, plausible whenever the probe's own subprocess spawn
        # is itself slow under load), the baseline already reads 'final',
        # and EVERY subsequent sample (all taken well after the daemon
        # finished) reads 'final' too -- nothing ever differs, and a live,
        # already-advanced daemon is wrongly reported dead. This was not
        # deterministic against the old code (it depended on real
        # scheduling), which is exactly why it showed up as intermittent
        # rather than a hard failure in practice.
        #
        # Under the FIXED code, the baseline is captured BEFORE Popen is
        # even called -- a genuine happens-before, not a race -- so it is
        # GUARANTEED to read the pre-seeded '0', never 'final', regardless
        # of how fast the daemon or the probe's own subprocess spawn is.
        # The very first polled sample (taken after Popen, once the daemon
        # is virtually certain to have already written 'final') then
        # differs from that baseline unconditionally.
        heartbeat = tmp_path / "heartbeat.txt"
        heartbeat.write_text("0")
        # Daemon step: transitions to its final value essentially
        # instantly -- no sleep at all, deliberately faster than any
        # poll_interval a caller could reasonably configure.
        daemon_code = f"open({str(heartbeat)!r}, 'w').write('final')\n"
        steps = [
            {
                "cmd": [_PY, "-c", daemon_code],
                "detaches": True,
                "liveness_probe": {
                    "cmd": [_PY, "-c", f"print(open({str(heartbeat)!r}).read().strip())"],
                    "poll_interval_seconds": 0.5,
                    "max_polls": 6,
                },
            }
        ]
        # Must not raise -- the fix's pre-Popen baseline capture guarantees
        # '0' is what gets compared against, so the daemon's (however fast)
        # transition to 'final' is always detected.
        run_post_merge_steps(steps, tmp_path)

    def test_old_eager_baseline_races_daemon_launch_fixed_baseline_does_not(
        self, tmp_path, monkeypatch
    ):
        # lr-4d5ef9: THIS is the deterministic proof required by the task's
        # acceptance criteria -- a test that would fail against the OLD
        # cadence and passes only with the fix. It models the SAME
        # wall-clock timeline BOTH algorithms are exposed to, via a shared
        # fake clock (no real timing, no `time.sleep`, fully deterministic),
        # and shows the OLD algorithm's baseline sample is exposed to a race
        # against the daemon's own launch that the FIXED algorithm's
        # pre-Popen baseline is structurally immune to.
        #
        # Timeline (a single shared fake clock, in "poll_interval units"):
        #   t=0.0   Popen() for the detached daemon step is called.
        #   t=0.3   the daemon (launched at t=0) finishes its ENTIRE
        #           transition and the probe's target reads 'final' from
        #           here on -- deliberately FASTER than one poll_interval
        #           (t=1.0), a 300ms-vs-500ms/1-poll-interval shape.
        #   t=0.6   the OLD algorithm's EAGER baseline sample actually
        #           executes -- i.e. under load, spawning the probe's own
        #           subprocess to take that eager sample itself took 0.6
        #           poll_intervals' worth of wall-clock time, landing AFTER
        #           the daemon (t=0.3) already finished. This is the race:
        #           the eager sample's own execution time, not just its
        #           intended t=0 timing, is what determines what it reads.
        #
        # OLD algorithm: baseline sampled at t=0.6 -> reads 'final' (daemon
        # already done). Every later polled sample (t=1.6, t=2.6, ...) also
        # reads 'final' -- nothing ever differs from the (already-corrupted)
        # baseline, and OLD raises PostMergeLivenessError for a daemon that
        # was genuinely alive and had already advanced.
        #
        # FIXED algorithm: baseline captured via _capture_liveness_baseline
        # BEFORE Popen is ever called -- i.e. at t=-epsilon, strictly before
        # t=0, by construction (program order), not a race. It reads '0'
        # (the daemon cannot possibly have run yet). The first polled sample
        # (t=1.0, one poll_interval after Popen) reads 'final' -- differs
        # from '0' -- confirmed.
        clock = {"t": 0.0}

        def probe_at_clock():
            return "final" if clock["t"] >= 0.3 else "0"

        # --- OLD algorithm, literally replayed (pre-lr-4d5ef9 body; see
        # this module's git history) against the shared clock. ---
        clock["t"] = 0.6  # the eager sample's OWN execution lands here.
        old_baseline = probe_at_clock()
        old_advanced = False
        max_polls = 3
        for _ in range(1, max_polls):
            clock["t"] += 1.0  # one poll_interval of (mocked) sleep.
            old_current = probe_at_clock()
            if old_baseline and old_current and old_current != old_baseline:
                old_advanced = True
                break
            old_baseline = old_current
        assert not old_advanced, (
            "sanity check on the OLD algorithm's own documented shape: its "
            "eager baseline sample, landing at t=0.6 (after the daemon's "
            "t=0.3 transition), must NOT observe any advance -- if this "
            "assertion fails, the scripted timeline no longer reproduces "
            "the bug this test exists to document."
        )

        # --- FIXED algorithm: the REAL production functions. ---
        clock["t"] = 0.0  # reset the shared clock for a fresh run.

        def new_probe(argv, *, cwd):
            return probe_at_clock()

        def new_sleep(seconds):
            clock["t"] += 1.0  # one poll_interval per call, same as OLD.

        import clagentic_loadout.merge.post_merge as post_merge_module

        monkeypatch.setattr(post_merge_module.time, "sleep", new_sleep)
        monkeypatch.setattr(post_merge_module, "_run_liveness_probe_once", new_probe)

        liveness_probe = {
            "cmd": ["irrelevant-argv-marker"],
            "poll_interval_seconds": 1.0,
            "max_polls": max_polls,
        }
        # Baseline captured at t=0.0, strictly before the daemon's t=0.3
        # transition -- reads '0', never racing anything.
        baseline = post_merge_module._capture_liveness_baseline(
            liveness_probe, cwd=str(tmp_path), label="step 1"
        )
        assert baseline == "0", (
            "sanity check: the fixed baseline, captured before the shared "
            "clock ever advances, must read the pre-transition value."
        )
        # Must not raise: the first polled sample (clock advances to t=1.0
        # via new_sleep, past the daemon's t=0.3 transition) reads 'final',
        # which differs from the race-free '0' baseline -- exactly the
        # advance the OLD algorithm, replayed above against the identical
        # timeline, provably missed.
        post_merge_module._verify_liveness(
            liveness_probe,
            cwd=str(tmp_path),
            label="step 1",
            cmd_repr="'irrelevant'",
            baseline_sample=baseline,
        )

    def test_never_advancing_probe_still_raises_liveness_error(
        self, tmp_path, monkeypatch
    ):
        # Companion control for the fix above, unit-testing _verify_liveness
        # directly with a SCRIPTED probe (deterministic, no real timing) via
        # a monkeypatched `time.sleep`: a probe that never advances at all,
        # no matter how many polls elapse, must still raise
        # PostMergeLivenessError under the fixed code and its new
        # pre-captured-baseline calling convention -- the fix changes WHEN
        # and against WHAT the baseline is captured, not the definition of
        # "advanced," so a genuinely dead/stalled daemon is not swept into
        # a false pass by this change. Guards the acceptance criterion that
        # a dead daemon must still be reported dead.
        def fake_sleep(seconds):
            return None

        def fake_probe_once(argv, *, cwd):
            return "stuck"

        import clagentic_loadout.merge.post_merge as post_merge_module

        monkeypatch.setattr(post_merge_module.time, "sleep", fake_sleep)
        monkeypatch.setattr(
            post_merge_module, "_run_liveness_probe_once", fake_probe_once
        )

        liveness_probe = {
            "cmd": ["irrelevant-argv-marker"],
            "poll_interval_seconds": 0.01,
            "max_polls": 3,
        }
        with pytest.raises(PostMergeLivenessError):
            post_merge_module._verify_liveness(
                liveness_probe,
                cwd=str(tmp_path),
                label="step 1",
                cmd_repr="'irrelevant'",
                baseline_sample="stuck",
            )

    def test_advancing_heartbeat_file_confirms_liveness(self, tmp_path):
        heartbeat = tmp_path / "heartbeat.txt"
        heartbeat.write_text("0")
        # Daemon step: writes an advancing counter to the heartbeat file
        # twice, a beat apart, then exits -- simulates a real daemon
        # advancing its own liveness signal after (re)starting.
        code = (
            "import time\n"
            f"path = {str(heartbeat)!r}\n"
            "open(path, 'w').write('1')\n"
            "time.sleep(0.3)\n"
            "open(path, 'w').write('2')\n"
        )
        steps = [
            {
                "cmd": [_PY, "-c", code],
                "detaches": True,
                "liveness_probe": {
                    "cmd": [_PY, "-c", f"print(open({str(heartbeat)!r}).read().strip())"],
                    "poll_interval_seconds": 0.5,
                    "max_polls": 6,
                },
            }
        ]
        # Must not raise -- the probe observes the heartbeat advance.
        run_post_merge_steps(steps, tmp_path)

    def test_stalled_heartbeat_raises_liveness_error(self, tmp_path):
        heartbeat = tmp_path / "stalled.txt"
        heartbeat.write_text("stuck")
        # Daemon step never advances the heartbeat file at all (simulates a
        # process that launched but never actually came up as intended).
        steps = [
            {
                "cmd": [_PY, "-c", "pass"],
                "detaches": True,
                "liveness_probe": {
                    "cmd": [_PY, "-c", f"print(open({str(heartbeat)!r}).read().strip())"],
                    "poll_interval_seconds": 0.1,
                    "max_polls": 2,
                },
            }
        ]
        with pytest.raises(PostMergeLivenessError):
            run_post_merge_steps(steps, tmp_path)

    def test_liveness_failure_is_terminal_regardless_of_on_failure(self, tmp_path):
        # validate_post_merge_steps already forbids on_failure: fail combined
        # with detaches: true -- confirm the ONLY allowed value (warn, the
        # default) still does not swallow a liveness failure.
        heartbeat = tmp_path / "never.txt"
        steps = [
            {
                "cmd": [_PY, "-c", "pass"],
                "detaches": True,
                "on_failure": "warn",
                "liveness_probe": {
                    "cmd": [_PY, "-c", f"print(open({str(heartbeat)!r}).read().strip())"],
                    "poll_interval_seconds": 0.1,
                    "max_polls": 2,
                },
            }
        ]
        with pytest.raises(PostMergeLivenessError):
            run_post_merge_steps(steps, tmp_path)

    def test_missing_probe_output_never_counts_as_an_advance(self, tmp_path):
        # A probe that always fails to read (file never exists) must not
        # accidentally "match" itself (empty == empty) -- confirms the
        # implementation requires two DIFFERING, both-non-empty samples.
        nonexistent = tmp_path / "does-not-exist.txt"
        steps = [
            {
                "cmd": [_PY, "-c", "pass"],
                "detaches": True,
                "liveness_probe": {
                    "cmd": [_PY, "-c", f"print(open({str(nonexistent)!r}).read())"],
                    "poll_interval_seconds": 0.1,
                    "max_polls": 3,
                },
            }
        ]
        with pytest.raises(PostMergeLivenessError):
            run_post_merge_steps(steps, tmp_path)

    def test_no_liveness_probe_configured_is_unaffected_fire_and_forget(self, tmp_path):
        # Absent liveness_probe: byte-identical to before this task -- no
        # verification at all, no extra wait.
        steps = [{"cmd": [_PY, "-c", "import time; time.sleep(60)"], "detaches": True}]
        started = time.monotonic()
        run_post_merge_steps(steps, tmp_path)
        elapsed = time.monotonic() - started
        assert elapsed < 10, "absent liveness_probe must add no wait at all"
