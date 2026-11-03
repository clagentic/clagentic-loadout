"""Shared fixtures for the loadout-merge and loadout-post-merge verb tests: a real
local git repository with a bare origin, forge-shaped request openers, token and
authority doubles, and a merge-config writer.

Imported by test_merge_verb_post_merge.py, test_merge_post_merge_verb.py and
test_merge_tool_merge_tree.py, so no test module reaches into another's private
helpers. The merge-side and post-merge-side openers differ (the merge verb drives
the whole gate, the post-merge verb only reads the PR), hence the two families.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone

import yaml

from tests._gate_repo import make_origin_confirmable

FULL_SHA = "a" * 40
MERGED_SHA = "b" * 40
BASE_BRANCH = "main"
OWNER = "some-owner"
REPO = "some-repo"


def run_git(args: list[str], *, cwd) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True, cwd=str(cwd))
    assert result.returncode == 0, f"git {args!r} failed: {result.stderr}"


def init_repo_with_origin(tmp_path):
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
    run_git(["init", "--bare", "-b", BASE_BRANCH, str(remote_dir)], cwd=tmp_path.parent)

    run_git(["init", "-b", BASE_BRANCH, str(tmp_path)], cwd=tmp_path.parent)
    run_git(["config", "user.email", "test@example.com"], cwd=tmp_path)
    run_git(["config", "user.name", "test"], cwd=tmp_path)
    (tmp_path / "README.md").write_text("seed\n", encoding="utf-8")
    run_git(["add", "README.md"], cwd=tmp_path)
    run_git(["commit", "-m", "seed"], cwd=tmp_path)
    run_git(["remote", "add", "origin", str(remote_dir)], cwd=tmp_path)
    run_git(["push", "origin", f"{BASE_BRANCH}:{BASE_BRANCH}"], cwd=tmp_path)
    make_origin_confirmable(tmp_path)

    rev_parse = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, cwd=str(tmp_path)
    )
    return record_base_sha(rev_parse.stdout.strip())


#: The seed commit of the repo the current test built. The merge gate reads its
#: declaration at the PR's base commit and fails closed without one, so the
#: default PR payloads below carry this as `base.sha` whenever a repo was built.
SEEDED_BASE: dict[str, str] = {"sha": ""}


def record_base_sha(sha: str) -> str:
    SEEDED_BASE["sha"] = sha
    return sha


def default_base() -> dict:
    return {"ref": BASE_BRANCH, **({"sha": SEEDED_BASE["sha"]} if SEEDED_BASE["sha"] else {})}


class RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class FakeResponse:
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


def json_resp(status: int, payload) -> FakeResponse:
    return FakeResponse(status, json.dumps(payload).encode("utf-8"))


def make_merge_opener(*, pr_info=None, files=None, comments=None, merge_status=200):
    """CI-status defaults to the no-runner-by-design empty shape (lr-afba)
    -- the tests using this opener reason about post-merge steps, not CI, so they
    keep passing through the CI-status gate exactly as before that gate
    existed.

    lr-7c5540: pr_info's default now carries a "base" ref matching
    BASE_BRANCH -- merge.tree_sync.resolve_base_branch reads this to know
    which branch to fetch before any post_merge_steps entry runs. Every test
    using this opener that reaches step 10's actual tree-sync/run path pairs this
    default with init_repo_with_origin(tmp_path), whose bare remote's
    default branch is the SAME BASE_BRANCH name.
    """
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": FULL_SHA},
        "title": "feat: a change",
        "base": default_base(),
    }
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []

    # Comments the verb posts (the merge-completion attestation) are kept and
    # echoed back by later reads, as the host would.
    posted_comments: list[dict] = []
    # Reports merged only after the merge request arrived, so the verb's
    # post-merge readback sees the state change a real host would make.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            if merge_status in (200, 204):
                _merge_landed[0] = True
                return FakeResponse(merge_status, b"{}")
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
            return json_resp(201, posted_comments[-1])
        if method == "GET" and url.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and url.split("?")[0].endswith("/comments"):
            return json_resp(200, comments + posted_comments)
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/actions/tasks"):
            return json_resp(200, {"total_count": 0})
        if method == "GET" and "/compare/" in url:
            # lr-835c57: empty branch-commit list -- these tests exercise
            # post_merge_steps wiring, not the commit-subject gate.
            return json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": "e" * 40}
                )
            return json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def make_merge_github_opener(*, pr_info=None, files=None, comments=None, merged_sha=None):
    """GitHub-platform counterpart to make_merge_opener above (lr-d95cdb GitHub-
    parity coverage for the tree-sync-after-merge default). Shapes requests
    per merge.github_backend's own documented endpoints: PUT .../merge
    returns {"merged": true, "sha": merged_sha} (the ONE field
    advance_repo_to_merged_sha's known_merged_sha path consumes -- see that
    backend's merge_pr docstring), GET check-runs/compare/status endpoints
    return the same no-runner-by-design-empty / empty-commit-list shapes
    make_merge_opener's Forgejo path uses."""
    pr_info = pr_info if pr_info is not None else {
        "head": {"sha": FULL_SHA},
        "title": "feat: a change",
        "base": default_base(),
    }
    files = files if files is not None else ["a.py"]
    comments = comments if comments is not None else []
    posted_reviews: list[dict] = []
    # lr-361de3: see make_merge_opener's identical post-merge-readback overlay
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
            return json_resp(200, body)
        if method == "GET" and url.endswith("/user"):
            return json_resp(200, {"login": "loadout-merger[bot]"})
        if method == "POST" and "/reviews" in url:
            posted_reviews.append(
                {
                    "id": 9001 + len(posted_reviews),
                    "user": {"login": "loadout-merger[bot]"},
                    "body": json.loads(req.data.decode("utf-8")).get("body", ""),
                    "submitted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            return json_resp(200, posted_reviews[-1])
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": f} for f in files])
        if method == "GET" and "/issues/" in url and url.split("?")[0].endswith("/comments"):
            return json_resp(200, comments)
        if method == "GET" and "/reviews" in url:
            return json_resp(200, posted_reviews)
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/check-runs"):
            return json_resp(200, {"total_count": 0, "check_runs": []})
        if method == "GET" and "/compare/" in url:
            return json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and url.endswith(f"/pulls/{1}"):
            if _merge_landed[0]:
                return json_resp(
                    200, {**pr_info, "merged": True, "merge_commit_sha": merged_sha or "e" * 40}
                )
            return json_resp(200, pr_info)
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def merge_base_args(**overrides) -> list[str]:
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


def write_merge_config(repo_root, steps: list[dict], *, git_working_tree: str | None = None) -> None:
    config_dir = repo_root / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    merge_section: dict = {"post_merge_steps": steps}
    if git_working_tree is not None:
        merge_section["git_working_tree"] = git_working_tree
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"merge": merge_section}), encoding="utf-8"
    )


def make_post_merge_opener(*, merged=True, merge_commit_sha=MERGED_SHA, base_ref=BASE_BRANCH):
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
            return FakeResponse(200, json.dumps(pr_info).encode("utf-8"))
        raise AssertionError(
            f"unexpected call: {method} {url} -- this verb must only ever "
            f"read PR info via the Forgejo API, never merge/comment"
        )

    return opener


def make_post_merge_github_opener(*, merged=True, merge_commit_sha=MERGED_SHA, base_ref=BASE_BRANCH):
    """A github-shaped opener that answers GET .../pulls/{n} with a
    merged/unmerged PR payload -- proves a --platform github invocation
    dispatches to github_backend.get_pr_info, never forgejo_backend's."""
    pr_info = {
        "merged": merged,
        "merge_commit_sha": merge_commit_sha,
        "base": {"ref": base_ref},
    }

    def opener(req, timeout=30):
        assert req.full_url == f"https://api.github.com/repos/{OWNER}/{REPO}/pulls/1"
        assert req.get_method() == "GET"
        return FakeResponse(200, json.dumps(pr_info).encode("utf-8"))

    return opener


def post_merge_base_args(repo_path: str, **overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--role": "merger",
        "--authorized-role": "merger",
        "--repo": f"{OWNER}/{REPO}",
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
