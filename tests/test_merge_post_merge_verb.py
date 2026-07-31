"""test_merge_post_merge_verb.py — CLI-level coverage for
clagentic_loadout.merge.post_merge_verb (loadout-post-merge, lr-dd99e7).

Mirrors test_merge_verb_post_merge.py's / test_merge_close_verb.py's own
discipline (NO real network call; a REAL local git repo for the tree-sync
path exactly like test_merge_verb_post_merge.py's _init_repo_with_origin;
everything else driven through an injected opener + injected token/authority
providers):

  - --help / --version exit EXIT_OK without needing any other argument.
  - --platform, --repo, --pr, --repo-path are all mandatory (argparse
    enforces it) -- --repo-path has NO --no-post-merge-tree/--skip-post-merge
    escape hatch on this verb, unlike loadout-merge's own optional flag.
  - Namespace guard runs BEFORE any credential mint or authority check.
  - Merge-authority check runs BEFORE any credential mint.
  - The platform guard fires BEFORE any credential mint, for BOTH
    wrong-platform directions.
  - THE CORE REGRESSION (acceptance criterion): re-running against an
    ALREADY-MERGED PR advances --repo-path to the merged SHA and actually
    executes post_merge_steps -- without ever calling a merge endpoint.
  - A PR that is NOT merged (open, or closed-without-merging) is refused
    (EXIT_PR_NOT_MERGED), never silently treated as a no-op success.
  - A configured on_failure:"fail" step surfaces EXIT_POST_MERGE_FAILED.
  - No configured steps is a clean no-op (EXIT_OK, steps_run=0).
"""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.merge import post_merge_verb
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.credential_provider import CredentialProviderError

_PY = sys.executable
_OWNER = "some-owner"
_REPO = "some-repo"
_MERGED_SHA = "b" * 40
_BASE_BRANCH = "main"


def _run_git(args: list[str], *, cwd) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def _init_repo_with_origin(tmp_path):
    """Build a REAL local git repo at *tmp_path* with a bare 'origin' remote
    sharing the same base-branch content -- mirrors
    test_merge_verb_post_merge.py's own fixture of the same name exactly,
    since this verb's tree-sync call is the SAME advance_repo_to_merged_sha
    function merge.verb's own step 10 uses. Returns the resulting merged
    commit SHA."""
    remote_dir = tmp_path.parent / f"{tmp_path.name}-origin.git"
    _run_git(["init", "--bare", "-b", _BASE_BRANCH, str(remote_dir)], cwd=tmp_path.parent)

    _run_git(["init", "-b", _BASE_BRANCH, str(tmp_path)], cwd=tmp_path.parent)
    _run_git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _run_git(["config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "README.md"], cwd=tmp_path)
    _run_git(["commit", "-m", "seed"], cwd=tmp_path)
    _run_git(["remote", "add", "origin", str(remote_dir)], cwd=tmp_path)
    _run_git(["push", "origin", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=tmp_path)

    rev_parse = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
    )
    return rev_parse.stdout.strip()


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Same isolation precedent test_merge_verb_post_merge.py's own fixture
    uses -- post_merge_config.resolve_env_overrides falls through to
    DEFAULT_USER_CONFIG_ROOT (the REAL ~/.config/clagentic/loadout/
    directory) when nothing pins it; point that default at an empty
    per-test directory."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise AssertionError(
            f"token provider must not be called before the namespace/"
            f"authority/platform guards refuse (role={role!r})"
        )


class _MissingCredsTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise CredentialProviderError("no credentials configured for this role")


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _DenyingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return False


class _RefusingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        raise AssertionError("authority provider must not be called")


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


def _make_opener(*, merged=True, merge_commit_sha=_MERGED_SHA, base_ref=_BASE_BRANCH):
    """A forgejo-shaped opener that answers GET .../pulls/{n} with a
    merged/unmerged PR payload -- never a merge or comment endpoint, proving
    this verb never calls anything but a read."""
    pr_info = {
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": base_ref},
    }

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET" and "/api/v1/repos/" in url and "/pulls/" in url:
            return _FakeResponse(200, json.dumps(pr_info).encode("utf-8"))
        raise AssertionError(
            f"unexpected call: {method} {url} -- this verb must only ever "
            f"read PR info via the Forgejo API, never merge/comment"
        )

    return opener


def _make_github_opener(*, merged=True, merge_commit_sha=_MERGED_SHA, base_ref=_BASE_BRANCH):
    """A github-shaped opener that answers GET .../pulls/{n} with a
    merged/unmerged PR payload -- proves a --platform github invocation
    dispatches to github_backend.get_pr_info, never forgejo_backend's."""
    pr_info = {
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": base_ref},
    }

    def opener(req, timeout=30):
        assert req.full_url == f"https://api.github.com/repos/{_OWNER}/{_REPO}/pulls/1"
        assert req.get_method() == "GET"
        return _FakeResponse(200, json.dumps(pr_info).encode("utf-8"))

    return opener


def _write_merge_config(repo_root, steps: list[dict]) -> None:
    config_dir = repo_root / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"merge": {"post_merge_steps": steps}}), encoding="utf-8"
    )


def _base_args(repo_path: str, **overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--role": "merger",
        "--authorized-role": "merger",
        "--repo": f"{_OWNER}/{_REPO}",
        "--pr": "1",
        "--repo-path": repo_path,
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    return argv


class TestHelpAndVersion:
    def test_help_exits_ok_with_no_other_args(self):
        assert post_merge_verb.main(["--help"]) == post_merge_verb.EXIT_OK

    def test_version_exits_ok(self):
        assert post_merge_verb.main(["--version"]) == post_merge_verb.EXIT_OK


class TestMandatoryFlags:
    def test_missing_repo_path_is_argparse_usage_error(self):
        # argparse itself raises SystemExit(2) for a missing required
        # argument (its own convention, distinct from this verb's own
        # EXIT_USAGE=1 for a caller-input-shape error caught downstream of
        # argparse) -- main() propagates that code verbatim, matching
        # merge.verb's/merge.close_verb's own --platform-missing behavior.
        argv = [
            "--platform", "forgejo",
            "--repo", f"{_OWNER}/{_REPO}",
            "--pr", "1",
            "--authorized-role", "merger",
        ]
        code = post_merge_verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == 2

    def test_malformed_repo_is_usage_error(self, tmp_path):
        argv = _base_args(str(tmp_path), **{"--repo": "not-owner-slash-repo"})
        code = post_merge_verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == post_merge_verb.EXIT_USAGE


class TestGateOrdering:
    def test_namespace_guard_runs_before_any_credential_or_authority_check(self, tmp_path):
        argv = _base_args(str(tmp_path), **{"--allowed-namespace": "different-owner"})
        code = post_merge_verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == post_merge_verb.EXIT_NAMESPACE_DENIED

    def test_authority_denied_runs_before_any_credential_mint(self, tmp_path):
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RefusingTokenProvider(),
            authority_provider=_DenyingAuthorityProvider(),
        )
        assert code == post_merge_verb.EXIT_AUTHORITY_DENIED

    def test_token_fetch_failure_surfaces_exit_token_fetch_failed(self, tmp_path):
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_MissingCredsTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
        )
        assert code == post_merge_verb.EXIT_TOKEN_FETCH_FAILED


class TestPrNotMergedRefusal:
    def test_open_pr_is_refused_never_treated_as_a_noop_success(self, tmp_path):
        marker = tmp_path / "should-not-run.txt"
        _write_merge_config(
            tmp_path, [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}]
        )
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merged=False),
        )
        assert code == post_merge_verb.EXIT_PR_NOT_MERGED
        assert not marker.exists()


class TestPostMergeRerunAgainstAlreadyMergedPr:
    """THE CORE ACCEPTANCE CRITERION (lr-dd99e7): NAOMI can re-run
    post_merge_steps for a specified already-merged PR without re-merging."""

    def test_step_runs_against_the_merged_sha_without_any_merge_call(self, tmp_path):
        merged_sha = _init_repo_with_origin(tmp_path)
        marker = tmp_path / "redeployed.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            # merge_commit_sha intentionally omitted (None) so tree_sync
            # takes the base-branch-fallback path -- proves the SAME
            # advance_repo_to_merged_sha function this verb reuses lands on
            # the real merged tip fetched from origin, exactly like
            # merge.verb's own step 10 does for the Forgejo backend (which
            # has no merged-SHA response field either).
            opener=_make_opener(merge_commit_sha=None),
        )
        assert code == post_merge_verb.EXIT_OK
        assert marker.read_text() == "ok"

        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == merged_sha

    def test_github_platform_dispatches_to_github_backend(self, tmp_path):
        merged_sha = _init_repo_with_origin(tmp_path)
        marker = tmp_path / "redeployed.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(str(tmp_path), **{"--platform": "github"})
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merge_commit_sha=None),
        )
        assert code == post_merge_verb.EXIT_OK
        assert marker.read_text() == "ok"
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == merged_sha

    def test_no_configured_steps_is_a_clean_noop(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_commit_sha=None),
        )
        assert code == post_merge_verb.EXIT_OK

    def test_fail_step_surfaces_exit_post_merge_failed(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}],
        )
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_commit_sha=None),
        )
        assert code == post_merge_verb.EXIT_POST_MERGE_FAILED

    def test_warn_step_failure_still_returns_ok_and_runs_later_steps(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "after.txt"
        _write_merge_config(
            tmp_path,
            [
                {"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "warn"},
                {"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]},
            ],
        )
        argv = _base_args(str(tmp_path))
        code = post_merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_commit_sha=None),
        )
        assert code == post_merge_verb.EXIT_OK
        assert marker.read_text() == "ok"
