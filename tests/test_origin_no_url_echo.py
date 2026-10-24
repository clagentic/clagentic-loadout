"""No merge-verb or binding output echoes the origin URL, raw or redacted.

A token can sit in the user part, the query, the fragment or the path of a
remote, in an https, ssh:// or scp-like form, or in a string that is not a
remote at all. Every refusal below must keep it out of BOTH stdout and stderr:
the unparseable origin, a host mismatch, an owner/name mismatch, the config-root
refusal and the --repo / --repo-path consistency check. Refusals name only the
remote name, the parsed host and owner/name and the resolved paths."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout import git_origin
from clagentic_loadout.merge import post_merge_verb, verb
from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.repo_path_consistency import (
    assert_repo_path_consistent,
    check_repo_path_consistency,
)
from tests._gate_repo import git, init_gate_repo, write_deployment_config
from tests._support.fakes import FakeResponse
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HOST = "git-host.example.test"
OTHER_HOST = "other.example.test"
SLUG = "some-owner/some-repo"
OTHER_SLUG = "other-owner/other-repo"
TOKEN = "tok3n-must-never-print"
STEPS = {"post_merge_steps": [{"cmd": "true"}]}
GATE = {"required_reviewer_roles": ["reviewer"]}

#: Each form places the token somewhere a remote can carry one.
FORMS = {
    "https-userinfo": "https://user:{token}@{host}/{slug}.git",
    "https-query": "https://{host}/{slug}?token={token}",
    "https-fragment": "https://{host}/{slug}#{token}",
    "https-path": "https://{host}/{slug}/{token}",
    "ssh-userinfo": "ssh://{token}@{host}/{slug}",
    "ssh-query": "ssh://git@{host}/{slug}?{token}",
    "scp-userinfo": "{token}@{host}:{slug}",
    "scp-query": "git@{host}:{slug}?token={token}",
    "scp-fragment": "git@{host}:{slug}#{token}",
    "scp-path": "git@{host}:{slug}/{token}",
    "junk-no-scheme": "{token}/not-a-remote",
    "junk-bare": "{token}",
}


def _origin(form: str, *, host: str = HOST, slug: str = SLUG) -> str:
    return FORMS[form].format(token=TOKEN, host=host, slug=slug)


@pytest.fixture(autouse=True)
def _configured_host(monkeypatch):
    monkeypatch.setenv("CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL", f"http://{HOST}:3000")


def _tree(tmp_path, origin, **kwargs):
    repo = init_gate_repo(tmp_path / "tree", **kwargs)
    git(repo.path, "remote", "add", "origin", origin)
    return repo


def _run_merge(repo, capsys):
    code = verb.main(
        base_args(**{"--repo-path": str(repo.path)}),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=make_opener(pr_info=repo.pr_info()),
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _assert_token_free(out: str, err: str) -> None:
    assert TOKEN not in out, "token reached stdout"
    assert TOKEN not in err, "token reached stderr"


@pytest.mark.parametrize("form", sorted(FORMS))
def test_a_host_mismatch_or_unparseable_origin_with_steps_never_prints_the_token(tmp_path, capsys, form):
    repo = _tree(tmp_path, _origin(form, host=OTHER_HOST), tracked_gate={}, deployment_merge=STEPS)

    code, out, err = _run_merge(repo, capsys)

    assert code == verb.EXIT_USAGE
    _assert_token_free(out, err)
    assert "post_merge_steps" in err
    assert "origin" in err


@pytest.mark.parametrize("form", sorted(FORMS))
def test_an_owner_and_name_mismatch_or_unparseable_origin_never_prints_the_token(tmp_path, capsys, form):
    repo = _tree(tmp_path, _origin(form, slug=OTHER_SLUG), tracked_gate={}, deployment_merge=STEPS)

    code, out, err = _run_merge(repo, capsys)

    assert code != verb.EXIT_OK
    _assert_token_free(out, err)


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_fallback_gate_refusal_never_prints_the_token(tmp_path, capsys, form):
    repo = _tree(tmp_path, _origin(form, host=OTHER_HOST), tracked_gate=None, deployment_merge=GATE)

    code, out, err = _run_merge(repo, capsys)

    assert code != verb.EXIT_OK
    _assert_token_free(out, err)


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_config_root_refusal_never_prints_the_token(tmp_path, capsys, form):
    wrapper = tmp_path / "wrapper"
    wrapper.mkdir()
    write_deployment_config(wrapper, STEPS)
    repo = init_gate_repo(wrapper / "tree", tracked_gate={})
    (repo.path / ".clagentic" / "loadout" / "config.yaml").unlink()
    git(repo.path, "remote", "add", "origin", _origin(form))

    code, out, err = _run_merge(repo, capsys)

    assert code == verb.EXIT_USAGE
    _assert_token_free(out, err)
    assert str(wrapper.resolve()) in err


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_post_merge_verb_refusal_never_prints_the_token(tmp_path, capsys, form):
    repo = _tree(tmp_path, _origin(form, host=OTHER_HOST), tracked_gate={}, deployment_merge=STEPS)
    pr_info = {"merged": True, "merge_commit_sha": None, "base": {"ref": "main"}}

    def opener(req, timeout=15):
        return FakeResponse(200, json.dumps(pr_info).encode("utf-8"))

    code = post_merge_verb.main(
        [
            "--platform", "forgejo", "--role", "merger", "--authorized-role", "merger",
            "--repo", SLUG, "--pr", "1", "--repo-path", str(repo.path),
        ],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()

    assert code == post_merge_verb.EXIT_USAGE
    _assert_token_free(captured.out, captured.err)


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_repo_path_consistency_check_never_prints_the_token(tmp_path, form):
    tree = tmp_path / "tree"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "remote", "add", "origin", _origin(form, slug=OTHER_SLUG))

    result = check_repo_path_consistency(SLUG, str(tree))
    try:
        assert_repo_path_consistent(SLUG, str(tree))
        raised = ""
    except MergeUsageError as exc:
        raised = str(exc)

    assert TOKEN not in result.reason and TOKEN not in result.remote_owner_repo
    assert TOKEN not in raised
    if result.checked:
        assert raised and OTHER_SLUG in raised
    else:
        assert "not a parseable remote URL" in result.reason and not raised


def test_a_query_bearing_https_origin_is_unparseable_not_a_slug_with_a_query(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    git(tree, "init", "-q", "-b", "main")
    git(tree, "remote", "add", "origin", f"https://{HOST}/{OTHER_SLUG}?token={TOKEN}")

    result = check_repo_path_consistency(SLUG, str(tree))

    assert not result.checked and result.remote_owner_repo == ""
    assert TOKEN not in result.reason


def test_the_merge_consistency_refusal_with_a_query_bearing_origin_never_prints_the_token(
    tmp_path, capsys
):
    repo = _tree(
        tmp_path, f"https://{HOST}/{OTHER_SLUG}?token={TOKEN}", tracked_gate={}, deployment_merge=STEPS
    )

    code, out, err = _run_merge(repo, capsys)

    assert code == verb.EXIT_USAGE
    _assert_token_free(out, err)


def test_the_url_echoing_helpers_are_gone_from_the_source_tree():
    from pathlib import Path

    assert not hasattr(git_origin, "redact_remote_url")
    assert not hasattr(git_origin, "describe_origin")
    assert "url" not in git_origin.OriginTarget.__dataclass_fields__
    source = Path(git_origin.__file__).parent
    offenders = [
        path.name
        for path in sorted(source.rglob("*.py"))
        if "redact_remote_url" in path.read_text(encoding="utf-8")
        or "describe_origin" in path.read_text(encoding="utf-8")
    ]
    assert not offenders, offenders
