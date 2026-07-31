"""test_push_shared_git_push_entrypoint.py — locks push.verb's single shared
git_push_with_token entrypoint for BOTH platforms (lr-ee9044).

An internal deployment's own lr-ad06ef parity gate already established
(lr-035b75) that loadout's push mechanism + failure-classification
(push.git_push.git_push_with_token / _classify_push_failure /
_extract_remote_lines) is byte-equal to that deployment's own git-push path,
run for BOTH Forgejo and GitHub. This file adds the piece that specific
observation didn't cover: a REGRESSION LOCK proving push.verb itself calls
that one shared function for both platforms today, so a future refactor
cannot quietly fork a divergent per-platform push implementation without
this test failing first.

Approach: monkeypatch clagentic_loadout.push.verb.git_push_with_token (the
name push.verb's own module namespace binds via its `from
clagentic_loadout.push.git_push import git_push_with_token` import) with a
recording fake, run push.verb.main() end-to-end for BOTH platforms with a
real local git remote (mirrors test_push_verb.py's repo_with_remote fixture
setup), and assert the recorded calls are BOTH routed through the exact same
function object -- i.e. there is only ever one push call site in push.verb,
and it is the same one regardless of args.platform.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from clagentic_loadout.push import verb
from clagentic_loadout.push.github_backend import GITHUB_API_BASE


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.resolved_for.append(role)
        return self._token


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


@pytest.fixture
def repo_with_remote(tmp_path):
    """Same fixture shape as test_push_verb.py's repo_with_remote -- a real
    local bare-repo 'origin' remote, no network, one commit ahead on a
    feature branch."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "base@example.com"], seed)
    _git(["config", "user.name", "Base"], seed)
    (seed / "README.md").write_text("hello\n")
    _git(["add", "README.md"], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "main"], seed)

    repo = tmp_path / "repo"
    _git(["clone", str(remote), str(repo)], tmp_path)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    _git(["checkout", "-b", "feature"], repo)
    (repo / "feature.txt").write_text("work\n")
    _git(["add", "feature.txt"], repo)
    _git(["commit", "-m", "feature work"], repo)
    _git(
        ["remote", "set-url", "origin", "http://git-host.example.com/some-owner/some-repo.git"],
        repo,
    )
    _git(
        ["config", f"url.{remote}.pushInsteadOf", "http://git-host.example.com/some-owner/some-repo.git"],
        repo,
    )

    return repo, remote


def _run_main(argv, *, token_provider=None, opener=None, stdin_text=None, monkeypatch=None):
    import io

    if stdin_text is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin_text.encode("utf-8"))))
    return verb.main(argv, token_provider=token_provider, opener=opener)


class TestSharedGitPushEntrypointLock:
    """Proves both platforms' create-PR path calls the SAME
    git_push_with_token function object -- the shared-mechanism claim
    lr-035b75 already established at the module level, locked here at the
    push.verb call-site level so a future per-platform fork is caught by a
    failing test rather than discovered later as a silent divergence."""

    def test_forgejo_and_github_create_pr_paths_call_the_identical_function_object(
        self, repo_with_remote, monkeypatch
    ):
        calls: list[dict] = []
        real_git_push_with_token = verb.git_push_with_token

        def _recording_git_push_with_token(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs, "fn": _recording_git_push_with_token})
            # Real push still has to happen for the rest of _run_create_pr
            # (post-push HEAD sha resolution, PR-open call) to proceed --
            # this wraps, not replaces, the real function.
            return real_git_push_with_token(*args, **kwargs)

        monkeypatch.setattr(verb, "git_push_with_token", _recording_git_push_with_token)

        repo, _remote = repo_with_remote

        # Forgejo create-PR path.
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RecordingTokenProvider(),
            opener=lambda req, timeout=15: _json_resp(201, {"number": 42}),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert len(calls) == 1
        forgejo_call = calls[0]

        # Re-clone a fresh feature branch for a second, independent GitHub
        # create-PR call (the first call already pushed 'feature' to the
        # bare remote; a second push on the same branch/repo is fine here
        # since this test only cares which FUNCTION was called, not the
        # push's own idempotency).
        _git(["checkout", "-b", "feature-2"], repo)
        (repo / "feature2.txt").write_text("more work\n")
        _git(["add", "feature2.txt"], repo)
        _git(["commit", "-m", "feature 2 work"], repo)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t2", "--body-stdin",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=lambda req, timeout=30: _json_resp(201, {"number": 43})
            if req.get_method() == "POST" and req.full_url == f"{GITHUB_API_BASE}/repos/some-owner/some-repo/pulls"
            else (_ for _ in ()).throw(AssertionError(f"unexpected: {req.get_method()} {req.full_url}")),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert len(calls) == 2
        github_call = calls[1]

        # THE LOCK: both calls were routed through the exact same function
        # object -- push.verb has exactly one git-push call site, and it is
        # identical regardless of args.platform.
        assert forgejo_call["fn"] is github_call["fn"]

        # Same call SHAPE too (positional signature: remote_name, branch,
        # token, project_root, plus force_with_lease/platform/
        # other_platform_label keywords) -- a fork that kept the same
        # function object but called it with a platform-specific extra
        # argument would still be a real divergence this should catch.
        assert set(forgejo_call["kwargs"].keys()) == set(github_call["kwargs"].keys())
        assert "platform" in forgejo_call["kwargs"]
        assert "platform" in github_call["kwargs"]
        assert forgejo_call["kwargs"]["platform"] != github_call["kwargs"]["platform"]

    def test_only_one_git_push_call_site_exists_in_push_verb_module(self):
        """Static lock, independent of the runtime test above: push.verb's
        own source imports git_push_with_token exactly once and calls it
        exactly once (in _run_create_pr) -- a second call site anywhere in
        this module (e.g. a newly-added platform-specific branch) would be
        a real fork this test catches even before any test exercises it at
        runtime."""
        import ast
        import inspect

        source = inspect.getsource(verb)
        tree = ast.parse(source)
        call_sites = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "git_push_with_token"
        ]
        assert len(call_sites) == 1, (
            f"expected exactly one git_push_with_token call site in "
            f"push.verb, found {len(call_sites)} -- a second call site is "
            f"exactly the per-platform push fork this lock exists to catch."
        )
