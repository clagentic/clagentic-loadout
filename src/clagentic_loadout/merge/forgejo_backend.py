"""merge.forgejo_backend — Forgejo PR reads + merge execution.

fetch_ci_status HEAD-scoping fix: lr-2d2293 (see that function's docstring —
GET .../actions/tasks is repo-global, not HEAD-scoped, and is no longer
queried here).

Wave B slice 4 (lr-885f, tome #688). Reuses transport.git_host_api.request()
for every HTTP call (redirect-guarded opener, Content-Type ownership,
write-method fail-on-non-2xx) rather than rolling a second urllib client —
the same reuse discipline push.forgejo_backend and review.forgejo_backend
already established. NEVER re-rolls urllib directly (the recurring
redirect-token-leak class a pre-merge security review already caught once
in this package — see transport.redirect_guard's own docstring).

Merge execution (merge_pr) preserves the reference module's HTTP-level
fidelity (merge_pr / lr-db96):
  - POST .../pulls/{n}/merge {"Do": <merge_method>[, "merge_message_field": ...]}
  - 200/204            -> clean success
  - 405                -> disambiguate: fetch PR info; if head SHA == base
                           SHA, refuse (empty-diff PR); else check whether
                           head is already an ancestor of base via the
                           compare API — if so, treat as already-merged
                           success (idempotent re-run); otherwise refuse
                           with the server's message
  - any other non-2xx  -> refuse with the HTTP status and server message

MERGE_METHOD THREADING (lr-14f704 — fixed a real defect, not a docs-only
note): before this fix, the `Do` field above was hardcoded to the literal
string `"merge"` — merge.verb's own `--merge-method` flag was parsed and
gated the branch commit-subject check (merge.commit_subjects) but was NEVER
forwarded to either backend's merge_pr, so a caller requesting
`--merge-method squash` got a real merge commit anyway (and, worse, the
commit-subject gate had already been skipped on the premise that a squash
was about to happen — see merge.verb's own module docstring and
merge.commit_subjects' for the full defect analysis). `merge_pr` now accepts
`merge_method` (default `DEFAULT_MERGE_METHOD`, `"merge"` — unchanged
default behavior for every caller that never passes the flag) and forwards
it verbatim as the `Do` value, validated against `VALID_DO_VALUES` BEFORE any
HTTP call. Forgejo's `Do` vocabulary (`merge`/`squash`/`rebase`/
`rebase-merge`/`manually-merged`) happens to share the caller-facing
`merge`/`squash`/`rebase` tokens github_backend.merge_pr's `merge_method`
already used — this module does not invent a second vocabulary; a caller
passing one of those three sees IDENTICAL behavior requesting the same
--merge-method value on either platform.

Response-body parsing reuses transport.git_host_api.parse_json_body()
(post-Wave-B extraction, lr-e1f9) rather than a locally-defined
_parse_json — the identical helper push.forgejo_backend needs for its own
write-response parsing, previously duplicated in both modules.
"""

from __future__ import annotations

import json
import re
from typing import Any

from clagentic_loadout.merge.ci_status import CiStatusResult
from clagentic_loadout.merge.errors import (
    GateFactUnavailableError,
    MergeExecutionError,
    PlatformMismatchError,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.sha import InvalidShaError, validate_sha
from clagentic_loadout.transport import git_host_api


def assert_platform_is_forgejo(owner: str, repo: str, *, explicit_platform: str) -> None:
    """Mirror-image guard for the Forgejo merge transport (lr-9c69, mirroring
    review.verb.assert_platform_is_forgejo exactly): fires BEFORE any
    credential mint or API call when the caller's own --platform value says
    the target is NOT Forgejo. Kept alongside github_backend's
    assert_platform_is_github so BOTH directions of the wrong-platform
    failure class fail fast and locally rather than reaching the wrong
    host's API.
    """
    if explicit_platform not in (PLATFORM_GITHUB, PLATFORM_FORGEJO):
        raise PlatformMismatchError(
            f"assert_platform_is_forgejo: unrecognized platform "
            f"{explicit_platform!r} for {owner}/{repo}. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}."
        )
    if explicit_platform != PLATFORM_FORGEJO:
        raise PlatformMismatchError(
            f"WRONG PLATFORM -- {owner}/{repo} PR resolves to platform "
            f"{explicit_platform!r}, but this backend only merges on "
            f"Forgejo. Use the GitHub backend instead. Refusing before "
            f"minting any credential or making any API call."
        )


def get_pr_info(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> dict[str, Any]:
    """Fetch PR metadata (GET .../pulls/{pr_number}).

    Raises merge.errors.GateFactUnavailableError on any non-200 response or
    network failure — a gate that cannot read PR metadata cannot verify any
    downstream gate fact (head SHA, title, base branch), so this fails
    closed rather than returning an empty/partial dict for the caller to
    silently tolerate.
    """
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise GateFactUnavailableError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    return git_host_api.parse_json_body(raw)


def get_pr_head_sha(pr_info: dict[str, Any]) -> str:
    """Extract the head SHA from a get_pr_info() response. Empty string if
    absent (caller decides whether that is fatal for its own gate)."""
    head = pr_info.get("head", {})
    return head.get("sha", "") if isinstance(head, dict) else ""


def get_pr_title(pr_info: dict[str, Any]) -> str:
    """Extract the title from a get_pr_info() response. Empty string if
    absent."""
    return pr_info.get("title", "") or ""


def fetch_changed_files(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> list[str]:
    """Fetch the list of changed filenames (GET .../pulls/{pr}/files).

    Raises merge.errors.GateFactUnavailableError on any non-200 response,
    non-list body, or network failure — the diff-scope gate cannot evaluate
    a cap it cannot see the real file list for.
    """
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/files",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise GateFactUnavailableError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read changed-file list for PR #{pr_number} in "
            f"{owner}/{repo}: HTTP {status}"
        )
    files_body = json.loads(raw.decode("utf-8")) if raw else []
    if not isinstance(files_body, list):
        raise GateFactUnavailableError(
            f"changed-file list endpoint returned a non-list body for PR "
            f"#{pr_number} in {owner}/{repo}"
        )
    return [f.get("filename", "<unknown>") for f in files_body if isinstance(f, dict)]


def fetch_comments(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> list[dict[str, Any]]:
    """Fetch all comments on a PR (GET .../issues/{pr}/comments — issues and
    PRs share the comments endpoint on Forgejo).

    Raises merge.errors.GateFactUnavailableError on any non-200 response,
    non-list body, or network failure — the verdict-fence gate cannot
    enforce a reviewer's verdict it cannot read.
    """
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/issues/{pr_number}/comments",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise GateFactUnavailableError(
            f"cannot read comments for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read comments for PR #{pr_number} in {owner}/{repo}: HTTP {status}"
        )
    if not raw:
        return []
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, list):
        raise GateFactUnavailableError(
            f"comments endpoint returned a non-list body for PR #{pr_number} "
            f"in {owner}/{repo}"
        )
    return parsed


def fetch_ci_status(
    api_base: str,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    token: str,
    opener=None,
) -> CiStatusResult:
    """Fetch CI-status evidence at *head_sha* (GET .../commits/{sha}/status
    for the combined commit-status state — the ONLY HEAD-scoped,
    CI-meaningful signal Forgejo exposes for a PR's head commit).

    lr-2d2293: this fetcher NO LONGER calls GET .../actions/tasks. That
    endpoint is repo-global (it counts mirror-sync tasks and ALL historical
    Actions tasks for the whole repo), not scoped to this PR's HEAD. Keying
    the gate's emptiness decision off it produced a false refusal on
    mirror-runner repos with no CI runner: zero commit statuses at HEAD
    (correctly "no CI ran") but a non-zero repo-global task count made the
    result look non-empty, so the gate fell through to a genuinely-blank
    combined_state and refused a legitimately-mergeable PR — see session
    d5aee241. merge.ci_status.CiStatusResult.run_count stays 0 from this
    backend (no HEAD-scoped runs signal exists on Forgejo); it is present on
    CiStatusResult purely for cross-platform parity with
    github_backend.fetch_ci_status (whose check-runs endpoint IS
    HEAD-scoped and legitimately populates it).

    Raises merge.errors.GateFactUnavailableError on a network failure or a
    non-200 response — mirrors every other gate-fact fetcher in this
    module: an unreachable API cannot verify a gate fact, so this fails
    closed rather than treating an unreadable endpoint as "no CI data"
    (which merge.ci_status would otherwise treat as a pass — see that
    module's docstring for why a genuine read failure must never be
    conflated with a genuine empty result).

    An API that RESPONDS with an empty combined state and zero commit-status
    entries is NOT a failure — that is the real, valid "no CI wired up"
    signal merge.ci_status.check_ci_status treats as a pass. Only an
    unreachable/non-200 endpoint raises here.

    *head_sha* is validated as a full 40-char hex SHA BEFORE it is
    interpolated into the request path (defense-in-depth: it is always
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

    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/commits/{head_sha}/status",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise GateFactUnavailableError(
            f"cannot read CI status for {head_sha!r} in {owner}/{repo}: {exc}"
        ) from exc
    if status != 200:
        raise GateFactUnavailableError(
            f"cannot read CI status for {head_sha!r} in {owner}/{repo}: HTTP {status}"
        )
    status_body = git_host_api.parse_json_body(raw)
    combined_state = (status_body.get("state") or "").lower()
    raw_statuses = status_body.get("statuses")
    status_count = len(raw_statuses) if isinstance(raw_statuses, list) else 0

    raw_states = tuple(
        (s.get("state", "") for s in raw_statuses if isinstance(s, dict))
    ) if isinstance(raw_statuses, list) else ()

    return CiStatusResult(
        combined_state=combined_state,
        status_count=status_count,
        run_count=0,
        raw_states=raw_states,
    )


def _fetch_compare(
    api_base: str,
    owner: str,
    repo: str,
    base_branch: str,
    head_sha: str,
    *,
    token: str,
    opener=None,
) -> dict[str, Any] | None:
    """GET .../compare/{base}...{head}; returns the parsed JSON body, or None
    on any non-200 response or network failure (cannot confirm). Single
    fetch point for the compare endpoint — reused by both
    _is_head_ancestor_of_base (the 405-disambiguation path) and
    fetch_branch_commit_subjects (lr-835c57), so a caller that already needs
    both ahead_by and the commit list never issues the request twice."""
    try:
        status, raw = git_host_api.request(
            api_base,
            "GET",
            f"/api/v1/repos/{owner}/{repo}/compare/{base_branch}...{head_sha}",
            token,
            opener=opener,
        )
    except git_host_api.GitHostApiError:
        return None
    if status != 200:
        return None
    return git_host_api.parse_json_body(raw)


def _is_head_ancestor_of_base(
    api_base: str,
    owner: str,
    repo: str,
    head_sha: str,
    base_branch: str,
    *,
    token: str,
    opener=None,
) -> bool:
    """True when ahead_by == 0 (head introduces no new commits beyond base —
    i.e. already merged). False when the compare fetch itself failed (cannot
    confirm) or ahead_by is absent/nonzero."""
    body = _fetch_compare(
        api_base, owner, repo, base_branch, head_sha, token=token, opener=opener
    )
    if body is None:
        return False
    return body.get("ahead_by", -1) == 0


def _first_line(message: str) -> str:
    """The commit SUBJECT is the first line of a (possibly multi-line) commit
    message only — matching merge.title_gate's own single-line PR-title
    check, which never sees a commit/PR body."""
    return message.split("\n", 1)[0]


def fetch_branch_commit_subjects(
    api_base: str,
    owner: str,
    repo: str,
    base_branch: str,
    head_sha: str,
    *,
    token: str,
    opener=None,
) -> list[tuple[str, str]]:
    """Fetch the ordered (sha, subject) pairs for every commit the PR
    introduces (base..head), via the SAME GET .../compare/{base}...{head}
    call _is_head_ancestor_of_base already makes on the 405-disambiguation
    path — this fetcher does not add a second round-trip against an
    already-fetched-elsewhere endpoint; it is simply the read side of the
    same compare response (lr-835c57's own "without an extra round-trip if
    the info is already fetched" constraint).

    Each entry's *subject* is the commit's FIRST LINE only (see
    _first_line) -- the merge.commit_subjects gate reused here checks
    exactly what merge.title_gate checks on a PR title: one line, no body.

    Raises merge.errors.GateFactUnavailableError on any non-200 response or
    network failure, or a response body whose 'commits' field is not a
    list — mirrors every other gate-fact fetcher in this module: an
    unreadable gate fact can never be treated as a passing gate.
    """
    body = _fetch_compare(
        api_base, owner, repo, base_branch, head_sha, token=token, opener=opener
    )
    if body is None:
        raise GateFactUnavailableError(
            f"cannot read branch commit subjects for {owner}/{repo} "
            f"(compare {base_branch}...{head_sha}): compare API unreachable "
            f"or returned a non-200 response."
        )
    commits = body.get("commits")
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


#: Default Forgejo merge method (the `Do` field on the merge API). "merge" (a
#: real merge commit) is the default for the same reason
#: github_backend.DEFAULT_MERGE_METHOD is: it is the only method that
#: preserves the PR title verbatim in a way merge.title_gate's Conventional-
#: Commits enforcement is meaningful against.
DEFAULT_MERGE_METHOD = "merge"

#: Forgejo's merge API accepts these literal `Do` values (Gitea/Forgejo
#: "merge style"): a real merge commit, a rebase that fast-forwards without
#: its own merge commit, a rebase that DOES add a trailing merge commit, a
#: squash into one commit, or a manual out-of-band "I merged this myself,
#: just mark it" record. `merge`/`squash`/`rebase` are the exact same three
#: caller-facing tokens github_backend.merge_pr's merge_method accepts and
#: commit_subjects.REAL_MERGE_METHOD/merge.verb's --merge-method already use
#: -- no separate Forgejo-specific vocabulary is exposed to the caller for
#: those three. "rebase-merge" and "manually-merged" are Forgejo-only shapes
#: with no GitHub equivalent; a caller wanting one of those passes it
#: through verbatim (validated against this exact set, never silently
#: coerced to the default).
VALID_DO_VALUES = frozenset({"merge", "squash", "rebase", "rebase-merge", "manually-merged"})


def merge_pr(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    merge_message: str = "",
    merge_method: str = DEFAULT_MERGE_METHOD,
    opener=None,
) -> str | None:
    """Merge a PR via the Forgejo API. Raises merge.errors.MergeExecutionError
    on any failure — never reports success unless the merge actually landed
    or is confirmed already-landed (idempotent re-run).

    *merge_method* is passed straight through as the Forgejo merge API's own
    `Do` field (lr-14f704 — see this module's own history: before this fix,
    the `Do` value was hardcoded to `"merge"` regardless of what a caller
    requested, the same defect merge.github_backend.merge_pr's
    merge_method parameter had already been threading correctly). Must be one
    of VALID_DO_VALUES; raises MergeExecutionError (a caller-input-shape
    problem discovered here, not a server refusal, but this module's only
    exception type for merge_pr) on any other value, BEFORE making any HTTP
    call, rather than sending an unrecognized `Do` value to the API and
    surfacing whatever error Forgejo itself returns for it.

    405-case disambiguation (preserved from the reference module):
      - head SHA == base SHA (empty-diff PR)   -> refuse
      - head is an ancestor of base (already merged, e.g. a re-run) -> success
      - anything else (genuine refusal)         -> refuse with server message

    NOTE on transport.git_host_api reuse: git_host_api.request() fails fast
    (raises GitHostApiError) on ANY non-2xx response for a write method (POST
    here), so the HTTP status code is not returned to the caller on failure
    — it is only recoverable from the exception's message text. The 405
    status is extracted from that message (git_host_api's own message shape
    is "git-host API {method} {path} returned HTTP {code}: {body}") so the
    disambiguation below stays reachable without loosening git_host_api's
    own fail-fast contract for every OTHER write-method caller in this
    package.

    Always returns None on success (lr-7c5540: unlike GitHub's merge
    endpoint, Forgejo's `POST .../pulls/{n}/merge` responds 200/204 with an
    EMPTY body on success — there is no documented response field carrying
    the merged commit SHA to return here). A caller needing the merged SHA
    (merge.tree_sync.advance_repo_to_merged_sha, wired from merge.verb._run)
    falls back to resolving it via `git fetch` + the PR's base branch, which
    works identically regardless of which backend merged the PR.
    """
    if merge_method not in VALID_DO_VALUES:
        raise MergeExecutionError(
            f"Forgejo merge FAILED for PR #{pr_number} in {owner}/{repo}: "
            f"merge_method {merge_method!r} is not a recognized Forgejo `Do` "
            f"value. Expected one of {sorted(VALID_DO_VALUES)!r}."
        )
    payload: dict[str, Any] = {"Do": merge_method}
    if merge_message:
        payload["merge_message_field"] = merge_message
    body_bytes = json.dumps(payload).encode("utf-8")

    try:
        git_host_api.request(
            api_base,
            "POST",
            f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}/merge",
            token,
            body_bytes=body_bytes,
            opener=opener,
        )
        return None  # 2xx — clean success; no SHA in Forgejo's response body
    except git_host_api.GitHostApiError as exc:
        status = _extract_http_status(str(exc))
        if status != 405:
            raise MergeExecutionError(
                f"Forgejo merge FAILED for PR #{pr_number} in {owner}/{repo}: {exc}"
            ) from exc

    # 405: disambiguate (already-merged idempotent re-run vs. genuine refusal
    # vs. empty-diff PR) via a fresh PR-info read.
    pr_info = get_pr_info(api_base, owner, repo, pr_number, token=token, opener=opener)
    head_sha = get_pr_head_sha(pr_info)
    base = pr_info.get("base", {})
    base_branch = (base.get("label") or base.get("ref") or "") if isinstance(base, dict) else ""
    base_sha = base.get("sha", "") if isinstance(base, dict) else ""

    if head_sha and base_branch:
        if head_sha == base_sha:
            raise MergeExecutionError(
                f"405: PR #{pr_number} in {owner}/{repo} has no commits to "
                f"merge (head SHA == base SHA); this is an empty-diff PR "
                f"that cannot be landed."
            )
        if _is_head_ancestor_of_base(
            api_base, owner, repo, head_sha, base_branch, token=token, opener=opener
        ):
            return None  # already merged (idempotent re-run) — success

    raise MergeExecutionError(
        f"405: Forgejo refused merge of PR #{pr_number} in {owner}/{repo} "
        f"(not mergeable — conflicts or a state the platform will not "
        f"merge)."
    )


def close_pr(
    api_base: str,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    token: str,
    opener=None,
) -> None:
    """Close a PR via the Forgejo API WITHOUT merging it (lr-2ba5e1).

    PATCH .../issues/{pr_number} {"state": "closed"} -- Forgejo treats a PR
    as an issue for state-change purposes (the same issues/<n> resource the
    comments endpoints already address in this module), so this is a PATCH
    to the issue resource, not the pulls resource. This is a DISTINCT
    operation from merge_pr: it abandons the PR without landing its diff,
    and is the ONLY sanctioned loadout write path that can retire a PR this
    way (see this module's own docstring cross-reference and
    merge.close_verb, the CLI wrapping this function).

    This body is a bare {"state": "closed"} state-change payload -- it MUST
    NOT be routed through transport.git_host_api.validate_body_stdin_content
    (that validator hardcodes the comment-post {"body": ...} shape and would
    refuse this payload outright, EXIT_BODY_STDIN_EMPTY, since it carries no
    'body' key at all). merge.close_verb constructs this payload directly,
    in-process, and calls this function -- there is no --body-stdin/
    --body-env path involved anywhere in the close flow.

    Raises merge.errors.MergeExecutionError on any non-2xx response or
    network failure -- never reports success unless the API confirmed the
    state change. Idempotent: closing an already-closed PR is treated as
    Forgejo itself treats it (typically another 2xx), so a retried close
    call is safe.
    """
    payload = {"state": "closed"}
    body_bytes = json.dumps(payload).encode("utf-8")
    try:
        git_host_api.request(
            api_base,
            "PATCH",
            f"/api/v1/repos/{owner}/{repo}/issues/{pr_number}",
            token,
            body_bytes=body_bytes,
            opener=opener,
        )
    except git_host_api.GitHostApiError as exc:
        raise MergeExecutionError(
            f"Forgejo close FAILED for PR #{pr_number} in {owner}/{repo}: {exc}"
        ) from exc


def _extract_http_status(git_host_api_error_message: str) -> int | None:
    """Best-effort extraction of the HTTP status code from a
    transport.git_host_api.GitHostApiError message of the shape
    "git-host API POST <path> returned HTTP <code>: <body>". Returns None if
    the message does not match that shape (e.g. a network/connection
    failure, which carries no status code at all)."""
    match = re.search(r"returned HTTP (\d+)", git_host_api_error_message)
    return int(match.group(1)) if match else None


__all__ = [
    "DEFAULT_MERGE_METHOD",
    "VALID_DO_VALUES",
    "assert_platform_is_forgejo",
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
