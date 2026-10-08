"""A config or state file that is not valid UTF-8 must be handled exactly like
an unreadable or malformed one: each reader raises its own documented config
error, or degrades the way it already does for a YAML/JSON syntax error. It
must never leak a raw UnicodeDecodeError traceback to the caller."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.merge.post_merge_config import (
    find_crew_yaml_files_declaring_post_merge_steps,
)
from clagentic_loadout.provisioning.model_routing import (
    InvalidModelRoutingConfigError,
    load_model_routing,
)
from clagentic_loadout.provisioning.roles import InvalidRoleConfigError, load_role_verbs
from clagentic_loadout.provisioning.writer import SettingsWriteError, merge_fragment_into_settings
from clagentic_loadout.push.cleanliness_config import (
    InvalidCleanlinessConfigError,
    load_scratch_patterns,
)
from clagentic_loadout.push.contention_config import (
    InvalidContentionConfigError,
    load_contention_config,
)
from clagentic_loadout.push.verify_config import InvalidVerifyConfigError, load_verify_entries
from clagentic_loadout.release.secrets_config import SecretEnvError, read_role_env_file
from clagentic_loadout.review.engine_breaker import BREAKER_FILENAME, EngineBreaker
from clagentic_loadout.review.profile_config import (
    ReviewProfileError,
    _read_rulebook,
    _read_yaml_mapping as read_review_yaml_mapping,
)
from clagentic_loadout.review.run_pipeline import (
    BINDING_FILENAME,
    _load_record,
    bind_run_dir,
)
from clagentic_loadout.transport import github_app_config, provider_config
from clagentic_loadout.wait.config import InvalidScopedTestConfigError, load_scoped_test_patterns

#: 0xff can never start a UTF-8 sequence.
NOT_UTF8 = b"\xff\xfe push: \xff\n"


def _repo_config(tmp_path: Path) -> Path:
    path = tmp_path / ".clagentic" / "loadout" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_bytes(NOT_UTF8)
    return tmp_path


@pytest.mark.parametrize(
    ("loader", "error"),
    [
        (load_scratch_patterns, InvalidCleanlinessConfigError),
        (load_contention_config, InvalidContentionConfigError),
        (load_verify_entries, InvalidVerifyConfigError),
        (load_scoped_test_patterns, InvalidScopedTestConfigError),
        (load_model_routing, InvalidModelRoutingConfigError),
        (load_role_verbs, InvalidRoleConfigError),
    ],
)
def test_repo_config_loader_raises_its_own_config_error(tmp_path, loader, error):
    repo = _repo_config(tmp_path)

    with pytest.raises(error, match="could not be read as YAML"):
        loader(repo)


def test_user_config_readers_treat_non_utf8_as_absent(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_bytes(NOT_UTF8)

    assert provider_config._read_yaml_mapping(path) == {}
    assert github_app_config._read_yaml_mapping(path) == {}


def test_dead_crew_config_scan_skips_a_non_utf8_file(tmp_path):
    crew = tmp_path / ".crew"
    crew.mkdir()
    (crew / "bad.yaml").write_bytes(NOT_UTF8)
    (crew / "good.yaml").write_text("post_merge_steps: []\n", encoding="utf-8")

    assert find_crew_yaml_files_declaring_post_merge_steps(tmp_path) == [str(crew / "good.yaml")]


def test_settings_writer_raises_settings_write_error(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_bytes(NOT_UTF8)

    with pytest.raises(SettingsWriteError, match="could not be read as JSON"):
        merge_fragment_into_settings(settings, ["Bash(ls)"])


def test_secret_env_file_raises_secret_env_error(tmp_path):
    env_file = tmp_path / "worker.env"
    env_file.write_bytes(NOT_UTF8)
    env_file.chmod(0o600)

    with pytest.raises(SecretEnvError, match="cannot read secret-env file"):
        read_role_env_file("worker", ("TOKEN",), config_root=tmp_path)


def test_review_repo_config_is_ignored_with_a_notice(tmp_path, capsys):
    path = tmp_path / "config.yaml"
    path.write_bytes(NOT_UTF8)

    assert read_review_yaml_mapping(path) == {}
    assert "ignoring unreadable repo-level config" in capsys.readouterr().err


def test_review_rulebook_raises_profile_error(tmp_path):
    rulebook = tmp_path / "rulebook.md"
    rulebook.write_bytes(NOT_UTF8)

    with pytest.raises(ReviewProfileError, match="cannot read rulebook"):
        _read_rulebook(rulebook, "reviewer")


def test_engine_breaker_starts_empty_over_a_non_utf8_file(tmp_path):
    path = tmp_path / BREAKER_FILENAME
    path.write_bytes(NOT_UTF8)

    assert EngineBreaker(path).reason("carrier") == ""


def test_run_state_files_that_are_not_utf8_count_as_absent(tmp_path):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "result-0001.json").write_bytes(NOT_UTF8)
    assert _load_record(state_dir, 1) is None

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / BINDING_FILENAME).write_bytes(NOT_UTF8)
    bind_run_dir(run_dir, "owner", "repo", 7, "a" * 40)
    rewritten = json.loads((run_dir / BINDING_FILENAME).read_text(encoding="utf-8"))
    assert rewritten["pr_number"] == 7
