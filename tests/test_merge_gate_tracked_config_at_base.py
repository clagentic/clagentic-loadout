"""The fallback gate (no tracked gate file at the PR base) reads the deployment
config.yaml's gate keys from the BASE COMMIT when that file is tracked there, so a
PR cannot relax its own gate by editing it; a gitignored or untracked file is read
from the working tree as before. Every merge also prints one gate-source line."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge import repo_gate_runtime, verb
from clagentic_loadout.merge.repo_gate_runtime import (
    GATE_SOURCE_NONE,
    GATE_SOURCE_TRACKED_CONFIG,
    GATE_SOURCE_TRACKED_GATE,
    GATE_SOURCE_UNREADABLE,
    GATE_SOURCE_UNTRACKED_CONFIG,
    gate_source_line,
)
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH, TRACKED_GATE_RELATIVE_PATH
from tests._gate_repo import GateRepo, git, init_gate_repo, make_origin_confirmable, write_deployment_config
from tests.test_merge_verb_pre_checks import _merge

BASE_ROLES = ["reviewer", "security"]


def _tracked_config_repo(tmp_path, base_text: bytes | str, tree_merge: dict | None = None) -> GateRepo:
    """A repo whose base commit TRACKS config.yaml with *base_text* and no gate.yaml;
    the working tree then holds *tree_merge* in its place, uncommitted."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    git(tmp_path, "init", "-q", "-b", "main")
    target = tmp_path / DEFAULT_CONFIG_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / "README.md").write_text("base\n", encoding="utf-8")
    target.write_bytes(base_text if isinstance(base_text, bytes) else base_text.encode("utf-8"))
    git(tmp_path, "add", "--", "README.md", DEFAULT_CONFIG_RELATIVE_PATH)
    git(tmp_path, "commit", "-q", "-m", "base")
    base_sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "-b", "pr")
    (tmp_path / "change.txt").write_text("pr\n", encoding="utf-8")
    git(tmp_path, "add", "--", "change.txt")
    git(tmp_path, "commit", "-q", "-m", "pr change")
    head_sha = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-q", "main")
    if tree_merge is not None:
        write_deployment_config(tmp_path, tree_merge)
    return GateRepo(path=tmp_path, base_sha=base_sha, head_sha=head_sha)


def _base_text(**merge) -> str:
    return yaml.safe_dump({"merge": {"sync_tree_after_merge": False, **merge}})


def _gate(repo: GateRepo):
    return repo_gate_runtime.load_gate_at_commit(repo.path, repo.base_sha, deployment_repo_path=repo.path)


def test_tracked_config_gate_keys_come_from_base_not_the_working_tree(tmp_path):
    repo = _tracked_config_repo(
        tmp_path,
        _base_text(required_reviewer_roles=BASE_ROLES),
        {"sync_tree_after_merge": False, "required_reviewer_roles": ["reviewer"]},
    )

    gate = _gate(repo)

    assert gate.reviewer_roles == tuple(BASE_ROLES)
    assert gate.source == GATE_SOURCE_TRACKED_CONFIG
    assert gate.source_sha == repo.base_sha[:12]
    assert any("tracked config.yaml at base" in n for n in gate.notices)


@pytest.mark.parametrize(
    "tree_merge",
    {
        "adds_a_key": {"required_reviewer_roles": BASE_ROLES, "ci_strict": False},
        "changes_pre_checks": {"required_reviewer_roles": BASE_ROLES, "pre_checks": [{"cmd": ["true"]}]},
        "drops_a_role": {"required_reviewer_roles": ["reviewer"]},
    }.values(),
    ids=["adds_a_key", "changes_pre_checks", "drops_a_role"],
)
def test_a_differing_working_tree_copy_is_not_used_and_warns_once(tmp_path, tree_merge):
    repo = _tracked_config_repo(tmp_path, _base_text(required_reviewer_roles=BASE_ROLES), tree_merge)

    gate = _gate(repo)

    assert gate.reviewer_roles == tuple(BASE_ROLES)
    assert gate.pre_checks == ()
    assert gate.ci_strict is False
    differing = [w for w in gate.warnings if "working-tree copy" in w]
    assert len(differing) == 1
    assert DEFAULT_CONFIG_RELATIVE_PATH in differing[0] and repo.base_sha[:12] in differing[0]
    assert gate.advisory_warnings == tuple(differing)


def test_a_matching_working_tree_copy_does_not_warn(tmp_path):
    repo = _tracked_config_repo(
        tmp_path,
        _base_text(required_reviewer_roles=BASE_ROLES),
        {"sync_tree_after_merge": False, "required_reviewer_roles": BASE_ROLES},
    )

    assert _gate(repo).warnings == ()


def test_a_tracked_config_with_no_gate_keys_declares_nothing_and_ignores_a_tree_gate(tmp_path):
    repo = _tracked_config_repo(
        tmp_path, _base_text(), {"sync_tree_after_merge": False, "required_reviewer_roles": ["reviewer"]}
    )

    gate = _gate(repo)

    assert gate.reviewer_roles == () and gate.source == GATE_SOURCE_NONE
    assert len([w for w in gate.warnings if "working-tree copy" in w]) == 1


def test_an_untracked_config_is_read_from_the_working_tree_as_before(tmp_path):
    repo = init_gate_repo(
        tmp_path, tracked_gate=None, deployment_merge={"required_reviewer_roles": ["reviewer", "security"]}
    )

    gate = _gate(repo)

    assert gate.reviewer_roles == ("reviewer", "security")
    assert gate.source == GATE_SOURCE_UNTRACKED_CONFIG
    assert gate.warnings == ()
    assert any("untracked deployment config.yaml" in n for n in gate.notices)


def test_a_config_above_the_git_tree_is_untracked_and_unchanged(tmp_path):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, {"required_reviewer_roles": ["reviewer"], "git_working_tree": "tree"})
    repo = init_gate_repo(wrapper / "tree", tracked_gate=None)
    (repo.path / DEFAULT_CONFIG_RELATIVE_PATH).unlink()

    gate = _gate(repo)

    assert gate.reviewer_roles == ("reviewer",)
    assert gate.source == GATE_SOURCE_UNTRACKED_CONFIG
    assert str(wrapper) in gate.source_path


_MALFORMED_AT_BASE = {
    "not_utf8": b"merge:\n  note: \xff\xfe\n",
    "not_yaml": b"merge: [unclosed\n",
    "merge_not_a_mapping": b"merge:\n  - a\n  - list\n",
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED_AT_BASE))
def test_a_malformed_tracked_config_fails_like_a_malformed_tracked_gate(tmp_path, shape):
    repo = _tracked_config_repo(tmp_path, _MALFORMED_AT_BASE[shape])

    gate = _gate(repo)

    assert gate.pre_checks_error
    assert gate.reviewer_roles == () and gate.required_scanners is None
    assert any("NOT ENFORCED" in w for w in gate.warnings)
    assert gate.source == GATE_SOURCE_UNREADABLE


def test_a_tracked_gate_file_is_unchanged_and_deployment_keys_stay_ignored(tmp_path):
    repo = init_gate_repo(
        tmp_path,
        tracked_gate={"required_reviewer_roles": ["reviewer"]},
        deployment_merge={"required_reviewer_roles": []},
    )

    gate = _gate(repo)

    assert gate.reviewer_roles == ("reviewer",)
    assert gate.source == GATE_SOURCE_TRACKED_GATE
    assert any("IGNORED" in w for w in gate.warnings)
    assert gate_source_line(gate) == f"gate read from {TRACKED_GATE_RELATIVE_PATH} at {repo.base_sha[:12]}"


def _gate_source_lines(capsys) -> list[str]:
    return [line for line in capsys.readouterr().err.splitlines() if line.startswith("merge: gate read from") or line == "merge: no gate declared"]


def test_the_merge_prints_one_line_for_a_tracked_gate(tmp_path, capsys):
    repo = init_gate_repo(tmp_path, tracked_gate={"required_reviewer_roles": []})
    assert _merge(repo) == verb.EXIT_OK
    assert _gate_source_lines(capsys) == [
        f"merge: gate read from {TRACKED_GATE_RELATIVE_PATH} at {repo.base_sha[:12]}"
    ]


def test_the_merge_prints_one_line_for_a_tracked_config_fallback(tmp_path, capsys):
    repo = _tracked_config_repo(tmp_path, _base_text(required_reviewer_roles=[]), None)
    assert _merge(repo) == verb.EXIT_OK
    assert _gate_source_lines(capsys) == [
        f"merge: gate read from {DEFAULT_CONFIG_RELATIVE_PATH} at {repo.base_sha[:12]} "
        f"(tracked config fallback; no tracked gate.yaml)"
    ]


def test_the_merge_prints_one_line_for_an_untracked_config_fallback(tmp_path, capsys):
    repo = init_gate_repo(tmp_path, tracked_gate=None)
    make_origin_confirmable(tmp_path)
    write_deployment_config(tmp_path, {"sync_tree_after_merge": False, "required_reviewer_roles": []})
    assert _merge(repo) == verb.EXIT_OK
    lines = _gate_source_lines(capsys)
    assert len(lines) == 1
    assert lines[0].startswith("merge: gate read from untracked deployment config.yaml at ")
    assert lines[0].endswith("(fallback)")


def test_the_merge_prints_one_line_when_no_gate_is_declared(tmp_path, capsys):
    repo = init_gate_repo(tmp_path, tracked_gate=None)
    assert _merge(repo) == verb.EXIT_OK
    assert _gate_source_lines(capsys) == ["merge: no gate declared"]
