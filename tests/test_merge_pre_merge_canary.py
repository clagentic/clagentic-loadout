"""test_merge_pre_merge_canary.py -- the operator-configured pre-merge canary.

The command is named in the user-level config's `merge:` section and runs as a
gate step of loadout-merge. Real subprocesses throughout (a mocked runner would
pass while the contract is broken); the git host is a fake opener and every
command is a throwaway script written for the test, so nothing here names a real
repository, host or tool.

Caller shapes covered, each on a real code path:
  1. loadout-merge on GitHub, no canary key           -> unchanged, no canary output
  2. loadout-merge on Forgejo, no canary key          -> unchanged, no canary output
  3. canary configured, exits 0                       -> merge proceeds
  4. canary exits non-zero                            -> refused, reason carries command, exit, tail
  5. canary not found / not executable                -> refused
  6. canary times out                                 -> refused
  7. --report-only with a canary configured           -> canary runs, nothing merges
  8. loadout-post-merge                               -> never runs the canary
"""

from __future__ import annotations

import json
import os
import sys

import pytest
import yaml

from clagentic_loadout.merge import post_merge_verb, verb
from clagentic_loadout.merge.pre_merge_canary import (
    DEFAULT_TIMEOUT_SECONDS,
    ENV_HEAD_SHA,
    ENV_PLATFORM,
    ENV_PR,
    ENV_REPO,
    OUTPUT_TAIL_CHARS,
    PreMergeCanary,
    PreMergeCanaryConfigError,
    PreMergeCanaryRefusedError,
    load_pre_merge_canary,
    run_pre_merge_canary,
)
from clagentic_loadout.transport import provider_config
from tests._gate_repo import git, init_gate_repo
from tests._support.ci_gate import HEAD_SHA, forgejo_ci_opener, github_ci_opener, run_merge
from tests._support.fakes import FakeResponse, RecordingTokenProvider
from tests._support.merge_verb import AllowingAuthorityProvider

_PY = sys.executable


def _script(code: str) -> list[str]:
    return [_PY, "-c", code]


def _write_user_config(root, canary=None, *, raw: str | None = None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    text = raw if raw is not None else yaml.safe_dump({"merge": {"pre_merge_canary": canary}})
    (root / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Point the user-level config root at a per-test directory and return a
    writer for its config file."""
    root = tmp_path / "user-config"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", root)

    def write(canary=None, *, raw=None):
        _write_user_config(root, canary, raw=raw)

    return write


def _record_env_script(path) -> list[str]:
    code = (
        "import json, os\n"
        f"keys = {[ENV_REPO, ENV_PR, ENV_HEAD_SHA, ENV_PLATFORM, 'SOME_SERVICE_TOKEN']!r}\n"
        f"open({str(path)!r}, 'w').write(json.dumps({{k: os.environ.get(k) for k in keys}}))\n"
    )
    return _script(code)


class TestLoading:
    def test_no_config_file_means_no_canary(self, tmp_path):
        assert load_pre_merge_canary(tmp_path / "absent") is None

    def test_a_config_without_the_key_means_no_canary(self, tmp_path):
        _write_user_config(tmp_path, raw=yaml.safe_dump({"merge": {"foreign_config_globs": []}}))
        assert load_pre_merge_canary(tmp_path) is None

    def test_a_config_that_does_not_parse_and_never_names_the_key_means_no_canary(self, tmp_path):
        _write_user_config(tmp_path, raw="merge: [unclosed")
        assert load_pre_merge_canary(tmp_path) is None

    def test_a_declared_canary_loads_with_the_default_timeout(self, tmp_path):
        _write_user_config(tmp_path, {"cmd": ["tool", "--flag"]})
        assert load_pre_merge_canary(tmp_path) == PreMergeCanary(
            argv=("tool", "--flag"), timeout_seconds=DEFAULT_TIMEOUT_SECONDS
        )

    def test_an_explicit_timeout_is_kept(self, tmp_path):
        _write_user_config(tmp_path, {"cmd": ["tool"], "timeout_seconds": 2.5})
        assert load_pre_merge_canary(tmp_path).timeout_seconds == 2.5

    @pytest.mark.parametrize(
        "declared",
        [
            pytest.param("tool --flag", id="not-a-mapping"),
            pytest.param({"cmd": "tool --flag"}, id="cmd-is-a-shell-string"),
            pytest.param({"cmd": []}, id="cmd-empty"),
            pytest.param({"cmd": ["tool", ""]}, id="cmd-empty-token"),
            pytest.param({"cmd": ["tool", 3]}, id="cmd-non-string-token"),
            pytest.param({}, id="cmd-missing"),
            pytest.param({"cmd": ["tool"], "timeout": 5}, id="unknown-key"),
            pytest.param({"cmd": ["tool"], "timeout_seconds": 0}, id="timeout-zero"),
            pytest.param({"cmd": ["tool"], "timeout_seconds": -1}, id="timeout-negative"),
            pytest.param({"cmd": ["tool"], "timeout_seconds": True}, id="timeout-bool"),
            pytest.param({"cmd": ["tool"], "timeout_seconds": "5"}, id="timeout-string"),
            pytest.param({"cmd": ["tool"], "timeout_seconds": float("inf")}, id="timeout-inf"),
        ],
    )
    def test_a_declaration_that_cannot_be_used_is_a_config_error(self, tmp_path, declared):
        _write_user_config(tmp_path, declared)
        with pytest.raises(PreMergeCanaryConfigError, match="pre_merge_canary"):
            load_pre_merge_canary(tmp_path)

    def test_a_config_that_names_the_key_but_does_not_parse_is_a_config_error(self, tmp_path):
        _write_user_config(tmp_path, raw="merge:\n  pre_merge_canary: [unclosed\n")
        with pytest.raises(PreMergeCanaryConfigError, match="not valid YAML"):
            load_pre_merge_canary(tmp_path)

    def test_a_config_that_names_the_key_but_is_not_text_is_a_config_error(self, tmp_path):
        tmp_path.mkdir(exist_ok=True)
        (tmp_path / "config.yaml").write_bytes(b"merge:\n  pre_merge_canary: \xff\xfe\n")
        with pytest.raises(PreMergeCanaryConfigError, match="cannot be read"):
            load_pre_merge_canary(tmp_path)


class TestRunning:
    def _run(self, argv, *, timeout=10, base_env=None):
        run_pre_merge_canary(
            PreMergeCanary(argv=tuple(argv), timeout_seconds=timeout),
            repo="some-owner/some-repo",
            pr_number=7,
            head_sha="c" * 40,
            platform="forgejo",
            base_env=base_env if base_env is not None else dict(os.environ),
        )

    def test_exit_zero_returns_and_the_command_sees_the_merge(self, tmp_path):
        seen = tmp_path / "env.json"
        self._run(_record_env_script(seen))
        assert json.loads(seen.read_text()) == {
            ENV_REPO: "some-owner/some-repo",
            ENV_PR: "7",
            ENV_HEAD_SHA: "c" * 40,
            ENV_PLATFORM: "forgejo",
            "SOME_SERVICE_TOKEN": None,
        }

    def test_credential_shaped_variables_are_withheld(self, tmp_path):
        seen = tmp_path / "env.json"
        self._run(
            _record_env_script(seen),
            base_env={**os.environ, "SOME_SERVICE_TOKEN": "secret-value"},
        )
        assert json.loads(seen.read_text())["SOME_SERVICE_TOKEN"] is None

    def test_a_non_zero_exit_refuses_naming_the_command_the_exit_and_both_tails(self):
        code = "import sys; print('to-stdout'); sys.stderr.write('to-stderr'); sys.exit(3)"
        with pytest.raises(PreMergeCanaryRefusedError) as raised:
            self._run(_script(code))
        message = str(raised.value)
        assert repr(_script(code)) in message
        assert "exited 3" in message
        assert "to-stdout" in message and "to-stderr" in message

    def test_only_the_tail_of_long_output_is_carried(self):
        code = "import sys; sys.stdout.write(chr(81) * 5000 + 'END'); sys.exit(1)"
        with pytest.raises(PreMergeCanaryRefusedError) as raised:
            self._run(_script(code))
        message = str(raised.value)
        assert "END" in message
        assert 100 < message.count("Q") <= OUTPUT_TAIL_CHARS

    def test_a_command_that_is_not_found_refuses(self, tmp_path):
        missing = str(tmp_path / "no-such-canary")
        with pytest.raises(PreMergeCanaryRefusedError, match="could not be started") as raised:
            self._run([missing])
        assert missing in str(raised.value)

    def test_a_file_that_is_not_executable_refuses(self, tmp_path):
        plain = tmp_path / "not-executable"
        plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        plain.chmod(0o644)
        with pytest.raises(PreMergeCanaryRefusedError, match="could not be started"):
            self._run([str(plain)])

    def test_a_command_that_outlives_its_timeout_refuses_with_its_partial_output(self):
        code = "import sys, time; print('partial', flush=True); time.sleep(30)"
        with pytest.raises(PreMergeCanaryRefusedError) as raised:
            self._run(_script(code), timeout=0.5)
        assert "timed out after 0.5s" in str(raised.value)
        assert "partial" in str(raised.value)


def _merge(platform, opener, extra=()):
    return run_merge(opener, platform=platform, extra=extra)


def _opener(platform, merge_calls):
    return (forgejo_ci_opener if platform == "forgejo" else github_ci_opener)(
        merge_calls=merge_calls
    )


class TestNoCanaryKeyChangesNothing:
    """Caller shapes 1 and 2."""

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_no_config_file(self, platform, capsys):
        merge_calls: list = []

        assert _merge(platform, _opener(platform, merge_calls)) == verb.EXIT_OK

        assert len(merge_calls) == 1
        assert "canary" not in capsys.readouterr().err

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_a_config_file_without_the_key(self, platform, user_config, capsys):
        user_config(raw="merge: [unclosed")
        merge_calls: list = []

        assert _merge(platform, _opener(platform, merge_calls)) == verb.EXIT_OK

        assert len(merge_calls) == 1
        assert "canary" not in capsys.readouterr().err


class TestCanaryPasses:
    """Caller shape 3."""

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_exit_zero_lets_the_merge_land_and_the_command_saw_the_merge(
        self, platform, user_config, tmp_path, capsys
    ):
        seen = tmp_path / "env.json"
        user_config({"cmd": _record_env_script(seen)})
        merge_calls: list = []

        assert _merge(platform, _opener(platform, merge_calls)) == verb.EXIT_OK

        assert len(merge_calls) == 1
        assert json.loads(seen.read_text()) == {
            ENV_REPO: "some-owner/some-repo",
            ENV_PR: "1",
            ENV_HEAD_SHA: HEAD_SHA,
            ENV_PLATFORM: platform,
            "SOME_SERVICE_TOKEN": None,
        }
        assert "pre-merge canary -- PASSED" in capsys.readouterr().err

    def test_the_canary_runs_before_the_merge_call(self, user_config, tmp_path):
        marker = tmp_path / "ran"
        user_config({"cmd": _script(f"open({str(marker)!r}, 'w').close()")})
        base = forgejo_ci_opener()
        violations: list[str] = []

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/merge") and not marker.exists():
                violations.append("merge POST fired before the canary ran")
            return base(req, timeout=timeout)

        assert _merge("forgejo", opener) == verb.EXIT_OK
        assert violations == []


class TestCanaryRefuses:
    """Caller shapes 4, 5 and 6."""

    def _refused(self, platform, capsys, extra=()):
        merge_calls: list = []
        code = _merge(platform, _opener(platform, merge_calls), extra)
        assert merge_calls == [], "the merge call must never be reached"
        return code, capsys.readouterr().err

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_a_non_zero_exit_refuses_with_command_exit_and_tail(self, platform, user_config, capsys):
        cmd = _script("import sys; sys.stderr.write('drift detected'); sys.exit(4)")
        user_config({"cmd": cmd})

        code, err = self._refused(platform, capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED
        assert "pre-merge canary REFUSED" in err
        assert repr(cmd) in err and "exited 4" in err and "drift detected" in err

    def test_a_command_that_is_not_found_refuses(self, user_config, tmp_path, capsys):
        missing = str(tmp_path / "no-such-canary")
        user_config({"cmd": [missing]})

        code, err = self._refused("forgejo", capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED
        assert missing in err and "could not be started" in err

    def test_a_file_that_is_not_executable_refuses(self, user_config, tmp_path, capsys):
        plain = tmp_path / "not-executable"
        plain.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        plain.chmod(0o644)
        user_config({"cmd": [str(plain)]})

        code, err = self._refused("github", capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED
        assert str(plain) in err

    @pytest.mark.parametrize("platform", ["github", "forgejo"])
    def test_a_timeout_refuses(self, platform, user_config, capsys):
        user_config({"cmd": _script("import time; time.sleep(30)"), "timeout_seconds": 0.5})

        code, err = self._refused(platform, capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED
        assert "timed out after 0.5s" in err

    def test_a_declaration_that_cannot_be_used_refuses(self, user_config, capsys):
        user_config({"cmd": "tool --flag"})

        code, err = self._refused("forgejo", capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED
        assert "pre-merge canary config FAILED to load" in err

    def test_a_config_naming_the_key_that_does_not_parse_refuses(self, user_config, capsys):
        user_config(raw="merge:\n  pre_merge_canary: [unclosed\n")

        code, _ = self._refused("forgejo", capsys)

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED

    def test_neither_skip_pre_checks_nor_ignore_repo_gate_lifts_it(self, user_config, capsys):
        user_config({"cmd": _script("raise SystemExit(1)")})

        code, _ = self._refused("forgejo", capsys, extra=["--skip-pre-checks", "--ignore-repo-gate"])

        assert code == verb.EXIT_PRE_MERGE_CANARY_FAILED


class TestReportOnly:
    """Caller shape 7: the report-only contract is that the whole gate chain runs
    and only the merge, post-merge steps and attestation are skipped."""

    def test_the_canary_runs_and_nothing_merges(self, user_config, tmp_path, capsys):
        marker = tmp_path / "ran"
        user_config({"cmd": _script(f"open({str(marker)!r}, 'w').close()")})
        merge_calls: list = []

        code = _merge("forgejo", forgejo_ci_opener(merge_calls=merge_calls), ["--report-only"])

        assert code == verb.EXIT_OK
        assert marker.exists()
        assert merge_calls == []
        assert "would be merged" in capsys.readouterr().out

    def test_a_failing_canary_returns_the_code_a_real_run_returns(self, user_config):
        user_config({"cmd": _script("raise SystemExit(1)")})

        real = _merge("forgejo", forgejo_ci_opener())
        report = _merge("forgejo", forgejo_ci_opener(), ["--report-only"])

        assert real == report == verb.EXIT_PRE_MERGE_CANARY_FAILED

    def test_without_a_canary_report_only_prints_no_canary_line(self, capsys):
        assert _merge("forgejo", forgejo_ci_opener(), ["--report-only"]) == verb.EXIT_OK
        assert "canary" not in capsys.readouterr().err


class TestPostMergeNeverRunsTheCanary:
    """Caller shape 8: loadout-post-merge re-runs post_merge_steps for a PR that
    already merged. It is not a gate and must not run the canary."""

    def test_post_merge_runs_its_steps_and_not_the_canary(self, user_config, tmp_path):
        canary_marker = tmp_path / "canary-ran"
        step_marker = tmp_path / "step-ran"
        user_config({"cmd": _script(f"open({str(canary_marker)!r}, 'w').close()")})
        repo = init_gate_repo(
            tmp_path / "repo",
            tracked_gate={},
            deployment_merge={
                "sync_tree_after_merge": True,
                "post_merge_steps": [
                    {"cmd": _script(f"open({str(step_marker)!r}, 'w').close()"), "on_failure": "fail"}
                ],
            },
        )
        origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        git(repo.path, "remote", "add", "origin", str(origin))
        git(repo.path, "push", "-q", "origin", "main")
        pr_info = {"merged": True, "merge_commit_sha": None, "base": {"ref": "main"}}

        def opener(req, timeout=15):
            assert req.get_method() == "GET", req.full_url
            return FakeResponse(200, json.dumps(pr_info).encode("utf-8"))

        code = post_merge_verb.main(
            [
                "--platform", "forgejo", "--role", "merger", "--authorized-role", "merger",
                "--repo", "some-owner/some-repo", "--pr", "1", "--repo-path", str(repo.path),
            ],
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=opener,
        )

        assert code == post_merge_verb.EXIT_OK
        assert step_marker.exists()
        assert not canary_marker.exists()
