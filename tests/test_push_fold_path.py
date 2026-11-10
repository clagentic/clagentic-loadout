"""test_push_fold_path.py -- the push verb's PR step against a stub forge, for
both platforms.

Coverage:
  - A create whose PR-create call returns 409/422 is the success it reports
    when exactly one open PR from this branch to --base is found (exit 0,
    pre_existing, title/body not applied); any other lookup outcome keeps the
    exit-4 failure.
  - The fold form (no --title): a branch with one open PR pushes, makes no
    create call and reports that PR; a branch with none exits with the usage
    code before any ref moves, naming both forms.
  - Any other create failure (401/403/500/network) is unchanged: exit 4, and
    no lookup is attempted.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from clagentic_loadout.push import verb
from tests._support.push_verb import (  # noqa: F401  (isolate_user_config_root is an autouse fixture)
    RecordingTokenProvider,
    StubForge,
    isolate_user_config_root,
    repo_with_remote,
    run_main,
    use_local_fetch_url,
)

PLATFORMS = ["forgejo", "github"]


def _argv(repo, remote, platform, *extra):
    argv = ["--repo-path", str(repo), "--platform", platform]
    if platform == "github":
        use_local_fetch_url(repo, remote)
        argv += ["--repo", "some-owner/some-repo"]
    return argv + list(extra)


def _create_argv(repo, remote, platform):
    return _argv(repo, remote, platform, "--title", "feat: t", "--body-stdin")


def _remote_branches(remote) -> str:
    return subprocess.run(
        ["git", "branch"], cwd=str(remote), capture_output=True, text=True, check=True
    ).stdout


def _stdout_payload(capsys):
    captured = capsys.readouterr()
    return json.loads(captured.out.strip().splitlines()[-1]), captured


def _run(argv, forge, monkeypatch, *, stdin_text=None):
    return run_main(
        argv,
        token_provider=RecordingTokenProvider(),
        opener=forge,
        stdin_text=stdin_text,
        monkeypatch=monkeypatch,
    )


BODY = json.dumps({"body": "some body"})


@pytest.mark.parametrize("platform", PLATFORMS)
class TestRedundantCreateIsSuccess:
    @pytest.mark.parametrize("status", [409, 422])
    def test_one_open_pr_reports_it_and_exits_zero(
        self, repo_with_remote, monkeypatch, capsys, platform, status
    ):
        repo, remote = repo_with_remote
        forge = StubForge(open_prs=[7], create_status=status)
        code = _run(_create_argv(repo, remote, platform), forge, monkeypatch, stdin_text=BODY)
        assert code == verb.EXIT_OK

        payload, captured = _stdout_payload(capsys)
        assert payload["pr_number"] == 7
        assert payload["pre_existing"] is True
        assert payload["pr_url"].endswith("/7") or payload["pr_url"].endswith("/pull/7")
        assert "already exists" in captured.out
        assert "not applied" in captured.out
        assert "--update-pr --pr 7" in captured.out
        assert "creation failed" not in captured.err
        assert "feature" in _remote_branches(remote)

    @pytest.mark.parametrize(
        "forge",
        [
            StubForge(open_prs=[], create_status=409),
            StubForge(open_prs=[7, 8], create_status=409),
            StubForge(open_prs=[7], create_status=422, list_status=500),
        ],
        ids=["no-match", "several", "lookup-error"],
    )
    def test_unconfirmed_lookup_keeps_exit_four(
        self, repo_with_remote, monkeypatch, capsys, platform, forge
    ):
        repo, remote = repo_with_remote
        forge.calls.clear()
        code = _run(_create_argv(repo, remote, platform), forge, monkeypatch, stdin_text=BODY)
        assert code == verb.EXIT_PR_FAILED
        assert len(forge.lists) == 1
        err = capsys.readouterr().err
        assert "already landed" in err or "push landed" in err
        assert "may mean a PR for this head/base pair already exists" in err

    @pytest.mark.parametrize("status", [401, 403, 500, "network"])
    def test_other_create_failures_are_unchanged_and_not_looked_up(
        self, repo_with_remote, monkeypatch, platform, status
    ):
        repo, remote = repo_with_remote
        forge = StubForge(open_prs=[7], create_status=status)
        code = _run(_create_argv(repo, remote, platform), forge, monkeypatch, stdin_text=BODY)
        assert code == verb.EXIT_PR_FAILED
        assert forge.lists == []

    def test_a_clean_create_carries_no_pre_existing_marker(
        self, repo_with_remote, monkeypatch, capsys, platform
    ):
        repo, remote = repo_with_remote
        forge = StubForge()
        code = _run(_create_argv(repo, remote, platform), forge, monkeypatch, stdin_text=BODY)
        assert code == verb.EXIT_OK
        payload, _captured = _stdout_payload(capsys)
        assert payload["pr_number"] == 99
        assert "pre_existing" not in payload
        assert forge.lists == []


@pytest.mark.parametrize("platform", PLATFORMS)
class TestFoldForm:
    def test_open_pr_is_pushed_to_and_reported_with_no_create(
        self, repo_with_remote, monkeypatch, capsys, platform
    ):
        repo, remote = repo_with_remote
        forge = StubForge(open_prs=[7])
        code = _run(_argv(repo, remote, platform), forge, monkeypatch)
        assert code == verb.EXIT_OK

        payload, _captured = _stdout_payload(capsys)
        assert payload["pr_number"] == 7
        assert payload["pre_existing"] is True
        assert forge.creates == []
        assert "feature" in _remote_branches(remote)

    def test_no_open_pr_exits_before_pushing_and_names_both_forms(
        self, repo_with_remote, monkeypatch, capsys, platform
    ):
        repo, remote = repo_with_remote
        forge = StubForge(open_prs=[])
        code = _run(_argv(repo, remote, platform), forge, monkeypatch)
        assert code == verb.EXIT_USAGE

        assert "feature" not in _remote_branches(remote)
        assert forge.creates == []
        err = " ".join(capsys.readouterr().err.split())
        assert "--title" in err
        assert "fold" in err
        assert "Nothing was pushed" in err

    def test_unreadable_lookup_exits_before_pushing(
        self, repo_with_remote, monkeypatch, capsys, platform
    ):
        repo, remote = repo_with_remote
        forge = StubForge(open_prs=[7], list_status=500)
        code = _run(_argv(repo, remote, platform), forge, monkeypatch)
        assert code == verb.EXIT_USAGE
        assert "feature" not in _remote_branches(remote)
        assert forge.creates == []


def test_github_lookup_filters_by_head_and_base(repo_with_remote, monkeypatch):
    repo, remote = repo_with_remote
    forge = StubForge(open_prs=[7])
    code = _run(_argv(repo, remote, "github"), forge, monkeypatch)
    assert code == verb.EXIT_OK
    (_method, url), = forge.lists
    assert "state=open" in url
    assert "head=some-owner%3Afeature" in url
    assert "base=main" in url
