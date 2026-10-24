"""The merge verbs bind --repo-path to the repository before the two inputs that
are not content-addressed are taken from it: the deployment-file gate used when
the base commit has no tracked gate file, and post_merge_steps.

Caller shapes (each against a real git tree with a real origin remote):
  M1 an HTTPS origin on the configured host, M2 an SSH origin on the same host,
  M3 a Forgejo-style configured host that is not the GitHub host, M4 the
  standalone post-merge verb, M5 no --repo-path, M6 trees that must be refused:
  the same owner/name on another host, a tree with no origin, a non-git
  directory, an origin that is not a recognised remote."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from clagentic_loadout.git_origin import ConfigTreeBinding
from clagentic_loadout.merge import post_merge_verb, verb
from clagentic_loadout.merge.config_binding import (
    config_binding_for,
    require_confirmed_tree_for_steps,
    require_repo_path_in_git_tree,
)
from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.repo_gate_runtime import load_gate_at_commit, load_repo_gate_at_base
from tests._gate_repo import git, init_gate_repo, write_deployment_config
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)
from tests._support.fakes import FakeResponse

HOST = "git-host.example.test"
SLUG = "some-owner/some-repo"
FALLBACK_NOTICE = "gate keys read from deployment config.yaml"
GATE = {"required_reviewer_roles": ["reviewer"]}
STEPS = {"post_merge_steps": [{"cmd": "true"}]}


@pytest.fixture(autouse=True)
def _configured_host(monkeypatch):
    monkeypatch.setenv("CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL", f"http://{HOST}:3000")


def _repo(tmp_path, origin, **kwargs):
    repo = init_gate_repo(tmp_path / "tree", **kwargs)
    if origin is not None:
        git(repo.path, "remote", "add", "origin", origin)
    return repo


def _merge(repo, capsys, *, opener=None, flags=()):
    code = verb.main(
        [*base_args(**{"--repo-path": str(repo.path)}), *flags],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener or make_opener(pr_info=repo.pr_info()),
    )
    return code, capsys.readouterr().err


@pytest.mark.parametrize(
    "origin",
    [
        f"http://{HOST}:3000/{SLUG}.git",
        f"https://{HOST}/Some-Owner/Some-Repo",
        f"git@{HOST}:{SLUG}.git",
        f"ssh://git@{HOST}:2222/{SLUG}",
    ],
    ids=["http-base-url", "https-case-insensitive", "scp-ssh", "ssh-url"],
)
def test_the_fallback_gate_is_read_from_a_tree_on_the_configured_host_in_either_remote_form(
    tmp_path, capsys, origin
):
    repo = _repo(tmp_path, origin, tracked_gate=None, deployment_merge=GATE)

    _, err = _merge(repo, capsys)

    assert FALLBACK_NOTICE in err
    assert "are not used" not in err


def test_the_fallback_gate_is_not_read_from_the_same_owner_and_name_on_another_host(tmp_path, capsys):
    repo = _repo(
        tmp_path,
        f"https://other.example.test/{SLUG}.git",
        tracked_gate=None,
        deployment_merge=GATE,
    )

    code, err = _merge(repo, capsys)

    assert code != verb.EXIT_OK
    assert FALLBACK_NOTICE not in err
    assert "other.example.test" in err and HOST in err
    assert repr(SLUG) in err and f"https://other.example.test/{SLUG}.git" not in err


@pytest.mark.parametrize(
    "origin, needle",
    [
        (None, "no readable 'origin'"),
        ("/srv/mirror/some-owner/some-repo.git", "not a parseable remote URL"),
        (f"https://{HOST}@other.example.test/{SLUG}", "other.example.test"),
    ],
    ids=["no-origin", "local-path-origin", "userinfo-naming-the-host"],
)
def test_the_fallback_gate_is_not_read_from_an_unconfirmable_tree(tmp_path, capsys, origin, needle):
    repo = _repo(tmp_path, origin, tracked_gate=None, deployment_merge=GATE)

    code, err = _merge(repo, capsys)

    assert code != verb.EXIT_OK
    assert FALLBACK_NOTICE not in err and needle in err


def test_a_tree_that_declares_no_fallback_gate_needs_no_origin(tmp_path, capsys):
    repo = _repo(tmp_path, None, tracked_gate=None)

    code, err = _merge(repo, capsys)

    assert code == verb.EXIT_OK
    assert "are not used" not in err


def test_the_tracked_gate_at_base_is_read_whatever_the_origin(tmp_path, capsys):
    repo = _repo(tmp_path, f"https://other.example.test/{SLUG}", tracked_gate={})

    code, err = _merge(repo, capsys)

    assert code == verb.EXIT_OK
    assert "are not used" not in err


def test_the_origin_is_read_not_the_tracking_remote(tmp_path, capsys):
    repo = _repo(tmp_path, f"https://{HOST}/{SLUG}", tracked_gate=None, deployment_merge=GATE)
    git(repo.path, "remote", "add", "upstream", f"https://other.example.test/{SLUG}")
    git(repo.path, "config", "branch.main.remote", "upstream")
    git(repo.path, "config", "branch.main.merge", "refs/heads/main")

    _, err = _merge(repo, capsys)

    assert FALLBACK_NOTICE in err


def test_a_foreign_origin_is_refused_even_when_the_tracking_remote_is_the_configured_host(
    tmp_path, capsys
):
    repo = _repo(
        tmp_path,
        f"https://other.example.test/{SLUG}",
        tracked_gate=None,
        deployment_merge=GATE,
    )
    git(repo.path, "remote", "add", "upstream", f"https://{HOST}/{SLUG}")
    git(repo.path, "config", "branch.main.remote", "upstream")
    git(repo.path, "config", "branch.main.merge", "refs/heads/main")

    _, err = _merge(repo, capsys)

    assert FALLBACK_NOTICE not in err and "other.example.test" in err


def test_the_github_host_binds_a_github_platform_merge_to_github_com(tmp_path):
    binding = config_binding_for("github", "http://unrelated.example.test", SLUG)
    repo = _repo(tmp_path, f"git@github.com:{SLUG}.git", tracked_gate=None, deployment_merge=GATE)

    gate = load_gate_at_commit(
        repo.path, repo.base_sha, deployment_repo_path=repo.path, config_binding=binding
    )

    assert gate.notices and FALLBACK_NOTICE in gate.notices[0]


def test_a_forgejo_tree_is_not_accepted_for_a_github_platform_merge(tmp_path):
    binding = config_binding_for("github", f"http://{HOST}:3000", SLUG)
    repo = _repo(tmp_path, f"https://{HOST}/{SLUG}", tracked_gate=None, deployment_merge=GATE)

    gate = load_gate_at_commit(
        repo.path, repo.base_sha, deployment_repo_path=repo.path, config_binding=binding
    )

    assert gate.pre_checks_error and "github.com" in gate.pre_checks_error


def test_an_unbound_library_caller_keeps_the_fallback_it_always_had(tmp_path):
    repo = _repo(tmp_path, None, tracked_gate=None, deployment_merge=GATE)

    gate = load_repo_gate_at_base(repo.path, base_sha=repo.base_sha)

    assert gate.notices and not gate.pre_checks_error


def test_the_fallback_gate_above_a_tree_without_a_declaration_refuses_bound_or_not(tmp_path):
    outer = tmp_path / "wrapper"
    outer.mkdir()
    write_deployment_config(outer, GATE)
    repo = init_gate_repo(outer / "tree", tracked_gate=None)
    (repo.path / ".clagentic" / "loadout" / "config.yaml").unlink()
    git(repo.path, "remote", "add", "origin", f"https://{HOST}/{SLUG}")
    binding = ConfigTreeBinding(HOST, SLUG)

    bound = load_gate_at_commit(
        repo.path, repo.base_sha, deployment_repo_path=repo.path, config_binding=binding
    )
    unbound = load_gate_at_commit(repo.path, repo.base_sha, deployment_repo_path=repo.path)

    for gate in (bound, unbound):
        assert not gate.notices
        assert gate.pre_checks_error and str(outer) in gate.pre_checks_error


# --- post_merge_steps: loadout-merge ------------------------------------------------


def _merge_calls_recorded(repo):
    calls: list[str] = []
    inner = make_opener(pr_info=repo.pr_info())

    def opener(req, timeout=15):
        calls.append(f"{req.get_method()} {req.full_url}")
        return inner(req, timeout)

    return opener, calls


def test_merge_refuses_steps_from_a_foreign_tree_before_merging(tmp_path, capsys):
    repo = _repo(
        tmp_path, f"https://other.example.test/{SLUG}", tracked_gate={}, deployment_merge=STEPS
    )
    opener, calls = _merge_calls_recorded(repo)

    code, err = _merge(repo, capsys, opener=opener)

    assert code == verb.EXIT_USAGE
    assert "post_merge_steps" in err and "other.example.test" in err and HOST in err
    assert not [call for call in calls if call.startswith("POST") and call.endswith("/merge")]


def test_merge_refuses_steps_from_a_tree_with_no_origin_before_merging(tmp_path, capsys):
    repo = _repo(tmp_path, None, tracked_gate={}, deployment_merge=STEPS)
    opener, calls = _merge_calls_recorded(repo)

    code, err = _merge(repo, capsys, opener=opener)

    assert code == verb.EXIT_USAGE and "no readable 'origin'" in err
    assert not [call for call in calls if call.endswith("/merge")]


def test_report_only_also_refuses_steps_from_a_foreign_tree(tmp_path, capsys):
    repo = _repo(
        tmp_path, f"https://other.example.test/{SLUG}", tracked_gate={}, deployment_merge=STEPS
    )

    code, err = _merge(repo, capsys, flags=["--report-only"])

    assert code == verb.EXIT_USAGE and "other.example.test" in err


def test_a_wrapper_directory_cannot_name_the_tree_that_vouches_for_its_steps(tmp_path):
    # The wrapper's config declares a working tree that IS a confirmed clone and
    # supplies the steps. One tree supplies origin, fallback config and steps:
    # the git top-level of --repo-path. The wrapper is not a git tree, so the
    # declared clone must not stand in for it.
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, {**STEPS, "git_working_tree": "inner"})
    inner = wrapper / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    git(inner, "remote", "add", "origin", f"https://{HOST}/{SLUG}")

    with pytest.raises(MergeUsageError) as info:
        require_confirmed_tree_for_steps(
            config_binding_for("forgejo", f"http://{HOST}:3000", SLUG), str(wrapper)
        )

    assert str(wrapper) in str(info.value) and "not inside a git work tree" in str(info.value)


def test_the_fallback_gate_is_not_confirmed_through_a_tree_the_config_declares(tmp_path):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, {**GATE, "git_working_tree": "inner"})
    repo = init_gate_repo(wrapper / "inner", tracked_gate=None)
    (repo.path / ".clagentic" / "loadout" / "config.yaml").unlink()
    git(repo.path, "remote", "add", "origin", f"https://{HOST}/{SLUG}")
    binding = ConfigTreeBinding(HOST, SLUG)

    gate = load_repo_gate_at_base(
        wrapper, base_sha=repo.base_sha, config_binding=binding
    )

    assert not gate.notices
    assert gate.pre_checks_error and "not inside a git work tree" in gate.pre_checks_error


# --- a remote's credentials never reach a message or output ------------------------

TOKEN = "s3cr3t-token-value"


def _merge_both_streams(repo, capsys, *, flags=()):
    code = verb.main(
        [*base_args(**{"--repo-path": str(repo.path)}), *flags],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=make_opener(pr_info=repo.pr_info()),
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.mark.parametrize(
    "origin",
    [
        f"https://user:{TOKEN}@other.example.test/{SLUG}.git",
        f"https://{TOKEN}@other.example.test/{SLUG}",
        f"https://user:{TOKEN}@{HOST}/{SLUG}/extra/segments",
        f"https://user:{TOKEN}@{HOST}/{SLUG}?x=1",
    ],
    ids=["foreign-host", "token-only-userinfo", "unparseable-path", "unparseable-query"],
)
@pytest.mark.parametrize("deployment", [GATE, STEPS], ids=["fallback-gate", "post-merge-steps"])
def test_a_refused_origin_carrying_a_token_never_prints_it(tmp_path, capsys, origin, deployment):
    repo = _repo(tmp_path, origin, tracked_gate=None if deployment is GATE else {}, deployment_merge=deployment)

    code, out, err = _merge_both_streams(repo, capsys)

    assert code != verb.EXIT_OK
    assert TOKEN not in out and TOKEN not in err
    assert "--repo-path" in err or "gate keys" in err


def test_a_refused_origin_carrying_a_token_names_the_parsed_parts_and_the_hosts(tmp_path, capsys):
    repo = _repo(
        tmp_path,
        f"https://user:{TOKEN}@other.example.test/{SLUG}.git",
        tracked_gate={},
        deployment_merge=STEPS,
    )

    code, out, err = _merge_both_streams(repo, capsys)

    assert code == verb.EXIT_USAGE
    assert "other.example.test" in err and repr(SLUG) in err and HOST in err
    assert f"https://other.example.test/{SLUG}.git" not in err
    assert TOKEN not in out and TOKEN not in err


def test_post_merge_refusal_of_an_origin_carrying_a_token_never_prints_it(tmp_path, capsys):
    repo = _repo(
        tmp_path,
        f"https://user:{TOKEN}@other.example.test/{SLUG}",
        tracked_gate={},
        deployment_merge=STEPS,
    )

    code, out, err = _post_merge(repo, capsys)

    assert code == post_merge_verb.EXIT_USAGE
    assert "other.example.test" in err
    assert TOKEN not in out and TOKEN not in err


def test_the_slug_consistency_refusal_does_not_print_a_token_either(tmp_path, capsys):
    repo = _repo(
        tmp_path, f"https://user:{TOKEN}@{HOST}/a/b/c/d", tracked_gate={}, deployment_merge=STEPS
    )

    code, out, err = _merge_both_streams(repo, capsys)

    assert code != verb.EXIT_OK
    assert TOKEN not in out and TOKEN not in err


def test_merge_with_skip_post_merge_does_not_ask_for_a_confirmed_tree(tmp_path, capsys):
    repo = _repo(tmp_path, None, tracked_gate={}, deployment_merge=STEPS)

    code, _ = _merge(repo, capsys, flags=["--skip-post-merge"])

    assert code == verb.EXIT_OK


def test_merge_with_no_repo_path_is_unchanged(capsys):
    code = verb.main(
        base_args(),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=make_opener(),
    )

    assert code == verb.EXIT_OK
    assert "refused as the source" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "origin",
    [f"https://{HOST}/{SLUG}.git", f"git@{HOST}:{SLUG}.git"],
    ids=["https", "ssh"],
)
def test_steps_from_a_confirmed_tree_in_either_remote_form_are_not_refused(tmp_path, origin):
    repo = _repo(tmp_path, origin, tracked_gate={}, deployment_merge=STEPS)

    require_confirmed_tree_for_steps(config_binding_for("forgejo", f"http://{HOST}:3000", SLUG), str(repo.path))


def test_a_tree_that_declares_no_steps_is_not_asked_for_an_origin(tmp_path):
    repo = _repo(tmp_path, None, tracked_gate={})

    require_confirmed_tree_for_steps(config_binding_for("forgejo", f"http://{HOST}:3000", SLUG), str(repo.path))


def test_a_directory_that_is_not_a_git_tree_with_steps_is_refused_naming_it(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    write_deployment_config(plain, STEPS)

    with pytest.raises(MergeUsageError) as info:
        require_confirmed_tree_for_steps(
            config_binding_for("forgejo", f"http://{HOST}:3000", SLUG), str(plain)
        )

    assert str(plain) in str(info.value) and "not inside a git work tree" in str(info.value)


def test_steps_declared_above_the_tree_without_a_declaration_are_refused_not_dropped(tmp_path):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, STEPS)
    repo = init_gate_repo(wrapper / "tree", tracked_gate={})
    (repo.path / ".clagentic" / "loadout" / "config.yaml").unlink()
    git(repo.path, "remote", "add", "origin", f"https://{HOST}/{SLUG}")

    with pytest.raises(MergeUsageError) as info:
        require_confirmed_tree_for_steps(
            config_binding_for("forgejo", f"http://{HOST}:3000", SLUG), str(repo.path)
        )

    message = str(info.value)
    assert str(repo.path) in message and str(wrapper) in message
    assert repr(SLUG) in message and repr(HOST) in message
    assert f"https://{HOST}/{SLUG}" not in message


# --- post_merge_steps: loadout-post-merge (M4) -------------------------------------


def _post_merge(repo, capsys):
    pr_info = {"merged": True, "merge_commit_sha": None, "base": {"ref": "main"}}

    def opener(req, timeout=15):
        assert req.get_method() == "GET", req.full_url
        return FakeResponse(200, json.dumps(pr_info).encode("utf-8"))

    argv = [
        "--platform", "forgejo", "--role", "merger", "--authorized-role", "merger",
        "--repo", SLUG, "--pr", "1", "--repo-path", str(repo.path),
    ]
    code = post_merge_verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_post_merge_refuses_a_foreign_tree_before_any_checkout(tmp_path, capsys):
    repo = _repo(
        tmp_path, f"https://other.example.test/{SLUG}", tracked_gate={}, deployment_merge=STEPS
    )
    head_before = git(repo.path, "rev-parse", "HEAD")

    code, _out, err = _post_merge(repo, capsys)

    assert code == post_merge_verb.EXIT_USAGE
    assert "other.example.test" in err and HOST in err
    assert git(repo.path, "rev-parse", "HEAD") == head_before


def test_post_merge_refuses_an_unconfirmable_tree_with_steps(tmp_path, capsys):
    repo = _repo(tmp_path, None, tracked_gate={}, deployment_merge=STEPS)

    code, _out, err = _post_merge(repo, capsys)

    assert code == post_merge_verb.EXIT_USAGE and "no readable 'origin'" in err


# --- --repo-path is the git tree ------------------------------------------------------


def _declared_wrapper(tmp_path, deployment, *, origin=f"https://{HOST}/{SLUG}"):
    """A non-git wrapper X declaring the git tree X/tree (which holds no config)."""
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, {**deployment, "git_working_tree": "tree"})
    repo = init_gate_repo(wrapper / "tree", tracked_gate=None)
    (repo.path / ".clagentic" / "loadout" / "config.yaml").unlink()
    git(repo.path, "remote", "add", "origin", origin)
    return wrapper, repo


def test_the_fallback_gate_declared_at_the_wrapper_is_read_for_the_git_tree(tmp_path, capsys):
    # The supported form: X declares gate keys, --repo-path is T, and the
    # binding is confirmed against T.
    _wrapper, repo = _declared_wrapper(tmp_path, {**GATE, "sync_tree_after_merge": False})

    _, err = _merge(repo, capsys)

    assert FALLBACK_NOTICE in err
    assert "are not used" not in err


def test_the_fallback_gate_declared_at_the_wrapper_is_not_read_for_a_foreign_tree(tmp_path, capsys):
    _wrapper, repo = _declared_wrapper(
        tmp_path, {**GATE, "sync_tree_after_merge": False}, origin=f"https://other.example.test/{SLUG}"
    )

    code, err = _merge(repo, capsys)

    assert code != verb.EXIT_OK
    assert FALLBACK_NOTICE not in err and "other.example.test" in err


def test_a_directory_outside_any_git_tree_is_refused_naming_it_and_no_remote(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(MergeUsageError) as info:
        require_repo_path_in_git_tree(str(plain))

    message = str(info.value)
    assert str(plain.resolve()) in message and "not inside a git working tree" in message
    assert "pass " not in message


def test_a_declared_wrapper_is_refused_with_the_directory_to_pass(tmp_path):
    wrapper, repo = _declared_wrapper(tmp_path, STEPS)

    with pytest.raises(MergeUsageError) as info:
        require_repo_path_in_git_tree(str(wrapper))

    message = str(info.value)
    assert f"pass {str(repo.path.resolve())!r} instead" in message
    assert HOST not in message and SLUG not in message


def test_a_tree_and_a_directory_inside_it_are_accepted(tmp_path):
    repo = _repo(tmp_path, None, tracked_gate={})
    inside = repo.path / "sub"
    inside.mkdir()

    require_repo_path_in_git_tree(str(repo.path))
    require_repo_path_in_git_tree(str(inside))


def test_a_path_that_does_not_exist_is_refused(tmp_path):
    with pytest.raises(MergeUsageError) as info:
        require_repo_path_in_git_tree(str(tmp_path / "absent"))

    assert "not inside a git working tree" in str(info.value)


def _called_names_in_run(module) -> list[tuple[str, int]]:
    source = Path(module.__file__).read_text(encoding="utf-8")
    run = next(
        node for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.FunctionDef) and node.name == "_run"
    )
    return sorted(
        (node.func.id, node.lineno)
        for node in ast.walk(run)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    )


@pytest.mark.parametrize("module", [verb, post_merge_verb], ids=["merge", "post-merge"])
def test_both_verbs_require_the_git_tree_before_the_origin_binding_or_any_mint(module):
    calls = _called_names_in_run(module)
    first = {name: min(line for n, line in calls if n == name) for name, _ in calls}

    assert "require_repo_path_in_git_tree" in first
    assert first["require_repo_path_in_git_tree"] < first["assert_repo_path_consistent"]
    assert first["require_repo_path_in_git_tree"] < first["_resolve_backend"]
