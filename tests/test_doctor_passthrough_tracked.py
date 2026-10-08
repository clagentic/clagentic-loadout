"""loadout-doctor warns when the deployment config holding
`merge.pre_checks_env_passthrough` is tracked by git, and only then."""

from __future__ import annotations

import pytest

from clagentic_loadout.doctor.checks import check_repo_loadout_schema
from clagentic_loadout.transport import provider_config
from tests._gate_repo import git

_CONFIG = ".clagentic/loadout/config.yaml"
_WITH_KEY = "merge:\n  required_reviewer_roles: []\n  pre_checks_env_passthrough: [DEPLOY_TOKEN]\n"
_WITHOUT_KEY = "merge:\n  required_reviewer_roles: []\n"


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _repo(tmp_path, text: str, *, tracked: bool):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    config = repo / _CONFIG
    config.parent.mkdir(parents=True)
    config.write_text(text, encoding="utf-8")
    if tracked:
        git(repo, "add", "--", _CONFIG)
    git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    return repo


def test_a_tracked_config_holding_the_key_warns(tmp_path):
    result = check_repo_loadout_schema(_repo(tmp_path, _WITH_KEY, tracked=True))
    assert "WARN:" in result.summary and "pre_checks_env_passthrough" in result.summary
    assert "tracked by git" in result.summary
    assert result.resolved["passthrough_tracked_warning"]


def test_an_untracked_config_holding_the_key_is_silent(tmp_path):
    result = check_repo_loadout_schema(_repo(tmp_path, _WITH_KEY, tracked=False))
    assert "tracked by git" not in result.summary
    assert result.resolved["passthrough_tracked_warning"] is None


def test_a_tracked_config_without_the_key_is_silent(tmp_path):
    result = check_repo_loadout_schema(_repo(tmp_path, _WITHOUT_KEY, tracked=True))
    assert "tracked by git" not in result.summary
    assert result.resolved["passthrough_tracked_warning"] is None


def test_the_warning_does_not_fail_the_check(tmp_path):
    tracked = check_repo_loadout_schema(_repo(tmp_path, _WITH_KEY, tracked=True))
    assert tracked.ok is True
