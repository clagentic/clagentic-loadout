"""merge.repo_path_consistency — pre-mint slug/tree consistency check for any
verb accepting BOTH --repo and --repo-path as independent arguments.

WHY THIS EXISTS: merge.verb, merge.close_verb, and merge.post_merge_verb all
take --repo (owner/repo, the merge target) and --repo-path (an OPTIONAL local
working-tree root) as two INDEPENDENT arguments — nothing before this module
ever cross-checked them against each other. On nearly every repo in a typical
fleet the directory basename equals the repo name, so the two values are
interchangeable in practice and the distinction is never exercised. The sole
counter-example this check must never flag is the '.github' org-profile repo
shape: GitHub mandates that exact repo name for an org-wide profile README,
so its directory basename (whatever the caller happens to have checked it out
as) commonly diverges from the repo slug's own trailing segment. A caller
that passes a --repo-path pointing at the WRONG tree for its --repo slug
previously discovered that only after a credential had already been minted
and the platform API rejected the request — typically a 422 whose wording
blames the App installation, misrouting an operator's diagnosis toward
credential/permission troubleshooting for what was actually a caller-side
argument mismatch. This check runs BEFORE any mint, so that defect class is
caught locally and named precisely instead.

WHAT IS COMPARED (the one invariant that actually matters): --repo-path's own
`git remote get-url origin`, NOT the directory's basename. A repo directory
may be checked out under any name a caller likes (including, deliberately,
the '.github' org-profile case above, or any other locally-renamed
checkout) — the git remote origin is the only value that reliably identifies
which repo a tree is actually a clone of. Comparing against the basename
instead would flag exactly the correct, intentional '.github' combination as
an error; this module never does that.

NORMALIZATION (both remote forms, before comparing):
  - HTTPS:  https://host[:port]/owner/repo[.git]
  - SSH:    git@host:owner/repo[.git]   (also the ssh://git@host/owner/repo
            long form)
  - a trailing '.git' suffix is optional and stripped either way
  - comparison is case-insensitive (git hosts treat owner/repo case-
    insensitively for routing purposes on every platform this package
    targets)
  - only owner/repo is compared, never the host -- a caller's --repo slug
    names an owner/repo pair, not a host, and the same repo can legitimately
    be cloned from more than one host alias (e.g. an SSH vs HTTPS remote, or
    a mirror) for the identical owner/repo.

EXPLICIT HANDLING OF THE DEGENERATE CASES (never a silent skip): a
--repo-path that does not exist, is not a git working tree, or whose origin
remote is unparseable is NOT treated as "nothing to check" -- it is reported
via RepoPathConsistencyResult.checked=False with a `reason` naming exactly
why, and the caller (merge.verb/close_verb/post_merge_verb) decides what to
do with an unconfirmable tree. An absent acknowledgment of "I could not
verify this" is never conflated with "this passed" -- see this module's own
check_repo_path_consistency docstring.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.errors import MergeUsageError

#: Matches an SSH-form git remote: git@host:owner/repo[.git], or the
#: ssh://git@host/owner/repo[.git] long form. Captures (owner, repo).
_SSH_SHORT_FORM_RE = re.compile(
    r"^[^@]+@[^:/]+:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
_SSH_LONG_FORM_RE = re.compile(
    r"^ssh://[^@]+@[^/]+(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)
#: HTTPS/HTTP-form git remote: scheme://[userinfo@]host[:port]/owner/repo[.git]
_HTTPS_FORM_RE = re.compile(
    r"^https?://(?:[^@/]+@)?[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"
)


def _parse_owner_repo_from_remote(remote_url: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a git remote URL in either SSH or HTTPS
    form. Returns None (never raises) on a shape this module does not
    recognize -- the caller treats that as "unparseable", not a crash."""
    url = remote_url.strip()
    for pattern in (_HTTPS_FORM_RE, _SSH_LONG_FORM_RE, _SSH_SHORT_FORM_RE):
        match = pattern.match(url)
        if match:
            return match.group("owner"), match.group("repo")
    return None


def _normalize_owner_repo(owner: str, repo: str) -> str:
    """Case-folded 'owner/repo' for comparison -- see module docstring
    "NORMALIZATION"."""
    return f"{owner.strip().lower()}/{repo.strip().lower()}"


@dataclass(frozen=True)
class RepoPathConsistencyResult:
    """Outcome of check_repo_path_consistency.

    `checked` is False for every degenerate case (tree absent, not a git
    repo, no origin remote, unparseable remote URL) -- see this module's
    docstring "EXPLICIT HANDLING OF THE DEGENERATE CASES". `reason` is
    always populated when `checked` is False, explaining exactly why no
    comparison could be made. `matches` is only meaningful when `checked`
    is True.
    """

    checked: bool
    matches: bool
    reason: str = ""
    remote_owner_repo: str = ""


def _read_origin_remote(repo_path: str) -> tuple[str | None, str]:
    """Best-effort read of `git remote get-url origin` at *repo_path*.

    Returns (url_or_None, reason). `url_or_None` is None whenever the tree
    does not exist, is not a git working tree, or has no `origin` remote
    configured -- `reason` names exactly which of those applies.
    """
    tree_root = Path(repo_path)
    if not tree_root.is_dir():
        return None, f"--repo-path {repo_path!r} does not exist or is not a directory."
    try:
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=str(tree_root),
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return None, f"could not invoke git in {repo_path!r}: {exc}."
    if result.returncode != 0:
        stderr = result.stderr.strip()
        detail = f": {stderr}" if stderr else ""
        return None, (
            f"{repo_path!r} has no readable 'origin' remote (git remote "
            f"get-url origin failed){detail}."
        )
    url = result.stdout.strip()
    if not url:
        return None, f"{repo_path!r}'s 'origin' remote resolved to an empty URL."
    return url, ""


def check_repo_path_consistency(
    repo_slug: str, repo_path: str | None
) -> RepoPathConsistencyResult:
    """Compare --repo's slug against --repo-path's own origin remote.

    Returns a RepoPathConsistencyResult; NEVER silently treats an
    unconfirmable tree as a pass -- see module docstring. `repo_path=None`
    or `""` (no local tree given at all) is `checked=False` with a `reason`
    naming that no tree was supplied -- there is nothing to compare against
    that case, but that is reported explicitly rather than folded into a
    silent "consistent" default.

    The comparison is ALWAYS against the tree's git remote, never the
    directory basename (see module docstring "WHAT IS COMPARED") -- a repo
    whose directory name differs from its slug's trailing segment (the
    '.github' org-profile shape) is CORRECT and must never be flagged here,
    provided its origin remote actually resolves to that same slug.
    """
    if not repo_path:
        return RepoPathConsistencyResult(
            checked=False, matches=False, reason="no --repo-path was supplied."
        )

    remote_url, reason = _read_origin_remote(repo_path)
    if remote_url is None:
        return RepoPathConsistencyResult(checked=False, matches=False, reason=reason)

    parsed = _parse_owner_repo_from_remote(remote_url)
    if parsed is None:
        return RepoPathConsistencyResult(
            checked=False,
            matches=False,
            reason=(
                f"{repo_path!r}'s origin remote {remote_url!r} does not match "
                f"a recognized SSH or HTTPS git remote shape "
                f"(<scheme>://<host>/<owner>/<repo>[.git] or "
                f"<user>@<host>:<owner>/<repo>[.git])."
            ),
        )
    remote_owner, remote_repo = parsed
    remote_norm = _normalize_owner_repo(remote_owner, remote_repo)

    slug_parts = repo_slug.strip().split("/")
    if len(slug_parts) != 2 or not slug_parts[0] or not slug_parts[1]:
        return RepoPathConsistencyResult(
            checked=False,
            matches=False,
            reason=f"--repo {repo_slug!r} is not a valid 'owner/repo' slug.",
            remote_owner_repo=remote_norm,
        )
    slug_norm = _normalize_owner_repo(slug_parts[0], slug_parts[1])

    return RepoPathConsistencyResult(
        checked=True,
        matches=(slug_norm == remote_norm),
        remote_owner_repo=remote_norm,
    )


def assert_repo_path_consistent(repo_slug: str, repo_path: str | None) -> None:
    """Fail-fast entry point for a caller (merge.verb / close_verb /
    post_merge_verb): refuse BEFORE any credential mint when --repo-path
    points at a real, parseable git tree whose origin remote names a
    DIFFERENT owner/repo than --repo. Raises MergeUsageError naming both the
    slug passed and the slug implied by the tree, plus which one to correct.

    A `checked=False` result (absent tree, no --repo-path, unreadable/
    unparseable remote) is NEVER an error here and is NEVER silently
    dropped either -- the caller is expected to have already logged the
    `reason` for audit (this function only enforces the fail-fast REFUSAL
    half of the contract; logging the explicit acknowledgment of an
    unconfirmable tree is the caller's job, mirroring the
    --no-post-merge-tree precedent elsewhere in this package).
    """
    result = check_repo_path_consistency(repo_slug, repo_path)
    if result.checked and not result.matches:
        raise MergeUsageError(
            f"--repo {repo_slug!r} does not match --repo-path {repo_path!r}'s "
            f"own git remote, which resolves to {result.remote_owner_repo!r}. "
            f"This check compares against the tree's ORIGIN REMOTE, never its "
            f"directory name, so a locally-renamed checkout (e.g. the "
            f"'.github' org-profile repo shape) is never flagged by itself -- "
            f"only a genuine slug/tree mismatch is. Correct whichever of "
            f"--repo or --repo-path names the wrong repo before retrying; "
            f"proceeding would have reached the credential mint with a "
            f"caller-side argument defect, which typically surfaces as an "
            f"opaque platform-API rejection instead of this clear message."
        )


__all__ = [
    "RepoPathConsistencyResult",
    "assert_repo_path_consistent",
    "check_repo_path_consistency",
]
