"""merge.github_backend — GitHub PR reads + merge execution.

Wave B slice 4b (lr-5375, tome #688). Deliberately deferred from lr-885f
(the Forgejo-only merge-gate slice) because GitHub's review-gate mechanism
and credential flow differ materially from Forgejo's — see merge.verb's
module docstring, "SCOPE (task lr-885f)" section, for the original deferral
rationale.

REUSE, NOT A FORK: this module provides ONLY the platform-specific request
shaping (GitHub PR read + merge API semantics) — the exact same shape
forgejo_backend.py provides for Forgejo. Every gate upstream of this module
(namespace guard, authority, stale-SHA, verdict-fence parse+assert,
diff-scope cap, PR-title gate) is platform-agnostic and is reused UNCHANGED;
this module does not touch merge.verdict, merge.diff_scope, merge.title_gate,
merge.authority, or merge.stale_sha. The fenced ```review-result``` verdict
block contract (merge.verdict) holds IDENTICALLY for a GitHub PR — a
reviewer's GitHub PR comment carries the same fenced block, read via this
module's fetch_comments() and parsed/enforced by merge.verdict exactly as
the Forgejo path does. This module never weakens or bypasses that parse.

Shares its GitHub HTTP transport shaping with review.github_backend and
push.github_backend via transport.github_client.request_json() (post-Wave-B
extraction, lr-e1f9 — see that module's docstring for what stayed local per
verb and why): the redirect-hardened opener, the API base URL, and the
request/response plumbing are now ONE shared primitive rather than three
independent local copies. This module keeps its own PR-read + merge-
execution endpoint shapes, payloads, and MergeExecutionError/
GateFactUnavailableError translation — genuinely merge-specific gate
semantics, never force-fit into the shared transport call. review.
github_backend is a POST-and-verify comment transport; this module is a
PR-read + merge-execution transport — distinct gate contracts, so neither
package's verb-specific logic becomes a dependency of the other's public
surface (no cross-layer coupling between review/ and merge/).

Credential flow: the caller (a deployment's own AuthorityProvider/
TokenProvider wiring, e.g. via merge.verb) resolves a token through the SAME
transport.credential_provider seam every other loadout verb uses — a GitHub
App installation-token minting provider is a TokenProvider implementation of
that seam, not a parallel credential path baked into this module. This
module accepts a resolved token string and never mints or fetches one
itself, matching forgejo_backend.py's shape exactly.

GitHub PR merge API semantics (real, documented GitHub REST API — see
https://docs.github.com/rest/pulls/pulls#merge-a-pull-request):
  PUT /repos/{owner}/{repo}/pulls/{pull_number}/merge
  {"merge_method": "merge"[, "commit_message": ...]}
  200                 -> {"merged": true, ...}   -> clean success
  200 {"merged": false} -> not mergeable (rare on 200; treated as refusal)
  405                 -> "Pull Request is not mergeable" (branch protection,
                          conflicts, required status checks not met, etc.)
  409                 -> the PR's head SHA changed since the caller last
                          read it (a base-branch-was-modified race); GitHub's
                          own SHA-guard, distinct from and layered underneath
                          this package's own stale-SHA gate (merge.stale_sha)
  404                 -> PR (or repo) not found / token lacks access
  any other non-2xx   -> refuse with the HTTP status and server message
"""

from __future__ import annotations

import urllib.error
from typing import Any

from clagentic_loadout.merge.ci_status import CiStatusResult
from clagentic_loadout.merge.errors import (
    GateFactUnavailableError,
    MergeExecutionError,
    PlatformMismatchError,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.sha import InvalidShaError, validate_sha
from clagentic_loadout.transport.github_client import GITHUB_API_BASE, request_json
from clagentic_loadout.transport.redirect_guard import no_redirect_opener

#: Default GitHub merge method. A caller wanting squash/rebase semantics
#: passes merge_method explicitly to merge_pr() — "merge" (a real merge
#: commit) is the default because it is the only method that preserves the
#: PR title verbatim in a way merge.title_gate's Conventional-Commits
#: enforcement is meaningful against (squash/rebase let GitHub itself
#: rewrite the resulting commit message).
DEFAULT_MERGE_METHOD = "merge"


def assert_platform_is_github(
    owner: str,
    repo: str,
    *,
    explicit_platform: str,
) -> None:
    """Host-keyed guard for the GitHub merge transport (lr-9c69, mirroring
    review.github_backend.assert_platform_is_github exactly).

    Callers (merge.verb) MUST invoke this BEFORE minting any credential or
    making any API call. *explicit_platform* is a mandatory keyword
    argument — there is no optional/no-op form that could be silently
    skipped by omitting it; the caller resolves platform independently
    (--platform) and passes the result in.

    Owner/repo alone is NEVER an acceptable substitute signal for platform —
    the same owner/namespace value can exist on both GitHub and a
    self-hosted Forgejo instance.

    Raises PlatformMismatchError immediately when *explicit_platform* is not
    GitHub, so a wrong-platform call fails fast and locally instead of
    reaching the wrong host's API and returning an opaque 4xx/422.
    """
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_github: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_GITHUB:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only merges on "
            f"GitHub. Use the Forgejo backend instead. Refusing before "
            f"minting any credential or making any API call."
        )


def _github_request(
    method: str,
    url: str,
    token: str,
    payload: dict[str, Any] | None = None,
    *,
    opener=None,
) -> tuple[int, dict[str, Any]]:
    """Make an authenticated GitHub API request. Token appears ONLY in the
    Authorization header — never in the URL, never logged.

    Thin wrapper over transport.github_client.request_json() in "strict"
    parse mode (a non-empty body is always JSON-parsed, matching this
    module's pre-extraction behavior exactly). `no_redirect_opener` is kept
    imported here (unused directly — request_json owns building it) solely
    so this module's own name stays monkeypatchable at
    `clagentic_loadout.merge.github_backend.no_redirect_opener`, the target
    test_merge_github_backend.py's redirect-hardening coverage patches; see
    that test file's TestRedirectHardeningDefaultOpener for the assertion
    this preserves.

    `opener` injects a urllib opener's .open callable for tests — no real
    network call is ever made when a fake opener is supplied.

    Returns (status_code, parsed_json_body). A non-JSON or empty body
    returns an empty dict rather than raising — callers key their own
    fail-closed behavior off the status code, not the presence of a body.
    """
    return request_json(
        method, url, token, payload, opener=opener, timeout=30,
        opener_factory=no_redirect_opener,
    )


def get_pr_info(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> dict[str, Any]:
    """Fetch PR metadata (GET /repos/{owner}/{repo}/pulls/{pr_number}).

    Raises merge.errors.GateFactUnavailableError on any non-200 response or
    network failure — a gate that cannot read PR metadata cannot verify any
    downstream gate fact (head SHA, title, base branch), so this fails
    closed rather than returning an empty/partial dict for the caller to
    silently tolerate. Mirrors forgejo_backend.get_pr_info's fail-closed
    contract exactly.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        status, body = _github_request("GET", url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return body


def get_pr_head_sha(pr_info: dict[str, Any]) -> str:
    """Extract the head SHA from a get_pr_info() response. Empty string if
    absent (caller decides whether that is fatal for its own gate). Mirrors
    forgejo_backend.get_pr_head_sha — GitHub's PR payload uses the same
    {"head": {"sha": ...}} shape as Forgejo's."""
    head = pr_info.get("head", {})
    return head.get("sha", "") if isinstance(head, dict) else ""


def get_pr_title(pr_info: dict[str, Any]) -> str:
    """Extract the title from a get_pr_info() response. Empty string if
    absent."""
    return pr_info.get("title", "") or ""


def fetch_changed_files(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> list[str]:
    """Fetch the list of changed filenames
    (GET /repos/{owner}/{repo}/pulls/{pr_number}/files).

    Raises merge.errors.GateFactUnavailableError on any non-200 response,
    non-list body, or network failure — the diff-scope gate cannot evaluate
    a cap it cannot see the real file list for. Mirrors
    forgejo_backend.fetch_changed_files exactly (GitHub's endpoint returns
    the same list-of-{"filename": ...} shape).
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/files"
    try:
        status, body = _github_request("GET", url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    if not isinstance(body, list):
        raise GateFactUnavailableError(
            f"changed-file list endpoint returned a non-list body for PR "
            f"#{pr_number} in {owner}/{repo}"
        )
    return [f.get("filename", "<unknown>") for f in body if isinstance(f, dict)]


def fetch_comments(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> list[dict[str, Any]]:
    """Fetch all issue-style comments on a PR
    (GET /repos/{owner}/{repo}/issues/{pr_number}/comments — GitHub shares
    the issue-comments endpoint between issues and PRs, matching Forgejo's
    own issues/PR comment-endpoint split).

    Raises merge.errors.GateFactUnavailableError on any non-200 response,
    non-list body, or network failure — the verdict-fence gate
    (merge.verdict) cannot enforce a reviewer's verdict it cannot read. The
    fenced ```review-result``` block contract is unchanged for GitHub: a
    reviewer's GitHub PR comment carries the identical fence, parsed by
    merge.verdict.read_reviewer_verdict against the list this function
    returns, with authorship verified by this list's own
    comment["user"]["login"] field — never by comment body text.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments"
    try:
        status, body = _github_request("GET", url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read comments for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read comments for PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    if not isinstance(body, list):
        raise GateFactUnavailableError(
            f"comments endpoint returned a non-list body for PR #{pr_number} "
            f"in {owner}/{repo}"
        )
    return body


def fetch_ci_status(
    owner: str,
    repo: str,
    head_sha: str,
    *,
    token: str,
    opener=None,
) -> CiStatusResult:
    """Fetch CI-status evidence at *head_sha*
    (GET /repos/{owner}/{repo}/commits/{sha}/status for the combined
    commit-status state, plus GET .../commits/{sha}/check-runs as evidence
    of GitHub Actions/App check-run activity for this commit).

    Raises merge.errors.GateFactUnavailableError on a network failure or a
    non-200 response from EITHER endpoint — mirrors forgejo_backend.
    fetch_ci_status's fail-closed contract exactly: an unreachable API
    cannot verify a gate fact, so a read failure is never conflated with a
    genuine empty ("no CI wired up") result.

    An API that RESPONDS with an empty combined state and/or zero
    check-runs is NOT a failure — see merge.ci_status's module docstring for
    why that is the real, valid no-runner-by-design signal.

    HEAD-scoping parity note (lr-2d2293, tome #688 both-platform parity):
    unlike Forgejo's repo-global `/actions/tasks` (fixed by dropping it from
    forgejo_backend.fetch_ci_status — see that function's docstring), this
    check-runs endpoint (`/commits/{sha}/check-runs`) IS already HEAD-scoped
    — it reports check runs for this exact commit, not the whole repo — so
    no equivalent transport bug exists on the GitHub side. The gate-level
    fix (merge.ci_status.CiStatusResult.is_empty keying only on
    status_count, never run_count) still applies here for defense in depth:
    run_count is treated as diagnostic-only regardless of platform.

    *head_sha* is validated as a full 40-char hex SHA BEFORE it is
    interpolated into either request path (defense-in-depth: it is always
    API-sourced from get_pr_head_sha() today, never raw argv, but every
    other SHA use in this package — merge.verdict, merge.stale_sha — goes
    through this same sha.py gate before use, and this fetcher should not
    be the one interpolation site that skips it). Raises
    GateFactUnavailableError on a malformed value, matching this
    function's existing fail-closed shape rather than a bare ValueError.
    """
    try:
        head_sha = validate_sha(head_sha, allow_abbreviated=False)
    except InvalidShaError as exc:
        raise GateFactUnavailableError(
            f"cannot read CI status for {owner}/{repo}: malformed head_sha "
            f"{head_sha!r}: {exc}"
        ) from exc

    status_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{head_sha}/status"
    try:
        status_code, status_body = _github_request("GET", status_url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read CI status for {head_sha!r} in {owner}/{repo}: {exc}"
        ) from exc
    if status_code != 200:
        raise GateFactUnavailableError(
            f"cannot read CI status for {head_sha!r} in {owner}/{repo}: HTTP {status_code}"
        )
    combined_state = (
        (status_body.get("state") or "").lower() if isinstance(status_body, dict) else ""
    )
    raw_statuses = status_body.get("statuses") if isinstance(status_body, dict) else None
    status_count = len(raw_statuses) if isinstance(raw_statuses, list) else 0

    check_runs_url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/commits/{head_sha}/check-runs"
    try:
        runs_code, runs_body = _github_request("GET", check_runs_url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read check-runs for {head_sha!r} in {owner}/{repo}: {exc}"
        ) from exc
    if runs_code != 200:
        raise GateFactUnavailableError(
            f"cannot read check-runs for {head_sha!r} in {owner}/{repo}: HTTP {runs_code}"
        )
    run_count = runs_body.get("total_count", 0) if isinstance(runs_body, dict) else 0
    if not isinstance(run_count, int):
        run_count = 0

    raw_states = tuple(
        (s.get("state", "") for s in raw_statuses if isinstance(s, dict))
    ) if isinstance(raw_statuses, list) else ()

    return CiStatusResult(
        combined_state=combined_state,
        status_count=status_count,
        run_count=run_count,
        raw_states=raw_states,
    )


def _first_line(message: str) -> str:
    """The commit SUBJECT is the first line of a (possibly multi-line) commit
    message only — matching merge.title_gate's own single-line PR-title
    check, which never sees a commit/PR body. Mirrors
    forgejo_backend._first_line exactly."""
    return message.split("\n", 1)[0]


def fetch_branch_commit_subjects(
    owner: str,
    repo: str,
    base_branch: str,
    head_sha: str,
    *,
    token: str,
    opener=None,
) -> list[tuple[str, str]]:
    """Fetch the ordered (sha, subject) pairs for every commit the PR
    introduces (base..head), via GET /repos/{owner}/{repo}/compare/
    {base}...{head} — GitHub's compare endpoint carries the same
    {"commits": [{"sha", "commit": {"message"}}, ...]} shape Forgejo's
    compare API does (see forgejo_backend.fetch_branch_commit_subjects's
    docstring for the identical-shape rationale and the "no extra
    round-trip" constraint this satisfies: this call was not previously
    made anywhere else in this module, but it is the SAME compare surface
    the task names as the GitHub analogue of forgejo_backend.py's already-
    fetched compare/{base}...{head} response).

    Each entry's *subject* is the commit's FIRST LINE only (see
    _first_line) -- the merge.commit_subjects gate reused here checks
    exactly what merge.title_gate checks on a PR title: one line, no body.

    Raises merge.errors.GateFactUnavailableError on any non-200 response,
    network failure, or a response body whose 'commits' field is not a
    list — mirrors every other gate-fact fetcher in this module: an
    unreadable gate fact can never be treated as a passing gate.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/compare/{base_branch}...{head_sha}"
    try:
        status, body = _github_request("GET", url, token, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise GateFactUnavailableError(
            f"cannot read branch commit subjects for {owner}/{repo} "
            f"(compare {base_branch}...{head_sha}): {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read branch commit subjects for {owner}/{repo} "
            f"(compare {base_branch}...{head_sha}): HTTP {status}"
        )
    commits = body.get("commits") if isinstance(body, dict) else None
    if not isinstance(commits, list):
        raise GateFactUnavailableError(
            f"compare API returned a non-list 'commits' field for "
            f"{owner}/{repo} (compare {base_branch}...{head_sha})."
        )
    result: list[tuple[str, str]] = []
    for entry in commits:
        if not isinstance(entry, dict):
            continue
        sha = entry.get("sha", "")
        commit_obj = entry.get("commit", {})
        message = commit_obj.get("message", "") if isinstance(commit_obj, dict) else ""
        result.append((sha, _first_line(message)))
    return result


def merge_pr(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    merge_message: str = "",
    merge_method: str = DEFAULT_MERGE_METHOD,
    opener=None,
) -> str | None:
    """Merge a PR via the GitHub API
    (PUT /repos/{owner}/{repo}/pulls/{pr_number}/merge). Raises
    merge.errors.MergeExecutionError on any failure — never reports success
    unless GitHub's own response confirms the merge landed
    ({"merged": true} on a 200).

    GitHub's merge endpoint (unlike Forgejo's) reports failure via TWO
    distinct signals that must BOTH be checked:
      - a non-2xx status  (405 not-mergeable, 409 SHA-changed race, 404
                            not-found, or any other non-2xx)
      - a 200 status whose body carries {"merged": false} — GitHub's
        documented "processed the request but did not merge" shape (e.g. a
        required status check regressed between validation and execution).
        A 200 alone is NEVER treated as success without also checking this
        field — that is the exact class of "trust the status code alone"
        bug this fail-closed gate chain refuses to reproduce.

    Returns the merged commit SHA on success (GitHub's merge response body
    documents a "sha" field on a 200+merged:true response — lr-7c5540: this
    is the ONE piece of information `merge.tree_sync.advance_repo_to_merged_sha`
    needs to advance a local `--repo-path` working tree straight to the exact
    merged commit rather than re-deriving it from a base-branch fetch).
    A malformed/absent "sha" field in an otherwise-successful response
    returns None rather than raising — the merge itself already succeeded by
    that point, and the caller falls back to resolving the merged tip via
    `git fetch` + the base branch, exactly as the Forgejo backend (which has
    no such response field at all) always does.
    """
    payload: dict[str, Any] = {"merge_method": merge_method}
    if merge_message:
        payload["commit_message"] = merge_message

    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}/merge"
    try:
        status, body = _github_request("PUT", url, token, payload, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MergeExecutionError(
            f"GitHub merge FAILED for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc

    if status == 200 and isinstance(body, dict) and body.get("merged") is True:
        merged_sha = body.get("sha")
        return merged_sha if isinstance(merged_sha, str) and merged_sha else None

    server_msg = body.get("message", "") if isinstance(body, dict) else ""
    detail = f": {server_msg}" if server_msg else ""
    if status == 200:
        # 200 but "merged" was not true -- GitHub's "processed, did not
        # merge" shape. Refuse; never trust the status code alone.
        raise MergeExecutionError(
            f"GitHub merge FAILED for PR #{pr_number} in {owner}/{repo}: "
            f"HTTP 200 but response did not confirm merged=true{detail}"
        )
    raise MergeExecutionError(
        f"GitHub merge FAILED for PR #{pr_number} in {owner}/{repo}: "
        f"HTTP {status}{detail}"
    )


def close_pr(
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> None:
    """Close a PR via the GitHub API WITHOUT merging it (lr-2ba5e1).

    PATCH /repos/{owner}/{repo}/pulls/{pull_number} {"state": "closed"} --
    GitHub's PR resource itself supports a state PATCH directly (unlike
    Forgejo, where PR state changes route through the shared issues/<n>
    resource — see forgejo_backend.close_pr's docstring). This is a
    DISTINCT operation from merge_pr: it abandons the PR without landing
    its diff, and mirrors forgejo_backend.close_pr's contract exactly on
    this platform.

    This body is a bare {"state": "closed"} state-change payload -- it is
    constructed here, in-process, and is never routed through any
    --body-stdin/--body-env comment-shape validator (see
    forgejo_backend.close_pr's docstring for why that validator would
    refuse this payload outright).

    Raises merge.errors.MergeExecutionError on any non-2xx response or
    network failure -- never reports success unless GitHub's own response
    confirms the state change. Idempotent: closing an already-closed PR is
    a 200 on GitHub's API, so a retried close call is safe.
    """
    url = f"{GITHUB_API_BASE}/repos/{owner}/{repo}/pulls/{pr_number}"
    try:
        status, body = _github_request("PATCH", url, token, {"state": "closed"}, opener=opener)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise MergeExecutionError(
            f"GitHub close FAILED for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status == 200:
        return
    server_msg = body.get("message", "") if isinstance(body, dict) else ""
    detail = f": {server_msg}" if server_msg else ""
    raise MergeExecutionError(
        f"GitHub close FAILED for PR #{pr_number} in {owner}/{repo}: "
        f"HTTP {status}{detail}"
    )


__all__ = [
    "DEFAULT_MERGE_METHOD",
    "GITHUB_API_BASE",
    "assert_platform_is_github",
    "close_pr",
    "fetch_branch_commit_subjects",
    "fetch_changed_files",
    "fetch_ci_status",
    "fetch_comments",
    "get_pr_head_sha",
    "get_pr_info",
    "get_pr_title",
    "merge_pr",
]
