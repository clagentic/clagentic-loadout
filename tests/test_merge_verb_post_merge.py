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
from tests._gate_repo import seed_base_commit

_PY = sys.executable
_FULL_SHA = "a" * 40
_BASE_BRANCH = "main"


def _run_git(args: list[str], *, cwd) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def _assert_landed_on_base_branch(repo_dir, expected_sha: str) -> None:
    """lr-cd3644 fold-in #4 (PEACHES finding on PR #30's re-review, comment
    5875590535): `git symbolic-ref --short HEAD` alone proves the tree is ON
    a branch, but not that the branch actually points at the merged commit.
    Asserts THREE independent facts: (1) the tree is on _BASE_BRANCH, not
    detached; (2) the working HEAD (`git rev-parse HEAD`) resolves to
    *expected_sha*; (3) the branch REF ITSELF
    (`git rev-parse refs/heads/<base>`), not merely the symbolic HEAD
    pointer, also resolves to *expected_sha* -- proving `land_on_base_branch`
    repointed the correct ref at the correct commit, not merely that the
    tree ended up on SOME branch named _BASE_BRANCH."""
    branch = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    assert branch.returncode == 0
    assert branch.stdout.strip() == _BASE_BRANCH
    rev_parse_head = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(repo_dir)
    )
    assert rev_parse_head.stdout.strip() == expected_sha
    rev_parse_ref = subprocess.run(
        ["git", "rev-parse", f"refs/heads/{_BASE_BRANCH}"],
        capture_output=True, text=True, cwd=str(repo_dir),
    )
    assert rev_parse_ref.returncode == 0
    assert rev_parse_ref.stdout.strip() == expected_sha


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
    return _record_base_sha(rev_parse.stdout.strip())


#: The seed commit of the repo the current test built. The merge gate reads its
#: declaration at the PR's base commit and fails closed without one, so the
#: default PR payloads below carry this as `base.sha` whenever a repo was built.
_SEEDED_BASE: dict[str, str] = {"sha": ""}


def _record_base_sha(sha: str) -> str:
    _SEEDED_BASE["sha"] = sha
    return sha


def _default_base() -> dict:
    return {"ref": _BASE_BRANCH, **({"sha": _SEEDED_BASE["sha"]} if _SEEDED_BASE["sha"] else {})}


@pytest.fixture(autouse=True)
def _reset_seeded_base():
    _SEEDED_BASE["sha"] = ""
    yield
    _SEEDED_BASE["sha"] = ""


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
        "base": _default_base(),
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
        "base": _default_base(),
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
        _record_base_sha(seed_base_commit(tmp_path))
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
        _record_base_sha(seed_base_commit(tmp_path))
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

    def test_fail_step_still_lands_tree_on_base_branch(self, tmp_path):
        """lr-cd3644 fold-in #3 (PR #30 re-review finding C): a
        post_merge_steps failure (on_failure: fail) still returns
        EXIT_POST_MERGE_FAILED (the ORIGINAL failure, unchanged), but the
        working tree that advance_repo_to_merged_sha already checked out
        BEFORE the failing step ran must not be left permanently detached --
        land_on_base_branch must still run on this exit path. Before the
        fix, an _fail() raised from run_post_merge_steps unwound straight
        past the (then-unconditional) `if tree_checked_out:` land call,
        leaving --repo-path DETACHED at the merged SHA with local main
        unmoved -- exactly the observed incident's second defect, but for
        the exception path rather than the drift-to-zero-steps path
        fold-in #4 already covers.

        lr-cd3644 fold-in #4 (PEACHES finding on PR #30's re-review, comment
        5875590535): `git symbolic-ref --short HEAD` alone proves the tree
        is ON a branch, but not that the branch actually points at the
        merged commit -- asserting `git rev-parse HEAD` AND
        `git rev-parse refs/heads/<base>` (the ref itself, not merely the
        symbolic HEAD pointer) both equal the merged sha is what actually
        proves `land_on_base_branch` repointed the correct ref at the
        correct commit, not merely that the tree ended up on SOME branch
        named base_branch."""
        merged_sha = _init_repo_with_origin(tmp_path)
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
        _assert_landed_on_base_branch(tmp_path, merged_sha)


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
        merged_sha = _init_repo_with_origin(tmp_path)
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
        # lr-cd3644 fold-in #4: assert the working HEAD AND the branch ref
        # itself (refs/heads/<base>), not only the symbolic-ref pointer --
        # see _assert_landed_on_base_branch's own docstring for why
        # symbolic-ref alone is insufficient.
        _assert_landed_on_base_branch(tmp_path, merged_sha)

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
        # fetch-only (no-checkout) path this shape now takes (lr-173768). The
        # tree is a git repo with a base commit but no remote to fetch from.
        _record_base_sha(seed_base_commit(tmp_path))
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
        _assert_landed_on_base_branch(tmp_path, merged_sha)

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
        _assert_landed_on_base_branch(tmp_path, merged_sha)

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
        _assert_landed_on_base_branch(tmp_path, merged_sha)


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
        # The misconfigured tree cannot supply a gate declaration either, which
        # refuses pre_checks first; skipping them reaches the tree-sync failure
        # this test is about.
        argv = _base_args(**{"--repo-path": str(wrapper_dir)}) + ["--skip-pre-checks"]
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED

    def test_knob_present_but_target_not_a_git_tree_refuses_pre_checks(self, tmp_path):
        # The same misconfiguration without the bypass: the tree cannot supply
        # a gate declaration, so pre_checks refuse before anything merges.
        (tmp_path / "not-a-repo").mkdir()
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "pass"]}],
            git_working_tree="not-a-repo",
        )
        code = verb.main(
            _base_args(**{"--repo-path": str(tmp_path)}),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_PRE_CHECKS_FAILED

    def test_drift_check_never_false_positives_in_wrapper_layout(self, tmp_path):
        """BOBBIE finding, lr-cd3644 fold-in #3: the config root (the
        wrapper) and the git tree (its `repo` subdirectory) are DIFFERENT
        directories here -- the committed config the drift check would need
        to compare against lives OUTSIDE the inner git tree entirely (never
        tracked by IT at any commit), so `load_post_merge_steps_from_git_sha`
        must correctly resolve "not comparable" (None) for this split, same
        as the plain untracked-config case. The regression proof: a
        wrapper-layout merge with post_merge_steps configured must still
        exit OK and actually run its step -- proving the drift check's own
        git-tree-relative resolution never interferes with the wrapper-layout
        path it was already supposed to support (lr-93d718)."""
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
        assert marker.read_text() == "ok"

    def test_drift_check_does_not_compare_against_an_unrelated_inner_tree_file(
        self, tmp_path
    ):
        """BOBBIE finding, lr-cd3644 fold-in #3, THE PRECISE REGRESSION
        PROOF: the INNER git tree happens to ALSO track a file at the exact
        same relative path (`.clagentic/loadout/config.yaml`) the wrapper's
        REAL config lives at -- an entirely UNRELATED file, coincidentally
        same-named, declaring a DIFFERENT step (a distinct marker). Before
        this fix, `load_post_merge_steps_from_git_sha` ran `git show
        <sha>:.clagentic/loadout/config.yaml` with cwd=git_tree_path using
        the BARE default relative path (as if git_tree_path were the config
        root) -- which this fixture makes SUCCEED (that path genuinely
        exists in the inner tree), so the drift check would compare the
        wrapper's real pre-sync steps against this UNRELATED inner-tree
        file's steps and could fire a bogus drift correction, running the
        WRONG step. `resolve_git_tree_relative_config_paths` must resolve
        (None, None) here instead (the wrapper's REAL config sits outside
        the inner tree entirely), so the inner tree's own unrelated tracked
        file at the same path is NEVER read as if it were the wrapper's
        config -- only the wrapper's own real, pre-sync-resolved step must
        run."""
        wrapper_dir = tmp_path
        git_tree_dir = wrapper_dir / "repo"
        git_tree_dir.mkdir()
        # The inner tree tracks its OWN, UNRELATED config.yaml at the same
        # relative path the wrapper's real config lives at (a coincidence a
        # correct implementation must never conflate with the wrapper's own
        # config).
        wrong_marker = wrapper_dir / "wrong-step-ran.txt"
        merged_sha = _init_repo_with_origin_and_tracked_config(
            git_tree_dir,
            [{"cmd": [_PY, "-c", f"open(r'{wrong_marker}', 'w').write('WRONG')"]}],
        )

        # The wrapper's REAL config (outside the inner tree) declares a
        # DIFFERENT step.
        right_marker = wrapper_dir / "right-step-ran.txt"
        _write_merge_config(
            wrapper_dir,
            [{"cmd": [_PY, "-c", f"open(r'{right_marker}', 'w').write('right')"]}],
            git_working_tree="repo",
        )
        argv = _base_args(**{"--repo-path": str(wrapper_dir), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK
        # The wrapper's OWN real step ran -- never the inner tree's
        # unrelated, coincidentally-same-path tracked file.
        assert right_marker.read_text() == "right"
        assert not wrong_marker.exists()

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


def _init_repo_with_origin_and_tracked_config(tmp_path, steps: list[dict]) -> str:
    """Variant of `_init_repo_with_origin` for the lr-cd3644 drift-check
    tests specifically: unlike that fixture (and `_write_merge_config`, used
    everywhere else in this file), THIS repo commits
    `.clagentic/loadout/config.yaml` as part of its seed commit -- matching
    the observed incident's own repo shape (clagentic-triage tracks this
    file; confirmed by a real `git diff --stat <old> <new> --
    .clagentic/loadout/config.yaml` showing a genuine diff between two
    commits there), NOT this package's own dogfooding convention (gitignored
    -- see .gitignore). The drift check only ever fires for a repo that
    commits this file; every other fixture in this module deliberately
    leaves it untracked, which is why they all use `_write_merge_config`
    (uncommitted) instead of this one. Returns the resulting seed-commit SHA
    on `origin`'s `main` (same contract as `_init_repo_with_origin`)."""
    remote_dir = tmp_path.parent / f"{tmp_path.name}-origin.git"
    _run_git(["init", "--bare", "-b", _BASE_BRANCH, str(remote_dir)], cwd=tmp_path.parent)

    _run_git(["init", "-b", _BASE_BRANCH, str(tmp_path)], cwd=tmp_path.parent)
    _run_git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    _run_git(["config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    _write_merge_config(tmp_path, steps)
    _run_git(["add", "."], cwd=tmp_path)
    _run_git(["commit", "-m", "seed"], cwd=tmp_path)
    _run_git(["remote", "add", "origin", str(remote_dir)], cwd=tmp_path)
    _run_git(["push", "origin", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=tmp_path)

    rev_parse = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
    )
    return _record_base_sha(rev_parse.stdout.strip())


def _push_tracked_config_commit_to_origin(
    tmp_path, steps: list[dict], *, parent_sha: str | None = None
) -> str:
    """Advance origin's own `main` tip by one commit whose tracked
    `.clagentic/loadout/config.yaml` declares *steps* (a well-formed
    post_merge_steps list). See `_push_tracked_config_text_commit_to_origin`
    for the shared plumbing this is a thin wrapper over, including the
    *parent_sha* override (needed to chain a SECOND pushed commit on top of
    a FIRST one -- see that function's own docstring)."""
    return _push_tracked_config_text_commit_to_origin(
        tmp_path,
        yaml.safe_dump({"merge": {"post_merge_steps": steps}}),
        parent_sha=parent_sha,
    )


def _push_malformed_tracked_config_commit_to_origin(tmp_path) -> str:
    """Advance origin's own `main` tip by one commit whose tracked
    `.clagentic/loadout/config.yaml` is INVALID YAML -- the one case
    lr-cd3644's drift check cannot self-correct (there is no "merged
    commit's steps" to treat as authoritative when the merged commit's own
    config does not parse at all). See
    `_push_tracked_config_text_commit_to_origin` for the shared plumbing."""
    return _push_tracked_config_text_commit_to_origin(tmp_path, "merge: [unclosed")


def _push_deleted_tracked_config_commit_to_origin(
    tmp_path, *, parent_sha: str | None = None
) -> str:
    """Advance origin's own `main` tip by one commit that DELETES the
    ALREADY-TRACKED `.clagentic/loadout/config.yaml` path entirely (lr-cd3644
    fold-in #3, PR #30 re-review finding B) -- built via the SAME temporary-
    index plumbing `_push_tracked_config_text_commit_to_origin` uses, except
    the path is removed from the new tree (`git update-index --force-remove`)
    rather than rewritten to new content. This is the "deleted-but-tracked"
    shape finding B distinguishes from "never tracked at all"
    (`_init_repo_with_origin` + `_write_merge_config`'s own untracked-config
    convention, see `test_untracked_config_repo_is_never_compared_and_never_
    false_positives`): the config path WAS present and git-tracked at the
    caller's pre-sync HEAD (`_init_repo_with_origin_and_tracked_config`'s own
    seed commit), but the merged commit's own tree no longer has it at all.
    Returns the new commit's SHA."""
    if parent_sha is None:
        parent_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        ).stdout.strip()

    scratch_index = tmp_path.parent / f"{tmp_path.name}-scratch.index"
    if scratch_index.exists():
        scratch_index.unlink()
    env = {**subprocess.os.environ, "GIT_INDEX_FILE": str(scratch_index)}
    read_tree = subprocess.run(
        ["git", "read-tree", parent_sha], capture_output=True, text=True, cwd=str(tmp_path), env=env
    )
    assert read_tree.returncode == 0, read_tree.stderr
    config_path = ".clagentic/loadout/config.yaml"
    remove_index = subprocess.run(
        ["git", "update-index", "--force-remove", config_path],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert remove_index.returncode == 0, remove_index.stderr
    write_tree = subprocess.run(
        ["git", "write-tree"], capture_output=True, text=True, cwd=str(tmp_path), env=env
    )
    assert write_tree.returncode == 0, write_tree.stderr
    tree_sha = write_tree.stdout.strip()
    scratch_index.unlink()

    commit_tree = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", parent_sha, "-m", "delete post_merge config"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env={
            **subprocess.os.environ,
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    assert commit_tree.returncode == 0, commit_tree.stderr
    new_sha = commit_tree.stdout.strip()

    push = subprocess.run(
        ["git", "push", "origin", f"{new_sha}:refs/heads/{_BASE_BRANCH}"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert push.returncode == 0, push.stderr
    return new_sha


def _push_tracked_config_text_commit_to_origin(
    tmp_path, config_text: str, *, parent_sha: str | None = None
) -> str:
    """Advance origin's own `main` tip (NOT the local clone's working tree or
    index at tmp_path) by one commit that rewrites the ALREADY-TRACKED
    `.clagentic/loadout/config.yaml` to *config_text* verbatim -- built via a
    SEPARATE, temporary git index (`GIT_INDEX_FILE`) populated from HEAD's
    own tree (`git read-tree`), overwritten with the new blob for that one
    path (`git update-index --add --cacheinfo`), and written out
    (`git write-tree` + `git commit-tree`), then pushed straight to the bare
    'origin' remote -- so the local WORKING TREE and the REAL index at
    tmp_path are left completely untouched (still checked out on
    `_init_repo_with_origin_and_tracked_config`'s original seed commit) --
    exactly the "caller's --repo-path is stale relative to what just got
    merged" shape lr-cd3644 closes. Returns the new commit's SHA (the
    "merged_sha" a test then feeds to the opener). Shared by
    `_push_tracked_config_commit_to_origin` (well-formed steps) and
    `_push_malformed_tracked_config_commit_to_origin` (invalid YAML, the
    fail-loud-preserved case) so the two never diverge on the underlying git
    plumbing -- only the config TEXT differs.

    *parent_sha*: defaults to the local clone's own HEAD (unchanged from
    before this parameter existed) -- correct for the common case of a
    single pushed commit built on the seed. A caller chaining a SECOND
    pushed commit on top of a FIRST one (lr-cd3644 fold-in #2's regression
    test -- simulating origin advancing twice before a promoted checkout
    ever runs) passes the first call's own returned SHA here explicitly,
    since the LOCAL clone's HEAD never moves (this function never touches
    tmp_path's own working tree or index) and would otherwise always branch
    a second call off the SAME seed commit, producing a non-fast-forward
    push rejection against origin's already-advanced tip."""
    hash_object = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        input=config_text, capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert hash_object.returncode == 0, hash_object.stderr
    blob_sha = hash_object.stdout.strip()

    if parent_sha is None:
        parent_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
        ).stdout.strip()

    scratch_index = tmp_path.parent / f"{tmp_path.name}-scratch.index"
    if scratch_index.exists():
        scratch_index.unlink()
    env = {**subprocess.os.environ, "GIT_INDEX_FILE": str(scratch_index)}
    read_tree = subprocess.run(
        ["git", "read-tree", parent_sha], capture_output=True, text=True, cwd=str(tmp_path), env=env
    )
    assert read_tree.returncode == 0, read_tree.stderr
    config_path = ".clagentic/loadout/config.yaml"
    update_index = subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{config_path}"],
        capture_output=True, text=True, cwd=str(tmp_path), env=env,
    )
    assert update_index.returncode == 0, update_index.stderr
    write_tree = subprocess.run(
        ["git", "write-tree"], capture_output=True, text=True, cwd=str(tmp_path), env=env
    )
    assert write_tree.returncode == 0, write_tree.stderr
    tree_sha = write_tree.stdout.strip()
    scratch_index.unlink()

    commit_tree = subprocess.run(
        ["git", "commit-tree", tree_sha, "-p", parent_sha, "-m", "wire post_merge_steps"],
        capture_output=True, text=True, cwd=str(tmp_path),
        env={
            **subprocess.os.environ,
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )
    assert commit_tree.returncode == 0, commit_tree.stderr
    new_sha = commit_tree.stdout.strip()

    push = subprocess.run(
        ["git", "push", "origin", f"{new_sha}:refs/heads/{_BASE_BRANCH}"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert push.returncode == 0, push.stderr
    return new_sha


class TestStalePreSyncConfigDriftCheck:
    """lr-cd3644: --repo-path's WORKING TREE is what `load_post_merge_steps`
    reads BEFORE this step ever advances that tree to the merged commit. A
    caller whose --repo-path is stale (still checked out on an EARLIER
    commit than the one actually being merged) can therefore resolve
    `steps` against config that disagrees with what the merged commit
    itself declares -- silently running fewer/different steps, with no
    signal at all. This is the DIRECT regression proof for that incident,
    using a repo shape that TRACKS its config in git (the observed
    incident's own repo shape -- see
    _init_repo_with_origin_and_tracked_config's own docstring for why this
    is a SEPARATE fixture from every other test in this file): the local
    clone stays on its original seed commit throughout, while origin's
    `main` tip (the "merged" commit fed to the opener as merge_commit_sha)
    is advanced, via raw git plumbing against the SAME object database, to
    a DIFFERENT commit whose own committed config declares a different
    post_merge_steps list.

    lr-cd3644 FOLLOWUP (HOLDEN-dispatched fold-in, same PR): the ORIGINAL
    version of this check failed the entire merge loudly
    (EXIT_POST_MERGE_FAILED) on ANY disagreement, even when the merged
    commit's own config had REAL steps the stale pre-sync read never saw --
    meaning a repo whose config gained post_merge_steps in the very commit
    this merge lands would get those steps refused outright rather than
    run. `merge.verb._run` now treats the merged SHA's committed config as
    AUTHORITATIVE: on drift, the merged commit's own `post_merge_steps` are
    what actually execute (see
    test_stale_repo_path_with_fewer_steps_than_merged_commit_runs_the_merged_
    commits_steps below), and the working tree still lands on base_branch
    afterward exactly as an ordinary, non-drifted merge would. FAIL LOUD is
    preserved for the one case the drift check cannot self-correct: the
    merged SHA's own config cannot be READ at all (malformed YAML at that
    commit -- see
    test_merged_commit_config_unreadable_still_fails_loud below)."""

    def test_stale_repo_path_with_fewer_steps_than_merged_commit_runs_the_merged_commits_steps(
        self, tmp_path
    ):
        # The LOCAL clone's own working-tree config: explicitly zero steps,
        # TRACKED in git (an informed "no steps" choice at the STALE commit
        # -- matching the observed incident's own repo shape, where the
        # caller's tree had no post_merge_steps section on the commit it was
        # left checked out on). origin's tip is then advanced, via plumbing
        # (no working-tree mutation), to a NEW commit whose TRACKED config
        # declares a real step. The merged commit's own step must actually
        # RUN (its marker file must exist) -- not be refused -- and the
        # working tree must land on base_branch afterward, exactly like any
        # other successful post_merge_steps run.
        _init_repo_with_origin_and_tracked_config(tmp_path, [])
        marker = tmp_path / "installed-from-merged-commit.txt"
        merged_sha = _push_tracked_config_commit_to_origin(
            tmp_path,
            [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ok')"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK
        assert marker.read_text() == "ok"
        # The tree lands on the base branch at the merged SHA afterward --
        # the drift correction promotes the stale pre-sync fetch-only
        # decision to a real checkout (steps_will_run flips False -> True),
        # and land_on_base_branch still runs against that checkout exactly
        # as it would for a non-drifted merge whose steps were known from
        # the start.
        _assert_landed_on_base_branch(tmp_path, merged_sha)

    def test_merged_commit_config_unreadable_still_fails_loud(self, tmp_path):
        # FAIL LOUD is preserved for the one case the drift check cannot
        # self-correct: the merged SHA's own committed config cannot be READ
        # at all (malformed YAML at that exact commit) -- there is no
        # "merged commit's steps" to treat as authoritative when the merged
        # commit's own config does not parse. Must never silently run zero
        # steps or fall back to the stale pre-sync list either.
        _init_repo_with_origin_and_tracked_config(tmp_path, [])
        merged_sha = _push_malformed_tracked_config_commit_to_origin(tmp_path)
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_POST_MERGE_FAILED

    def test_matching_pre_and_post_sync_config_still_succeeds(self, tmp_path):
        # Negative control: when --repo-path's pre-sync config already
        # matches the merged commit's own TRACKED config (the common,
        # non-stale case), the drift check must be a silent no-op -- the
        # merge succeeds and the step still runs exactly as before this task.
        step = {"cmd": [_PY, "-c", "pass"]}
        _init_repo_with_origin_and_tracked_config(tmp_path, [step])
        merged_sha = _push_tracked_config_commit_to_origin(tmp_path, [step])
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK

    def test_forgejo_promotion_passes_landed_sha_as_known_merged_sha(
        self, tmp_path, monkeypatch
    ):
        """BOBBIE finding, lr-cd3644 fold-in #2: on the FORGEJO platform the
        merge backend's own API response never carries the merged SHA (see
        merge.tree_sync's module docstring, "TRADE-OFF NAMED") -- `merged_sha`
        in merge.verb._run stays None for the whole invocation. Before this
        fix, the drift-promoted `advance_repo_to_merged_sha` call passed
        `known_merged_sha=merged_sha` (None on this platform), which falls
        into that function's base-branch-FALLBACK resolution: re-fetch
        *base_branch* and check out whatever ITS CURRENT remote tip is AT
        PROMOTION TIME -- not necessarily `landed_sha`, the exact commit
        `fetch_merged_sha_object` already fetched and independently verified
        earlier in the SAME invocation. A real-git-timing reproduction would
        need origin's tip to move DURING the single synchronous merge.verb
        invocation, which a local bare-repo fixture cannot express -- this
        test instead asserts the call-site CONTRACT directly: monkeypatch
        `merge.verb.advance_repo_to_merged_sha` to a recording spy and
        assert the drift-promoted call's own `known_merged_sha` kwarg equals
        `landed_sha` (the value `fetch_merged_sha_object` already resolved
        and verified), never `None`/`merged_sha`. This is the exact
        precondition the base-branch-fallback divergence needs -- proving
        the call site never reaches it closes the defect regardless of
        whether any given test run's real git timing happens to expose it."""
        _init_repo_with_origin_and_tracked_config(tmp_path, [])
        merged_sha = _push_tracked_config_commit_to_origin(
            tmp_path,
            [{"cmd": [_PY, "-c", "pass"]}],
        )

        recorded_calls: list[dict] = []
        real_advance = verb.advance_repo_to_merged_sha

        def _recording_advance(*args, **kwargs):
            recorded_calls.append(kwargs)
            return real_advance(*args, **kwargs)

        monkeypatch.setattr(verb, "advance_repo_to_merged_sha", _recording_advance)

        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "forgejo"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            # Forgejo's own merge response never carries a SHA -- merged_sha
            # stays None for the whole invocation, the exact precondition
            # this regression needs (see docstring above).
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
        # Exactly one promoted checkout call (the fetch-only path never ran
        # advance_repo_to_merged_sha; only the drift promotion did).
        assert len(recorded_calls) == 1
        assert recorded_calls[0]["known_merged_sha"] == merged_sha
        assert recorded_calls[0]["known_merged_sha"] is not None

    def test_drift_correction_to_empty_list_still_lands_on_base_branch(
        self, tmp_path
    ):
        """BOBBIE finding, lr-cd3644 fold-in #4: the pre-sync `--repo-path`
        HAD real, non-empty post_merge_steps (steps_will_run True from the
        start -- a REAL, verified checkout happens BEFORE the drift check
        ever runs), but the MERGED commit's own tracked config declares
        ZERO steps -- a legitimate drift-correction result, not an error
        (see test_merged_commit_config_unreadable_still_fails_loud for the
        one case that DOES fail loud; this is not that case). Before this
        fix, land_on_base_branch was gated on `steps_will_run` -- which the
        drift check correctly re-derives to False here (zero steps actually
        ran) -- leaving a REAL, VERIFIED, DETACHED checkout on disk with no
        signal to the next dispatch. The tree must land on base_branch
        whenever the sync actually advanced/checked it out
        (`tree_checked_out`), regardless of the final step count."""
        # Pre-sync (stale) config: one real step. This makes steps_will_run
        # True from the START, so tree_sync takes the REAL-checkout branch
        # (advance_repo_to_merged_sha, tree_checked_out=True) rather than
        # the fetch-only branch -- exactly the precondition this defect
        # needs (a checkout that already happened before drift corrects
        # steps_will_run back down).
        _init_repo_with_origin_and_tracked_config(
            tmp_path, [{"cmd": [_PY, "-c", "pass"]}]
        )
        # Merged commit's own TRACKED config: explicitly zero steps -- an
        # informed choice at the commit actually being merged.
        merged_sha = _push_tracked_config_commit_to_origin(tmp_path, [])
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK
        # THE REGRESSION PROOF: the tree must be on base_branch, not
        # detached, even though the FINAL (post-drift-correction) step count
        # was zero.
        _assert_landed_on_base_branch(tmp_path, merged_sha)

    def test_deleted_but_tracked_config_makes_merged_commit_authoritative_zero_steps(
        self, tmp_path
    ):
        """lr-cd3644 fold-in #3 (PR #30 re-review finding B): --repo-path's
        PRE-SYNC HEAD tracks the config with a real step (an informed
        choice at THAT commit). The merged commit DELETES the config path
        entirely. This is NOT the same as "never tracked at all"
        (test_untracked_config_repo_is_never_compared_and_never_false_
        positives below) -- the merged commit is DELETING a real,
        previously-tracked post_merge_steps declaration, so it must be
        treated as authoritative (zero steps, a real drift-corrected
        result) rather than 'not comparable, keep running the stale
        pre-sync step'. Before the fix, load_post_merge_steps_from_git_sha
        returning None for the deleted path was indistinguishable from
        'never tracked' at the merge.verb._run call site, so the stale
        step from the pre-sync HEAD kept running even though the merged
        commit had deleted its declaration."""
        marker = tmp_path / "should-never-run.txt"
        _init_repo_with_origin_and_tracked_config(
            tmp_path, [{"cmd": [_PY, "-c", f"open(r'{marker}', 'w').write('ran')"]}]
        )
        merged_sha = _push_deleted_tracked_config_commit_to_origin(tmp_path)
        argv = _base_args(**{"--repo-path": str(tmp_path), "--platform": "github"})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_github_opener(merged_sha=merged_sha),
        )
        assert code == verb.EXIT_OK
        # THE REGRESSION PROOF: the merged commit's deletion is
        # authoritative -- the stale pre-sync step must NEVER run.
        assert not marker.exists()
        # The tree still lands on base_branch afterward, exactly like any
        # other drift-corrected merge (fold-in #4's own contract).
        _assert_landed_on_base_branch(tmp_path, merged_sha)

    def test_untracked_config_repo_is_never_compared_and_never_false_positives(
        self, tmp_path
    ):
        # THE lr-cd3644 NEGATIVE-SPACE PROOF: a repo that keeps
        # .clagentic/loadout/config.yaml UNTRACKED (this package's own
        # dogfooding convention -- see .gitignore) must NEVER be flagged by
        # this check, no matter how "stale" --repo-path looks relative to
        # origin's tip -- git show finds nothing at ANY commit for an
        # untracked path, so load_post_merge_steps_from_git_sha returns
        # None and the comparison is skipped entirely. Uses the ordinary
        # _init_repo_with_origin + _write_merge_config fixtures (uncommitted
        # config) exactly like every other test in this file.
        _init_repo_with_origin(tmp_path)
        _write_merge_config(
            tmp_path,
            [{"cmd": [_PY, "-c", "pass"]}],
        )
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_OK
