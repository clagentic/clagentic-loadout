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
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clagentic_loadout.push import verb
from clagentic_loadout.push.git_push import GitFetchError, git_fetch_with_token
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
    use_local_fetch_url,
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


@pytest.mark.parametrize("extra", [(), ("--force-with-lease",)])
def test_v6_off_host_fetch_url_is_refused_before_the_mint(
    extra, repo_with_remote, listener, monkeypatch, capsys
):
    """The push goes to a local bare repo, but the pre-lease fetch and the
    readback present the token to the fetch url, so it is judged like the push
    destination: refused before the mint, with the lease safety intact."""
    repo, _remote = repo_with_remote
    git(["remote", "set-url", "origin", listener.url], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(_github_args(repo, *extra), monkeypatch, provider=provider, opener=_github_opener)

    _assert_refused_before_mint(code, provider, listener, capsys)
    assert code == verb.EXIT_REMOTE_ERROR


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


def _install_env_recording_hook(remote, record):
    """A pre-receive hook in the destination repo that records everything a
    hostile destination could read: its environment, and the file GIT_ASKPASS
    names (the generated script) plus the token file that script reads."""
    hook = remote / "hooks" / "pre-receive"
    hook.write_text(
        "#!/bin/sh\n"
        f'env >> "{record}"\n'
        f'echo "ASKPASS=${{GIT_ASKPASS-unset}}" >> "{record}"\n'
        'if [ -n "$GIT_ASKPASS" ] && [ -r "$GIT_ASKPASS" ]; then\n'
        f'  cat "$GIT_ASKPASS" >> "{record}"\n'
        f'  cat "$(dirname "$GIT_ASKPASS")/tok" >> "{record}"\n'
        "fi\n"
        "exit 0\n"
    )
    hook.chmod(0o755)


@pytest.mark.parametrize("extra", [(), ("--force-with-lease",)])
def test_a_local_destination_hook_sees_no_credential(
    extra, repo_with_remote, monkeypatch, capsys, tmp_path
):
    """A local receive-pack runs the destination's hooks with the pushing
    process's environment, so that git call carries no GIT_ASKPASS, no token
    file and no token anywhere in the environment."""
    repo, remote = repo_with_remote
    use_local_fetch_url(repo, remote)
    record = tmp_path / "hook-record.txt"
    _install_env_recording_hook(remote, record)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(
        _github_args(repo, "--skip-branch-commit-check", *extra), monkeypatch,
        provider=provider, opener=_github_opener,
    )

    out, err = capsys.readouterr()
    assert code == verb.EXIT_OK, err
    assert "feature" in git(["branch"], remote).stdout
    seen = record.read_text()
    assert "ASKPASS=unset" in seen
    assert "GIT_ASKPASS=" not in seen
    assert TOKEN not in seen
    assert "askpass.sh" not in seen


def test_a_network_destination_still_receives_the_askpass_environment(tmp_path, monkeypatch):
    """The other half of the rule: for an http(s) destination the token is
    still presented, so the change removes exposure to local transports only."""
    seen: dict = {}
    real_run = subprocess.run

    def _spy(cmd, **kwargs):
        if "fetch" in cmd:
            seen["env"] = kwargs["env"]
        return real_run(cmd, **kwargs)

    repo = tmp_path / "r"
    repo.mkdir()
    git(["init", "-b", "main"], repo)
    git(["remote", "add", "origin", "http://127.0.0.1:1/o/r.git"], repo)
    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", _spy)

    with pytest.raises(GitFetchError):
        git_fetch_with_token("origin", "main", TOKEN, repo)

    assert seen["env"]["GIT_ASKPASS"]


def test_a_local_fetch_destination_carries_no_askpass(repo_with_remote, monkeypatch):
    repo, remote = repo_with_remote
    use_local_fetch_url(repo, remote)
    seen: dict = {}
    real_run = subprocess.run

    def _spy(cmd, **kwargs):
        if "fetch" in cmd:
            seen["env"] = kwargs["env"]
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", _spy)
    monkeypatch.setenv("GIT_ASKPASS", "/ambient/askpass.sh")

    git_fetch_with_token("origin", "main", TOKEN, repo)

    assert "GIT_ASKPASS" not in seen["env"]
    assert TOKEN not in " ".join(seen["env"].values())


@pytest.mark.parametrize("platform", ["github", "forgejo"])
def test_a_mixed_http_and_local_push_set_is_refused_before_the_mint(
    platform, repo_with_remote, monkeypatch, capsys
):
    repo, remote = repo_with_remote
    if platform == "github":
        use_local_fetch_url(repo, remote)
        anchored_push = GITHUB_URL
        args = _github_args(repo, "--skip-branch-commit-check")
    else:
        anchored_push = f"http://{ANCHORED_HOST}/some-owner/some-repo.git"
        args = _forgejo_args(repo, "--skip-branch-commit-check")
    git(["config", "--add", "remote.origin.pushurl", anchored_push], repo)
    provider = RecordingTokenProvider(token=TOKEN)

    code = _run(args, monkeypatch, provider=provider, opener=_github_opener)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_REMOTE_ERROR
    assert provider.resolved_for == []
    assert "mix a network destination with a local one" in err
    assert str(remote) not in out + err
    assert "feature" not in git(["branch"], remote).stdout


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
