"""The merge verbs read --repo-path's config through one resolver
(merge.config_root over repo_config.resolve_bound_config_root).

Caller shapes, each against real git trees and a real origin remote:
  M7 wrapper layout: the config sits in the directory X holding the git tree T and
     declares merge.git_working_tree naming T; --repo-path is T. Steps, gate keys
     and merge_requirements load from X.
  M8 the same layout without the declaration: refused, naming T, X and the remote.
  M9 a config in an ancestor of T that is not T's direct parent: refused.
Also: a declaration through a symlink or with more than one path segment, a
declaration naming another directory, a config at T winning over the wrapper,
settings-only files above the tree being ignored, unreadable files above the tree
being refused, and every reader going through the one resolver."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from clagentic_loadout import repo_config
from clagentic_loadout.git_origin import ConfigTreeBinding
from clagentic_loadout.merge import config_root, gate_config, post_merge_config, pre_checks_config
from clagentic_loadout.merge import pre_checks_sandbox, repo_gate_runtime, verb
from clagentic_loadout.merge.config_binding import (
    config_binding_for,
    require_confirmed_tree_for_steps,
)
from clagentic_loadout.merge.config_root import MergeConfigRootRefusedError
from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH
from tests._gate_repo import git, init_gate_repo, write_deployment_config
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HOST = "git-host.example.test"
SLUG = "some-owner/some-repo"
ORIGIN = f"https://{HOST}/{SLUG}"
STEPS = {"post_merge_steps": [{"cmd": "true"}]}
REQUIREMENTS = {"merge_requirements": {"tests_pass": True}}
GATE = {"required_reviewer_roles": ["reviewer"]}
SRC = Path(__file__).resolve().parent.parent / "src" / "clagentic_loadout"


@pytest.fixture(autouse=True)
def _configured_host(monkeypatch):
    monkeypatch.setenv("CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL", f"http://{HOST}:3000")


def _wrapper_layout(tmp_path, wrapper_merge, *, tracked_gate=None, origin=ORIGIN):
    """A directory X holding the config *wrapper_merge* and the git tree
    ``X/tree`` that has no config of its own."""
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir(exist_ok=True)
    write_deployment_config(wrapper, wrapper_merge)
    repo = init_gate_repo(wrapper / "tree", tracked_gate=tracked_gate)
    (repo.path / DEFAULT_CONFIG_RELATIVE_PATH).unlink()
    if origin is not None:
        git(repo.path, "remote", "add", "origin", origin)
    return wrapper, repo


def _binding():
    return config_binding_for("forgejo", f"http://{HOST}:3000", SLUG)


# --- M7: the declared wrapper layout ------------------------------------------------


def test_a_declared_wrapper_config_supplies_steps_and_the_tree_sync_runs_in_the_tree(tmp_path):
    wrapper, repo = _wrapper_layout(
        tmp_path, {**STEPS, **REQUIREMENTS, "git_working_tree": "tree", "sync_tree_after_merge": False}
    )

    steps = post_merge_config.load_post_merge_steps(repo.path)
    requirements = gate_config.load_merge_requirements(repo.path)
    sync = post_merge_config.resolve_sync_tree_after_merge(repo.path)
    working_tree = post_merge_config.resolve_git_working_tree(repo.path)

    assert steps == STEPS["post_merge_steps"]
    assert requirements == REQUIREMENTS["merge_requirements"]
    assert sync is False
    assert working_tree.resolve() == repo.path.resolve()
    require_confirmed_tree_for_steps(_binding(), str(repo.path))


def test_a_declared_wrapper_config_supplies_the_fallback_gate_for_the_confirmed_tree(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, {**GATE, "git_working_tree": "tree"})

    gate = repo_gate_runtime.load_gate_at_commit(
        repo.path, repo.base_sha, deployment_repo_path=repo.path, config_binding=ConfigTreeBinding(HOST, SLUG)
    )

    assert gate.notices and not gate.pre_checks_error
    assert str(wrapper) in gate.notices[0]


def test_a_declared_wrapper_config_is_not_used_for_a_tree_on_another_host(tmp_path):
    _, repo = _wrapper_layout(
        tmp_path, {**GATE, "git_working_tree": "tree"}, origin=f"https://other.example.test/{SLUG}"
    )

    gate = repo_gate_runtime.load_gate_at_commit(
        repo.path, repo.base_sha, deployment_repo_path=repo.path, config_binding=_binding()
    )

    assert not gate.notices
    assert gate.pre_checks_error and "other.example.test" in gate.pre_checks_error


def test_a_config_at_the_tree_wins_over_an_undeclared_wrapper_config(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, STEPS)
    write_deployment_config(repo.path, {"post_merge_steps": [{"cmd": "false"}]})

    assert post_merge_config.load_post_merge_steps(repo.path) == [{"cmd": "false"}]


def test_a_config_above_the_tree_that_holds_only_settings_is_ignored(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, {"sync_tree_after_merge": False})

    assert post_merge_config.resolve_sync_tree_after_merge(repo.path) is True
    assert post_merge_config.load_post_merge_steps(repo.path) == []


def test_a_path_that_is_in_no_git_tree_is_read_where_it_is(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    write_deployment_config(plain, STEPS)

    assert post_merge_config.load_post_merge_steps(plain) == STEPS["post_merge_steps"]


# --- M8 and M9: refusals ------------------------------------------------------------


def test_an_undeclared_wrapper_config_is_refused_naming_the_tree_the_root_and_the_remote(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, STEPS, origin=f"https://user:s3cr3t@{HOST}/{SLUG}.git")

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    message = str(info.value)
    assert str(repo.path.resolve()) in message and str(wrapper.resolve()) in message
    assert f"https://{HOST}/{SLUG}.git" in message
    assert "s3cr3t" not in message


def test_an_undeclared_wrapper_config_is_a_config_error_not_an_empty_list(tmp_path):
    _, repo = _wrapper_layout(tmp_path, STEPS)

    with pytest.raises(PostMergeConfigError):
        post_merge_config.load_post_merge_steps(repo.path)
    with pytest.raises(PostMergeConfigError):
        post_merge_config.post_merge_steps_key_declared(repo.path)


def test_a_config_in_an_ancestor_that_is_not_the_direct_parent_is_refused(tmp_path):
    outer = tmp_path / "outer"
    inner = outer / "middle"
    inner.mkdir(parents=True)
    write_deployment_config(outer, {**STEPS, "git_working_tree": "middle"})
    repo = init_gate_repo(inner / "tree", tracked_gate={})
    (repo.path / DEFAULT_CONFIG_RELATIVE_PATH).unlink()
    git(repo.path, "remote", "add", "origin", ORIGIN)

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    assert str(outer.resolve()) in str(info.value) and "not in the directory holding" in str(info.value)


def test_an_ancestor_config_is_refused_even_when_the_direct_parent_is_declared(tmp_path):
    outer = tmp_path / "outer"
    wrapper = outer / "wrapper"
    wrapper.mkdir(parents=True)
    write_deployment_config(outer, STEPS)
    write_deployment_config(wrapper, {"git_working_tree": "tree"})
    repo = init_gate_repo(wrapper / "tree", tracked_gate={})
    (repo.path / DEFAULT_CONFIG_RELATIVE_PATH).unlink()

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    assert str(outer.resolve()) in str(info.value)


def test_a_declaration_through_a_symlink_is_refused(tmp_path):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, {**STEPS, "git_working_tree": "link"})
    repo = init_gate_repo(wrapper / "real", tracked_gate={})
    (repo.path / DEFAULT_CONFIG_RELATIVE_PATH).unlink()
    (wrapper / "link").symlink_to(repo.path, target_is_directory=True)

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(wrapper / "link")

    assert "symlink" in str(info.value)


@pytest.mark.parametrize("declared", ["./tree", "tree/", "sub/../tree", "../wrapper/tree", ".", ".."])
def test_a_declaration_with_more_than_one_path_segment_is_refused(tmp_path, declared):
    (tmp_path / "wrapper" / "sub").mkdir(parents=True)
    _, repo = _wrapper_layout(tmp_path, {**STEPS, "git_working_tree": declared})

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    assert "single path segment" in str(info.value)


def test_a_declaration_that_names_another_directory_is_refused(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, {**STEPS, "git_working_tree": "elsewhere"})
    (wrapper / "elsewhere").mkdir()

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    assert "not the git tree" in str(info.value)


def test_an_unreadable_config_above_the_tree_is_refused(tmp_path):
    wrapper, repo = _wrapper_layout(tmp_path, STEPS)
    (wrapper / DEFAULT_CONFIG_RELATIVE_PATH).write_text("merge: [unclosed", encoding="utf-8")

    with pytest.raises(MergeConfigRootRefusedError) as info:
        post_merge_config.load_post_merge_steps(repo.path)

    assert "could not be read" in str(info.value)


@pytest.mark.parametrize("layout", [GATE, REQUIREMENTS], ids=["gate-keys", "merge-requirements"])
def test_gate_keys_and_requirements_above_the_tree_are_refused_like_steps(tmp_path, layout):
    _, repo = _wrapper_layout(tmp_path, layout)

    with pytest.raises(gate_config.InvalidMergeGateConfigError):
        gate_config.load_merge_requirements(repo.path)


# --- the merge verb end to end ------------------------------------------------------


def _merge(repo, capsys, *, flags=()):
    calls: list[str] = []
    inner = make_opener(pr_info=repo.pr_info())

    def opener(req, timeout=15):
        calls.append(f"{req.get_method()} {req.full_url}")
        return inner(req, timeout)

    code = verb.main(
        [*base_args(**{"--repo-path": str(repo.path)}), *flags],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    return code, capsys.readouterr().err, calls


def test_merge_refuses_an_undeclared_wrapper_layout_before_merging(tmp_path, capsys):
    wrapper, repo = _wrapper_layout(tmp_path, STEPS, tracked_gate={})

    code, err, calls = _merge(repo, capsys)

    assert code == verb.EXIT_USAGE
    assert str(wrapper.resolve()) in err and str(repo.path.resolve()) in err and ORIGIN in err
    assert not [call for call in calls if call.endswith("/merge")]


def test_merge_with_skip_post_merge_still_refuses_an_undeclared_wrapper_layout(tmp_path, capsys):
    _, repo = _wrapper_layout(tmp_path, STEPS, tracked_gate={})

    code, err, calls = _merge(repo, capsys, flags=["--skip-post-merge"])

    assert code == verb.EXIT_USAGE
    assert not [call for call in calls if call.endswith("/merge")]


def test_merge_accepts_a_declared_wrapper_layout(tmp_path, capsys):
    _, repo = _wrapper_layout(
        tmp_path,
        {**STEPS, "git_working_tree": "tree", "sync_tree_after_merge": False},
        tracked_gate={},
    )

    code, err, calls = _merge(repo, capsys)

    assert code == verb.EXIT_OK, err
    assert [call for call in calls if call.endswith("/merge")]


def test_merge_refuses_a_declared_layout_whose_tree_is_on_another_host(tmp_path, capsys):
    _, repo = _wrapper_layout(
        tmp_path,
        {**STEPS, "git_working_tree": "tree", "sync_tree_after_merge": False},
        tracked_gate={},
        origin=f"https://other.example.test/{SLUG}",
    )

    code, err, calls = _merge(repo, capsys)

    assert code == verb.EXIT_USAGE and "other.example.test" in err
    assert not [call for call in calls if call.endswith("/merge")]


# --- every reader goes through the one resolver -------------------------------------

READERS = {
    "load_post_merge_steps": lambda p: post_merge_config.load_post_merge_steps(p),
    "post_merge_steps_key_declared": lambda p: post_merge_config.post_merge_steps_key_declared(p),
    "resolve_git_working_tree": lambda p: post_merge_config.resolve_git_working_tree(p),
    "resolve_git_tree_relative_config_paths": lambda p: post_merge_config.resolve_git_tree_relative_config_paths(p, p),
    "resolve_sync_tree_after_merge": lambda p: post_merge_config.resolve_sync_tree_after_merge(p),
    "resolve_enforce_merge_shape": lambda p: post_merge_config.resolve_enforce_merge_shape(p),
    "resolve_enforce_single_verdict_fence": lambda p: post_merge_config.resolve_enforce_single_verdict_fence(p),
    "resolve_require_model_attestation": lambda p: post_merge_config.resolve_require_model_attestation(p),
    "resolve_model_attestation_denylist": lambda p: post_merge_config.resolve_model_attestation_denylist(p),
    "resolve_post_merge_step_timeout_seconds": lambda p: post_merge_config.resolve_post_merge_step_timeout_seconds(p),
    "load_merge_requirements": lambda p: gate_config.load_merge_requirements(p),
    "load_required_reviewer_roles": lambda p: gate_config.load_required_reviewer_roles(p),
    "load_authorized_roles": lambda p: gate_config.load_authorized_roles(p),
    "load_required_scanners": lambda p: gate_config.load_required_scanners(p),
    "load_repo_gate_declarations": lambda p: gate_config.load_repo_gate_declarations(p),
    "load_pre_checks": lambda p: pre_checks_config.load_pre_checks(p),
    "resolve_pre_checks_env_passthrough": lambda p: pre_checks_config.resolve_pre_checks_env_passthrough(p),
    "decide_pre_checks_env_passthrough": lambda p: pre_checks_config.decide_pre_checks_env_passthrough(p),
    "gate_from_deployment_config": lambda p: repo_gate_runtime.gate_from_deployment_config(p),
    "ignored_deployment_gate_warnings": lambda p: repo_gate_runtime.ignored_deployment_gate_warnings(p),
    "ignored_repo_sandbox_warnings": lambda p: pre_checks_sandbox.ignored_repo_sandbox_warnings(p),
}

#: Readers whose contract is to report a failure to their caller rather than raise.
DEGRADING = {
    "gate_from_deployment_config": lambda result: bool(result.pre_checks_error),
    "ignored_deployment_gate_warnings": lambda result: result == (),
    "ignored_repo_sandbox_warnings": lambda result: result == (),
}


@pytest.fixture
def resolver_calls(monkeypatch):
    calls: list[str] = []
    for name in ("resolve_bound_config_root", "resolve_bound_config_path"):
        real = getattr(repo_config, name)

        def spy(*args, _real=real, _name=name, **kwargs):
            calls.append(_name)
            return _real(*args, **kwargs)

        monkeypatch.setattr(config_root, name, spy)
    return calls


@pytest.mark.parametrize("name", sorted(READERS))
def test_every_reader_resolves_the_config_through_the_one_resolver(tmp_path, resolver_calls, name):
    _, repo = _wrapper_layout(tmp_path, {"git_working_tree": "tree"})

    try:
        READERS[name](repo.path)
    except (PostMergeConfigError, gate_config.InvalidMergeGateConfigError):
        # A declared wrapper that omits a key the reader requires; the question
        # here is only which resolver found the file.
        pass

    assert resolver_calls, f"{name} did not use merge.config_root"


@pytest.mark.parametrize("name", sorted(READERS))
def test_every_reader_refuses_loudly_for_an_undeclared_wrapper_config(tmp_path, name):
    _, repo = _wrapper_layout(tmp_path, {**STEPS, **GATE, **REQUIREMENTS})

    if name in DEGRADING:
        assert DEGRADING[name](READERS[name](repo.path))
        return
    with pytest.raises((PostMergeConfigError, gate_config.InvalidMergeGateConfigError)) as info:
        READERS[name](repo.path)
    assert "above the git tree" in str(info.value)


def test_no_merge_module_reads_the_config_through_the_generic_resolvers():
    offenders = []
    for path in sorted((SRC / "merge").glob("*.py")):
        if path.name == "config_root.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        } | {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        used = names & {"resolve_repo_config_path", "resolve_repo_config_root"}
        if used:
            offenders.append(f"{path.name}: {sorted(used)}")
    assert not offenders, offenders


def test_describe_origin_drops_credentials_and_names_a_missing_origin(tmp_path):
    from clagentic_loadout.git_origin import describe_origin

    repo = init_gate_repo(tmp_path / "tree", tracked_gate={})
    assert describe_origin(repo.path) == "<no readable origin>"
    git(repo.path, "remote", "add", "origin", f"https://user:s3cr3t@{HOST}/{SLUG}.git")

    assert describe_origin(repo.path) == f"https://{HOST}/{SLUG}.git"
    assert describe_origin(tmp_path / "missing") == "<no readable origin>"


def test_the_resolver_vocabulary_covers_every_gate_key():
    vocabulary = config_root._vocabulary()

    assert set(repo_gate_runtime.GATE_KEYS) <= set(vocabulary["guarded_keys"])
    assert {"post_merge_steps", "merge_requirements"} <= set(vocabulary["guarded_keys"])
    assert vocabulary["declaration_key"] == "git_working_tree"
