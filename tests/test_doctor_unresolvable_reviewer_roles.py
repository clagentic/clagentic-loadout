"""loadout-doctor judges the tracked merge gate by asking the merge runtime for
it, never by re-deriving the rules: what doctor reports for a gate file is the
runtime's own warnings and `pre_checks_error` for the same text.

A declared reviewer role the deployment cannot resolve to a platform login
degrades PER ROLE at merge time (that role is dropped from the floor, its own
required_scanners entry is skipped, every other role stays enforced). Doctor
reports the runtime's warning for it as a FAIL so the gap is visible. Gate keys
sitting in the deployment `config.yaml` are IGNORED by merge when a tracked gate
exists, so doctor reports that notice as a WARN and never resolves those roles.
With no tracked gate they ARE the gate (merge's fallback), and doctor judges
them through the same loader."""

from __future__ import annotations

import pytest

from clagentic_loadout.doctor.checks import check_repo_loadout_schema
from clagentic_loadout.merge.repo_gate_runtime import (
    ignored_deployment_gate_warnings,
    load_repo_gate_at_base,
    with_resolvable_reviewer_roles,
)
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.github_app_config import GithubAppSlugNotConfiguredError
from tests._gate_repo import git

_GITHUB_REMOTE = "https://github.com/some-owner/some-repo.git"
_GATE_LINE_PREFIX = "merge (gate declaration): "


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _repo(tmp_path, *, remote=_GITHUB_REMOTE, tracked_gate=None, deployment_yaml=None, commit=True):
    """A git repo whose HEAD commit carries *tracked_gate* (bytes, str or None);
    *deployment_yaml* is written to the uncommitted deployment config.yaml."""
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    if remote:
        git(repo, "remote", "add", "origin", remote)
    if tracked_gate is not None:
        target = repo / TRACKED_GATE_RELATIVE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(tracked_gate.encode("utf-8") if isinstance(tracked_gate, str) else tracked_gate)
        git(repo, "add", "--", TRACKED_GATE_RELATIVE_PATH)
    if commit:
        git(repo, "commit", "-q", "--allow-empty", "-m", "base")
    if deployment_yaml is not None:
        config = repo / ".clagentic" / "loadout" / "config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(deployment_yaml, encoding="utf-8")
    return repo


def _slugs(monkeypatch, known: set[str]):
    def resolve(*, caller):
        if caller not in known:
            raise GithubAppSlugNotConfiguredError(f"github_app.slugs.{caller}")
        return f"slug-{caller}"

    monkeypatch.setattr("clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", resolve)


def _gate_lines(result):
    return [line for line in result.resolved["errors"] if line.startswith(_GATE_LINE_PREFIX)]


def test_an_unresolvable_tracked_role_fails_and_names_the_per_role_drop(tmp_path, monkeypatch):
    _slugs(monkeypatch, {"security"})
    repo = _repo(
        tmp_path,
        tracked_gate="merge:\n  required_reviewer_roles: [reviewer, security]\n",
        deployment_yaml="merge:\n  sync_tree_after_merge: false\n",
    )
    result = check_repo_loadout_schema(repo)
    assert result.ok is False
    assert "'reviewer'" in result.summary and "github" in result.summary
    assert "DROPPED" in result.summary
    assert "'security'" not in result.summary


def test_resolvable_tracked_roles_pass(tmp_path, monkeypatch):
    _slugs(monkeypatch, {"reviewer"})
    repo = _repo(
        tmp_path,
        tracked_gate="merge:\n  required_reviewer_roles: [reviewer]\n",
        deployment_yaml="merge:\n  sync_tree_after_merge: false\n",
    )
    assert check_repo_loadout_schema(repo).ok is True


def test_a_forgejo_remote_always_resolves(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(
        tmp_path,
        remote="http://git-host.example.com:3000/o/r.git",
        tracked_gate="merge:\n  required_reviewer_roles: [reviewer]\n",
        deployment_yaml="merge:\n  sync_tree_after_merge: false\n",
    )
    assert check_repo_loadout_schema(repo).ok is True


def test_no_remote_means_no_platform_and_nothing_is_resolved(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(
        tmp_path,
        remote=None,
        tracked_gate="merge:\n  required_reviewer_roles: [reviewer]\n",
        deployment_yaml="merge:\n  sync_tree_after_merge: false\n",
    )
    assert check_repo_loadout_schema(repo).ok is True


def test_roles_in_the_deployment_file_are_ignored_not_resolved_when_a_tracked_gate_exists(
    tmp_path, monkeypatch
):
    _slugs(monkeypatch, set())
    repo = _repo(
        tmp_path,
        tracked_gate="merge:\n  required_reviewer_roles: []\n",
        deployment_yaml="merge:\n  required_reviewer_roles: [reviewer]\n",
    )
    result = check_repo_loadout_schema(repo)
    assert result.ok is True
    assert "IGNORED" in result.summary and TRACKED_GATE_RELATIVE_PATH in result.summary
    assert result.resolved["gate_warnings"] == list(ignored_deployment_gate_warnings(repo))
    assert "cannot be resolved" not in result.summary


def test_roles_only_in_the_deployment_file_are_judged_as_the_fallback_gate(tmp_path, monkeypatch):
    _slugs(monkeypatch, set())
    repo = _repo(tmp_path, deployment_yaml="merge:\n  required_reviewer_roles: [reviewer]\n")
    result = check_repo_loadout_schema(repo)
    assert "IGNORED" not in result.summary
    assert "gate keys read from deployment config.yaml; no tracked gate.yaml at base" in result.summary
    assert "'reviewer'" in result.summary and "DROPPED" in result.summary
    assert result.ok is False


def test_a_resolvable_fallback_gate_passes_with_only_the_notice(tmp_path, monkeypatch):
    _slugs(monkeypatch, {"reviewer"})
    repo = _repo(tmp_path, deployment_yaml="merge:\n  required_reviewer_roles: [reviewer]\n")
    result = check_repo_loadout_schema(repo)
    assert result.ok is True
    assert len(result.resolved["gate_warnings"]) == 1
    assert "no tracked gate.yaml at base" in result.resolved["gate_warnings"][0]


def test_the_tracked_gate_is_judged_even_without_a_deployment_file(tmp_path):
    repo = _repo(tmp_path, tracked_gate=b"merge: [unclosed")
    result = check_repo_loadout_schema(repo)
    assert result.ok is False
    assert "NOT ENFORCED" in result.summary


def test_a_tree_with_no_commit_has_no_tracked_gate_to_judge(tmp_path):
    repo = _repo(tmp_path, tracked_gate="merge: [unclosed", commit=False)
    assert check_repo_loadout_schema(repo).ok is True


def test_a_non_utf8_repo_config_is_a_fail_never_a_crash(tmp_path):
    repo = _repo(tmp_path, remote=None, deployment_yaml="merge: {}\n")
    (repo / ".clagentic" / "loadout" / "config.yaml").write_bytes(b"merge: \xff\xfe\n")
    assert check_repo_loadout_schema(repo).ok is False


_PARITY_FIXTURES = {
    "not-utf8": (b"merge:\n  required_reviewer_roles: [\xff\xfe]\n", None),
    "not-yaml": (b"merge: [unclosed", None),
    "not-a-mapping": (b"- just\n- a list\n", None),
    "malformed-pre-checks": (b"merge:\n  required_reviewer_roles: []\n  pre_checks: nope\n", None),
    "malformed-reviewer-key": (b"merge:\n  required_reviewer_roles: reviewer\n", None),
    "unresolvable-role": (b"merge:\n  required_reviewer_roles: [reviewer]\n", None),
    "clean": (b"merge:\n  required_reviewer_roles: []\n", None),
    "gate-key-only-in-config": (None, "merge:\n  required_reviewer_roles: [reviewer]\n"),
    "gate-key-in-config-and-tracked": (
        b"merge:\n  required_reviewer_roles: []\n",
        "merge:\n  required_reviewer_roles: [reviewer]\n",
    ),
    "malformed-pre-checks-only-in-config": (
        None,
        "merge:\n  required_reviewer_roles: []\n  pre_checks: nope\n",
    ),
    "config-without-gate-keys": (None, "merge:\n  sync_tree_after_merge: false\n"),
}


@pytest.mark.parametrize("fixture", sorted(_PARITY_FIXTURES))
def test_doctor_reports_exactly_what_the_merge_runtime_produces(tmp_path, monkeypatch, fixture):
    tracked, deployment = _PARITY_FIXTURES[fixture]
    _slugs(monkeypatch, set())
    repo = _repo(tmp_path, tracked_gate=tracked, deployment_yaml=deployment)
    head = git(repo, "rev-parse", "HEAD")

    gate = with_resolvable_reviewer_roles(load_repo_gate_at_base(repo, base_sha=head), "github")
    ignored = ignored_deployment_gate_warnings(repo)
    expected_errors = [w for w in gate.warnings if w not in ignored]
    if gate.pre_checks_error:
        expected_errors.append(gate.pre_checks_error)

    result = check_repo_loadout_schema(repo)
    assert _gate_lines(result) == [_GATE_LINE_PREFIX + line for line in expected_errors]
    assert result.resolved["gate_warnings"] == [w for w in gate.warnings if w in ignored] + list(gate.notices)
    assert result.ok is (not expected_errors)
