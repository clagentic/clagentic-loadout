"""test_push_git_hermeticity.py — tests for clagentic_loadout.push.git_hermeticity
(lr-a868d2).

SYNTHETIC HOSTILE ENVIRONMENT SUITE: every credentialed git subprocess this
package spawns must ignore whatever ambient credential machinery the host
happens to have configured -- a credential.helper at system, XDG-fallback-
global, or repo-local scope, an ambient GIT_CONFIG_SYSTEM/GIT_CONFIG_COUNT
injection, a fake ~/.netrc, or a fake SSH identity. Each test below PROVES
non-vacuity per the task's own requirement, verified directly (not merely
argued) by monkeypatching `neutralize_ambient_git_env` to a no-op (and, for
the system-scope and XDG-scope cases specifically, ALSO the `-c
credential.helper=` argv prefix -- see those tests' own docstrings for why
disabling the env layer alone is not enough to prove non-vacuity there)
during this suite's own development, and confirming every sentinel test
that exercises the NEW neutralization logic then FAILS -- proving each
assertion actually depends on the code this suite exists to cover, not on
some other code path that would have produced the same observable result
regardless.

WHY THESE TESTS ROUTE OVER REAL HTTP(S), NOT A BARE LOCAL PATH:
`credential.helper` (at any scope) and `~/.netrc` are ONLY consulted by
git's HTTP(S) transport -- a bare filesystem-path "remote" (the shape most
of this package's other push tests use) never triggers credential
resolution at all, which would make a hostile-helper assertion pass
VACUOUSLY regardless of whether this fix's neutralization code ran. Each
sentinel test below therefore runs a tiny local HTTP server that would
answer a git-smart-http credential/auth exchange -- proving the plumbing
actually reaches libcurl's credential-resolution path, the same one a real
ambient credential would be consulted from, before the connection
necessarily fails for an unrelated reason (this stub server does not speak
the full git-smart-http protocol; see push.lease_control's own existing
test for the same technique and the same honest limit).

NOTE ON RIGOR (task's own honesty requirement): there is no published
standard hermetic-push test harness. This suite's pattern (a stub local
HTTP server, a genuinely hostile ambient config/env value, a sentinel side
effect proving consultation) is well-grounded in real git/libcurl
semantics and in the isolated-HOME regression test push.lease_control
already carries, but is novel as a suite -- it is not itself an
established, widely-used test methodology.
"""

from __future__ import annotations

import base64
import http.server
import os
import subprocess
import threading
from pathlib import Path

import pytest

from clagentic_loadout.push.errors import GitPushError
from clagentic_loadout.push.git_hermeticity import (
    MIN_GIT_VERSION,
    GitVersionTooOldError,
    RepoLocalConfigHazardError,
    check_git_version,
    check_repo_local_config_hazards,
    neutralize_ambient_git_env,
)
from clagentic_loadout.push.git_push import git_push_with_token


def _git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env,
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

    return repo, remote


def _make_repo_only(tmp_path: Path) -> Path:
    """A repo with a commit but NO remote configured -- the caller adds an
    explicit HTTP(S) remote URL directly to each stub-server test's push
    call, since a stub server has no real git backend to `git init --bare`
    against."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "widget"], repo)
    _git(["config", "user.email", "author@example.invalid"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "f.txt").write_text("content\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "chore: work"], repo)
    return repo


class _RecordingAuthHandler(http.server.BaseHTTPRequestHandler):
    """Stub HTTP server: records the Authorization header it received (or
    its absence) and always answers 401 -- this package's push will fail
    against it (it is not a real git-smart-http backend), but the ONLY way
    to observe which credential reached libcurl is via this recorded
    header, which is populated before this handler ever runs."""

    seen_auth_headers: list[str] = []

    def do_GET(self):
        self.__class__.seen_auth_headers.append(self.headers.get("Authorization", ""))
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="git"')
        self.end_headers()

    def do_POST(self):
        self.do_GET()

    def log_message(self, *args):
        pass


def _basic_auth_value(username: str, secret: str) -> str:
    return base64.b64encode(f"{username}:{secret}".encode()).decode()


class TestCheckGitVersion:
    def test_resolved_version_meets_minimum_on_this_host(self, tmp_path):
        """The suite's own host must satisfy MIN_GIT_VERSION -- a smoke
        assertion that also proves the resolved-tuple return contract."""
        resolved = check_git_version()
        assert resolved[:2] >= MIN_GIT_VERSION

    def test_reports_resolved_version_in_error_not_a_guess(self, tmp_path, monkeypatch):
        """CLAUDE.md hard rule 4: an old-version refusal must report the
        RESOLVED version this host's git actually printed, not a stale
        assumption -- proven via a fake git binary reporting a version
        below MIN_GIT_VERSION."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        wrapper = fake_bin / "git"
        wrapper.write_text("#!/bin/sh\necho 'git version 2.10.0'\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}")
        with pytest.raises(GitVersionTooOldError) as exc_info:
            check_git_version()
        assert "2.10.0" in str(exc_info.value)
        assert "2.20" in str(exc_info.value)

    def test_unparseable_version_output_is_a_hard_failure(self, tmp_path, monkeypatch):
        """A version this check cannot confirm meets the minimum must never
        be silently treated as passing."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        wrapper = fake_bin / "git"
        wrapper.write_text("#!/bin/sh\necho 'not a version string'\n")
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}")
        with pytest.raises(GitVersionTooOldError):
            check_git_version()


class TestNeutralizeAmbientGitEnv:
    def test_redirects_global_and_system_config_to_dev_null(self):
        env = neutralize_ambient_git_env({})
        assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"
        assert env["GIT_CONFIG_SYSTEM"] == "/dev/null"
        assert env["GIT_CONFIG_NOSYSTEM"] == "1"

    def test_strips_ambient_askpass_and_ssh_vars(self):
        env = neutralize_ambient_git_env({
            "GIT_ASKPASS": "/ambient/askpass.sh",
            "SSH_ASKPASS": "/ambient/ssh-askpass.sh",
            "GIT_SSH": "/ambient/ssh-wrapper.sh",
            "GIT_SSH_COMMAND": "ssh -i /ambient/key",
            "UNRELATED": "kept",
        })
        assert "GIT_ASKPASS" not in env
        assert "SSH_ASKPASS" not in env
        assert "GIT_SSH" not in env
        assert "GIT_SSH_COMMAND" not in env
        assert env["UNRELATED"] == "kept"

    def test_strips_git_config_injection_channel(self):
        env = neutralize_ambient_git_env({
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "!echo malicious",
            "OTHER": "kept",
        })
        assert "GIT_CONFIG_COUNT" not in env
        assert "GIT_CONFIG_KEY_0" not in env
        assert "GIT_CONFIG_VALUE_0" not in env
        assert env["OTHER"] == "kept"

    def test_does_not_mutate_input_dict(self):
        original = {"GIT_ASKPASS": "/ambient/askpass.sh"}
        neutralize_ambient_git_env(original)
        assert original == {"GIT_ASKPASS": "/ambient/askpass.sh"}


class TestCheckRepoLocalConfigHazards:
    def test_no_hazard_on_a_plain_repo(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        assert check_repo_local_config_hazards(repo) == ()

    def test_detects_repo_local_credential_helper(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        _git(["config", "--local", "credential.helper", "!echo should-not-run"], repo)
        hazards = check_repo_local_config_hazards(repo)
        assert any(h.startswith("credential.") for h in hazards)

    def test_detects_http_extraheader(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        _git(
            ["config", "--local", "http.https://example.invalid/.extraheader",
             "Authorization: Bearer sentinel-token"],
            repo,
        )
        hazards = check_repo_local_config_hazards(repo)
        assert any(h.endswith(".extraheader") for h in hazards)

    def test_detects_includeif(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        include_target = tmp_path / "included.gitconfig"
        include_target.write_text("[user]\n\tname = Included\n")
        _git(
            ["config", "--local", f"includeif.gitdir:{repo}/.path",
             str(include_target)],
            repo,
        )
        hazards = check_repo_local_config_hazards(repo)
        assert any(h.startswith("includeif.") for h in hazards)

    def test_detects_url_insteadof(self, tmp_path):
        """BOBBIE finding (bobbie.sast.repo-local-hazard-coverage-gap): a
        url.<base>.insteadOf rewrite rule is a credential-EXPOSURE hazard
        (it can silently redirect a push to an attacker-chosen host, which
        then receives the minted token this package presents via
        GIT_ASKPASS) even though the directive itself carries no secret."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        _git(
            ["config", "--local", "url.https://attacker.invalid/.insteadOf",
             "https://example.invalid/"],
            repo,
        )
        hazards = check_repo_local_config_hazards(repo)
        assert any(h.endswith(".insteadof") for h in hazards)

    def test_detects_url_pushinsteadof(self, tmp_path):
        """pushInsteadOf is the MORE directly relevant variant here: it
        rewrites only the push destination, exactly the operation this
        package's minted token is presented against."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        _git(
            ["config", "--local", "url.https://attacker.invalid/.pushInsteadOf",
             "https://example.invalid/"],
            repo,
        )
        hazards = check_repo_local_config_hazards(repo)
        assert any(h.endswith(".pushinsteadof") for h in hazards)

    def test_bare_repo_with_no_local_config_hazards_is_empty(self, tmp_path):
        bare = tmp_path / "bare.git"
        bare.mkdir()
        _git(["init", "--bare"], bare)
        assert check_repo_local_config_hazards(bare) == ()


class TestSyntheticHostileEnvironment:
    """The core hermeticity guarantee: plant a genuinely hostile ambient
    mechanism BEFORE calling git_push_with_token, and prove it is never
    consulted. Each test targets an HTTP(S) URL (see module docstring for
    why credential.helper/.netrc are otherwise never even reached) served
    by a stub server that records the Authorization header it observes."""

    def test_ambient_system_scope_credential_helper_never_consulted(self, tmp_path, monkeypatch):
        """Plant a fake SYSTEM-scope credential.helper via an ambient
        GIT_CONFIG_SYSTEM env var pointing at a hostile config file that
        supplies a WRONG sentinel credential -- assert the request this
        package's push makes carries NO Authorization header derived from
        it (this package's own GIT_ASKPASS-supplied token also cannot
        authenticate against this stub server, since it never validates
        any credential -- the assertion is specifically that the HOSTILE
        credential's value never appears, not that authentication
        succeeded). This is the axis HOME isolation (already present
        before this fix) does NOT cover: system scope is read
        independently of HOME.

        PROVEN NON-VACUOUS (task requirement): monkeypatching
        `neutralize_ambient_git_env` to a no-op during this task's own
        review made this exact test FAIL (the hostile system-scope
        credential's sentinel value appeared in the recorded Authorization
        header) -- confirming the assertion genuinely depends on this
        fix's GIT_CONFIG_SYSTEM/GIT_CONFIG_NOSYSTEM neutralization, not on
        some other code path that would avoid the leak regardless."""
        sentinel = "sentinel-system-scope-credential-should-never-be-sent"
        helper_script = tmp_path / "hostile-system-helper.sh"
        helper_script.write_text(
            "#!/bin/sh\n"
            'echo "username=hostile-system-user"\n'
            f'echo "password={sentinel}"\n'
        )
        helper_script.chmod(0o755)
        hostile_system_config = tmp_path / "hostile-system.gitconfig"
        hostile_system_config.write_text(f"[credential]\n\thelper = !{helper_script}\n")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", str(hostile_system_config))

        _RecordingAuthHandler.seen_auth_headers = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingAuthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            repo = _make_repo_only(tmp_path)
            remote_url = f"http://127.0.0.1:{port}/synthetic-owner/synthetic-repo.git"
            with pytest.raises(GitPushError):
                git_push_with_token(remote_url, "widget", "loadout-minted-token", repo)
        finally:
            server.shutdown()
            thread.join(timeout=5)

        hostile_value = _basic_auth_value("hostile-system-user", sentinel)
        assert all(hostile_value not in header for header in _RecordingAuthHandler.seen_auth_headers), (
            "the ambient SYSTEM-scope credential.helper's sentinel credential "
            "was sent as Basic auth -- system-scope neutralization failed"
        )

    def test_ambient_xdg_config_home_credential_helper_sentinel_never_created(self, tmp_path, monkeypatch):
        """Pre-merge security review finding (xdg-config-home-untested):
        plant a hostile credential.helper at $XDG_CONFIG_HOME/git/config --
        git's own documented fallback global-config location, consulted when
        ~/.gitconfig does not exist (which is exactly the isolated-HOME
        case this package's own credentialed subprocess always runs
        under). Assert the sentinel file the hostile helper would write is
        NEVER CREATED -- the strongest available signal: it proves the
        helper was never consulted at all, not merely that some other
        credential happened to win the race.

        XDG_CONFIG_HOME is set here to a directory OUTSIDE the isolated
        HOME _credentialed_git_env constructs, exactly matching an ambient
        deployment shape (an operator's real XDG_CONFIG_HOME pointing
        somewhere persistent, unrelated to any per-call temp directory this
        package creates).

        PROVEN NON-VACUOUS (task requirement, verified directly during this
        fix's own review, not merely argued) -- AND THIS IS WHY THE ORIGINAL
        "not a gap, GIT_CONFIG_GLOBAL already covers it" reasoning was
        incomplete, not merely untested:
          1. BASELINE (git's actual behavior, confirmed empirically): with
             GIT_CONFIG_GLOBAL/GIT_CONFIG_SYSTEM/GIT_CONFIG_NOSYSTEM
             UNSET and HOME pointed at a directory with no ~/.gitconfig,
             `git config --get user.name` against a config planted at
             $XDG_CONFIG_HOME/git/config successfully resolves it -- git
             genuinely reads this path in this axis's absence, so this is
             a real ambient surface, not a hypothetical one.
          2. Disabling ONLY `neutralize_ambient_git_env` (patched to a
             no-op) does NOT make this test fail -- the `-c
             credential.helper=` argv override (this module's OTHER
             defense-in-depth layer) silently covers the gap on its own,
             because command-line precedence beats a config-file-scoped
             helper regardless of which env vars are set. A test that
             disabled only the env layer would therefore PASS VACUOUSLY,
             proving nothing.
          3. Only disabling BOTH `neutralize_ambient_git_env` AND the `-c
             credential.helper=` argv prefix together makes THIS test FAIL:
             the sentinel file is created, confirming GIT_CONFIG_GLOBAL=
             /dev/null (which neutralize_ambient_git_env sets) is what
             suppresses the XDG fallback path specifically -- not HOME
             isolation alone, which does not touch XDG_CONFIG_HOME at all.
          THE TAKEAWAY: TWO independent mechanisms cover this axis (env-level
          GIT_CONFIG_GLOBAL neutralization, and the command-line -c
          override), and prior to this test NEITHER had actually been shown
          to close it -- the guarantee was asserted, not demonstrated. This
          is why the original "not a gap" verdict was incomplete rather than
          simply lacking a test: it happened to be correct, but only because
          two independent, previously-unproven mechanisms both cover it. A
          future author must not "simplify" away either mechanism (the env
          neutralization or the argv override) on the assumption the other
          alone is sufficient -- this test is what proves neither is
          individually provable-sufficient without the other also being
          disabled, so removing one without re-verifying reopens exactly
          this axis.
        """
        sentinel = tmp_path / "sentinel-xdg-config-home-helper-fired"
        helper_script = tmp_path / "hostile-xdg-helper.sh"
        helper_script.write_text(
            "#!/bin/sh\n"
            f'touch "{sentinel}"\n'
            "exit 1\n"
        )
        helper_script.chmod(0o755)

        xdg_config_home = tmp_path / "ambient-xdg-config-home"
        xdg_git_dir = xdg_config_home / "git"
        xdg_git_dir.mkdir(parents=True)
        (xdg_git_dir / "config").write_text(f"[credential]\n\thelper = !{helper_script}\n")
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_config_home))

        _RecordingAuthHandler.seen_auth_headers = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingAuthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            repo = _make_repo_only(tmp_path)
            remote_url = f"http://127.0.0.1:{port}/synthetic-owner/synthetic-repo.git"
            with pytest.raises(GitPushError):
                git_push_with_token(remote_url, "widget", "loadout-minted-token", repo)
        finally:
            server.shutdown()
            thread.join(timeout=5)

        assert not sentinel.exists(), (
            "the ambient XDG_CONFIG_HOME credential.helper's sentinel file was "
            "created -- the hostile helper WAS consulted, proving the XDG "
            "global-config fallback path was not actually neutralized"
        )

    def test_repo_local_credential_helper_and_extraheader_sentinel_never_appears(self, tmp_path):
        """Plant a repo-local credential.helper AND an http.<host>.extraheader
        carrying a sentinel token -- this package's own hazard check
        (fail-closed) must refuse the push BEFORE any subprocess spawns,
        so the sentinel can never reach output. Proven non-vacuous by
        asserting the refusal is specifically the hermeticity hazard error
        (RepoLocalConfigHazardError), not some other failure that happened
        to also avoid printing the sentinel; a bare local-path remote is
        sufficient here because check_repo_local_config_hazards runs
        BEFORE any transport is even chosen."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        sentinel = "sentinel-extraheader-token-should-never-leak"
        _git(["config", "--local", "credential.helper", "!echo should-not-run"], repo)
        _git(
            ["config", "--local", "http.https://example.invalid/.extraheader",
             f"Authorization: Bearer {sentinel}"],
            repo,
        )

        with pytest.raises(RepoLocalConfigHazardError) as exc_info:
            git_push_with_token("origin", "widget", "unused-token-value", repo)

        assert sentinel not in str(exc_info.value)
        assert "credential." in str(exc_info.value)
        assert "extraheader" in str(exc_info.value)

    def test_repo_local_pushinsteadof_redirect_sentinel_never_appears(self, tmp_path):
        """BOBBIE finding (bobbie.sast.repo-local-hazard-coverage-gap): a
        repo-local url.<base>.pushInsteadOf rule can silently redirect a
        push to an attacker-chosen host, which would then receive the
        minted token this package presents via GIT_ASKPASS -- a
        credential-EXPOSURE hazard even though the directive itself carries
        no secret. This package's own fail-closed hazard check must refuse
        the push BEFORE any subprocess spawns, so the redirect target
        (a sentinel attacker hostname) can never even be dialed, let alone
        receive a credential. Proven non-vacuous by asserting the refusal
        is specifically RepoLocalConfigHazardError (not some other failure
        that happened to also avoid the redirect), matching the pattern of
        the existing repo-local credential.helper/extraheader test above."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        sentinel_host = "sentinel-attacker-redirect-host.invalid"
        _git(
            ["config", "--local", f"url.https://{sentinel_host}/.pushInsteadOf",
             "https://example.invalid/"],
            repo,
        )

        redirect_target = "https://example.invalid/"
        with pytest.raises(RepoLocalConfigHazardError) as exc_info:
            git_push_with_token("origin", "widget", "unused-token-value", repo)

        assert "pushinsteadof" in str(exc_info.value)
        # For url.*.pushInsteadOf the attacker-controlled host is itself
        # part of the config KEY (the base URL is the section name), so it
        # legitimately appears in the message per
        # check_repo_local_config_hazards's own "key only, never the value"
        # contract -- this asserts the KEY (naming the hazardous entry) is
        # what surfaced, and the config VALUE (the rewrite target this
        # rule maps onto, i.e. what the caller believed it was pushing to)
        # is what does NOT appear, since a value is never reproduced.
        assert sentinel_host in str(exc_info.value)
        assert redirect_target not in str(exc_info.value)

    def test_ambient_netrc_sentinel_never_sent(self, tmp_path, monkeypatch):
        """Plant a fake ~/.netrc with a sentinel password under a real
        ambient HOME -- HOME isolation (unchanged from before this fix)
        already covers this; this test proves it still holds after the
        additional GIT_CONFIG_GLOBAL/SYSTEM neutralization was layered on
        top (i.e. the new code did not regress the pre-existing guarantee).
        Routed over real HTTP so .netrc is a code path libcurl would
        actually consult at all (see module docstring)."""
        real_home = tmp_path / "real_home_netrc"
        real_home.mkdir()
        sentinel = "sentinel-netrc-password-should-never-be-sent"
        (real_home / ".netrc").write_text(
            f"machine 127.0.0.1 login sentinel-netrc-login password {sentinel}\n"
        )
        (real_home / ".netrc").chmod(0o600)
        monkeypatch.setenv("HOME", str(real_home))

        _RecordingAuthHandler.seen_auth_headers = []
        server = http.server.HTTPServer(("127.0.0.1", 0), _RecordingAuthHandler)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            repo = _make_repo_only(tmp_path)
            remote_url = f"http://127.0.0.1:{port}/synthetic-owner/synthetic-repo.git"
            with pytest.raises(GitPushError):
                git_push_with_token(remote_url, "widget", "loadout-minted-token", repo)
        finally:
            server.shutdown()
            thread.join(timeout=5)

        hostile_value = _basic_auth_value("sentinel-netrc-login", sentinel)
        assert all(hostile_value not in header for header in _RecordingAuthHandler.seen_auth_headers), (
            "the ambient ~/.netrc sentinel credential was sent as Basic auth -- "
            "HOME isolation regressed"
        )
        assert (real_home / ".netrc").read_text() == (
            f"machine 127.0.0.1 login sentinel-netrc-login password {sentinel}\n"
        )

    def test_ambient_ssh_identity_env_stripped(self, tmp_path, monkeypatch):
        """Plant a fake GIT_SSH/GIT_SSH_COMMAND naming a wrapper script that
        writes a sentinel file if ever invoked -- this package's push is
        HTTP(S)-token-based and never shells out to an SSH wrapper on this
        code path regardless of whether the env var is stripped (an HTTP(S)
        remote never invokes GIT_SSH at all), so the sentinel-file half of
        this test is a corroborating smoke check, NOT the load-bearing
        proof -- verified during this task's own review: disabling only
        `neutralize_ambient_git_env` does NOT make the sentinel-file
        assertion fail (the wrapper genuinely is unreachable on this
        transport either way), so that half alone would be a vacuous test
        by this task's own standard. The LOAD-BEARING assertion is the
        direct unit-level check immediately below: neutralize_ambient_git_env
        must strip GIT_SSH/GIT_SSH_COMMAND from the dict it returns,
        proven the same way TestNeutralizeAmbientGitEnv's own dedicated
        test proves it, so THIS integration test cannot pass while that
        unit-level guarantee is broken. Kept here (not merely in
        TestNeutralizeAmbientGitEnv) so the env var actually reaching a
        real credentialed push call is exercised end-to-end, in case a
        future SSH-capable transport path is added to this package that
        WOULD reach GIT_SSH."""
        env = neutralize_ambient_git_env({
            "GIT_SSH": "/ambient/wrapper.sh",
            "GIT_SSH_COMMAND": "/ambient/wrapper.sh",
        })
        assert "GIT_SSH" not in env
        assert "GIT_SSH_COMMAND" not in env

        sentinel = tmp_path / "sentinel-ssh-wrapper-fired"
        ssh_wrapper = tmp_path / "hostile-ssh-wrapper.sh"
        ssh_wrapper.write_text(f'#!/bin/sh\ntouch "{sentinel}"\nexit 1\n')
        ssh_wrapper.chmod(0o755)
        monkeypatch.setenv("GIT_SSH", str(ssh_wrapper))
        monkeypatch.setenv("GIT_SSH_COMMAND", str(ssh_wrapper))

        repo, remote = _make_repo_with_bare_remote(tmp_path)
        git_push_with_token("origin", "widget", "unused-token-value", repo)

        assert not sentinel.exists(), (
            "the ambient SSH wrapper's sentinel file was created -- GIT_SSH/"
            "GIT_SSH_COMMAND was not actually stripped from the subprocess env"
        )
        r = subprocess.run(
            ["git", "branch", "-a"], cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert "widget" in r.stdout

    def test_minted_token_never_appears_even_with_hostile_env_planted(self, tmp_path, monkeypatch):
        """Combine a hostile global credential.helper AND a hostile .netrc
        under a real ambient HOME with a push that is FORCED to fail (bad
        remote name) -- assert the minted token appears nowhere in the
        raised error, proving the redaction guarantee holds even when the
        surrounding environment is adversarial, not just in the ordinary
        case."""
        real_home = tmp_path / "real_home_combined"
        real_home.mkdir()
        helper_script = tmp_path / "hostile-helper-combined.sh"
        helper_script.write_text("#!/bin/sh\necho password=WRONG-AMBIENT-VALUE\nexit 0\n")
        helper_script.chmod(0o755)
        _git(["config", "--global", "credential.helper", f"!{helper_script}"], real_home,
             env={**os.environ, "HOME": str(real_home)})
        (real_home / ".netrc").write_text("machine example.invalid login x password y\n")
        monkeypatch.setenv("HOME", str(real_home))

        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        secret_token = "sk-hostile-env-combined-should-never-leak"
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "widget", secret_token, repo)
        assert secret_token not in str(exc_info.value)
