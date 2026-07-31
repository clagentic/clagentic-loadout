"""test_push_git_push.py — tests for clagentic_loadout.push.git_push
(lr-09ca, Wave B slice 3; push-failure observability parity lr-035b75).

Exercises git_push_with_token against a REAL local bare repo used as the
"remote" (filesystem path, not a network endpoint) -- no real network call
is ever made; git's own local-transport code path handles a file:// /
bare-path remote without touching a socket.

no-token-in-logs (SECURITY POSTURE preserved from the reference push
transport's test_token_not_in_logs guarantee): asserts the raw token value
never appears in any raised exception message, even on a failed push.

Push-failure observability (lr-035b75, ported from the reference push
transport's lr-a1fd4d / lr-8460a4 fixes):
  - a server-side pre-receive hook rejection is simulated via a real
    `hooks/pre-receive` script on the bare "remote" repo, which prints to
    stderr and exits non-zero -- git prefixes every line it sends back with
    "remote: ", and this test asserts that content survives into the
    raised GitPushError message (previously only the generic client-side
    summary line survived).
  - a LOCAL pre-push hook abort is simulated via `hooks/pre-push` on the
    local repo (client-side, fires before any network I/O) -- exercises
    the local-hook-rejected classification path distinctly from both a
    remote rejection and a transport failure.
  - a transport-only failure (no such remote configured) asserts no
    fabricated remote/hook block appears.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.push.errors import GitPushError
from clagentic_loadout.push.git_push import git_push_with_token


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


def _make_repo_with_bare_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "feature"], repo)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "f.txt").write_text("content\n")
    _git(["add", "f.txt"], repo)
    _git(["commit", "-m", "work"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)

    return repo, remote


class TestGitPushWithToken:
    def test_successful_push_to_local_bare_remote(self, tmp_path):
        repo, remote = _make_repo_with_bare_remote(tmp_path)
        # A bare local-path remote never consults GIT_ASKPASS (no HTTP auth
        # prompt), so this exercises the full push plumbing (env, HOME
        # isolation, cwd, cleanup) without needing a fake credential helper
        # to actually be invoked.
        git_push_with_token("origin", "feature", "unused-token-value", repo)

        r = subprocess.run(
            ["git", "branch", "-a"], cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert "feature" in r.stdout

    def test_push_failure_raises_git_push_error(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        # Point at a remote name with no configured URL -> git push fails
        # deterministically without any network access.
        with pytest.raises(GitPushError):
            git_push_with_token("nonexistent-remote", "feature", "some-token-value", repo)

    def test_token_value_never_appears_in_raised_message(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        secret_token = "sk-super-secret-token-xyz-should-never-leak"
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", secret_token, repo)
        assert secret_token not in str(exc_info.value)

    def test_force_with_lease_flag_is_accepted(self, tmp_path):
        repo, remote = _make_repo_with_bare_remote(tmp_path)
        git_push_with_token("origin", "feature", "unused-token-value", repo)
        # Amend to simulate a history rewrite, then push again with
        # force_with_lease -- would fail as non-fast-forward without it.
        _git(["commit", "--amend", "-m", "work (amended)"], repo)
        git_push_with_token("origin", "feature", "unused-token-value", repo, force_with_lease=True)

    def test_platform_mismatch_hint_included_on_auth_shaped_failure(self, tmp_path, monkeypatch):
        """A synthetic auth-shaped git stderr (via a wrapper 'git' that
        fails with an auth-marker message) should carry the platform-
        mismatch hint naming the other platform -- proven via a fake git
        binary on PATH that fails with the exact marker text."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True, check=True).stdout.strip()
        wrapper = fake_bin / "git"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "push" ]; then\n'
            '  echo "remote: Invalid username or token." 1>&2\n'
            "  exit 1\n"
            "fi\n"
            f'exec "{real_git}" "$@"\n'
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token(
                "origin", "feature", "tok", repo,
                platform="github", other_platform_label="forgejo",
            )
        assert "PLATFORM MISMATCH" in str(exc_info.value)
        assert "forgejo" in str(exc_info.value)

    def test_pre_receive_rejection_surfaces_remote_message(self, tmp_path):
        """A real server-side pre-receive hook rejection: git prefixes every
        line the remote sends back with "remote: " -- the ACTUAL rejection
        reason. Previously only the generic client-side summary line
        ("error: failed to push some refs") survived; this asserts the
        remote-side text is now present in the raised message, tagged with
        the pre-receive-rejected sub_cause."""
        repo, remote = _make_repo_with_bare_remote(tmp_path)
        hooks_dir = remote / "hooks"
        pre_receive = hooks_dir / "pre-receive"
        pre_receive.write_text(
            "#!/bin/sh\n"
            'echo "policy: direct pushes to protected branches are rejected" 1>&2\n'
            "exit 1\n"
        )
        pre_receive.chmod(0o755)

        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("origin", "feature", "unused-token-value", repo)

        message = str(exc_info.value)
        assert "pre-receive-rejected" in message
        assert "policy: direct pushes to protected branches are rejected" in message
        assert "REMOTE MESSAGE" in message

    def test_local_pre_push_hook_abort_classifies_local_hook_rejected(self, tmp_path):
        """A LOCAL .git/hooks/pre-push abort exits non-zero BEFORE any
        network negotiation -- zero "remote: " lines, no transport
        substrings. Without local-hook detection this collapses to
        sub_cause=unknown, indistinguishable from a dead connection. This
        asserts the distinct local-hook-rejected classification and that
        the hook's own stderr (not the generic summary alone) is
        surfaced."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        local_hook = repo / ".git" / "hooks" / "pre-push"
        local_hook.write_text(
            "#!/bin/sh\n"
            'echo "docs-staleness gate: CHANGELOG.md was not updated" 1>&2\n'
            "exit 1\n"
        )
        local_hook.chmod(0o755)

        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("origin", "feature", "unused-token-value", repo)

        message = str(exc_info.value)
        assert "local-hook-rejected" in message
        assert "unknown" not in message
        assert "docs-staleness gate: CHANGELOG.md was not updated" in message
        assert "LOCAL PRE-PUSH HOOK MESSAGE" in message
        assert "REMOTE MESSAGE" not in message

    def test_transport_failure_fabricates_no_remote_or_hook_block(self, tmp_path):
        """A transport-only failure (no such remote configured) never
        reaches the server and has no local pre-push hook installed --
        asserts neither a REMOTE MESSAGE nor a LOCAL PRE-PUSH HOOK MESSAGE
        block is fabricated for a failure class that has neither."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", "some-token-value", repo)

        message = str(exc_info.value)
        assert "REMOTE MESSAGE" not in message
        assert "LOCAL PRE-PUSH HOOK MESSAGE" not in message

    def test_git_trace_env_var_does_not_leak_token(self, tmp_path, monkeypatch):
        """The opt-in GIT_TRACE diagnostic passthrough folds git's own
        packet/hook/transport trace into stderr -- assert enabling it still
        never leaks the token value into the raised message (no-token-leak
        invariant holds even with verbose tracing on)."""
        monkeypatch.setenv("CLAGENTIC_LOADOUT_PUSH_GIT_TRACE", "1")
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        secret_token = "sk-super-secret-token-xyz-should-never-leak"
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", secret_token, repo)
        assert secret_token not in str(exc_info.value)
