# ruff: noqa: F811
# (the shared fixtures are imported into this namespace for pytest to register)
"""The pre-push guard fetches carry no ambient credential.

The branch-commit check and the task-id guard each fetch the base branch from
the tracking remote. The destination check that precedes them reads the remote's
urls under the neutralized git environment, so a user-level `insteadOf` rewrite
is invisible to it while an ambient-environment fetch would apply the rewrite and
answer the host's 401 from an ambient credential helper. Each case runs the real
verb entrypoint against a real git repo, a throwaway HOME whose global config
holds a credential helper, and a local HTTP listener that answers 401 Basic and
records every Authorization header.

  G1  a user-level insteadOf rewrites the anchored fetch url to the listener
  G2  an off-host fetch url is refused before the guards run
  G3  a repo-local insteadOf onto the listener is refused before the guards run
  G4  positive control: a local fetch url, the guards run and the push proceeds
  G5  an anchored http(s) fetch url: the guard fetch presents the minted token,
      never the ambient credential, and the guards run without a soft-fail warning
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from clagentic_loadout.push import branch_commit_check, verb
from clagentic_loadout.push.remote_readback import RemoteReadbackError
from tests._support.push_verb import (  # noqa: F401  (isolate_user_config_root is an autouse fixture)
    RecordingTokenProvider,
    git,
    isolate_user_config_root,
    json_resp,
    repo_with_remote,
    run_main,
    use_local_fetch_url,
)

TOKEN = "FAKETOKEN-guard-fetch"
AMBIENT = "AMBIENT-guard-fetch"
GITHUB_URL = "https://github.com/some-owner/some-repo.git"


class _Listener:
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
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}/some-owner/some-repo.git"
        self.base = f"http://127.0.0.1:{port}/"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Listener":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def received_a_credential(self) -> bool:
        for header in self.authorizations:
            scheme, _, payload = header.partition(" ")
            if scheme.lower() != "basic":
                continue
            decoded = base64.b64decode(payload).decode(errors="replace")
            if AMBIENT in decoded or TOKEN in decoded:
                return True
        return False


@pytest.fixture
def listener():
    with _Listener() as server:
        yield server


@pytest.fixture
def ambient_home(tmp_path, monkeypatch):
    """A HOME whose global git config holds a credential helper that answers
    every request with AMBIENT."""
    home = tmp_path / "ambient-home"
    home.mkdir()
    helper = f"!f() {{ test \"$1\" = get && printf 'username=x\\npassword={AMBIENT}\\n'; }}; f"
    subprocess.run(
        ["git", "config", "--file", str(home / ".gitconfig"), "credential.helper", helper],
        check=True,
    )
    for name in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "GIT_ASKPASS", "GIT_CONFIG_NOSYSTEM"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(home))
    return home


@pytest.fixture
def guard_fetches(monkeypatch):
    """The guards fetch the base branch (`main`); the pre-lease fetch names the
    pushed branch, so the trailing argument tells them apart."""
    calls: list[list[str]] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[0] == "git" and "fetch" in cmd and cmd[-1] == "main":
            calls.append(list(cmd))
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", spy)
    return calls


@pytest.fixture(autouse=True)
def no_network_readback(monkeypatch):
    """A positive control names an anchored host, which a real readback would
    resolve on the network."""

    def fake_read_remote_head(remote, ref, project_root, *, token=None):
        raise RemoteReadbackError("stubbed")

    monkeypatch.setattr(verb, "read_remote_head", fake_read_remote_head)


def _task_id_guard_config(repo) -> None:
    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True)
    (config_dir / "config.yaml").write_text("push:\n  task_id_guard_pattern: 'ZZZ-never-9'\n")


def _run(repo, monkeypatch, *extra):
    provider = RecordingTokenProvider(token=TOKEN)
    code = run_main(
        [
            "--repo-path", str(repo), "--platform", "github", "--repo", "some-owner/some-repo",
            "--title", "feat: t", "--body-stdin", *extra,
        ],
        token_provider=provider,
        opener=lambda req, timeout=30: json_resp(201, {"number": 7}),
        stdin_text=json.dumps({"body": "some body"}),
        monkeypatch=monkeypatch,
    )
    return code, provider


#: Each guard fetch alone, then both together.
_GUARD_MODES = [
    pytest.param((), id="both-guards"),
    pytest.param(("--skip-branch-commit-check",), id="task-id-guard-only"),
]


@pytest.mark.parametrize("lease", [(), ("--force-with-lease",)], ids=["no-lease", "lease"])
@pytest.mark.parametrize("guards", _GUARD_MODES)
def test_g1_user_level_insteadof_does_not_carry_an_ambient_credential_to_a_guard_fetch(
    guards, lease, repo_with_remote, listener, ambient_home, guard_fetches, monkeypatch, capsys
):
    repo, _remote = repo_with_remote
    git(["remote", "set-url", "origin", GITHUB_URL], repo)
    # Keeps the run offline once the fetches target the anchored host: a dead proxy
    # scoped to that host's url fails any fetch of it fast, and does not match the
    # rewritten listener url, so a fetch that applied the rewrite is not hidden.
    git(["config", "http.https://github.com/.proxy", "http://127.0.0.1:1"], repo)
    subprocess.run(
        ["git", "config", "--file", str(ambient_home / ".gitconfig"),
         f"url.{listener.base}.insteadOf", "https://github.com/"],
        check=True,
    )
    _task_id_guard_config(repo)

    code, _provider = _run(repo, monkeypatch, *guards, *lease)

    out, err = capsys.readouterr()
    assert guard_fetches, err
    assert listener.requests == []
    assert not listener.received_a_credential()
    assert AMBIENT not in out + err
    assert listener.url not in out + err


@pytest.mark.parametrize("guards", _GUARD_MODES)
def test_g2_off_host_fetch_url_is_refused_before_any_guard_fetch(
    guards, repo_with_remote, listener, ambient_home, guard_fetches, monkeypatch, capsys
):
    repo, _remote = repo_with_remote
    git(["remote", "set-url", "origin", listener.url], repo)
    _task_id_guard_config(repo)

    code, provider = _run(repo, monkeypatch, *guards)

    assert code == verb.EXIT_REMOTE_ERROR
    assert provider.resolved_for == []
    assert guard_fetches == []
    assert listener.requests == []


@pytest.mark.parametrize("guards", _GUARD_MODES)
def test_g3_repo_local_insteadof_onto_the_listener_is_refused_before_any_guard_fetch(
    guards, repo_with_remote, listener, ambient_home, guard_fetches, monkeypatch, capsys
):
    repo, _remote = repo_with_remote
    git(["remote", "set-url", "origin", GITHUB_URL], repo)
    git(["config", f"url.{listener.base}.insteadOf", "https://github.com/"], repo)
    _task_id_guard_config(repo)

    code, provider = _run(repo, monkeypatch, *guards)

    assert code == verb.EXIT_REMOTE_ERROR
    assert provider.resolved_for == []
    assert guard_fetches == []
    assert listener.requests == []


@pytest.mark.parametrize("lease", [(), ("--force-with-lease",)], ids=["no-lease", "lease"])
@pytest.mark.parametrize("guards", _GUARD_MODES)
def test_g4_a_local_fetch_url_still_runs_the_guards_and_pushes(
    guards, lease, repo_with_remote, listener, ambient_home, guard_fetches, monkeypatch, capsys
):
    repo, remote = repo_with_remote
    use_local_fetch_url(repo, remote)
    _task_id_guard_config(repo)

    code, provider = _run(repo, monkeypatch, *guards, *lease)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_OK, err
    assert guard_fetches
    assert provider.resolved_for
    assert listener.requests == []
    assert "feature" in git(["branch"], remote).stdout


@pytest.mark.parametrize("guards", _GUARD_MODES)
def test_g5_an_anchored_guard_fetch_presents_the_minted_token_and_the_guards_run(
    guards, repo_with_remote, listener, ambient_home, monkeypatch, capsys
):
    repo, remote = repo_with_remote
    git(["remote", "set-url", "origin", GITHUB_URL], repo)
    git(["config", "http.https://github.com/.proxy", "http://127.0.0.1:1"], repo)
    _task_id_guard_config(repo)
    presented: list[str] = []
    real_run = subprocess.run

    def spy(cmd, **kwargs):
        if cmd[0] == "git" and "fetch" in cmd and cmd[-1] == "main":
            askpass = kwargs["env"]["GIT_ASKPASS"]
            answer = real_run([askpass, "Password"], capture_output=True, text=True)
            presented.append(answer.stdout.strip())
            assert kwargs["env"]["GIT_CONFIG_GLOBAL"] == "/dev/null"
            # The anchored host is not reachable offline: the same fetch is
            # redirected to the local bare repo, keeping the envelope as built.
            cmd = [*cmd[:-2], str(remote), "main"]
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", spy)

    _run(repo, monkeypatch, *guards)

    _out, err = capsys.readouterr()
    assert presented
    assert set(presented) == {TOKEN}
    assert AMBIENT not in err
    assert "could not run" not in err
    assert listener.requests == []


def test_the_guard_fetch_environment_is_neutralized(repo_with_remote, monkeypatch):
    """The environment handed to the fetch has no global or system config, no
    ambient askpass and an empty HOME."""
    repo, remote = repo_with_remote
    use_local_fetch_url(repo, remote)
    seen: dict = {}
    real_run = subprocess.run

    def _spy(cmd, **kwargs):
        if "fetch" in cmd:
            seen["env"] = kwargs["env"]
            seen["home_entries"] = os.listdir(kwargs["env"]["HOME"])
        return real_run(cmd, **kwargs)

    monkeypatch.setattr("clagentic_loadout.push.git_push.subprocess.run", _spy)
    monkeypatch.setenv("GIT_ASKPASS", "/ambient/askpass.sh")
    monkeypatch.setenv("HOME", "/ambient/home")

    branch_commit_check.fetch_branch_commit_subjects(repo, "main")

    env = seen["env"]
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "GIT_ASKPASS" not in env
    assert env["HOME"] != "/ambient/home"
    assert seen["home_entries"] == []
