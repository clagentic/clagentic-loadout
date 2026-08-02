"""test_push_git_push.py — tests for clagentic_loadout.push.git_push
(lr-09ca, Wave B slice 3; push-failure observability parity lr-035b75;
reject-reason-parser classifier corpus lr-f57f13).

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

CLASSIFICATION-CORRECTNESS CORPUS (lr-f57f13, task requirement 6):
TestClassifierCorpus below asserts sub_cause CORRECTNESS against fixtures
GENERATED FROM REAL GIT INVOCATIONS against local synthetic bare repos --
never hand-written stderr strings, which would encode the same wrong
assumptions the classifier already had (this task's own finding: two prior
"fixes" to this classifier both shipped without a test on this axis and
neither caught any of the three proven bugs). Each fixture function below
actually runs `git push` against a real bare repo constructed to trigger
the named failure shape, using invented local paths/branch names only (no
internal host/org/repo identity anywhere -- CLAUDE.md hard rules 1/8).
test_every_sub_cause_label_has_a_corpus_case is the self-policing test
required by this task: it enumerates push.push_failure_labels.SUB_CAUSE_LABELS
and fails if any label lacks a covering fixture, so the taxonomy's own
growth cannot silently regress this coverage the way the two prior fixes
did.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import Callable

import pytest

from clagentic_loadout.push.errors import GitPushError
from clagentic_loadout.push.git_push import (
    GitFetchError,
    _classify_push_failure,
    git_fetch_with_token,
    git_push_with_token,
)
from clagentic_loadout.push.push_failure_labels import (
    SUB_CAUSE_BAD_REFSPEC,
    SUB_CAUSE_LABELS,
    SUB_CAUSE_LOCAL_HOOK_REJECTED,
    SUB_CAUSE_NON_FAST_FORWARD,
    SUB_CAUSE_OTHER_REJECT_REASON,
    SUB_CAUSE_PRE_RECEIVE_REJECTED,
    SUB_CAUSE_TRANSPORT,
    SUB_CAUSE_UNKNOWN,
)


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
        # Match "push" ANYWHERE in argv, not just "$1" -- git_push_with_token
        # now prepends a hermetic "-c credential.helper=" argv prefix
        # (lr-a868d2) ahead of the "push" subcommand itself, so "$1" is "-c"
        # rather than "push" on every real invocation this module makes.
        wrapper.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "push" ]; then\n'
            '    echo "remote: Invalid username or token." 1>&2\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
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

    def test_sentinel_token_never_appears_in_typed_fields_or_str(self, tmp_path):
        """lr-f57f13 SECRET REDACTION requirement: a sentinel token value
        must appear nowhere in the exception string NOR in any typed field
        on the raised object (raw_stderr, reject_reason, remote_lines,
        local_hook_lines) -- redaction happens ONCE at GitPushError
        construction (push.push_redaction), so every field is already safe
        by the time a caller reads it, not only the formatted string."""
        repo, remote = _make_repo_with_bare_remote(tmp_path)
        sentinel = "sk-sentinel-should-never-leak-anywhere-abc123"
        hooks_dir = remote / "hooks"
        pre_receive = hooks_dir / "pre-receive"
        pre_receive.write_text(
            "#!/bin/sh\n"
            f'echo "remote token in transit: {sentinel}" 1>&2\n'
            "exit 1\n"
        )
        pre_receive.chmod(0o755)

        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("origin", "feature", sentinel, repo)

        exc = exc_info.value
        assert sentinel not in str(exc)
        assert sentinel not in exc.raw_stderr
        assert all(sentinel not in line for line in exc.remote_lines)
        assert all(sentinel not in line for line in exc.local_hook_lines)
        assert exc.reject_reason is None or sentinel not in exc.reject_reason

    def test_str_of_git_push_error_contains_raw_stderr(self, tmp_path):
        """lr-f57f13 invariant test (seq 4, TYPED ERROR OBJECT section):
        str(exc) must contain exc.raw_stderr -- proving the fields are not
        merely decorative parallel data a future author could let drift out
        of sync with the displayed message."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", "tok", repo)
        exc = exc_info.value
        assert exc.raw_stderr.strip()[:200] in str(exc)

    def test_typed_fields_populated_on_every_failure(self, tmp_path):
        """GitPushError's typed fields (lr-f57f13) must be populated, not
        left at their dataclass-less defaults, on an ordinary failure."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", "tok", repo)
        exc = exc_info.value
        assert exc.exit_code != 0
        assert exc.sub_cause in SUB_CAUSE_LABELS
        assert exc.raw_stderr
        assert exc.remote == "nonexistent-remote"
        assert exc.refspec == "feature:feature"

    def test_git_push_error_direct_construction_still_redacts(self):
        """Pre-merge security review finding: redaction must be STRUCTURAL
        (performed inside GitPushError.__init__ itself), not merely true by
        convention because the sole current caller (push.git_push)
        happened to pre-redact every argument before constructing.

        This test constructs GitPushError DIRECTLY, bypassing
        push.git_push entirely, with UNREDACTED sentinel values on every
        string-bearing field -- if redaction were only convention at the
        git_push.py call site (the gap the review found), every field
        asserted here would come back containing the raw sentinel."""
        sentinel_token = "sk-direct-construction-sentinel-should-never-survive"
        exc = GitPushError(
            f"unredacted message carrying token {sentinel_token} inline",
            exit_code=1,
            sub_cause="unknown",
            raw_stderr=f"raw stderr with token {sentinel_token} embedded",
            reject_reason=f"reason mentioning {sentinel_token}",
            remote_lines=(f"remote line with {sentinel_token}",),
            local_hook_lines=(f"hook line with {sentinel_token}",),
            known_secrets=(sentinel_token,),
        )
        assert sentinel_token not in str(exc)
        assert sentinel_token not in exc.raw_stderr
        assert sentinel_token not in exc.reject_reason
        assert all(sentinel_token not in line for line in exc.remote_lines)
        assert all(sentinel_token not in line for line in exc.local_hook_lines)

    def test_git_push_error_strips_ansi_escapes_from_remote_controlled_text(self):
        """Pre-merge security review finding: ANSI escape sequences in
        remote-controlled text (a 'remote: '-prefixed line, a parsed
        reject-reason string) must never reach operator-visible stderr --
        a malicious remote could otherwise inject terminal escapes.
        Constructed directly (mirrors the redaction test above) so this
        proves the guarantee at the GitPushError boundary itself."""
        malicious_reason = "stale info\x1b[31;1m INJECTED RED TEXT\x1b[0m"
        malicious_remote_line = "policy: rejected\x1b]0;pwned-title\x07 more text"
        exc = GitPushError(
            f"push failed: {malicious_reason}",
            exit_code=1,
            sub_cause="pre-receive-rejected",
            raw_stderr=malicious_remote_line,
            reject_reason=malicious_reason,
            remote_lines=(malicious_remote_line,),
        )
        assert "\x1b" not in str(exc)
        assert "\x1b" not in exc.raw_stderr
        assert "\x1b" not in exc.reject_reason
        assert all("\x1b" not in line for line in exc.remote_lines)
        # The non-escape content survives -- this is sanitization, not a
        # blanket wipe of the diagnostic text.
        assert "stale info" in exc.reject_reason
        assert "policy: rejected" in exc.remote_lines[0]

    def test_git_push_error_strips_c1_control_characters(self):
        """Second-pass security review finding: the module docstring
        originally claimed C0/C1 control-character stripping, but the
        regex only stripped C0 plus DEL -- the C1 range (0x80-0x9F) was
        NOT actually stripped. This asserts the fixed regex genuinely
        strips C1, closing the documented-but-undelivered gap (the same
        defect shape this whole task exists to eliminate: a public claim
        the code did not back). Built via chr() so the codepoints under
        test are unambiguous."""
        pad_char = chr(0x80)
        nel_char = chr(0x85)
        malicious_reason = "stale info" + pad_char + nel_char + " trailing"
        exc = GitPushError(
            "push failed",
            exit_code=1,
            sub_cause="non-fast-forward",
            raw_stderr="irrelevant",
            reject_reason=malicious_reason,
        )
        assert pad_char not in exc.reject_reason
        assert nel_char not in exc.reject_reason
        assert "stale info" in exc.reject_reason
        assert "trailing" in exc.reject_reason

    def test_control_char_stripping_preserves_legitimate_multibyte_utf8(self):
        """Second-pass security review finding, safety condition: stripping
        the C1 codepoint range is only safe because this module operates
        on already-decoded str text, never raw bytes -- at the byte level,
        0x80-0x9F are continuation bytes inside legitimate multi-byte UTF-8
        sequences, and a naive byte-level strip would corrupt genuine
        non-ASCII content. This asserts real multi-byte UTF-8 text (a
        precomposed accented Latin letter and a non-Latin letter a remote
        message might legitimately contain) survives redaction intact."""
        e_acute = chr(0xE9)
        cyrillic_a = chr(0x430)
        legitimate_text = "pre-receive hook declined: caf" + e_acute + " ok " + cyrillic_a + "bc"
        exc = GitPushError(
            "push failed",
            exit_code=1,
            sub_cause="pre-receive-rejected",
            raw_stderr="irrelevant",
            remote_lines=(legitimate_text,),
        )
        assert exc.remote_lines[0] == legitimate_text


class TestGitFetchWithToken:
    """git_fetch_with_token (lr-f57f13, pre-merge security review finding):
    the pre-lease fetch used by push.lease_control.resolve_lease must run
    through the SAME credentialed envelope git_push_with_token uses, never
    a bare ambient `git fetch` -- and its failure message must already be
    redacted, since push.lease_control folds it directly into a printed
    warning with no second redaction pass."""

    def test_successful_fetch_to_local_bare_remote(self, tmp_path):
        repo, remote = _make_repo_with_bare_remote(tmp_path)
        git_push_with_token("origin", "feature", "unused-token-value", repo)
        # A second, independent clone fetches the branch the first push
        # above landed -- proves git_fetch_with_token's own subprocess call
        # (not git_push_with_token's) actually reaches the remote.
        other_repo = tmp_path / "other-clone"
        _git(["clone", str(remote), str(other_repo)], tmp_path)
        git_fetch_with_token("origin", "feature", "unused-token-value", other_repo)
        r = subprocess.run(
            ["git", "rev-parse", "refs/remotes/origin/feature"],
            cwd=str(other_repo), capture_output=True, text=True,
        )
        assert r.returncode == 0

    def test_fetch_failure_raises_git_fetch_error(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitFetchError):
            git_fetch_with_token("nonexistent-remote", "feature", "some-token-value", repo)

    def test_fetch_failure_message_is_already_redacted(self, tmp_path, monkeypatch):
        """push.lease_control folds str(GitFetchError) directly into a
        printed warning with no second redaction step -- this asserts the
        message git_fetch_with_token raises is safe on its own."""
        fake_bin = tmp_path / "fakebin"
        fake_bin.mkdir()
        real_git = subprocess.run(["which", "git"], capture_output=True, text=True, check=True).stdout.strip()
        wrapper = fake_bin / "git"
        # Match "fetch" ANYWHERE in argv, not just "$1" -- git_fetch_with_token
        # now prepends a hermetic "-c credential.helper=" argv prefix
        # (lr-a868d2) ahead of the "fetch" subcommand itself, so "$1" is "-c"
        # rather than "fetch" on every real invocation this module makes. A
        # "$1"-only match here would silently fall through to real git and
        # never actually exercise the sentinel secret -- a vacuous pass this
        # task's own test-quality requirement calls out by name.
        wrapper.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "fetch" ]; then\n'
            '    echo "fatal: could not read Username for '"'"'https://x:sk-leaked-secret@host'"'"': terminal prompts disabled" 1>&2\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            f'exec "{real_git}" "$@"\n'
        )
        wrapper.chmod(0o755)
        monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitFetchError) as exc_info:
            git_fetch_with_token("origin", "feature", "sk-leaked-secret", repo)
        assert "sk-leaked-secret" not in str(exc_info.value)
        assert "sk-leaked-secret" not in repr(exc_info.value)

    def test_fetch_token_never_appears_in_raised_message(self, tmp_path):
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        secret_token = "sk-fetch-secret-token-should-never-leak"
        with pytest.raises(GitFetchError) as exc_info:
            git_fetch_with_token("nonexistent-remote", "feature", secret_token, repo)
        assert secret_token not in str(exc_info.value)


# ---------------------------------------------------------------------------
# CLASSIFICATION-CORRECTNESS CORPUS (lr-f57f13, task requirement 6)
# ---------------------------------------------------------------------------
#
# Every fixture below runs a REAL `git push` against a REAL local bare repo
# constructed to trigger the named failure shape -- never a hand-written
# stderr string. Synthetic invented names only (no internal host/org/repo
# identity -- CLAUDE.md hard rules 1/8).


def _run_git(args: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)


def _isolated_env(tmp_path: Path) -> dict:
    """An isolated HOME (mirrors git_push_with_token's own posture) so a
    real host's ambient git config/credentials never leak into a fixture's
    corpus-generation push."""
    home = tmp_path / "isohome"
    home.mkdir(exist_ok=True)
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def _fixture_fetch_first(tmp_path: Path) -> str:
    """Real git repro of a plain client-side non-fast-forward rejection:
    two independent clones push different commits to the same branch: the
    second push is rejected because the remote has work the second clone
    does not have locally."""
    d = tmp_path / "fetch_first"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", "-b", "widget"], d, env=env)
    _run_git(["init", "-q", "--bare", "-b", "widget", str(srv)], d, env=env)
    wc_a = d / "wcA"
    wc_b = d / "wcB"
    _run_git(["clone", "-q", str(srv), str(wc_a)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc_a, env=env)
    _run_git(["config", "user.name", "A"], wc_a, env=env)
    (wc_a / "seed.txt").write_text("seed\n")
    _run_git(["add", "seed.txt"], wc_a, env=env)
    _run_git(["commit", "-qm", "chore: seed"], wc_a, env=env)
    _run_git(["push", "-q", "origin", "widget:widget"], wc_a, env=env)

    _run_git(["clone", "-q", str(srv), str(wc_b)], d, env=env)
    _run_git(["config", "user.email", "b@example.invalid"], wc_b, env=env)
    _run_git(["config", "user.name", "B"], wc_b, env=env)

    (wc_a / "second.txt").write_text("second\n")
    _run_git(["add", "second.txt"], wc_a, env=env)
    _run_git(["commit", "-qm", "chore: second"], wc_a, env=env)
    _run_git(["push", "-q", "origin", "widget:widget"], wc_a, env=env)

    (wc_b / "third.txt").write_text("third\n")
    _run_git(["add", "third.txt"], wc_b, env=env)
    _run_git(["commit", "-qm", "chore: third"], wc_b, env=env)

    result = _run_git(["push", "origin", "widget:widget"], wc_b, env=env)
    assert result.returncode != 0, "fixture setup failed to reproduce fetch-first"
    return result.stderr


def _fixture_stale_info(tmp_path: Path) -> str:
    """Real git repro of the lr-f57f13-central bug: a --force-with-lease
    push evaluated against a STALE local remote-tracking ref (the fetch is
    deliberately SKIPPED here) produces "(stale info)" -- the exact shape
    that fell through to sub_cause=unknown before this fix, and the exact
    shape git itself prints with NO explanatory hint block at all."""
    d = tmp_path / "stale_info"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", "-b", "widget", str(srv)], d, env=env)
    wc_a = d / "wcA"
    wc_b = d / "wcB"
    _run_git(["clone", "-q", str(srv), str(wc_a)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc_a, env=env)
    _run_git(["config", "user.name", "A"], wc_a, env=env)
    (wc_a / "seed.txt").write_text("seed\n")
    _run_git(["add", "seed.txt"], wc_a, env=env)
    _run_git(["commit", "-qm", "chore: seed"], wc_a, env=env)
    _run_git(["push", "-q", "origin", "widget:widget"], wc_a, env=env)

    _run_git(["clone", "-q", str(srv), str(wc_b)], d, env=env)
    _run_git(["config", "user.email", "b@example.invalid"], wc_b, env=env)
    _run_git(["config", "user.name", "B"], wc_b, env=env)
    _run_git(["checkout", "-q", "-B", "widget", "origin/widget"], wc_b, env=env)

    (wc_a / "second.txt").write_text("second\n")
    _run_git(["add", "second.txt"], wc_a, env=env)
    _run_git(["commit", "-qm", "chore: second"], wc_a, env=env)
    _run_git(["push", "-q", "origin", "widget:widget"], wc_a, env=env)

    (wc_b / "third.txt").write_text("third\n")
    _run_git(["add", "third.txt"], wc_b, env=env)
    _run_git(["commit", "-qm", "chore: third"], wc_b, env=env)

    # NO fetch here -- wcB's remote-tracking ref is deliberately stale,
    # reproducing the exact defect (loadout never fetches before a forced
    # lease evaluation).
    result = _run_git(
        ["push", "origin", "widget:widget", "--force-with-lease"], wc_b, env=env,
    )
    assert result.returncode != 0, "fixture setup failed to reproduce stale info"
    assert "stale info" in result.stderr, f"fixture did not reproduce stale info: {result.stderr!r}"
    return result.stderr


def _fixture_cannot_lock_ref(tmp_path: Path) -> str:
    """Real git repro of a server-side "cannot lock ref" race: two
    concurrent pushes target the same new branch on the same bare remote;
    the second to arrive loses the ref-creation race."""
    d = tmp_path / "cannot_lock"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", str(srv)], d, env=env)
    hookdir = srv / "hooks"
    (hookdir / "pre-receive").write_text("#!/bin/sh\nsleep 2\nexit 0\n")
    (hookdir / "pre-receive").chmod(0o755)

    wc_a = d / "wcA"
    wc_b = d / "wcB"
    _run_git(["clone", "-q", str(srv), str(wc_a)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc_a, env=env)
    _run_git(["config", "user.name", "A"], wc_a, env=env)
    (wc_a / "f.txt").write_text("f\n")
    _run_git(["add", "f.txt"], wc_a, env=env)
    _run_git(["commit", "-qm", "chore: f"], wc_a, env=env)

    _run_git(["clone", "-q", str(srv), str(wc_b)], d, env=env)
    _run_git(["config", "user.email", "b@example.invalid"], wc_b, env=env)
    _run_git(["config", "user.name", "B"], wc_b, env=env)
    (wc_b / "g.txt").write_text("g\n")
    _run_git(["add", "g.txt"], wc_b, env=env)
    _run_git(["commit", "-qm", "chore: g"], wc_b, env=env)

    proc_a = subprocess.Popen(
        ["git", "push", "origin", "HEAD:refs/heads/widget"],
        cwd=str(wc_a), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    time.sleep(0.3)
    proc_b = subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/widget"],
        cwd=str(wc_b), capture_output=True, text=True, env=env,
    )
    proc_a.communicate()
    assert proc_b.returncode != 0, "fixture setup failed to reproduce cannot lock ref"
    assert "cannot lock ref" in proc_b.stderr, (
        f"fixture did not reproduce cannot lock ref: {proc_b.stderr!r}"
    )
    return proc_b.stderr


def _fixture_bad_refspec(tmp_path: Path) -> str:
    """Real git repro of lr-f57f13 bug 3: pushing a refspec whose SOURCE
    does not resolve to any local ref -- e.g. loadout building
    "<branch>:<branch>" from a branch name that does not exist locally
    under that exact name. Previously misclassified as local-hook-rejected
    (WORSE than unknown, a confidently FALSE label)."""
    d = tmp_path / "bad_refspec"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", str(srv)], d, env=env)
    wc = d / "wc"
    _run_git(["clone", "-q", str(srv), str(wc)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc, env=env)
    _run_git(["config", "user.name", "A"], wc, env=env)
    (wc / "f.txt").write_text("f\n")
    _run_git(["add", "f.txt"], wc, env=env)
    _run_git(["commit", "-qm", "chore: f"], wc, env=env)

    result = _run_git(
        ["push", "origin", "totally-invented-nonexistent-branch:totally-invented-nonexistent-branch",
         "--set-upstream"],
        wc, env=env,
    )
    assert result.returncode != 0, "fixture setup failed to reproduce bad refspec"
    assert "does not match any" in result.stderr
    return result.stderr


def _fixture_other_reject_reason(tmp_path: Path) -> str:
    """Real git repro of SUB_CAUSE_OTHER_REJECT_REASON: re-pushing a
    force-moved tag without --force produces
    "! [rejected] <tag> -> <tag> (already exists)" -- a client-side
    reject-reason parenthetical with no "remote: " lines, no local-hook
    shape, and no known non-fast-forward-family literal. Proves the parser
    finds A reason and reports it verbatim rather than silently relabeling
    an unrecognized-but-real reason as "unknown"."""
    d = tmp_path / "other_reason"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", str(srv)], d, env=env)
    wc = d / "wc"
    _run_git(["clone", "-q", str(srv), str(wc)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc, env=env)
    _run_git(["config", "user.name", "A"], wc, env=env)
    (wc / "f.txt").write_text("f\n")
    _run_git(["add", "f.txt"], wc, env=env)
    _run_git(["commit", "-qm", "chore: f"], wc, env=env)
    _run_git(["tag", "v-synthetic-1"], wc, env=env)
    _run_git(["push", "-q", "origin", "v-synthetic-1"], wc, env=env)

    (wc / "f2.txt").write_text("f2\n")
    _run_git(["add", "f2.txt"], wc, env=env)
    _run_git(["commit", "-qm", "chore: f2"], wc, env=env)
    _run_git(["tag", "-f", "v-synthetic-1"], wc, env=env)

    result = _run_git(["push", "origin", "v-synthetic-1"], wc, env=env)
    assert result.returncode != 0, "fixture setup failed to reproduce a re-pushed tag rejection"
    assert "already exists" in result.stderr
    return result.stderr


def _fixture_pre_receive_decline(tmp_path: Path) -> str:
    """Real git repro of a server-side pre-receive hook decline."""
    d = tmp_path / "pre_receive"
    d.mkdir()
    env = _isolated_env(d)
    srv = d / "srv.git"
    _run_git(["init", "-q", "--bare", str(srv)], d, env=env)
    hookdir = srv / "hooks"
    (hookdir / "pre-receive").write_text(
        "#!/bin/sh\necho \"policy: rejected by synthetic gate\" 1>&2\nexit 1\n"
    )
    (hookdir / "pre-receive").chmod(0o755)
    wc = d / "wc"
    _run_git(["clone", "-q", str(srv), str(wc)], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], wc, env=env)
    _run_git(["config", "user.name", "A"], wc, env=env)
    (wc / "f.txt").write_text("f\n")
    _run_git(["add", "f.txt"], wc, env=env)
    _run_git(["commit", "-qm", "chore: f"], wc, env=env)

    result = _run_git(["push", "origin", "HEAD:refs/heads/widget"], wc, env=env)
    assert result.returncode != 0, "fixture setup failed to reproduce pre-receive decline"
    return result.stderr


def _fixture_transport_unreachable(tmp_path: Path) -> str:
    """Real git repro of a transport-level failure: an HTTP endpoint on a
    reserved, guaranteed-unbound local port -- never reaches any server-side
    hook, no sideband at all."""
    d = tmp_path / "transport"
    d.mkdir()
    env = _isolated_env(d)
    _run_git(["init", "-q", "-b", "widget"], d, env=env)
    _run_git(["config", "user.email", "a@example.invalid"], d, env=env)
    _run_git(["config", "user.name", "A"], d, env=env)
    (d / "f.txt").write_text("f\n")
    _run_git(["add", "f.txt"], d, env=env)
    _run_git(["commit", "-qm", "chore: f"], d, env=env)

    result = _run_git(
        ["push", "http://127.0.0.1:1/synthetic-owner/synthetic-repo.git", "widget:widget"],
        d, env=env,
    )
    assert result.returncode != 0, "fixture setup failed to reproduce transport failure"
    return result.stderr


#: The full classification-correctness corpus: sub_cause label -> a fixture
#: function that REPRODUCES that shape via a real git invocation (task
#: requirement 6). Used both to assert per-case correctness and, via
#: test_every_sub_cause_label_has_a_corpus_case, to self-police that every
#: label in push.push_failure_labels.SUB_CAUSE_LABELS has a covering case.
_CLASSIFIER_CORPUS: dict[str, Callable[[Path], str]] = {
    SUB_CAUSE_NON_FAST_FORWARD: _fixture_fetch_first,
    SUB_CAUSE_PRE_RECEIVE_REJECTED: _fixture_pre_receive_decline,
    SUB_CAUSE_BAD_REFSPEC: _fixture_bad_refspec,
    SUB_CAUSE_TRANSPORT: _fixture_transport_unreachable,
    SUB_CAUSE_OTHER_REJECT_REASON: _fixture_other_reject_reason,
}


class TestClassifierCorpus:
    """lr-f57f13 task requirement 6: classification CORRECTNESS asserted
    against fixtures generated from REAL git invocations -- not transcript
    recoverability (never broken; a corpus asserting only that would have
    caught none of the three proven bugs)."""

    def test_every_sub_cause_label_has_a_corpus_case(self):
        """Self-policing test (task requirement 6, final bullet): a new
        sub_cause label added to push.push_failure_labels.SUB_CAUSE_LABELS
        without a corresponding corpus fixture must FAIL this test.
        SUB_CAUSE_UNKNOWN and SUB_CAUSE_LOCAL_HOOK_REJECTED are covered by
        dedicated tests elsewhere in this module (the observed-incident
        single-line transcript, and the pre-existing local pre-push hook
        test above) rather than the fixture-function table, since both are
        already exercised end-to-end there; every OTHER label must appear
        in _CLASSIFIER_CORPUS."""
        externally_covered = {SUB_CAUSE_UNKNOWN, SUB_CAUSE_LOCAL_HOOK_REJECTED}
        missing = SUB_CAUSE_LABELS - set(_CLASSIFIER_CORPUS) - externally_covered
        assert not missing, (
            f"sub_cause label(s) {missing} have no covering corpus fixture -- "
            f"add a case to _CLASSIFIER_CORPUS (or to `externally_covered` "
            f"with a named dedicated test elsewhere in this module)"
        )

    @pytest.mark.parametrize("sub_cause", sorted(_CLASSIFIER_CORPUS))
    def test_corpus_case_classifies_to_its_own_label(self, sub_cause, tmp_path):
        stderr = _CLASSIFIER_CORPUS[sub_cause](tmp_path)
        assert _classify_push_failure(stderr) == sub_cause

    def test_stale_info_never_classifies_as_unknown(self, tmp_path):
        """lr-f57f13 bug 1, the central proven defect: a --force-with-lease
        rejection against a stale local remote-tracking ref prints
        "(stale info)" with NO hint block -- previously fell through to
        sub_cause=unknown."""
        stderr = _fixture_stale_info(tmp_path)
        sub_cause = _classify_push_failure(stderr)
        assert sub_cause != SUB_CAUSE_UNKNOWN
        assert sub_cause == SUB_CAUSE_NON_FAST_FORWARD

    def test_cannot_lock_ref_never_classifies_as_unknown(self, tmp_path):
        """lr-f57f13 bug 2: a server-side "cannot lock ref" race must never
        classify as unknown."""
        stderr = _fixture_cannot_lock_ref(tmp_path)
        assert _classify_push_failure(stderr) != SUB_CAUSE_UNKNOWN

    def test_bad_refspec_never_classifies_as_local_hook_rejected(self, tmp_path):
        """lr-f57f13 bug 3: worse than unknown -- a confidently FALSE label.
        "error: src refspec ... does not match any" occurs before any hook
        could run and must never be attributed to one."""
        stderr = _fixture_bad_refspec(tmp_path)
        sub_cause = _classify_push_failure(stderr)
        assert sub_cause != SUB_CAUSE_LOCAL_HOOK_REJECTED
        assert sub_cause == SUB_CAUSE_BAD_REFSPEC

    def test_observed_incident_single_line_transcript_still_classifies_unknown_but_carries_facts(self, tmp_path):
        """The originating incident's own literal transcript (a single
        client-side summary line with no reject-reason line, no remote
        lines, no local-hook lines) genuinely has no reason for the
        classifier to name -- this asserts it STILL correctly resolves to
        "unknown" (this shape really is unclassifiable from the transcript
        alone) AND that the raised message carries positively-stated
        observed facts (exit code, byte count, reached_transport) rather
        than a guessed narrative about why the remote was silent (CLAUDE.md
        hard rule 4; this task's own REJECTED-ALTERNATIVE decision)."""
        repo, _remote = _make_repo_with_bare_remote(tmp_path)
        with pytest.raises(GitPushError) as exc_info:
            git_push_with_token("nonexistent-remote", "feature", "tok", repo)
        exc = exc_info.value
        assert exc.sub_cause == SUB_CAUSE_UNKNOWN
        message = str(exc)
        assert "unknown" in message
        assert f"exit={exc.exit_code}" in message
        assert "stderr_bytes=" in message
        assert "reached_transport=" in message
        # Never a guessed narrative claiming to know WHY the signal is
        # absent -- this shape's absence-of-sideband is genuinely
        # unresolved, and the message must not assert otherwise.
        assert "never reached the server-side hook" not in message
        assert "was refused before it" not in message
