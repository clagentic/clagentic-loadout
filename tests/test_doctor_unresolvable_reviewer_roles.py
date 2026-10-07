"""loadout-doctor reports a declared reviewer role that the deployment cannot
resolve to a platform login as a FAIL. loadout-merge only warns for the same
role (it falls back to flags), so doctor is where the gap is made visible."""

from __future__ import annotations

import pytest

from clagentic_loadout.doctor.checks import check_repo_loadout_schema
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.github_app_config import GithubAppSlugNotConfiguredError
from tests._gate_repo import git

_GITHUB_REMOTE = "https://github.com/some-owner/some-repo.git"


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _repo(tmp_path, *, remote, roles_yaml, tracked_gate_yaml=None):
    repo = tmp_path / "repo"
    (repo / ".clagentic" / "loadout").mkdir(parents=True)
    git(repo, "init", "-q", "-b", "main")
    if remote:
        git(repo, "remote", "add", "origin", remote)
    (repo / ".clagentic" / "loadout" / "config.yaml").write_text(
        f"merge:\n  required_reviewer_roles: {roles_yaml}\n", encoding="utf-8"
    )
    if tracked_gate_yaml is not None:
        (repo / TRACKED_GATE_RELATIVE_PATH).write_text(tracked_gate_yaml, encoding="utf-8")
    return repo


def _slugs(monkeypatch, known: set[str]):
    def resolve(*, caller):
        if caller not in known:
            raise GithubAppSlugNotConfiguredError(f"github_app.slugs.{caller}")
        return f"slug-{caller}"

    monkeypatch.setattr("clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", resolve)


def test_an_unresolvable_role_on_a_github_remote_fails(tmp_path, monkeypatch):
    _slugs(monkeypatch, {"security"})
    repo = _repo(tmp_path, remote=_GITHUB_REMOTE, roles_yaml="[reviewer, security]")
    result = check_repo_loadout_schema(repo)
    assert result.ok is False
    assert "'reviewer'" in result.summary and "github" in result.summary
    assert "'security'" not in result.summary


def test_a_role_declared_only_in_the_tracked_gate_file_is_checked_too(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(
        tmp_path,
        remote=_GITHUB_REMOTE,
        roles_yaml="[]",
        tracked_gate_yaml="merge:\n  required_reviewer_roles: [reviewer]\n",
    )
    result = check_repo_loadout_schema(repo)
    assert result.ok is False
    assert "'reviewer'" in result.summary


@pytest.mark.parametrize(
    "raw",
    [b"merge:\n  required_reviewer_roles: [\xff\xfe]\n", b"merge: [unclosed", b"- just\n- a list\n"],
    ids=["not-utf8", "not-yaml", "not-a-mapping"],
)
@pytest.mark.parametrize("remote", [_GITHUB_REMOTE, None], ids=["with-remote", "no-remote"])
def test_an_unreadable_tracked_gate_is_a_fail_never_a_crash(tmp_path, monkeypatch, raw, remote):
    _slugs(monkeypatch, {"reviewer"})
    repo = _repo(tmp_path, remote=remote, roles_yaml="[reviewer]")
    (repo / TRACKED_GATE_RELATIVE_PATH).write_bytes(raw)
    result = check_repo_loadout_schema(repo)
    assert result.ok is False
    assert TRACKED_GATE_RELATIVE_PATH in result.summary
    assert "cannot be read as a gate declaration" in result.summary


def test_a_non_utf8_repo_config_is_a_fail_never_a_crash(tmp_path):
    repo = _repo(tmp_path, remote=None, roles_yaml="[]")
    (repo / ".clagentic" / "loadout" / "config.yaml").write_bytes(b"merge: \xff\xfe\n")
    result = check_repo_loadout_schema(repo)
    assert result.ok is False


def test_resolvable_roles_pass(tmp_path, monkeypatch):
    _slugs(monkeypatch, {"reviewer"})
    repo = _repo(tmp_path, remote=_GITHUB_REMOTE, roles_yaml="[reviewer]")
    assert check_repo_loadout_schema(repo).ok is True


def test_a_forgejo_remote_always_resolves(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(tmp_path, remote="http://git-host.example.com:3000/o/r.git", roles_yaml="[reviewer]")
    assert check_repo_loadout_schema(repo).ok is True


def test_no_remote_means_no_platform_and_nothing_is_reported(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(tmp_path, remote=None, roles_yaml="[reviewer]")
    assert check_repo_loadout_schema(repo).ok is True
