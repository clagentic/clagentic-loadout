# ruff: noqa: F811
# (the shared fixtures are imported into this namespace for pytest to register)
"""The minted push token is never delivered to a host other than the platform's.

Each vector runs the real verb entrypoint against a real git repo and a real
local HTTP listener that stands in for the unanchored host: it answers 401 with
`WWW-Authenticate: Basic` (which makes git consult GIT_ASKPASS) and records every
request. A vector passes only when the token provider was never asked for a
token (the refusal came before the mint) and the listener saw nothing.

  V1  GitHub, `branch.<name>.remote` holds the listener URL
  V2  GitHub, `branch.<name>.remote` names an alias whose url is the listener
  V3  Forgejo, `branch.<name>.remote` holds the listener URL (fails closed already)
  V4  Forgejo, an alias whose url is the anchored host and whose pushurl is the listener
  V5  V1 with --force-with-lease (the pre-lease fetch is another token-bearing call)
  V6  GitHub, push destination is local but the fetch url is the listener
"""

from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clagentic_loadout.push import verb
from clagentic_loadout.push.remote_readback import RemoteReadbackError
from tests._support.push_verb import (  # noqa: F401  (isolate_user_config_root is an autouse fixture)
    RecordingTokenProvider,
    RefusingTokenProvider,
    forgejo_create_opener,
    git,
    isolate_user_config_root,
    json_resp,
    repo_with_remote,
    run_main,
)

TOKEN = "FAKETOKEN-684312"
ANCHORED_HOST = "git-host.example.com"
GITHUB_URL = "https://github.com/some-owner/some-repo.git"


class _Listener:
    """A local HTTP server that demands Basic auth and records what it gets."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.authorizations: list[str] = []
        listener = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                listener.requests.append(self.path)
                header = self.headers.get("Authorization")
                if header:
                    listener.authorizations.append(header)
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="x"')
                self.send_header("Content-Length", "0")
                self.end_headers()

            do_GET = do_POST = do_PUT = do_HEAD = _answer

            def log_message(self, *args) -> None:
                return

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/o/r.git"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Listener":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def received_token(self) -> bool:
        for header in self.authorizations:
            scheme, _, payload = header.partition(" ")
            if scheme.lower() == "basic" and TOKEN in base64.b64decode(payload).decode(errors="replace"):
                return True
        return False


@pytest.fixture
def listener():
    with _Listener() as server:
        yield server


def _github_args(repo, *extra):
    return [
        "--repo-path", str(repo), "--platform", "github", "--repo", "some-owner/some-repo",
        "--title", "feat: t", "--body-stdin", *extra,
    ]


def _forgejo_args(repo, *extra):
    return [
        "--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin",
        "--allowed-host", ANCHORED_HOST, *extra,
    ]


def _github_opener(req, timeout=30):
    return json_resp(201, {"number": 7})


def _run(argv, monkeypatch, *, provider, opener=None):
    return run_main(
        argv,
        token_provider=provider,
        opener=opener or forgejo_create_opener(),
        stdin_text=json.dumps({"body": "some body"}),
        monkeypatch=monkeypatch,
    )


def _assert_refused_before_mint(code, provider, listener, capsys):
    out, err = capsys.readouterr()
    assert code != verb.EXIT_OK, err
    assert provider.resolved_for == []
    assert listener.requests == []
    assert not listener.received_token()
    assert TOKEN not in out + err
    assert listener.url not in out + err


def test_v1_github_url_valued_tracking_remote(repo_with_remote, listener, monkeypatch, capsys):
    repo, _remote = repo_with_remote
    git(["config", "branch.feature.remote", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_github_args(repo), monkeypatch, provider=provider, opener=_github_opener)

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


def test_v2_github_alias_whose_url_is_off_host(repo_with_remote, listener, monkeypatch, capsys):
    repo, _remote = repo_with_remote
    git(["remote", "add", "rogue", listener.url], repo)
    git(["config", "branch.feature.remote", "rogue"], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_github_args(repo), monkeypatch, provider=provider, opener=_github_opener)

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


def test_v3_forgejo_url_valued_tracking_remote_fails_closed(repo_with_remote, listener, monkeypatch, capsys):
    """Forgejo resolves its coordinates with `git remote get-url`, which a URL
    cannot satisfy: the create path stops before any mint with a category-only
    message and EXIT_REMOTE_ERROR, as the update path does -- not a traceback."""
    repo, _remote = repo_with_remote
    git(["config", "branch.feature.remote", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_forgejo_args(repo), monkeypatch, provider=provider)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_REMOTE_ERROR
    assert "Traceback" not in err
    assert provider.resolved_for == []
    assert listener.requests == []
    assert not listener.received_token()
    assert listener.url not in out + err


def test_v4_forgejo_anchored_url_with_off_host_pushurl(repo_with_remote, listener, monkeypatch, capsys):
    repo, _remote = repo_with_remote
    git(["config", "remote.origin.pushurl", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_forgejo_args(repo), monkeypatch, provider=provider)

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


def test_v4_forgejo_second_pushurl_is_checked_too(repo_with_remote, listener, monkeypatch, capsys):
    repo, _remote = repo_with_remote
    git(["config", "--add", "remote.origin.pushurl", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_forgejo_args(repo), monkeypatch, provider=provider)

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


def test_v5_forced_lease_pre_fetch_cannot_reach_an_off_host_remote(
    repo_with_remote, listener, monkeypatch, capsys
):
    repo, _remote = repo_with_remote
    git(["config", "branch.feature.remote", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(
        _github_args(repo, "--force-with-lease"), monkeypatch, provider=provider, opener=_github_opener
    )

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


def test_v6_off_host_fetch_url_gets_neither_the_lease_fetch_nor_the_readback(
    repo_with_remote, listener, monkeypatch, capsys
):
    """The push goes to a local bare repo, so it proceeds; the two other
    token-bearing calls would go to the fetch url, so both are skipped."""
    repo, _remote = repo_with_remote
    git(["remote", "set-url", "origin", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(
        _github_args(repo, "--force-with-lease", "--skip-branch-commit-check"),
        monkeypatch, provider=provider, opener=_github_opener,
    )

    out, err = capsys.readouterr()
    assert code == verb.EXIT_OK, err
    assert provider.resolved_for
    assert listener.requests == []
    assert not listener.received_token()
    assert "pre-lease fetch attempted=False" in err
    assert "pre-lease fetch skipped" in err
    payload = json.loads(out.strip().splitlines()[-1])
    assert payload["remote_head_sha"] is None
    assert payload["readback"]["verified"] is False


@pytest.fixture
def no_network_readback(monkeypatch):
    """Positive controls name an anchored host, which a real readback would
    resolve on the network; the stub records the call and stands in for it."""
    calls: list[str] = []

    def fake_read_remote_head(remote, ref, project_root, *, token=None):
        calls.append(ref)
        raise RemoteReadbackError("stubbed")

    monkeypatch.setattr(verb, "read_remote_head", fake_read_remote_head)
    return calls


@pytest.mark.parametrize("tracking", ["explicit-origin", "unset-falls-back-to-origin"])
def test_github_aliased_origin_on_the_anchored_host_still_pushes(
    tracking, repo_with_remote, listener, monkeypatch, capsys, no_network_readback
):
    repo, remote = repo_with_remote
    git(["remote", "set-url", "origin", GITHUB_URL], repo)
    git(["config", "remote.origin.pushurl", str(remote)], repo)
    if tracking == "explicit-origin":
        git(["config", "branch.feature.remote", "origin"], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(
        _github_args(repo, "--skip-branch-commit-check"), monkeypatch, provider=provider,
        opener=_github_opener,
    )

    out, err = capsys.readouterr()
    assert code == verb.EXIT_OK, err
    assert provider.resolved_for
    assert no_network_readback == ["feature"]
    assert listener.requests == []
    assert "feature" in git(["branch"], remote).stdout


@pytest.mark.parametrize("tracking", ["explicit-origin", "unset-falls-back-to-origin"])
def test_forgejo_aliased_origin_on_the_anchored_host_still_pushes(
    tracking, repo_with_remote, listener, monkeypatch, capsys, no_network_readback
):
    repo, remote = repo_with_remote
    if tracking == "explicit-origin":
        git(["config", "branch.feature.remote", "origin"], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(
        _forgejo_args(repo, "--skip-branch-commit-check"), monkeypatch, provider=provider,
    )

    out, err = capsys.readouterr()
    assert code == verb.EXIT_OK, err
    assert provider.resolved_for
    assert no_network_readback == ["feature"]
    assert listener.requests == []
    assert "feature" in git(["branch"], remote).stdout


@pytest.mark.parametrize(
    "destination",
    ["ssh://git@example.invalid/o/r.git", "git://example.invalid/o/r.git", "ext::sh -c true", "git@example.invalid:o/r.git"],
)
def test_transports_that_reach_a_peer_are_refused_before_the_mint(
    destination, repo_with_remote, monkeypatch, capsys
):
    repo, _remote = repo_with_remote
    git(["remote", "add", "peer", destination], repo)
    git(["config", "branch.feature.remote", "peer"], repo)
    provider = RefusingTokenProvider()

    code = _run(_github_args(repo), monkeypatch, provider=provider, opener=_github_opener)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_REMOTE_ERROR
    assert "example.invalid" not in out + err
