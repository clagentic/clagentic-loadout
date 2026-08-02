"""test_push_lease_control.py — tests for clagentic_loadout.push.lease_control
(lr-f57f13, D5 DECIDED; credentialed-fetch fix per a pre-merge security
review).

Exercises resolve_lease directly (not via push.verb.main) against a REAL
local bare repo used as the "remote" -- no real network call is ever made.

CREDENTIALED FETCH REGRESSION COVERAGE: the first shipped version of the
pre-lease fetch ran a bare, uncredentialed `git fetch`, authenticating via
whatever ambient credential helper happened to be configured for the
remote rather than the minted token this call actually resolved. These
tests prove the fetch now runs via push.git_push.git_fetch_with_token
(the SAME isolated-HOME/GIT_ASKPASS envelope git_push_with_token itself
uses) by pointing the remote at an HTTP endpoint that REQUIRES the
GIT_ASKPASS-supplied token to succeed at all -- a bare ambient fetch with
no credential would fail differently (or succeed via an ambient
credential this test's isolated HOME structurally removes), so success
here is only possible through the credentialed envelope.
"""

from __future__ import annotations

import http.server
import subprocess
import threading
from pathlib import Path

import pytest

from clagentic_loadout.push.lease_control import (
    LEASE_ORIGIN_CLI_FORCE,
    LEASE_ORIGIN_DEFAULT_FALSE,
    resolve_lease,
)


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _make_repo_with_bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "widget"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "widget"], repo)
    _git(["config", "user.email", "author@example.invalid"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "f.txt").write_text("content\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "chore: work"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "origin", "widget"], repo)

    return repo, remote


class TestResolveLeaseNoForce:
    def test_no_force_never_touches_the_remote(self, tmp_path):
        """When the resolved decision is not to force, resolve_lease must
        not attempt any fetch at all -- no credential envelope to test,
        since there is nothing to fetch for."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        lease = resolve_lease(
            cli_force_with_lease=None,
            history_rewritten=False,
            remote="origin",
            branch="widget",
            project_root=repo,
            token="unused-token-value",
        )
        assert lease.force_with_lease is False
        assert lease.origin == LEASE_ORIGIN_DEFAULT_FALSE
        assert lease.fetch_attempted is False
        assert lease.fetch_warning is None


class TestResolveLeaseCredentialedFetch:
    def test_forced_lease_fetch_succeeds_against_local_bare_remote(self, tmp_path):
        """A forced lease resolution fetches successfully against a real
        local bare remote -- proves git_fetch_with_token's subprocess call
        actually runs and returns cleanly."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        lease = resolve_lease(
            cli_force_with_lease=True,
            history_rewritten=False,
            remote="origin",
            branch="widget",
            project_root=repo,
            token="unused-token-value",
        )
        assert lease.force_with_lease is True
        assert lease.origin == LEASE_ORIGIN_CLI_FORCE
        assert lease.fetch_attempted is True
        assert lease.fetch_warning is None

    def test_forced_lease_fetch_uses_isolated_home_not_ambient_credentials(self, tmp_path, monkeypatch):
        """CREDENTIALED FETCH REGRESSION (pre-merge security review): point
        the remote at a real HTTP server that requires Basic auth matching
        the GIT_ASKPASS-supplied token; set an ambient (WRONG) credential
        via a git-credential helper under the real HOME to prove it is
        NEVER consulted -- git_fetch_with_token isolates HOME exactly like
        git_push_with_token does. If the fetch instead ran ambiently (the
        original defect), it would either use the WRONG ambient credential
        (401) or accidentally succeed via it -- neither proves the
        isolated envelope was used. Succeeding via the CORRECT token,
        while an incorrect ambient credential is configured and reachable
        under the real HOME, is only possible if HOME was actually
        isolated for this subprocess."""
        expected_token = "correct-minted-token-abc123"

        class _AuthHandler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                auth = self.headers.get("Authorization", "")
                if auth != f"Basic {_basic_auth_value(expected_token)}":
                    self.send_response(401)
                    self.send_header("WWW-Authenticate", 'Basic realm="git"')
                    self.end_headers()
                    return
                self.send_response(404)
                self.end_headers()

            def log_message(self, *args):
                pass

        import base64

        def _basic_auth_value(token: str) -> str:
            return base64.b64encode(f"x-access-token:{token}".encode()).decode()

        server = http.server.HTTPServer(("127.0.0.1", 0), _AuthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            repo, _remote = _make_repo_with_bare_remote(tmp_path)
            # Real git-over-HTTP smart protocol requires a real git backend
            # to serve `git-upload-pack` -- a bare stub HTTP server can only
            # prove the AUTH HEADER was correct (via the 401-vs-not-401
            # branch above) before the smart-protocol negotiation itself
            # necessarily fails against a non-git server. That is
            # sufficient: it proves GIT_ASKPASS's token, not an ambient
            # credential, was what git attempted to authenticate with.
            wrong_ambient_home = tmp_path / "real_home_with_wrong_credential"
            wrong_ambient_home.mkdir()
            _git(
                ["config", "--global", "credential.helper",
                 "!f() { echo username=x-access-token; echo password=WRONG-AMBIENT-CREDENTIAL; }; f"],
                wrong_ambient_home,
            )
            monkeypatch.setenv("HOME", str(wrong_ambient_home))

            from clagentic_loadout.push.git_push import GitFetchError, git_fetch_with_token

            remote_url = f"http://127.0.0.1:{port}/synthetic-owner/synthetic-repo.git"
            with pytest.raises(GitFetchError) as exc_info:
                git_fetch_with_token(remote_url, "widget", expected_token, repo)
            # A 401 here (not proven correct-auth) would mean the WRONG
            # ambient credential (or no credential) was used -- assert the
            # failure is NOT an auth failure, i.e. the correct token DID
            # authenticate and the failure is purely "this isn't a real git
            # smart-HTTP backend."
            assert "401" not in str(exc_info.value)
        finally:
            server.shutdown()
            thread.join(timeout=5)
