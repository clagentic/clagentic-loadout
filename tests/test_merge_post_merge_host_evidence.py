"""PASS/FAIL lines name the executing host and say whether the outcome was
confirmed; the optional `verify` key turns an exit-code-only PASS into a
confirmed one without changing any existing step list."""

from __future__ import annotations

import platform
import sys

import pytest

from clagentic_loadout.merge.post_merge import (
    PostMergeConfigError,
    PostMergeStepFailedError,
    run_post_merge_steps,
    validate_post_merge_steps,
)

_PY = sys.executable


def _lines(capsys) -> list[str]:
    return capsys.readouterr().err.splitlines()


def _result_line(capsys, word: str) -> str:
    return next(line for line in _lines(capsys) if f": {word} (" in line)


class TestHostAndOutcomeOnTheLogLine:
    def test_pass_line_names_host_and_marks_exit_code_only_as_unverified(self, tmp_path, capsys):
        run_post_merge_steps([{"cmd": [_PY, "-c", "pass"]}], tmp_path)
        line = _result_line(capsys, "PASS")
        assert "PASS (exit=0, cwd=" in line
        assert f"host={platform.node()!r}" in line
        assert "outcome=unverified" in line

    def test_fail_line_names_host(self, tmp_path, capsys):
        run_post_merge_steps([{"cmd": [_PY, "-c", "import sys; sys.exit(3)"]}], tmp_path)
        line = _result_line(capsys, "FAIL")
        assert "FAIL (exit=3, cwd=" in line
        assert f"host={platform.node()!r}" in line

    def test_host_comes_from_the_platform(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(platform, "node", lambda: "example-host")
        run_post_merge_steps([{"cmd": [_PY, "-c", "pass"]}], tmp_path)
        assert "host='example-host'" in _result_line(capsys, "PASS")

    def test_an_unreported_hostname_is_still_named(self, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(platform, "node", lambda: "")
        run_post_merge_steps([{"cmd": [_PY, "-c", "pass"]}], tmp_path)
        assert "host='unknown-host'" in _result_line(capsys, "PASS")


class TestExistingStepListsAreUnchanged:
    """A step list shaped like a real consumer's (env prefix, string cmd,
    warn and fail gating, no new keys) validates and runs exactly as before."""

    STEPS = [
        {"cmd": f"FOO=bar {_PY} -c 'import os, sys; sys.exit(0 if os.environ[\"FOO\"] == \"bar\" else 1)'",
         "description": "env-prefixed string cmd", "on_failure": "fail"},
        {"cmd": [_PY, "-c", "import sys; sys.exit(2)"], "on_failure": "warn"},
        {"cmd": f"{_PY} -c pass", "timeout_seconds": 30},
    ]

    def test_validates_without_any_new_key(self):
        validate_post_merge_steps(self.STEPS)

    def test_runs_with_the_same_gating(self, tmp_path, capsys):
        run_post_merge_steps(self.STEPS, tmp_path)
        err = capsys.readouterr().err
        assert err.count(": PASS (exit=0") == 2
        assert err.count(": FAIL (exit=2") == 1
        assert "warning: command exited 2, continuing" in err
        assert "outcome=confirmed" not in err

    def test_fail_gated_step_still_raises(self, tmp_path):
        with pytest.raises(PostMergeStepFailedError):
            run_post_merge_steps(
                [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}], tmp_path
            )


class TestVerify:
    def test_passing_verify_marks_the_step_confirmed_and_logs_evidence(self, tmp_path, capsys):
        run_post_merge_steps(
            [{"cmd": [_PY, "-c", "pass"], "verify": [_PY, "-c", "print('sha256=abc')"]}], tmp_path
        )
        line = _result_line(capsys, "PASS")
        assert "outcome=confirmed" in line
        assert "sha256=abc" in line

    def test_failing_verify_is_terminal_even_when_the_step_is_warn_gated(self, tmp_path, capsys):
        with pytest.raises(PostMergeStepFailedError, match="did not confirm"):
            run_post_merge_steps(
                [
                    {
                        "cmd": [_PY, "-c", "pass"],
                        "on_failure": "warn",
                        "verify": [_PY, "-c", "import sys; sys.stderr.write('missing'); sys.exit(1)"],
                    }
                ],
                tmp_path,
            )
        line = _result_line(capsys, "FAIL")
        assert "outcome=verification-failed" in line
        assert "missing" in line

    def test_verify_launch_failure_is_a_failed_verification(self, tmp_path):
        with pytest.raises(PostMergeStepFailedError, match="did not confirm"):
            run_post_merge_steps(
                [{"cmd": [_PY, "-c", "pass"], "verify": ["/nonexistent/verifier"]}], tmp_path
            )

    def test_verify_is_not_run_when_the_step_itself_failed(self, tmp_path, capsys):
        marker = tmp_path / "verified"
        run_post_merge_steps(
            [
                {
                    "cmd": [_PY, "-c", "import sys; sys.exit(1)"],
                    "verify": [_PY, "-c", f"open({str(marker)!r}, 'w')"],
                }
            ],
            tmp_path,
        )
        assert not marker.exists()
        assert "FAIL (exit=1" in capsys.readouterr().err

    def test_verify_rejected_on_a_detached_step(self):
        with pytest.raises(PostMergeConfigError, match="verify"):
            validate_post_merge_steps(
                [{"cmd": "true", "detaches": True, "verify": ["true"]}]
            )

    def test_verify_rejects_a_shell_operator_string(self):
        with pytest.raises(PostMergeConfigError):
            validate_post_merge_steps([{"cmd": "true", "verify": "a && b"}])
