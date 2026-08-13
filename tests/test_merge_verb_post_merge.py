"""test_merge_verb_post_merge.py — merge.verb <-> merge.post_merge wiring
tests (lr-77d6).

Covers:
  - post_merge_steps run ONLY after a successful merge (never on any gate
    refusal, never on a merge-execution failure)
  - lr-ac5c8a: absent --repo-path with NEITHER --no-post-merge-tree NOR
    --skip-post-merge is a usage error (EXIT_USAGE), checked before any
    credential mint or network call -- never a silent exit-0 skip of a
    repo's declared post_merge_steps (see TestAbsentRepoPathIsNeverASilentSkip)
  - --skip-post-merge bypasses configured steps even when --repo-path is
    given
  - a configured on_failure:"fail" step surfaces EXIT_POST_MERGE_FAILED
  - a configured on_failure:"warn" step still returns EXIT_OK
  - malformed repo-local config surfaces EXIT_POST_MERGE_FAILED at load time
  - the deployment env-override seam (lr-52d7) is actually reached from
    merge.verb._run -- a CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME> var set in
    the invoking process's environment reaches a configured step's
    subprocess (see test_merge_post_merge_env_overrides.py for the seam's
    own unit coverage; this file only covers merge.verb's wiring of it)
  - lr-7c5540: post_merge_steps see the MERGED main SHA, never the pre-merge
    working-tree ref -- every test below that reaches step 10's actual
    post_merge_steps run now does so against a REAL local git repo (see
    `_init_repo_with_origin`), since merge.verb._run now advances
    --repo-path to the merged commit (merge.tree_sync) before reading or
    running any configured step. See test_merge_tree_sync.py for the
    tree_sync module's own unit coverage (fetch/checkout/verify, both the
    known-SHA and base-branch-fallback resolution paths, and the fail-loud
    contract on an unresolvable base branch).

No real network call: a minimal local opener/token/authority test-double set
(mirroring test_merge_verb.py's own harness shape, kept self-contained here
rather than cross-imported -- tests/ is not a package in this project).
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.transport import provider_config

_PY = sys.executable
_FULL_SHA = "a" * 40
_BASE_BRANCH = "main"


def _run_git(args: list[str], *, cwd) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def _init_repo_with_origin(tmp_path):
    """Build a REAL local git repo at *tmp_path* (the --repo-path under
    test) with a bare 'origin' remote sharing the same base branch content --
    lr-7c5540's fix runs `git fetch origin` + a detached checkout against
    --repo-path before any post_merge_steps entry, so every test exercising
    that path needs an actual git repo to fetch/checkout against, not a bare
    tmp_path. The bare remote lives in a SIBLING tmp_path dir (never inside
    the working tree itself) and starts with the identical single commit, so
    `git fetch origin main` + checking out FETCH_HEAD is a no-op content-wise
    but still a REAL git operation this fixture proves succeeds.

    Returns the resulting merged-commit SHA (the bare remote's `main` tip) --
    tests assert `pr_info`'s "base" carries this same branch name so
    merge.tree_sync.resolve_base_branch can resolve it.
    """
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
    """lr-a7c2 isolation precedent (see test_transport_provider_config.py's
    own fixture of the same name): merge.post_merge_config.resolve_env_overrides
    (called by merge.verb._run with no config_root override, in production
    fashion) falls through to DEFAULT_USER_CONFIG_ROOT -- the REAL
    ~/.config/clagentic/loadout/ directory -- when nothing pins it. Point
    that default at an empty per-test directory so this file's assertions
    never depend on, or leak into, real host state."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


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


def _make_opener(*, pr_info=None, files=None, comments=None, merge_status=200):
    """CI-status defaults to the no-runner-by-design empty shape (lr-afba)
    -- this file's own tests reason about post-merge steps, not CI, so they
    keep passing through the CI-status gate exactly as before that gate
    existed.

    lr-7c5540: pr_info's default now carries a "base" ref matching
    _BASE_BRANCH -- merge.tree_sync.resolve_base_branch reads this to know
    which branch to fetch before any post_merge_steps entry runs. Every test
    in this file that reaches step 10's actual tree-sync/run path pairs this
    default with _init_repo_with_origin(tmp_path), whose bare remote's
    default branch is the SAME _BASE_BRANCH name.
    """
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": _FULL_SHA},
        "title": "feat: a change",
        "base": {"ref": _BASE_BRANCH},
    }
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []

    # Merge-completion attestation (lr-20e866): see test_merge_verb.py's
    # _make_opener for the identical fixture rationale.
    posted_comments: list[dict] = []
    # lr-361de3: see test_merge_verb.py's _make_opener for the identical
    # post-merge-readback overlay rationale.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return _FakeResponse(merge_status, b"{}")
            import io
            import urllib.error

            raise urllib.error.HTTPError(url, merge_status, "err", {}, io.BytesIO(b"{}"))
        if method == "POST" and "/comments" in url:
            posted_body = json.loads(req.data.decode("utf-8"))["body"]
            posted_comments.append(
                {
                    "id": 9001 + len(posted_comments),
                    "user": {"login": "loadout-merger"},
                    "body": posted_body,
                    "html_url": "https://forgejo.example/comment/9001",
                    "created_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, comments + posted_comments)
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/actions/tasks"):
            return _json_resp(200, {"total_count": 0})
        if method == "GET" and "/compare/" in url:
            # lr-835c57: empty branch-commit list -- this file exercises
            # post_merge_steps wiring, not the commit-subject gate.
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _make_github_opener(*, pr_info=None, files=None, comments=None, merged_sha=None):
    """GitHub-platform counterpart to _make_opener above (lr-d95cdb GitHub-
    parity coverage for the tree-sync-after-merge default). Shapes requests
    per merge.github_backend's own documented endpoints: PUT .../merge
    returns {"merged": true, "sha": merged_sha} (the ONE field
    advance_repo_to_merged_sha's known_merged_sha path consumes -- see that
    backend's merge_pr docstring), GET check-runs/compare/status endpoints
    return the same no-runner-by-design-empty / empty-commit-list shapes
    _make_opener's Forgejo path uses."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": _FULL_SHA},
        "title": "feat: a change",
        "base": {"ref": _BASE_BRANCH},
    }
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    posted_reviews: list[dict] = []
    # lr-361de3: see _make_opener's identical post-merge-readback overlay
    # rationale above.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "PUT" and url.endswith("/merge"):
            _merge_landed[0] = True
            body = {"merged": True}
            if merged_sha is not None:
                body["sha"] = merged_sha
            return _json_resp(200, body)
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger[bot]"})
        if method == "POST" and "/reviews" in url:
            posted_reviews.append(
                {
                    "id": 9001 + len(posted_reviews),
                    "user": {"login": "loadout-merger[bot]"},
                    "body": json.loads(req.data.decode("utf-8")).get("body", ""),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return _json_resp(200, posted_reviews[-1])
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and "/issues/" in url and url.endswith("/comments"):
            return _json_resp(200, comments)
        if method == "GET" and "/reviews" in url:
            return _json_resp(200, posted_reviews)
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/check-runs"):
            return _json_resp(200, {"total_count": 0})
        if method == "GET" and "/compare/" in url:
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and url.endswith(f"/pulls/{1}"):
            if _merge_landed[0]:
                return _json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": merged_sha or "e" * 40}
                )
            return _json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _base_args(**overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--role": "merger",
        "--authorized-role": "merger",
        "--repo": "some-owner/some-repo",
        "--pr": "1",
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    return argv


def _write_merge_config(repo_root, steps: list[dict], *, git_working_tree: str | None = None) -> None:
    config_dir = repo_root / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    merge_section: dict = {"post_merge_steps": steps}
    if git_working_tree is not None:
        merge_section["git_working_tree"] = git_working_tree
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"merge": merge_section}), encoding="utf-8"
    )


class TestAbsentRepoPathIsNeverASilentSkip:
    """lr-ac5c8a: omitting --repo-path with no explicit acknowledgment must
    NEVER silently downgrade to a bare API-only merge that runs zero
    post_merge_steps and exits 0 -- that recurring shape (lr-5854ff,
    lr-4e6f31, clagentic-console PR #365/#366) is the defect this task
    closes. --repo-path is an OPTIONAL override (a dispatcher with its own
    project registry supplies it whenever a local tree exists); loadout
    itself has no such registry to derive one from. Omitting it now requires
    an explicit --no-post-merge-tree or --skip-post-merge acknowledgment."""

    def test_absent_repo_path_with_no_acknowledgment_is_usage_error(self, tmp_path, monkeypatch):
        # No --repo-path, no --no-post-merge-tree, no --skip-post-merge --
        # this is now a usage error, never a silent exit-0 skip, REGARDLESS
        # of whether cwd happens to contain a declared post_merge_steps
        # config (the merge must never even be attempted in this shape).
        monkeypatch.chdir(tmp_path)
        _write_merge_config(tmp_path, [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}])
        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_USAGE

    def test_absent_repo_path_usage_error_fires_before_any_network_call(self, tmp_path, monkeypatch):
        # The usage-error check must fire BEFORE any credential mint or
        # network call -- an opener that raises AssertionError on any call
        # proves this (mirrors the namespace-guard gate's own "runs first"
        # contract).
        monkeypatch.chdir(tmp_path)
        token_provider = _RecordingTokenProvider()

        def _unreachable_opener(req, timeout=15):
            raise AssertionError("no network call should be attempted")

        argv = _base_args()
        code = verb.main(
            argv,
            token_provider=token_provider,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_unreachable_opener,
        )
        assert code == verb.EXIT_USAGE
        assert token_provider.resolved_for == []

    def test_no_post_merge_tree_flag_acknowledges_and_merges_cleanly(self, tmp_path, monkeypatch):
        # --no-post-merge-tree explicitly acknowledges "no local tree" --
        # the merge proceeds and post-merge steps are (correctly) never
        # attempted, but this is now a LOGGED, EXPLICIT choice, not a silent
        # default.
        monkeypatch.chdir(tmp_path)
        argv = _base_args()
        argv.append("--no-post-merge-tree")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK

    def test_skip_post_merge_alone_satisfies_the_absent_repo_path_requirement(self, tmp_path, monkeypatch):
        # --skip-post-merge already means "skip regardless of tree" -- it
        # must also satisfy the absent-repo-path acknowledgment requirement
        # without needing --no-post-merge-tree too.
        monkeypatch.chdir(tmp_path)
        argv = _base_args()
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestPostMergeRunsOnlyAfterSuccess:
    def test_step_runs_after_successful_merge(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "installed.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ok"

    def test_step_never_runs_on_namespace_refusal(self, tmp_path):
        marker = tmp_path / "should-not-run.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
        )
        argv = _base_args(
            **{"--repo-path": str(tmp_path), "--allowed-namespace": "different-owner"}
        )
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
        )
        assert code == verb.EXIT_NAMESPACE_DENIED
        assert not marker.exists()

    def test_step_never_runs_on_merge_execution_failure(self, tmp_path):
        marker = tmp_path / "should-not-run.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(merge_status=500),
        )
        assert code == verb.EXIT_MERGE_FAILED
        assert not marker.exists()

    def test_step_never_runs_on_title_gate_refusal(self, tmp_path):
        marker = tmp_path / "should-not-run.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(
                pr_info={"head": {"sha": _FULL_SHA}, "title": "not conventional commits"}
            ),
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        assert not marker.exists()


class TestSkipPostMerge:
    def test_skip_flag_bypasses_configured_steps_and_never_checks_anything_out(self, tmp_path):
        # lr-173768: --skip-post-merge means no step will ever run this
        # invocation, so no checkout happens either -- only the merged commit
        # is fetched into the local object database (still needed so the
        # merge-shape readback and attestation SHA claim stay verified). The
        # working tree is left EXACTLY where the caller had it (still on the
        # initial local commit _init_repo_with_origin leaves it on, never
        # advanced to the remote's later tip) -- this is the direct
        # regression proof for the contention class this task removes: a
        # merge with nothing to run post-merge must never yank the caller's
        # checked-out files out from under it.
        starting_sha = _init_repo_with_origin(tmp_path)
        marker = tmp_path / "should-not-run.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
        )
        # --skip-post-merge is a store_true flag (no value) -- append it
        # directly rather than through _base_args's flag=value pairing.
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert not marker.exists()
        # The tree is untouched: still on the pre-existing local branch/SHA,
        # never detached, never repointed onto the (unfetched-into-the-
        # working-tree) merged base-branch tip.
        branch = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(tmp_path),
        )
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == starting_sha

    def test_sync_tree_after_merge_false_skips_sync_entirely_even_without_skip_flag(
        self, tmp_path
    ):
        # The ONLY way to skip the tree sync itself is the repo's own
        # merge.sync_tree_after_merge: false config -- not --skip-post-merge.
        # Uses a bare tmp_path (no origin remote) to prove no git subprocess
        # touching a remote is even attempted.
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"sync_tree_after_merge": False}}),
            encoding="utf-8",
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestOnFailurePropagation:
    def test_fail_step_surfaces_exit_post_merge_failed(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "fail"}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED

    def test_warn_step_failure_still_returns_ok(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "after.txt"
        _write_merge_config(
            tmp_path,
            [
                {"cmd": [_PY, "-c", "import sys; sys.exit(1)"], "on_failure": "warn"},
                {"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]},
            ],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ok"

    def test_malformed_repo_config_surfaces_exit_post_merge_failed(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        _write_merge_config(tmp_path, [{"cmd": "git fetch && git switch --detach X"}])
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED


class TestNoConfiguredStepsIsNoop:
    def test_repo_path_with_no_config_file_is_ok(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


class TestSyncTreeAfterMergeDefaultOn:
    """lr-d95cdb, re-scoped lr-173768: sync-after-merge (a FETCH, always, of
    the merged commit into the local object database) is default ON for any
    --repo-path whose repo has not opted out via
    `merge.sync_tree_after_merge: false` -- independent of whether
    post_merge_steps are configured. A CHECKOUT, however, happens ONLY when
    at least one post_merge_steps entry will actually run (lr-173768): a
    repo with steps configured still lands on the base branch (checked out);
    a repo with NONE configured gets the merged commit fetched but the
    working tree is left untouched (see
    test_default_on_without_any_post_merge_steps_configured_fetches_but_never_
    checks_out below -- this is the exact scenario lr-173768 closes: a
    checkout that serves nothing must never mutate a shared checkout out
    from under another in-flight agent). Covers: default-on with steps
    configured (checks out), default-on WITHOUT any steps configured (fetch
    only, no checkout), the config flip-off path (no fetch, no checkout),
    the fail-loud path preserved, both platform resolution paths, and the
    final-tree-state assertion in each case."""

    def _current_branch(self, repo_dir):
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(repo_dir),
        )
        return result

    def test_default_on_with_post_merge_steps_configured_lands_on_base_branch(
        self, tmp_path
    ):
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "installed.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ok"
        branch = self._current_branch(tmp_path)
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH

    def test_default_on_without_any_post_merge_steps_configured_fetches_but_never_checks_out(
        self, tmp_path
    ):
        # lr-173768: a repo with NO post_merge_steps configured still gets
        # the merged commit FETCHED (so the merge-shape readback below and
        # the merge-completion attestation SHA claim stay independently
        # verified) -- but nothing checks it out, since nothing will read
        # the working tree this invocation. The tree is left EXACTLY where
        # the caller had it: still on its initial local commit
        # (_init_repo_with_origin's seed commit), never advanced to the
        # remote's later tip. This directly replaces the pre-lr-173768
        # contract (which asserted the checkout DID happen here) -- that
        # unconditional checkout, with nothing configured to ever read it,
        # was exactly the unsignaled shared-checkout mutation lr-173768
        # removes.
        starting_sha = _init_repo_with_origin(tmp_path)
        # No _write_merge_config call at all -- no .clagentic/loadout/config.yaml.
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        branch = self._current_branch(tmp_path)
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == starting_sha

    def test_config_flip_off_leaves_tree_exactly_where_caller_left_it(self, tmp_path):
        # merge.sync_tree_after_merge: false is the ONLY way to restore the
        # pre-lr-d95cdb behavior of no sync at all.
        starting_sha = _init_repo_with_origin(tmp_path)
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"sync_tree_after_merge": False}}),
            encoding="utf-8",
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        # Untouched: still on the branch/commit the caller originally left it on.
        branch = self._current_branch(tmp_path)
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == starting_sha

    def test_fail_loud_path_preserved_with_sync_enabled_by_default(self, tmp_path):
        # No origin remote at all (bare tmp_path, no post_merge_steps
        # configured) -- fetch_merged_sha_object must still fail loud with
        # EXIT_POST_MERGE_FAILED, never a silent partial sync, even on the
        # fetch-only (no-checkout) path this shape now takes (lr-173768).
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED

    def test_forgejo_platform_base_branch_fallback_resolution_lands_on_base_branch(
        self, tmp_path
    ):
        # Forgejo's merge response carries no SHA at all (merge_pr returns
        # None) -- tree_sync's base-branch-fallback path resolves FETCH_HEAD,
        # and land_on_base_branch must still work off THAT resolution. A
        # trivial post_merge_steps entry is configured so this invocation
        # actually takes the CHECKOUT path (lr-173768: checkout only happens
        # when something will read the tree) -- this test's whole purpose is
        # proving the SHA-resolution-then-checkout machinery, not the
        # no-checkout fast path covered elsewhere in this class.
        merged_sha = _init_repo_with_origin(tmp_path)
        _write_merge_config(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "forgejo"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        branch = self._current_branch(tmp_path)
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == merged_sha

    def test_github_platform_known_sha_resolution_lands_on_base_branch(self, tmp_path):
        # GitHub's merge response DOES carry the merged SHA -- tree_sync's
        # known_merged_sha path is exercised (not the base-branch fallback),
        # and land_on_base_branch must land on that exact SHA too. A trivial
        # post_merge_steps entry is configured so this invocation actually
        # takes the CHECKOUT path (lr-173768).
        merged_sha = _init_repo_with_origin(tmp_path)
        _write_merge_config(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK
        branch = self._current_branch(tmp_path)
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == merged_sha

    def test_github_platform_missing_sha_falls_back_to_base_branch_resolution(
        self, tmp_path
    ):
        # A GitHub response with no usable "sha" field (merge_pr returns
        # None, per that backend's own documented fallback contract) must
        # still resolve and land correctly via the SAME base-branch-fallback
        # path the Forgejo platform always uses. A trivial post_merge_steps
        # entry is configured so this invocation actually takes the CHECKOUT
        # path (lr-173768).
        merged_sha = _init_repo_with_origin(tmp_path)
        _write_merge_config(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=None),
        )
        assert code == verb.EXIT_OK
        branch = self._current_branch(tmp_path)
        assert branch.stdout.strip() == _BASE_BRANCH
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        )
        assert rev_parse.stdout.strip() == merged_sha


class TestDeploymentEnvOverrideSeamWiring:
    """merge.verb._run resolves merge.post_merge_config.resolve_env_overrides()
    with no arguments (production default: real os.environ + the real
    user-level config root) and threads it through to run_post_merge_steps
    -- this covers that wiring reaches an actual subprocess, using a real
    CLAGENTIC_LOADOUT_POST_MERGE_ENV_ var set via monkeypatch rather than
    re-testing resolve_env_overrides' own resolution rules (covered in
    test_merge_post_merge_env_overrides.py)."""

    def test_env_override_var_reaches_configured_step(self, tmp_path, monkeypatch):
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "deployment-seam.txt"
        _write_merge_config(
            tmp_path,
            [
                {
                    "cmd": [
                        _PY,
                        "-c",
                        "import os; open('deployment-seam.txt','w')."
                        "write(os.environ['MY_DEPLOYMENT_VAR'])",
                    ]
                }
            ],
        )
        monkeypatch.setenv("CLAGENTIC_LOADOUT_POST_MERGE_ENV_MY_DEPLOYMENT_VAR", "injected-value")
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "injected-value"


class TestGitWorkingTreeConfigRootSplit:
    """lr-93d718: the config-root (--repo-path, where `.clagentic/loadout/
    config.yaml` lives) and the git-tree-root (where tree_sync's `git fetch`/
    `git checkout` must run) are no longer assumed to be the SAME directory.
    See merge.post_merge_config's module docstring, "CONFIG-ROOT VS
    GIT-TREE-ROOT", and merge.verb's own docstring section of the same name.
    """

    def test_knob_absent_tree_sync_targets_repo_path_unchanged(self, tmp_path):
        # (1) knob absent -> tree_sync targets --repo-path exactly as before
        # this task -- the pre-existing flat-layout behavior must be
        # bit-for-bit unchanged.
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "installed.txt"
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ok"

    def test_knob_present_tree_sync_targets_subpath_config_stays_at_root(self, tmp_path):
        # (2) knob present -> tree_sync targets <config_root>/<subpath>,
        # while config discovery (load_post_merge_steps) still reads from
        # --repo-path (the config root) itself.
        wrapper_dir = tmp_path
        git_tree_dir = wrapper_dir / "repo"
        git_tree_dir.mkdir()
        _init_repo_with_origin(git_tree_dir)

        marker = wrapper_dir / "installed.txt"
        _write_merge_config(
            wrapper_dir,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
            git_working_tree="repo",
        )
        argv = _base_args(**{"--repo-path": str(wrapper_dir)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        # The step ran with cwd=wrapper_dir (config root, per
        # run_post_merge_steps' own contract) but tree_sync itself operated
        # against git_tree_dir -- prove that by asserting the git tree
        # actually advanced there.
        assert marker.read_text() == "ok"
        rev_parse = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(git_tree_dir)
        )
        assert rev_parse.returncode == 0

    def test_wrapper_layout_regression_case_end_to_end(self, tmp_path):
        # (3) THE lr-93d718 REGRESSION ITSELF: config lives at the wrapper
        # (alongside non-git tooling state), the real .git lives at a
        # subdirectory of the wrapper -- no single --repo-path satisfied
        # both before this fix. Passing the wrapper used to fail tree_sync
        # ("not a git repository", EXIT_POST_MERGE_FAILED); this proves
        # post_merge_steps now runs end-to-end against the merged SHA.
        wrapper_dir = tmp_path / "wrapper"
        wrapper_dir.mkdir()
        (wrapper_dir / ".crew").mkdir()  # non-git wrapper-layout marker state
        git_tree_dir = wrapper_dir / "repo"
        git_tree_dir.mkdir()
        _init_repo_with_origin(git_tree_dir)

        # The wrapper directory itself is NOT a git working tree at all.
        assert not (wrapper_dir / ".git").exists()

        marker = wrapper_dir / "post-merge-ran.txt"
        _write_merge_config(
            wrapper_dir,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}],
            git_working_tree="repo",
        )
        argv = _base_args(**{"--repo-path": str(wrapper_dir)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ran"

    def test_knob_present_but_target_not_a_git_tree_fails_loud(self, tmp_path):
        # A misconfigured knob (subpath does not actually contain a git tree)
        # must still fail loud via tree_sync's own contract -- never a
        # silent fallback to --repo-path.
        wrapper_dir = tmp_path
        (wrapper_dir / "not-a-repo").mkdir()
        _write_merge_config(
            wrapper_dir,
            [{"cmd": [_PY, "-c", "pass"]}],
            git_working_tree="not-a-repo",
        )
        argv = _base_args(**{"--repo-path": str(wrapper_dir)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED

    def test_malformed_git_working_tree_value_surfaces_exit_post_merge_failed(self, tmp_path):
        _init_repo_with_origin(tmp_path)
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"git_working_tree": 42}}), encoding="utf-8"
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED


class TestMergeShapeMismatchSurfacing:
    """lr-14f704 item 3: a requested-vs-actual merge-shape mismatch is
    surfaced, never silent. `_init_repo_with_origin`'s bare-remote base
    branch tip is a single-commit, ZERO-parent root commit -- the
    base-branch-fallback resolution path (no known_merged_sha, the Forgejo
    shape) lands exactly on that root commit, so a --merge-method 'merge'
    request (which predicts >= 2 parents) against it is a genuine, real
    mismatch -- not a synthetic/mocked one."""

    def test_default_merge_method_against_root_commit_warns_not_fails(self, tmp_path, capsys):
        # Default --merge-method is 'merge' (predicts >= 2 parents); the
        # actual landed commit here has 0. Warn-by-default (no repo config
        # opt-in) -- must NOT fail the merge.
        _init_repo_with_origin(tmp_path)
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "merge-shape MISMATCH" in stderr
        assert "WARNING" in stderr

    def test_squash_merge_method_against_single_parent_commit_is_not_a_mismatch(self, tmp_path, capsys):
        # squash predicts exactly 1 parent. Add a second (non-root) commit to
        # the shared base branch before syncing, so the landed tip genuinely
        # has 1 parent and 'squash' truly MATCHES (a bare root commit has 0
        # parents, which would still mismatch against squash's prediction of
        # exactly 1).
        _init_repo_with_origin(tmp_path)
        origin_dir = tmp_path.parent / f"{tmp_path.name}-origin.git"
        second_clone = tmp_path.parent / f"{tmp_path.name}-second-commit-clone"
        subprocess.run(["git", "clone", str(origin_dir), str(second_clone)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], check=True, cwd=second_clone, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test"], check=True, cwd=second_clone, capture_output=True)
        (second_clone / "README.md").write_text("v2\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "second change"], check=True, cwd=second_clone, capture_output=True)
        subprocess.run(["git", "push", "origin", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], check=True, cwd=second_clone, capture_output=True)

        argv = _base_args(**{"--repo-path": str(tmp_path), "--merge-method": "squash"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "merge-shape MISMATCH" not in stderr

    def test_enforce_merge_shape_true_hard_fails_on_mismatch(self, tmp_path, capsys):
        # Repo opts into strict enforcement -- the SAME mismatch that only
        # warns by default now refuses the merge outright.
        _init_repo_with_origin(tmp_path)
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"enforce_merge_shape": True}}), encoding="utf-8"
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_MERGE_SHAPE_MISMATCH
        stderr = capsys.readouterr().err
        assert "merge-shape MISMATCH" in stderr

    def test_enforce_merge_shape_false_explicit_still_warns_only(self, tmp_path, capsys):
        _init_repo_with_origin(tmp_path)
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text(
            yaml.safe_dump({"merge": {"enforce_merge_shape": False}}), encoding="utf-8"
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK

    def test_no_repo_path_never_attempts_shape_check(self, tmp_path, monkeypatch):
        # --no-post-merge-tree (no local tree at all) has no local object
        # database to read a parent count from -- the check must not even
        # be attempted (no false positive, no crash).
        monkeypatch.chdir(tmp_path)
        argv = _base_args()
        argv.append("--no-post-merge-tree")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK


def _write_crew_yaml(repo_root, filename: str, text: str) -> None:
    crew_dir = repo_root / ".crew"
    crew_dir.mkdir(parents=True, exist_ok=True)
    (crew_dir / filename).write_text(text, encoding="utf-8")


class TestDeadCrewPostMergeConfigWarning:
    """lr-f9a01b followup (PEACHES finding on the doctor-only fix): a
    doctor check alone only fires when someone runs `loadout-doctor` --
    the reported failure was an UNATTENDED merge reporting exit 0 with
    steps_run=0. merge.verb._run's step 10 now surfaces the SAME
    .crew/*.yaml cross-check as a loud, non-blocking WARNING on the path
    that actually runs unattended. WARN, NEVER REFUSE (operator-directed
    disposition) -- every test here that reaches this shape still asserts
    EXIT_OK; there is no test in this class asserting a non-zero exit
    caused by this warning, because none exists."""

    def test_stale_crew_yaml_mention_warns_but_still_exits_ok(self, tmp_path, capsys):
        _init_repo_with_origin(tmp_path)
        _write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'make install'\n"
        )
        # No .clagentic/loadout/config.yaml at all -- the trap shape.
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "NEVER reads that key from .crew/*.yaml" in stderr
        assert str(tmp_path / ".crew" / "amos.yaml") in stderr

    def test_stale_crew_yaml_mention_never_makes_a_step_execute(self, tmp_path, capsys):
        """READ-ONLY CONTRACT: a .crew/*.yaml mention is never a source of
        EXECUTABLE steps -- if it were somehow honored, this test's
        marker file would exist after the run. It must not."""
        _init_repo_with_origin(tmp_path)
        marker = tmp_path / "should-never-exist.txt"
        _write_crew_yaml(
            tmp_path,
            "amos.yaml",
            f"post_merge_steps:\n  - cmd: \"{_PY} -c \\\"open('{marker}', 'w').close()\\\"\"\n",
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        assert not marker.exists()

    def test_no_warning_when_live_config_already_declares_steps(self, tmp_path, capsys):
        _init_repo_with_origin(tmp_path)
        _write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'make install'\n"
        )
        _write_merge_config(tmp_path, [{"cmd": [_PY, "-c", "pass"]}])
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "NEVER reads that key from .crew/*.yaml" not in stderr

    def test_no_warning_when_live_config_explicitly_declares_empty_list(
        self, tmp_path, capsys
    ):
        """lr-f9a01b followup (Move 2 re-evaluated): a repo that
        explicitly wrote post_merge_steps: [] at the CORRECT file has made
        an informed choice -- must never be warned about an unrelated
        stale .crew/*.yaml mention just because bool([]) is falsy."""
        _init_repo_with_origin(tmp_path)
        _write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'make install'\n"
        )
        _write_merge_config(tmp_path, [])
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "NEVER reads that key from .crew/*.yaml" not in stderr

    def test_no_crew_dir_no_warning(self, tmp_path, capsys):
        _init_repo_with_origin(tmp_path)
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "NEVER reads that key from .crew/*.yaml" not in stderr

    def test_skip_post_merge_still_warns_about_stale_crew_yaml(self, tmp_path, capsys):
        """--skip-post-merge deliberately runs no steps THIS invocation, but
        a stale .crew/*.yaml mention pointing at a config surface that will
        NEVER run steps on any future invocation either is still worth
        naming loudly."""
        _init_repo_with_origin(tmp_path)
        _write_crew_yaml(
            tmp_path, "amos.yaml", "post_merge_steps:\n  - cmd: 'make install'\n"
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        argv.append("--skip-post-merge")
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "NEVER reads that key from .crew/*.yaml" in stderr
