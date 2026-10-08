"""A pre_check child process does not receive identity, attestation or
credential variables from the merger's environment.

The scrub is unit-tested on explicit mappings (so no test depends on the real
process environment) and then exercised end to end: the merge verb runs real
commands in a real merge-result clone and the commands themselves report
what they can see."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.pre_check_env import (
    CREDENTIAL_NAME_PATTERNS,
    GIT_LOCATION_ENV_NAMES,
    is_denied_pre_check_name,
    pre_check_env,
)
from clagentic_loadout.merge.pre_checks_config import (
    PassthroughDecision,
    decide_pre_checks_env_passthrough,
    resolve_pre_checks_env_passthrough,
)
from clagentic_loadout.transport.attestation import (
    ATTESTED_IDENTITY_ENV_VAR,
    ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR,
    attestation_env_var_names,
)
from tests._gate_repo import GateRepo, git, init_gate_repo, write_deployment_config
from tests._support.gate_merge import run_gate_merge as _merge

_PY = sys.executable


def _write_user_config(root: Path, attestation_section: dict) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(
        yaml.safe_dump({"attestation": attestation_section}), encoding="utf-8"
    )
    return root


class TestAttestationNamesComeFromTheResolverCode:
    def test_the_env_tier_variables_are_always_named(self, tmp_path):
        names = attestation_env_var_names(env={}, config_root=tmp_path)
        assert {ATTESTED_IDENTITY_ENV_VAR, ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} <= names

    def test_configured_identity_env_and_sidecar_session_envs_are_named(self, tmp_path):
        root = _write_user_config(
            tmp_path / "cfg",
            {
                "identity_env": "WHO_AM_I",
                "sidecars": [
                    {"dir": "/x", "file_prefix": "a", "session_id_env": "CLAGENTIC_SUBAGENT_ID"},
                    {"dir": "/y", "file_prefix": "b", "session_id_env": "CLAUDE_CODE_SESSION_ID"},
                    {"dir": "/z", "file_prefix": "c"},
                ],
            },
        )
        names = attestation_env_var_names(env={}, config_root=root)
        assert {"WHO_AM_I", "CLAGENTIC_SUBAGENT_ID", "CLAUDE_CODE_SESSION_ID"} <= names

    def test_a_variable_named_by_the_env_override_is_named(self, tmp_path):
        names = attestation_env_var_names(
            env={ATTESTED_IDENTITY_ENV_VAR: "OVERRIDE_IDENTITY"}, config_root=tmp_path
        )
        assert "OVERRIDE_IDENTITY" in names


class TestMalformedAttestationConfigNeverRaises:
    @pytest.mark.parametrize("bad", [["a", "b"], {"k": "v"}, 7, True])
    def test_a_non_string_identity_env_or_session_id_env_is_ignored(self, tmp_path, bad):
        root = _write_user_config(
            tmp_path / "cfg",
            {
                "identity_env": bad,
                "sidecars": [
                    {"session_id_env": bad},
                    {"session_id_env": "REAL_SESSION_ENV"},
                ],
            },
        )
        names = attestation_env_var_names(env={}, config_root=root)
        assert "REAL_SESSION_ENV" in names
        assert all(isinstance(name, str) for name in names)

    def test_a_list_valued_env_override_value_is_ignored(self, tmp_path):
        names = attestation_env_var_names(env={ATTESTED_IDENTITY_ENV_VAR: ["x"]}, config_root=tmp_path)
        assert {ATTESTED_IDENTITY_ENV_VAR, ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} <= names


class TestTheScrub:
    @staticmethod
    def _env() -> dict[str, str]:
        return {
            "PATH": "/usr/bin",
            "HOME": "/home/x",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "VIRTUAL_ENV": "/venv",
            "PLAIN_SETTING": "1",
            "CLAGENTIC_SUBAGENT_ID": "spawn-1",
            "CLAUDE_CODE_SESSION_ID": "sess-1",
            "WHO_AM_I": "agent",
            ATTESTED_IDENTITY_ENV_VAR: "WHO_AM_I",
            ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: "/tmp/side",
            "CLAGENTIC_LOADOUT_TELEMETRY_SINK": "webhook",
            "SERVICE_TOKEN": "t",
            "SERVICE_SECRET": "s",
            "DB_PASSWORD": "p",
            "OPENAI_API_KEY": "k",
            "GH_HOST": "h",
            "GITHUB_TOKEN": "g",
            "FORGEJO_BASE_URL": "u",
            "BAO_ADDR": "b",
            "VAULT_ADDR": "v",
        }

    @pytest.fixture
    def root(self, tmp_path) -> Path:
        return _write_user_config(
            tmp_path / "cfg",
            {
                "identity_env": "WHO_AM_I",
                "sidecars": [
                    {"session_id_env": "CLAGENTIC_SUBAGENT_ID"},
                    {"session_id_env": "CLAUDE_CODE_SESSION_ID"},
                ],
            },
        )

    def test_identity_sidecar_and_credential_variables_are_removed(self, root):
        scrubbed = pre_check_env(self._env(), config_root=root)
        assert set(scrubbed) == {"PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "PLAIN_SETTING"}

    def test_the_input_mapping_is_not_modified(self, root):
        env = self._env()
        before = dict(env)
        pre_check_env(env, config_root=root)
        assert env == before

    def test_passthrough_keeps_exactly_the_named_variables(self, root):
        scrubbed = pre_check_env(
            self._env(), passthrough=["SERVICE_TOKEN", "CLAUDE_CODE_SESSION_ID"], config_root=root
        )
        assert scrubbed["SERVICE_TOKEN"] == "t"
        assert scrubbed["CLAUDE_CODE_SESSION_ID"] == "sess-1"
        assert "SERVICE_SECRET" not in scrubbed and "CLAGENTIC_SUBAGENT_ID" not in scrubbed

    @pytest.mark.parametrize("pattern", CREDENTIAL_NAME_PATTERNS)
    def test_every_credential_pattern_matches_a_lower_case_name_too(self, pattern):
        sample = pattern.replace("*", "x").lower()
        assert is_denied_pre_check_name(sample)

    @pytest.mark.parametrize(
        "name", ["PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "VIRTUAL_ENV", "PYTHONPATH", "KEYBOARD", "PASSENGER"]
    )
    def test_ordinary_names_are_kept(self, name):
        assert not is_denied_pre_check_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_REGION",
            "AZURE_CLIENT_ID",
            "GITHUB_PAT",
            "github_pat",
            "SSH_AUTH_SOCK",
            "DATABASE_URL",
            "FOO_CREDENTIALS",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "SERVICE_PASSWD",
            "DB_PASS",
            "SENTRY_DSN",
            "SIGNING_KEY",
            "STRIPE_ACCESS_KEY_ID",
            "TLS_PRIVATE_KEY_PATH",
            "TOKENIZERS_PARALLELISM",
        ],
    )
    def test_widened_credential_names_are_denied(self, name):
        assert is_denied_pre_check_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "GIT_ASKPASS",
            "SSH_ASKPASS",
            "SUDO_ASKPASS",
            "NETRC",
            "KUBECONFIG",
            "DOCKER_CONFIG",
            "GIT_CONFIG_GLOBAL",
            "GIT_CONFIG_SYSTEM",
            "GIT_CONFIG_COUNT",
            "GIT_CONFIG_KEY_0",
            "GIT_CONFIG_VALUE_0",
            "git_config_key_12",
        ],
    )
    def test_credential_source_pointers_are_denied(self, name, tmp_path):
        assert is_denied_pre_check_name(name)
        assert name not in pre_check_env({name: "x", "PATH": "/usr/bin"}, config_root=tmp_path)

    def test_a_credential_source_pointer_can_be_passed_through_deliberately(self, tmp_path):
        scrubbed = pre_check_env({"KUBECONFIG": "/k"}, passthrough=["KUBECONFIG"], config_root=tmp_path)
        assert scrubbed == {"KUBECONFIG": "/k"}

    def test_the_widened_denylist_strips_credentials_and_keeps_the_ordinary_environment(self, tmp_path):
        env = {
            "AWS_SECRET_ACCESS_KEY": "a",
            "GITHUB_PAT": "b",
            "SSH_AUTH_SOCK": "/s",
            "DATABASE_URL": "postgres://x",
            "FOO_CREDENTIALS": "c",
            "PATH": "/usr/bin",
            "HOME": "/h",
            "LANG": "C",
            "LC_ALL": "C",
            "VIRTUAL_ENV": "/v",
            "PYTHONPATH": "/p",
            "TMPDIR": "/t",
        }
        scrubbed = pre_check_env(env, config_root=tmp_path)
        assert set(scrubbed) == {"PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH", "TMPDIR"}


class TestPassthroughConfig:
    def test_absent_is_empty(self, tmp_path):
        write_deployment_config(tmp_path, {})
        assert resolve_pre_checks_env_passthrough(tmp_path) == ()

    def test_no_repo_is_empty(self):
        assert resolve_pre_checks_env_passthrough(None) == ()

    def test_names_are_returned_in_order_without_duplicates(self, tmp_path):
        write_deployment_config(tmp_path, {"pre_checks_env_passthrough": ["B_TOKEN", "A", "B_TOKEN"]})
        assert resolve_pre_checks_env_passthrough(tmp_path) == ("B_TOKEN", "A")

    @pytest.mark.parametrize(
        "value",
        ["A_TOKEN", ["not a name"], [1], {"A": 1}, ["API_TOKEN\n"], ["API_TOKEN\n\n"], ["\nAPI_TOKEN"]],
        ids=repr,
    )
    def test_a_malformed_value_is_an_error(self, tmp_path, value):
        write_deployment_config(tmp_path, {"pre_checks_env_passthrough": value})
        with pytest.raises(PostMergeConfigError, match="pre_checks_env_passthrough"):
            resolve_pre_checks_env_passthrough(tmp_path)

    @pytest.mark.parametrize("name", sorted(GIT_LOCATION_ENV_NAMES))
    def test_naming_a_git_selector_is_a_config_error(self, tmp_path, name):
        write_deployment_config(tmp_path, {"pre_checks_env_passthrough": ["OK_NAME", name]})
        with pytest.raises(PostMergeConfigError, match=name):
            resolve_pre_checks_env_passthrough(tmp_path)


class TestGitSelectorsAreAlwaysScrubbed:
    @pytest.mark.parametrize("name", sorted(GIT_LOCATION_ENV_NAMES))
    def test_each_selector_is_denied_and_removed_even_when_passed_through(self, tmp_path, name):
        env = {name: "/x", name.lower(): "/y", "PATH": "/usr/bin"}
        assert is_denied_pre_check_name(name)
        assert pre_check_env(env, config_root=tmp_path) == {"PATH": "/usr/bin"}
        assert pre_check_env(env, passthrough=[name], config_root=tmp_path) == {"PATH": "/usr/bin"}

    def test_the_selector_list_is_complete(self):
        assert GIT_LOCATION_ENV_NAMES == {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_ALTERNATE_OBJECT_DIRECTORIES",
            "GIT_NAMESPACE",
            "GIT_CEILING_DIRECTORIES",
            "GIT_DISCOVERY_ACROSS_FILESYSTEM",
            "GIT_PREFIX",
        }


class TestPassthroughIsHonouredOnlyFromAnUntrackedFile:
    def _repo_with_config(self, tmp_path, *, tracked: bool):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        write_deployment_config(repo, {"pre_checks_env_passthrough": ["AWS_SECRET_ACCESS_KEY"]})
        if tracked:
            git(repo, "add", "-f", "--", ".clagentic/loadout/config.yaml")
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
        return repo

    def test_an_untracked_file_is_honoured(self, tmp_path):
        decision = decide_pre_checks_env_passthrough(self._repo_with_config(tmp_path, tracked=False))
        assert decision.names == ("AWS_SECRET_ACCESS_KEY",) and decision.ignored_file is None

    def test_a_tracked_file_is_ignored_and_named(self, tmp_path):
        repo = self._repo_with_config(tmp_path, tracked=True)
        decision = decide_pre_checks_env_passthrough(repo)
        assert decision.names == ()
        assert decision.ignored_file == repo / ".clagentic/loadout/config.yaml"
        assert "tracked by git" in decision.reason

    def test_a_tree_git_cannot_judge_is_ignored(self, tmp_path):
        write_deployment_config(tmp_path, {"pre_checks_env_passthrough": ["A"]})
        decision = decide_pre_checks_env_passthrough(tmp_path)
        assert decision.names == () and decision.ignored_file is not None
        assert "could not be checked" in decision.reason

    def test_no_key_is_nothing_to_ignore(self, tmp_path):
        write_deployment_config(tmp_path, {})
        assert decide_pre_checks_env_passthrough(tmp_path) == PassthroughDecision()


def _probe(*, absent: list[str] = (), present: list[str] = ()) -> dict:
    code = (
        "import os, sys; "
        f"bad = [n for n in {list(absent)!r} if n in os.environ] + "
        f"[n for n in {list(present)!r} if n not in os.environ]; "
        "print('env probe mismatch:', bad, file=sys.stderr); sys.exit(1 if bad else 0)"
    )
    return {"cmd": [_PY, "-c", code], "on_failure": "fail"}


def _repo(tmp_path, steps, *, head_files=None, deployment_merge=None) -> GateRepo:
    return init_gate_repo(
        tmp_path / "shared",
        tracked_gate={"required_reviewer_roles": [], "pre_checks": steps},
        head_files=head_files,
        deployment_merge=deployment_merge,
    )


class TestThroughTheMergeVerb:
    @pytest.fixture(autouse=True)
    def _ambient(self, monkeypatch):
        monkeypatch.setenv("DEPLOY_TOKEN", "t")
        monkeypatch.setenv("DEPLOY_PASSWORD", "p")
        monkeypatch.setenv("CLAGENTIC_LOADOUT_PRECHECK_PROBE", "x")
        monkeypatch.setenv("PRECHECK_PLAIN_SETTING", "kept")
        monkeypatch.setenv("HOME", "/precheck-home")

    def test_a_check_cannot_see_denied_variables_and_keeps_the_rest(self, tmp_path, scratch_tmp):
        repo = _repo(
            tmp_path,
            [
                _probe(
                    absent=["DEPLOY_TOKEN", "DEPLOY_PASSWORD", "CLAGENTIC_LOADOUT_PRECHECK_PROBE"],
                    present=["PATH", "HOME", "PRECHECK_PLAIN_SETTING"],
                )
            ],
        )
        assert _merge(repo) == verb.EXIT_OK

    def test_the_probe_is_red_when_a_denied_variable_leaks(self, tmp_path, scratch_tmp):
        repo = _repo(tmp_path, [_probe(present=["DEPLOY_TOKEN"])])
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED

    def test_passthrough_keeps_a_named_variable_only(self, tmp_path, scratch_tmp):
        repo = _repo(
            tmp_path,
            [_probe(present=["DEPLOY_TOKEN"], absent=["DEPLOY_PASSWORD"])],
            deployment_merge={"pre_checks_env_passthrough": ["DEPLOY_TOKEN"]},
        )
        assert _merge(repo) == verb.EXIT_OK

    def test_a_malformed_passthrough_refuses_before_any_check_runs(self, tmp_path, scratch_tmp, capsys):
        repo = _repo(
            tmp_path,
            [_probe()],
            deployment_merge={"pre_checks_env_passthrough": "DEPLOY_TOKEN"},
        )
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        assert "pre_checks_env_passthrough" in capsys.readouterr().err

    def test_a_tracked_config_cannot_widen_the_environment_and_the_log_says_why(
        self, tmp_path, scratch_tmp, monkeypatch, capsys
    ):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
        repo = _repo(
            tmp_path,
            [_probe(absent=["AWS_SECRET_ACCESS_KEY"])],
            deployment_merge={"pre_checks_env_passthrough": ["AWS_SECRET_ACCESS_KEY"]},
        )
        config = ".clagentic/loadout/config.yaml"
        git(repo.path, "add", "-f", "--", config)
        git(repo.path, "commit", "-q", "-m", "track the config")
        assert _merge(repo) == verb.EXIT_OK
        err = capsys.readouterr().err
        assert str(repo.path / config) in err and "tracked by git" in err

    def test_an_untracked_config_does_widen_it(self, tmp_path, scratch_tmp, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s")
        repo = _repo(
            tmp_path,
            [_probe(present=["AWS_SECRET_ACCESS_KEY"])],
            deployment_merge={"pre_checks_env_passthrough": ["AWS_SECRET_ACCESS_KEY"]},
        )
        assert _merge(repo) == verb.EXIT_OK

    def test_a_git_selector_in_the_passthrough_refuses_before_any_check_runs(
        self, tmp_path, scratch_tmp, capsys
    ):
        repo = _repo(
            tmp_path, [_probe()], deployment_merge={"pre_checks_env_passthrough": ["GIT_DIR"]}
        )
        assert _merge(repo) == verb.EXIT_PRE_CHECKS_FAILED
        assert "GIT_DIR" in capsys.readouterr().err

    def test_the_tracked_gate_cannot_declare_the_passthrough(self, tmp_path, scratch_tmp):
        gate = {
            "required_reviewer_roles": [],
            "pre_checks": [_probe(absent=["DEPLOY_TOKEN"])],
            "pre_checks_env_passthrough": ["DEPLOY_TOKEN"],
        }
        repo = init_gate_repo(tmp_path / "shared", tracked_gate=gate)
        assert _merge(repo) == verb.EXIT_OK

    def test_a_repo_relative_script_shaped_like_the_crew_manifest_checks_still_passes(
        self, tmp_path, scratch_tmp
    ):
        script = (
            "import sys\nfrom pathlib import Path\n"
            "root = Path(__file__).resolve().parent.parent\n"
            "sys.exit(0 if (root / 'change.txt').exists() and '--quiet' in sys.argv else 1)\n"
        )
        steps = [
            {"cmd": [_PY, "scripts/validate.py", "--merge-gate", "--quiet"], "on_failure": "fail"},
            {"cmd": [_PY, "scripts/validate.py", "full", "--quiet"], "on_failure": "fail"},
        ]
        repo = _repo(tmp_path, steps, head_files={"scripts/validate.py": script, "change.txt": "x\n"})
        assert _merge(repo) == verb.EXIT_OK

    def test_the_gate_log_names_the_clone_as_the_execution_tree(self, tmp_path, scratch_tmp, capsys):
        marker = tmp_path / "ran-in"
        repo = _repo(
            tmp_path,
            [
                {
                    "cmd": [_PY, "-c", f"import os; open({str(marker)!r}, 'w').write(os.getcwd())"],
                    "on_failure": "fail",
                }
            ],
        )
        assert _merge(repo) == verb.EXIT_OK
        clone = marker.read_text(encoding="utf-8")
        line = next(
            ln for ln in capsys.readouterr().err.splitlines() if "pre_checks gate -- running" in ln
        )
        assert f"in the merge-result clone {clone!r}" in line
        assert f"check(s) in {str(repo.path)!r}" not in line
        assert "is not used for execution" in line
