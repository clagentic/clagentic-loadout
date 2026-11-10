"""Shared fixtures for the push verb's tests: real-git helpers, a local repo
with a bare 'origin', a fake Forgejo create-PR opener, the verb runner, and
the user-config isolation fixture. A test module imports the fixtures from
here to have pytest register them in its own namespace."""

from __future__ import annotations

import io
import subprocess

import pytest

from clagentic_loadout.push import verb
from clagentic_loadout.transport import provider_config
from tests._support.fakes import (
    FakeResponse,
    RecordingTokenProvider,
    RefusingTokenProvider,
    json_resp,
)

__all__ = [
    "FakeResponse",
    "RecordingTokenProvider",
    "RefusingTokenProvider",
    "StubForge",
    "forgejo_create_opener",
    "forgejo_open_prs_opener",
    "git",
    "isolate_user_config_root",
    "json_resp",
    "repo_with_remote",
    "run_main",
]


def git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def forgejo_create_opener(*, pr_number=42):
    def opener(req, timeout=15):
        if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
            return json_resp(201, {"number": pr_number})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


class StubForge:
    """A fake git-host for the PR step: records every request, answers the
    PR-create POST and the open-PR list GET for either platform.

    *create_status* None makes the create succeed (PR number *created_number*);
    an int raises that HTTP error; "network" raises a URLError. *open_prs* are
    the numbers the list answers with (head "feature", base "main", head repo
    this repository); *fork_prs* answer with the same branch and base from a
    different head repository, *repoless_prs* with no head repository at all;
    *list_status* != 200 makes the list fail."""

    def __init__(
        self, *, open_prs=(), create_status=None, list_status=200, created_number=99,
        fork_prs=(), repoless_prs=(),
    ):
        self.open_prs = list(open_prs)
        self.fork_prs = list(fork_prs)
        self.repoless_prs = list(repoless_prs)
        self.create_status = create_status
        self.list_status = list_status
        self.created_number = created_number
        self.calls: list[tuple[str, str]] = []

    @property
    def creates(self):
        return [c for c in self.calls if c[0] == "POST"]

    @property
    def lists(self):
        return [c for c in self.calls if c[0] == "GET"]

    def __call__(self, req, timeout=15):
        import urllib.error

        method, url = req.get_method(), req.full_url
        self.calls.append((method, url))
        if method == "POST" and url.endswith("/pulls"):
            if self.create_status is None:
                return json_resp(201, {"number": self.created_number})
            if self.create_status == "network":
                raise urllib.error.URLError("unreachable")
            raise urllib.error.HTTPError(
                url, self.create_status, "err", {}, io.BytesIO(b'{"message": "refused"}')
            )
        if method == "GET" and "/pulls?" in url:
            if self.list_status != 200:
                raise urllib.error.HTTPError(
                    url, self.list_status, "err", {}, io.BytesIO(b'{"message": "refused"}')
                )
            items = [
                {
                    "number": n,
                    "head": {"ref": "feature", "repo": {"full_name": "some-owner/some-repo"}},
                    "base": {"ref": "main"},
                }
                for n in self.open_prs
            ]
            items += [
                {
                    "number": n,
                    "head": {"ref": "feature", "repo": {"full_name": "fork-owner/some-repo"}},
                    "base": {"ref": "main"},
                }
                for n in self.fork_prs
            ]
            items += [
                {"number": n, "head": {"ref": "feature"}, "base": {"ref": "main"}}
                for n in self.repoless_prs
            ]
            return json_resp(200, items)
        raise AssertionError(f"unexpected: {method} {url}")


def forgejo_open_prs_opener(open_prs):
    return StubForge(open_prs=open_prs)


@pytest.fixture
def repo_with_remote(tmp_path):
    """A local repo with a bare-repo 'origin' remote (real git, no network), a
    base main and a feature branch one commit ahead."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(["init", "--bare", "-b", "main"], remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    git(["init", "-b", "main"], seed)
    git(["config", "user.email", "base@example.com"], seed)
    git(["config", "user.name", "Base"], seed)
    (seed / "README.md").write_text("hello\n")
    git(["add", "README.md"], seed)
    git(["commit", "-m", "initial"], seed)
    git(["remote", "add", "origin", str(remote)], seed)
    git(["push", "origin", "main"], seed)

    repo = tmp_path / "repo"
    git(["clone", str(remote), str(repo)], tmp_path)
    git(["config", "user.email", "author@example.com"], repo)
    git(["config", "user.name", "Author"], repo)
    git(["checkout", "-b", "feature"], repo)
    (repo / "feature.txt").write_text("work\n")
    git(["add", "feature.txt"], repo)
    # Conventional-Commits-shaped: every push-time gate validates real commit
    # subjects, so the fixture's own commit must conform rather than trip a
    # gate that is not the one under test.
    git(["commit", "-m", "feat: add feature work"], repo)
    git(
        ["remote", "set-url", "origin", "http://git-host.example.com/some-owner/some-repo.git"],
        repo,
    )
    # `git push` must reach the real local bare repo while `git remote
    # get-url origin` (used for owner/repo/api_base parsing) keeps returning
    # the neutral Forgejo-shaped URL above. remote.origin.pushurl does that
    # without any of the repo-local config shapes the hermeticity check
    # refuses (credential.*, http.*.extraheader, includeIf.*,
    # url.*.insteadOf/pushInsteadOf), none of which a fail-closed check could
    # tell apart from a hostile use.
    git(["config", "remote.origin.pushurl", str(remote)], repo)

    return repo, remote


def use_local_fetch_url(repo, remote) -> None:
    """Point origin's fetch url at the local bare repo.

    The default fixture keeps a Forgejo-shaped fetch url because the Forgejo
    path parses its coordinates from it. A GitHub-platform run never parses it,
    and the token-destination guard (push.destination_guard) refuses a fetch
    url on a host other than github.com, so those runs use a local path, which
    the guard allows."""
    git(["remote", "set-url", "origin", str(remote)], repo)


def run_main(
    argv, *, token_provider=None, opener=None, stdin_text=None, monkeypatch=None,
    host_config_root=None,
):
    if stdin_text is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin_text.encode("utf-8"))))
    return verb.main(
        argv, token_provider=token_provider, opener=opener, host_config_root=host_config_root,
    )


@pytest.fixture(autouse=True)
def isolate_user_config_root(tmp_path, monkeypatch):
    """Isolation from the real user config root: push.host_guard's config
    tier reads through transport.provider_config, which falls back to
    DEFAULT_USER_CONFIG_ROOT (the real ~/.config/clagentic/loadout/) for any
    call that omits host_config_root. A real deployment config on the host
    running these tests must never leak a live allowlist into a test asserting
    the config-unset precedence."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)
