"""test_merge_tree_sync.py — unit coverage for clagentic_loadout.merge.tree_sync
(lr-7c5540, extended lr-d95cdb).

Proves the defect's regression contract directly at the module level (see
test_merge_verb_post_merge.py for the merge.verb wiring-level coverage using
real local git repos):

  - advance_repo_to_merged_sha, given a known_merged_sha, fetches and checks
    out EXACTLY that commit -- proving post_merge_steps would see the merged
    SHA, never whatever ref the working tree started on.
  - the base-branch-fallback path (known_merged_sha absent, the Forgejo
    shape) resolves FETCH_HEAD after fetching the base branch and lands on
    the SAME commit the remote's base branch tip actually is.
  - a working tree deliberately left on a DIFFERENT (stale) ref before the
    call is moved OFF that stale ref by both paths -- the direct regression
    for the defect ("post_merge_steps ran against the pre-merge branch
    HEAD").
  - an unresolvable base branch (empty string) raises TreeSyncError before
    any git subprocess runs, never a guessed ref.
  - a git fetch/checkout failure (nonexistent remote) raises TreeSyncError,
    never a silent no-op.
  - resolve_base_branch reads the {"base": {"ref": ...}} shape both
    forgejo_backend.get_pr_info and github_backend.get_pr_info return, and
    is "" (never a guess) when absent.
  - (lr-d95cdb) land_on_base_branch moves a detached tree onto base_branch,
    repointed at exactly the already-verified landed SHA -- never a merge/
    rebase -- and fails loud on a mismatch or a checkout failure.
  - (lr-173768) fetch_merged_sha_object fetches the merged commit WITHOUT
    checking anything out, on both the known_merged_sha and base-branch-
    fallback resolution paths -- and (security-review finding, folded into
    lr-173768) independently verifies the fetched object both EXISTS
    locally and IS A COMMIT via `git cat-file -e <sha>^{commit}`, on BOTH
    paths, rather than trusting a caller-supplied SHA on a bare fetch
    exit-code success alone.

Uses REAL local git repos in tmp_path (git subprocess calls are the whole
point of this module) -- no real network access anywhere (every remote is a
local bare repo on disk).
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
    land_on_base_branch,
    resolve_base_branch,
)

_BASE_BRANCH = "main"


def _run_git(args: list[str], *, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))


def _git_ok(args: list[str], *, cwd) -> None:
    result = _run_git(args, cwd=cwd)
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def _head_sha(repo_dir) -> str:
    result = _run_git(["rev-parse", "HEAD"], cwd=repo_dir)
    assert result.returncode == 0
    return result.stdout.strip()


@pytest.fixture
def repo_with_origin(tmp_path):
    """A local working tree ('clone') tracking a bare 'origin' remote (the
    'merged main' side of the story), pre-positioned on a STALE feature-branch
    commit -- mirroring the defect's exact starting condition (merge.verb's
    caller leaves --repo-path checked out on the feature branch HEAD, not
    main). Returns (clone_dir, origin_dir, merged_sha) where merged_sha is
    the commit that landed on origin's base branch AFTER the clone's own
    stale checkout -- proving both resolution paths must actually fetch,
    never just read what is already locally present."""
    origin_dir = tmp_path / "origin.git"
    clone_dir = tmp_path / "clone"
    seed_dir = tmp_path / "seed"

    _git_ok(["init", "--bare", "-b", _BASE_BRANCH, str(origin_dir)], cwd=tmp_path)

    _git_ok(["init", "-b", _BASE_BRANCH, str(seed_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=seed_dir)
    _git_ok(["config", "user.name", "test"], cwd=seed_dir)
    (seed_dir / "f.txt").write_text("v1\n", encoding="utf-8")
    _git_ok(["add", "f.txt"], cwd=seed_dir)
    _git_ok(["commit", "-m", "initial"], cwd=seed_dir)
    _git_ok(["remote", "add", "origin", str(origin_dir)], cwd=seed_dir)
    _git_ok(["push", "origin", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=seed_dir)

    _git_ok(["clone", str(origin_dir), str(clone_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=clone_dir)
    _git_ok(["config", "user.name", "test"], cwd=clone_dir)
    _git_ok(["checkout", "-b", "feature/stale"], cwd=clone_dir)
    (clone_dir / "f.txt").write_text("feature-branch-content\n", encoding="utf-8")
    _git_ok(["commit", "-am", "feature branch change"], cwd=clone_dir)
    stale_sha = _head_sha(clone_dir)

    # A follow-up commit lands on origin's base branch AFTER the clone's
    # stale checkout -- the "merged main" content the clone has NEVER seen
    # locally yet, matching the defect's exact shape: the merge landed
    # server-side, but the local tree (still on stale_sha) has no idea.
    (seed_dir / "f.txt").write_text("v2-merged\n", encoding="utf-8")
    _git_ok(["commit", "-am", "merged change"], cwd=seed_dir)
    _git_ok(["push", "origin", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=seed_dir)
    merged_sha = _head_sha(seed_dir)

    assert _head_sha(clone_dir) == stale_sha  # sanity: still on the stale ref
    return clone_dir, origin_dir, merged_sha, stale_sha


class TestKnownMergedShaPath:
    def test_advances_to_the_exact_known_sha(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_origin
        landed = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        assert landed == merged_sha
        assert _head_sha(clone_dir) == merged_sha
        assert _head_sha(clone_dir) != stale_sha

    def test_working_tree_content_reflects_merged_commit(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        # The direct regression proof: a post-merge step reading this file
        # (e.g. `npm pack`ing the repo) now sees the MERGED content, not the
        # feature-branch content the tree started on.
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "v2-merged\n"


class TestBaseBranchFallbackPath:
    """known_merged_sha absent -- the Forgejo shape (that backend's merge
    API response carries no SHA field at all, see forgejo_backend.merge_pr's
    docstring)."""

    def test_advances_to_the_base_branch_tip(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_origin
        landed = advance_repo_to_merged_sha(clone_dir, base_branch=_BASE_BRANCH)
        assert landed == merged_sha
        assert landed != stale_sha
        assert _head_sha(clone_dir) == merged_sha

    def test_working_tree_content_reflects_merged_commit(self, repo_with_origin):
        clone_dir, _origin_dir, _merged_sha, _stale_sha = repo_with_origin
        advance_repo_to_merged_sha(clone_dir, base_branch=_BASE_BRANCH)
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "v2-merged\n"

    def test_land_on_base_branch_after_forgejo_fallback_resolution(self, repo_with_origin):
        # lr-d95cdb: the Forgejo shape (no known_merged_sha) must ALSO be
        # landable on the base branch afterward -- land_on_base_branch takes
        # whatever advance_repo_to_merged_sha resolved, regardless of which
        # of the two resolution paths produced it.
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        landed = advance_repo_to_merged_sha(clone_dir, base_branch=_BASE_BRANCH)
        result = land_on_base_branch(clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed)
        assert result == merged_sha
        branch = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=clone_dir)
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH


class TestFailLoudNeverStaleRef:
    def test_empty_base_branch_raises_before_any_git_call(self, repo_with_origin):
        clone_dir, _origin_dir, _merged_sha, stale_sha = repo_with_origin
        with pytest.raises(TreeSyncError, match="no base branch"):
            advance_repo_to_merged_sha(clone_dir, base_branch="")
        # Refusing to guess means the tree is untouched -- still stale, never
        # silently left in some other unverified state either.
        assert _head_sha(clone_dir) == stale_sha

    def test_unresolvable_remote_branch_raises(self, repo_with_origin):
        clone_dir, _origin_dir, _merged_sha, stale_sha = repo_with_origin
        with pytest.raises(TreeSyncError, match="git fetch"):
            advance_repo_to_merged_sha(clone_dir, base_branch="branch-that-does-not-exist")
        assert _head_sha(clone_dir) == stale_sha

    def test_known_sha_mismatch_after_checkout_raises(self, repo_with_origin, monkeypatch):
        # Defense-in-depth: even if `git fetch`/`git checkout` for
        # known_merged_sha both exit 0, the post-checkout `git rev-parse
        # HEAD` readback is compared against known_merged_sha before this
        # function trusts the tree -- a caller-supplied SHA that does not
        # match what the tree ACTUALLY landed on (e.g. a future backend bug
        # returning the wrong value) must refuse loudly rather than run
        # post_merge_steps unverified. Forcing this branch requires making
        # the readback itself lie relative to the real checkout, since a
        # normal, non-buggy git fetch+checkout of a real known_merged_sha
        # always lands exactly on it -- monkeypatch merge.tree_sync._run_git
        # to return a different SHA only for the final rev-parse call.
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        import clagentic_loadout.merge.tree_sync as tree_sync_module

        real_run_git = tree_sync_module._run_git
        call_log: list[list[str]] = []

        def _fake_run_git(args, *, cwd):
            call_log.append(args)
            result = real_run_git(args, cwd=cwd)
            if args[:2] == ["rev-parse", "HEAD"]:
                result.stdout = "f" * 40 + "\n"
            return result

        monkeypatch.setattr(tree_sync_module, "_run_git", _fake_run_git)

        with pytest.raises(TreeSyncError, match="refusing to run post-merge steps"):
            advance_repo_to_merged_sha(
                clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
            )

    def test_nonexistent_repo_path_raises(self, tmp_path):
        missing = tmp_path / "does-not-exist"
        missing.mkdir()
        with pytest.raises(TreeSyncError):
            advance_repo_to_merged_sha(missing, base_branch=_BASE_BRANCH)


@pytest.fixture
def repo_with_non_origin_remote(tmp_path):
    """Same shape as repo_with_origin, but the clone's sole remote is named
    'github' instead of 'origin' -- the exact non-conforming-tree condition
    from lr-ffede4 (a repo whose remote isn't named 'origin', e.g. because it
    tracks a fork/mirror under a different name). Proves
    advance_repo_to_merged_sha derives the remote from the tracking config
    rather than assuming 'origin', on both the known_merged_sha and
    base-branch-fallback paths."""
    origin_dir = tmp_path / "upstream.git"
    clone_dir = tmp_path / "clone"
    seed_dir = tmp_path / "seed"

    _git_ok(["init", "--bare", "-b", _BASE_BRANCH, str(origin_dir)], cwd=tmp_path)

    _git_ok(["init", "-b", _BASE_BRANCH, str(seed_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=seed_dir)
    _git_ok(["config", "user.name", "test"], cwd=seed_dir)
    (seed_dir / "f.txt").write_text("v1\n", encoding="utf-8")
    _git_ok(["add", "f.txt"], cwd=seed_dir)
    _git_ok(["commit", "-m", "initial"], cwd=seed_dir)
    _git_ok(["remote", "add", "github", str(origin_dir)], cwd=seed_dir)
    _git_ok(["push", "github", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=seed_dir)

    # Clone via a plain path clone, then rename the auto-created 'origin'
    # remote to 'github' -- reproducing a tree whose sole remote is NOT
    # named 'origin' and whose branch.<base>.remote tracking config points
    # at that non-'origin' name.
    _git_ok(["clone", str(origin_dir), str(clone_dir)], cwd=tmp_path)
    _git_ok(["config", "user.email", "test@example.com"], cwd=clone_dir)
    _git_ok(["config", "user.name", "test"], cwd=clone_dir)
    _git_ok(["remote", "rename", "origin", "github"], cwd=clone_dir)
    _git_ok(["checkout", "-b", "feature/stale"], cwd=clone_dir)
    (clone_dir / "f.txt").write_text("feature-branch-content\n", encoding="utf-8")
    _git_ok(["commit", "-am", "feature branch change"], cwd=clone_dir)
    stale_sha = _head_sha(clone_dir)

    (seed_dir / "f.txt").write_text("v2-merged\n", encoding="utf-8")
    _git_ok(["commit", "-am", "merged change"], cwd=seed_dir)
    _git_ok(["push", "github", f"{_BASE_BRANCH}:{_BASE_BRANCH}"], cwd=seed_dir)
    merged_sha = _head_sha(seed_dir)

    assert _head_sha(clone_dir) == stale_sha  # sanity: still on the stale ref
    return clone_dir, origin_dir, merged_sha, stale_sha


class TestNonOriginRemote:
    """Regression coverage for lr-ffede4: advance_repo_to_merged_sha must not
    hardcode 'origin' -- a tree whose sole remote is named something else
    (here 'github') must still run its full post_merge_steps after a
    successful merge, not silently no-op because `git fetch origin` fails."""

    def test_known_sha_path_advances_via_non_origin_remote(
        self, repo_with_non_origin_remote
    ):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_non_origin_remote
        landed = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        assert landed == merged_sha
        assert _head_sha(clone_dir) == merged_sha
        assert _head_sha(clone_dir) != stale_sha

    def test_base_branch_fallback_path_advances_via_non_origin_remote(
        self, repo_with_non_origin_remote
    ):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_non_origin_remote
        landed = advance_repo_to_merged_sha(clone_dir, base_branch=_BASE_BRANCH)
        assert landed == merged_sha
        assert landed != stale_sha
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "v2-merged\n"

    def test_no_origin_remote_exists_at_all(self, repo_with_non_origin_remote):
        # Direct proof this is not accidentally succeeding via a leftover
        # 'origin' remote -- the clone has exactly one remote, 'github'.
        clone_dir, _origin_dir, _merged_sha, _stale_sha = repo_with_non_origin_remote
        result = _run_git(["remote"], cwd=clone_dir)
        assert result.stdout.split() == ["github"]


class TestResolveBaseBranch:
    def test_extracts_ref_from_base_dict(self):
        assert resolve_base_branch({"base": {"ref": "main"}}) == "main"

    def test_missing_base_is_empty(self):
        assert resolve_base_branch({}) == ""

    def test_non_dict_base_is_empty(self):
        assert resolve_base_branch({"base": "not-a-dict"}) == ""

    def test_missing_ref_within_base_is_empty(self):
        assert resolve_base_branch({"base": {}}) == ""


class TestLandOnBaseBranch:
    """lr-d95cdb: land_on_base_branch moves a detached tree onto base_branch,
    repointed at exactly the already-verified landed SHA -- called AFTER
    advance_repo_to_merged_sha (and, in merge.verb, after any post_merge_steps
    ran against the detached tree)."""

    def test_lands_on_base_branch_at_the_landed_sha(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        landed_sha = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        # Sanity: detached before landing.
        symbolic = _run_git(["symbolic-ref", "-q", "HEAD"], cwd=clone_dir)
        assert symbolic.returncode != 0

        result = land_on_base_branch(
            clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed_sha
        )
        assert result == merged_sha
        assert _head_sha(clone_dir) == merged_sha

        branch = _run_git(["symbolic-ref", "--short", "HEAD"], cwd=clone_dir)
        assert branch.returncode == 0
        assert branch.stdout.strip() == _BASE_BRANCH

    def test_resets_a_pre_existing_local_branch_of_the_same_name(self, repo_with_origin):
        # A pre-existing local base_branch (e.g. a stale prior clone of it,
        # still pointing at the OLD stale_sha since this fixture's clone_dir
        # already has a local 'main' from the initial clone) must be RESET
        # (-B, not -b) to the landed SHA, never left at its old value.
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_origin
        pre_existing = _run_git(["rev-parse", _BASE_BRANCH], cwd=clone_dir)
        assert pre_existing.returncode == 0  # sanity: a local 'main' already exists

        landed_sha = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        land_on_base_branch(clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed_sha)

        post = _run_git(["rev-parse", _BASE_BRANCH], cwd=clone_dir)
        assert post.stdout.strip() == merged_sha
        assert post.stdout.strip() != stale_sha

    def test_working_tree_content_reflects_landed_commit(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        landed_sha = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        land_on_base_branch(clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed_sha)
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "v2-merged\n"

    def test_checkout_failure_raises_tree_sync_error(self, repo_with_origin, monkeypatch):
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        landed_sha = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        import clagentic_loadout.merge.tree_sync as tree_sync_module

        real_run_git = tree_sync_module._run_git

        def _fake_run_git(args, *, cwd):
            if args[:1] == ["checkout"]:
                result = subprocess.CompletedProcess(args, 1, stdout="", stderr="boom")
                return result
            return real_run_git(args, cwd=cwd)

        monkeypatch.setattr(tree_sync_module, "_run_git", _fake_run_git)

        with pytest.raises(TreeSyncError, match="git checkout -B"):
            land_on_base_branch(clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed_sha)

    def test_post_checkout_mismatch_raises_tree_sync_error(self, repo_with_origin, monkeypatch):
        # Defense-in-depth mirroring advance_repo_to_merged_sha's own
        # known_merged_sha-mismatch check: even if `git checkout -B` exits 0,
        # the post-checkout `git rev-parse HEAD` readback must equal the
        # requested landed_sha, or this refuses loudly rather than leaving
        # the tree in an unverified state.
        clone_dir, _origin_dir, merged_sha, _stale_sha = repo_with_origin
        landed_sha = advance_repo_to_merged_sha(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        import clagentic_loadout.merge.tree_sync as tree_sync_module

        real_run_git = tree_sync_module._run_git

        def _fake_run_git(args, *, cwd):
            result = real_run_git(args, cwd=cwd)
            if args[:2] == ["rev-parse", "HEAD"]:
                result.stdout = "f" * 40 + "\n"
            return result

        monkeypatch.setattr(tree_sync_module, "_run_git", _fake_run_git)

        with pytest.raises(TreeSyncError, match="refusing to leave the tree"):
            land_on_base_branch(clone_dir, base_branch=_BASE_BRANCH, landed_sha=landed_sha)


class TestFetchMergedShaObject:
    """lr-173768: fetch_merged_sha_object fetches the merged commit into the
    local object database WITHOUT ever checking anything out. (Security-
    review finding, folded into lr-173768): the object's presence AND
    commit-ness is independently verified via `git cat-file -e <sha>^{commit}`
    on BOTH the known_merged_sha and base-branch-fallback resolution paths --
    this class pins that contract directly so a future refactor of this
    function cannot silently drop the readback and have no test notice."""

    def test_known_sha_path_fetches_without_checking_out(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_origin
        fetched = fetch_merged_sha_object(
            clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=merged_sha
        )
        assert fetched == merged_sha
        # No checkout: HEAD/working tree are untouched, still on stale_sha.
        assert _head_sha(clone_dir) == stale_sha
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "feature-branch-content\n"

    def test_base_branch_fallback_path_fetches_without_checking_out(self, repo_with_origin):
        clone_dir, _origin_dir, merged_sha, stale_sha = repo_with_origin
        fetched = fetch_merged_sha_object(clone_dir, base_branch=_BASE_BRANCH)
        assert fetched == merged_sha
        assert _head_sha(clone_dir) == stale_sha
        assert (clone_dir / "f.txt").read_text(encoding="utf-8") == "feature-branch-content\n"

    def test_known_sha_path_verifies_object_exists_and_is_a_commit(
        self, repo_with_origin, monkeypatch
    ):
        # THE BOBBIE FINDING'S DIRECT REGRESSION PROOF: a caller-supplied
        # known_merged_sha that does NOT correspond to any object actually
        # present in the local object database (e.g. a stale/wrong value
        # from a buggy backend) must be refused HERE -- never returned on
        # the strength of a bare `git fetch` exit code alone. A real fetch
        # cannot itself succeed against a nonexistent SHA (git refuses the
        # fetch outright, proven separately elsewhere) -- so this simulates
        # the narrower case the cat-file readback exists specifically to
        # catch: the fetch step reports success, but the object it claims to
        # have fetched is not actually present locally (e.g. a backend that
        # returns a SHA it never itself pushed/fetched). Only the `fetch`
        # call is faked; the cat-file call runs for real against this tree's
        # actual object database.
        clone_dir, _origin_dir, _merged_sha, _stale_sha = repo_with_origin
        nonexistent_sha = "f" * 40
        import clagentic_loadout.merge.tree_sync as tree_sync_module

        real_run_git = tree_sync_module._run_git

        def _fake_run_git(args, *, cwd):
            if args[:1] == ["fetch"]:
                return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
            return real_run_git(args, cwd=cwd)

        monkeypatch.setattr(tree_sync_module, "_run_git", _fake_run_git)

        with pytest.raises(TreeSyncError, match="git cat-file -e"):
            fetch_merged_sha_object(
                clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=nonexistent_sha
            )

    def test_known_sha_path_refuses_a_non_commit_object(self, repo_with_origin):
        # A well-formed, LOCALLY-PRESENT object that is not a commit (here: a
        # blob, the sha of a tracked file's own content) must also be
        # refused -- proving the check asserts TYPE, not merely existence.
        clone_dir, _origin_dir, _merged_sha, _stale_sha = repo_with_origin
        blob_sha_result = _run_git(
            ["rev-parse", "HEAD:f.txt"], cwd=clone_dir
        )
        assert blob_sha_result.returncode == 0
        blob_sha = blob_sha_result.stdout.strip()
        with pytest.raises(TreeSyncError, match="git cat-file -e"):
            fetch_merged_sha_object(
                clone_dir, base_branch=_BASE_BRANCH, known_merged_sha=blob_sha
            )

    def test_base_branch_fallback_path_verification_is_reached(self, repo_with_origin, monkeypatch):
        # Proves the SAME cat-file readback also runs on the fallback path
        # (no known_merged_sha) -- forcing `git rev-parse FETCH_HEAD` to
        # report a well-formed-but-nonexistent SHA (a resolver defect this
        # function must not trust blindly) must refuse loudly here too.
        clone_dir, _origin_dir, _merged_sha, _stale_sha = repo_with_origin
        import clagentic_loadout.merge.tree_sync as tree_sync_module

        real_run_git = tree_sync_module._run_git
        nonexistent_sha = "e" * 40

        def _fake_run_git(args, *, cwd):
            if args[:2] == ["rev-parse", "FETCH_HEAD"]:
                result = subprocess.CompletedProcess(
                    args, 0, stdout=nonexistent_sha + "\n", stderr=""
                )
                return result
            return real_run_git(args, cwd=cwd)

        monkeypatch.setattr(tree_sync_module, "_run_git", _fake_run_git)

        with pytest.raises(TreeSyncError, match="git cat-file -e"):
            fetch_merged_sha_object(clone_dir, base_branch=_BASE_BRANCH)

    def test_empty_base_branch_raises_before_any_git_call(self, repo_with_origin):
        clone_dir, _origin_dir, _merged_sha, stale_sha = repo_with_origin
        with pytest.raises(TreeSyncError, match="no base branch"):
            fetch_merged_sha_object(clone_dir, base_branch="")
        assert _head_sha(clone_dir) == stale_sha

    def test_unresolvable_remote_branch_raises(self, repo_with_origin):
        clone_dir, _origin_dir, _merged_sha, stale_sha = repo_with_origin
        with pytest.raises(TreeSyncError, match="git fetch"):
            fetch_merged_sha_object(clone_dir, base_branch="branch-that-does-not-exist")
        assert _head_sha(clone_dir) == stale_sha
