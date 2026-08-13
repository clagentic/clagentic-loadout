"""merge.verb — the merge-gate CLI: the full gate chain, then merge.

Wave B slice 4 (lr-885f, tome #688) + slice 4b's CLI wiring (lr-9c69). Ported
from the reference merge gate; the source module stays primary until its
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

THIS IS THE LOAD-BEARING RELEASE GATE — the code that decides whether
anything lands on main. Every gate link below is ALL FAIL-CLOSED; do not
weaken any of them. The platform choice (--platform, mandatory) affects
ONLY which backend fetches gate facts and executes the merge — the gate
chain itself (steps 1-7 below) runs IDENTICALLY regardless of platform.

THE GATE CHAIN (in enforcement order, mirroring the reference module's own
documented step ordering):
  0. Repo-path/slug consistency (merge.repo_path_consistency, lr-4522a3) —
     when --repo-path points at a real, parseable git tree, its OWN origin
     remote must name the same owner/repo as --repo, or the merge refuses
     naming both values. Runs before step 1, so a caller-side argument
     defect (the wrong local tree for the requested slug) never reaches a
     credential mint and surfaces as a confusing platform-API rejection.
     Compares against the tree's remote, never its directory name — see
     that module's own docstring for why the '.github' org-profile shape
     (directory basename diverges from slug by design) is correctly never
     flagged.
  1. Namespace guard (merge.verb, reusing push.namespace_guard verbatim —
     the same config-driven allowed-namespace seam, no second
     implementation). Runs first: a refusal here must never mint a
     credential or make a network call.
  2. Merge-authority check (merge.authority) — FAIL-CLOSED provider seam.
     Runs before any credential is minted for an out-of-scope/unauthorized
     request.
  3. Platform guard + credential resolution (transport.credential_provider)
     — the platform guard (assert_platform_is_forgejo /
     assert_platform_is_github, BOTH directions, fail-closed) runs FIRST,
     then the SAME credential seam every other loadout verb resolves a
     git-host token through. Runs only after 1-2 pass, so an unauthorized or
     wrong-platform request never causes a token resolution attempt.
  4. Stale-SHA refusal (merge.stale_sha) — compares --expected-head-sha
     against the PR's LIVE current head, read fresh from the resolved
     backend's own platform API.
  5. Reviewer-verdict fences (merge.verdict) — for each required reviewer:
     locate their latest PR comment (authorship verified by user.login,
     never comment body text), parse the fenced ```review-result``` block,
     refuse on missing/malformed/stale/blocking/role-mismatched. The
     role-mismatch check (lr-23fe19) is defense-in-depth ON TOP OF the
     user.login binding: it additionally asserts the block's own
     self-declared 'reviewer' field matches the required-reviewer name this
     login slot is for, catching a right-App/wrong-content verdict (see
     merge.verdict's module docstring and merge.errors.
     VerdictRoleMismatchError). Identical on both platforms — see
     merge.github_backend's module docstring. ENFORCED BY DEFAULT (lr-5260f9
     — merge.post_merge_config.resolve_enforce_single_verdict_fence): a
     selected comment body carrying MORE THAN ONE fenced block is ALSO a
     refusal (VerdictMalformedError) rather than silently parsing the last
     one. A repo with legacy multi-fence comments opts OUT explicitly via
     `merge: enforce_single_verdict_fence: false` — see that resolver's own
     docstring for the full trade-off.
  6. Diff-scope cap (merge.diff_scope) — refuse a PR whose changed-file
     count exceeds the configured limit.
  7. PR-title gate (merge.title_gate) — Conventional Commits grammar, with a
     --skip-title-check bypass (logged when used).
  7b. Branch commit-subject gate (merge.commit_subjects, lr-835c57) — on a
     RESOLVED --merge-method='merge' (real, non-squash) repo, validates
     EACH branch commit subject (base..head) against the SAME Conventional
     Commits grammar step 7 already applies to the PR title, since
     semantic-release parses individual branch commit subjects (not the
     promoted title) on a real merge. A no-op on any other --merge-method
     value (squash/rebase rewrite the resulting commit subject FROM the
     already-gated PR title). BLOCKS, never rewrites history — a
     --skip-commit-check bypass mirrors --skip-title-check exactly (logged
     when used). See that module's docstring for the full rationale.
  8. CI-status gate (merge.ci_status, lr-afba CI-status-gate slice) — reads
     CI evidence at the PR's HEAD from the resolved backend. AN EMPTY
     RESULT (zero commit-status entries AND zero check/workflow runs) IS AN
     EXPLICIT PASS, not a fall-through: many repos (this one included, by
     design — lr-368c, "runner explicitly out of scope") have no CI runner
     wired up at all, and a gate that fails closed on "no CI data" would
     falsely refuse every merge in such a repo. A NON-EMPTY result gates on
     the real combined state: "success" passes; any other non-empty state
     (failure, error, pending, or an unrecognized value) refuses, reporting
     the actual state and evidence counts seen — never a collapsed guess.
     See merge.ci_status's module docstring for the full decision and
     merge.forgejo_backend.fetch_ci_status / merge.github_backend.
     fetch_ci_status for the fail-closed fetch contract (unreachable/non-200
     still raises GateFactUnavailableError exactly like every other
     gate-fact fetch below).
  9. Only after ALL of the above pass: execute the merge via the resolved
     backend's merge_pr (Forgejo or GitHub, both via a redirect-hardened
     transport), passing args.merge_method THROUGH to the backend (lr-14f704
     — before this fix, --merge-method was parsed and gated step 7b above but
     was NEVER forwarded to either backend's merge_pr, so a caller requesting
     --merge-method squash silently got a real merge commit anyway; see
     merge.forgejo_backend's module docstring, "MERGE_METHOD THREADING", for
     the full defect history). On ANY gate failure above: refuse and exit
     non-zero — never merge. The GitHub path's 200+merged:false
     disambiguation (never trust the status code alone — see
     merge.github_backend.merge_pr) is reached unchanged through this same
     call site. When --repo-path is given, step 10 below FIRST fetches the
     merged commit into that tree's local object database (merge.tree_sync,
     lr-7c5540 — a CHECKOUT only when post_merge_steps will actually run
     this invocation, see lr-173768), THEN (lr-14f704 item 3) reads back
     that landed commit's ACTUAL parent count and compares it against what
     the requested --merge-method predicts — a mismatch is logged loudly
     (WARN by default; a hard refusal, EXIT_MERGE_SHAPE_MISMATCH, only for a
     repo that opts in via `merge: enforce_merge_shape: true` — see
     merge.merge_shape's own docstring for the full trade-off) before any
     post_merge_steps entry runs — see "WORKING-TREE SYNC BEFORE POST-MERGE
     STEPS" further down.

PLATFORM DISPATCH (lr-9c69): mirrors review.verb's platform-parameterized
dispatch shape exactly (see that module's docstring and its build_backend).
_resolve_backend below runs the platform guard BEFORE constructing either
backend or resolving any credential — there is no call path that reaches
token resolution before the platform has been confirmed to match the
resolved backend, for either direction.

SCOPE (task lr-885f, extended by lr-5375 + lr-9c69): the Forgejo backend
(lr-885f) and GitHub backend (lr-5375, merge.github_backend) both existed
before this CLI wiring landed; lr-9c69 is the completion that makes the
GitHub path CLI-reachable. GitHub's review-gate mechanism was a materially
different port (GitHub review *state* is NOT used — the same fenced
```review-result``` comment contract is reused unchanged, see
merge.github_backend's docstring) from a separate migration slice, not a
mechanical restatement of the Forgejo slice.

IDENTITY / SEAM STRIP FROM THE SOURCE MODULE (full inventory in the PR body):
  1. The hardcoded 'clagentic' namespace check is now push.namespace_guard's
     config-driven allowed-namespace seam (reused, not reimplemented).
  2. The hardcoded release-gate-role caller default and the directory-
     service-specific merge-authority check are now merge.authority's
     provider seam: which ROLE may authorize a merge is config (--role / an
     AuthorityProvider), never a baked identity.
  3. Credential mint (an OpenBao self-fetch + a gatekeeper-CLI subprocess
     call) is now the SAME transport.credential_provider seam every other
     loadout verb uses — a TokenProvider resolves a token for a
     caller-supplied role, never a hardcoded broker client or fixed
     binary/config path.
  4. An operator-specific Forgejo host is never baked in — the API base is
     CLI input (--git-host-base-url) or an env var, matching
     transport.git_host_api's own resolution.
  5. The gate-note post-merge comment and the LORE task-signal comment (both
     lore-coupled, best-effort audit trail features in the reference module)
     are out of this package's task boundary (CLAUDE.md hard rule 6a: lore
     never appears in product code) and are not ported.
  6. The reference module's per-repo pre_checks config loading (a
     reference-implementation-specific config-file convention) is not part
     of the gate chain itself and is not ported in this slice.

POST-MERGE STEPS (lr-77d6, added after the identity-strip inventory above
was written): the reference module's post_merge_steps MECHANISM (not its
config file, not its identity) IS ported, as merge.post_merge — an ordered,
config-driven list of commands run in the repo's own working tree ONLY after
step 8 below has ACTUALLY merged the PR, never on any refusal path. Wired
into this module via --repo-path (an OPTIONAL override the caller supplies
whenever a local tree exists — loadout has no project registry of its own to
derive one from, see the "ABSENT --repo-path IS NEVER A SILENT SKIP"
paragraph below) and --skip-post-merge (explicit opt-out, logged when used).
Steps are read from the repo's OWN `.clagentic/loadout/config.yaml` `merge:`
section (merge.post_merge_config) — see that module's docstring for why this
one config surface is repo-local by design, unlike the credentials tier.

ABSENT --repo-path IS NEVER A SILENT SKIP (lr-ac5c8a): before lr-ac5c8a,
omitting --repo-path silently downgraded a merge to "no post-merge run
attempted", exit 0, with no warning — even for a repo whose OWN committed
config declares post_merge_steps. That recurred three times across three
repos (lr-5854ff, lr-4e6f31, clagentic-console PR #365/#366) precisely
because a missing flag and an intentional skip were indistinguishable.
_run now REQUIRES one of three explicit shapes whenever --repo-path is
omitted, checked BEFORE any credential mint or network call (same fail-fast
placement as the owner/repo parse): --repo-path itself (a tree exists, steps
may run), --no-post-merge-tree (an explicit acknowledgment that this
invocation genuinely has no local tree, e.g. a bare API-only merge), or
--skip-post-merge (skip regardless of tree). Omitting --repo-path with
NEITHER of the other two flags is now MergeUsageError -> EXIT_USAGE. Which
of these three a caller should pass is NOT resolved here: deriving a
project's working-tree root from a project/task registry is a DISPATCHER
concern (this package is orchestration-agnostic — see this module's own
docstring point 2 above and this repo's CLAUDE.md rule 2/6a); loadout's
contract is only that the choice must be explicit, never implicit.

WORKING-TREE SYNC BEFORE POST-MERGE STEPS, AND LANDING ON THE BASE BRANCH
AFTER (lr-7c5540, extended lr-d95cdb, re-scoped lr-173768): step 9's
`backend.merge_pr` is a server-side API merge — it never advances the local
`--repo-path` tree. Whenever `--repo-path` is given and the repo's own
`merge.sync_tree_after_merge` config key (default True) has not opted out,
step 10 below fetches the merged commit into that tree's local object
database — but a `git checkout` (detached or otherwise) is performed ONLY
when at least one `post_merge_steps` entry will actually run this
invocation (a non-empty, non-`--skip-post-merge`-bypassed list): a step that
packages/installs the repo (e.g. `scripts/install.sh` reading
pyproject.toml/package source off disk) has no way to see "what merged"
other than a real, populated checkout, via
merge.tree_sync.advance_repo_to_merged_sha (fetch + detached checkout, with
a post-checkout `git rev-parse HEAD` readback verified against the merge
API's own reported SHA when one was returned — see that module's docstring
for why only that path can independently confirm the landed SHA). When NO
steps will run (none configured, or `--skip-post-merge`), step 10 instead
calls merge.tree_sync.fetch_merged_sha_object -- the SAME fetch and SHA
verification, but with NO checkout at all: the working tree, index, and
HEAD are left exactly as the caller had them. This still keeps the merge-
shape readback (below) and the merge-completion attestation's SHA claim
independently confirmed against a real fetched object in EVERY case, while
eliminating the unsignaled working-tree mutation on every merge whose repo
either has no post_merge_steps configured or is invoked with
--skip-post-merge — the documented cross-agent contention source on a host
where multiple agents share one on-disk checkout (see merge.tree_sync's own
module docstring, "NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE
TREE", for the full rationale). See that module's docstring for the
per-backend SHA-resolution trade-off. Any failure to verify the tree/object
landed on the merged commit is EXIT_POST_MERGE_FAILED, never a silent run
against the stale ref.

AFTER post_merge_steps run (only reachable when steps_will_run above was
True — see below), merge.tree_sync.land_on_base_branch moves the tree OFF
the detached HEAD onto the PR's base branch, repointed (`git checkout -B`,
never a merge/rebase) at the SAME already-verified landed SHA -- so the tree
is left positioned exactly where the NEXT dispatch into this repo needs it:
on an updated local base branch, not detached. When steps_will_run is False
(no checkout ever happened), land_on_base_branch is skipped entirely too --
there is no detached HEAD to move off of, and repointing the caller's branch
ref with nothing having read the tree would be exactly the same class of
unsignaled mutation this task removes. `--skip-post-merge` therefore skips
BOTH the configured steps AND any checkout that would otherwise exist only
to serve them — it no longer forces a sync-then-do-nothing checkout the way
it did between lr-d95cdb and lr-173768; a repo that wants to suppress even
the FETCH (not just the checkout) sets `merge.sync_tree_after_merge: false`
in its own config instead.

CONFIG-ROOT VS GIT-TREE-ROOT (lr-93d718): --repo-path is not always a git
working tree. A wrapper-layout repo keeps `.clagentic/loadout/config.yaml`
at a wrapper directory (alongside other non-git tooling state) while its
actual `.git` lives at a subdirectory of that wrapper -- no single
--repo-path value satisfied BOTH tree_sync (requires a git working tree) and
config discovery (requires the config-bearing root) for that layout before
this task; passing the wrapper failed tree_sync outright, passing the
subdirectory silently loaded zero post_merge_steps. Step 10 now resolves an
OPTIONAL `merge.git_working_tree` key (merge.post_merge_config.
resolve_git_working_tree) from the repo's own config -- when present, a path
relative to the config root naming the actual git tree, which becomes
tree_sync's target while config discovery (load_post_merge_steps) keeps
reading from --repo-path exactly as always. Absent (the common, flat-layout
case): tree_sync targets --repo-path unchanged, identical to pre-lr-93d718
behavior. See merge.post_merge_config's module docstring for the full
"CONFIG-ROOT VS GIT-TREE-ROOT" rationale and the upward-.git-search
alternative that was considered and rejected in favor of this explicit knob.

DEPLOYMENT ENV-OVERRIDE SEAM (lr-52d7): a step's subprocess environment is
also layered with merge.post_merge_config.resolve_env_overrides() — a
deployment-owned, non-repo-local tier (env vars named
CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME>, plus the user-level config file's
post_merge_env: section) for injecting a machine-specific value (e.g. HOME
in an isolated-HOME spawn harness) into every step without ever hardcoding
it into this repo's own committed .clagentic/loadout/config.yaml. A step's
own inline VAR=VALUE prefix still wins over this tier for the same name. See
that module's docstring for the full design.

PRESERVED (load-bearing, not identity — see each gate module's own
docstring for its individual fail-closed contract):
  - Namespace guard, merge-authority check, stale-SHA refusal, verdict-fence
    parse+assert (including the same-line-tag fence requirement and the
    authorship-by-user.login rule), diff-scope cap, PR-title gate.
  - The Forgejo merge_pr HTTP-level fidelity (200/204 success, 405
    three-case disambiguation, any-other-non-2xx refusal) — see
    merge.forgejo_backend.merge_pr.
  - Token never touches os.environ of this process, never appears in logs.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error

from clagentic_loadout._version import get_version
from clagentic_loadout.merge import (
    ci_status,
    commit_subjects,
    diff_scope,
    forgejo_backend,
    github_backend,
    title_gate,
    verdict,
)
from clagentic_loadout.merge.attestation import build_attestation_body
from clagentic_loadout.merge.authority import (
    AuthorityProvider,
    StaticRoleAuthorityProvider,
    check_authority,
)
from clagentic_loadout.merge.errors import (
    AuthorityDeniedError,
    CiStatusFailedError,
    CommitSubjectInvalidError,
    DiffScopeExceededError,
    GateFactUnavailableError,
    MergeExecutionError,
    MergeUsageError,
    PlatformMismatchError,
    StaleHeadShaError,
    TitleInvalidError,
    VerdictBlockingError,
    VerdictMalformedError,
    VerdictMissingError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)
from clagentic_loadout.merge.merge_readback import verify_merge_landed
from clagentic_loadout.merge.merge_shape import (
    MergeShapeCheckError,
    check_merge_shape,
    format_mismatch_message,
)
from clagentic_loadout.merge.post_merge import (
    EXIT_POST_MERGE_FAILED as _EXIT_POST_MERGE_FAILED,
    PostMergeConfigError,
    PostMergeLivenessError,
    PostMergeStepFailedError,
    PostMergeStepTimeoutError,
    run_post_merge_steps,
)
from clagentic_loadout.merge.post_merge_config import (
    DEFAULT_CONFIG_RELATIVE_PATH as DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH,
    load_post_merge_steps,
    resolve_enforce_merge_shape,
    resolve_enforce_single_verdict_fence,
    resolve_env_overrides,
    resolve_git_working_tree,
    resolve_post_merge_step_timeout_seconds,
    resolve_sync_tree_after_merge,
)
from clagentic_loadout.merge.repo_path_consistency import assert_repo_path_consistent
from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)
from clagentic_loadout.merge.stale_sha import check_stale_head_sha
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
    land_on_base_branch,
    resolve_base_branch,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.task_id_guard import (
    TaskIdGuardViolation,
    load_task_id_guard_config,
)
from clagentic_loadout.push.errors import NamespaceDeniedError, RemoteResolutionError
from clagentic_loadout.push.git_coords import parse_owner_repo
from clagentic_loadout.push.issue_link import parse_closes_issue_number
from clagentic_loadout.push.namespace_guard import (
    ALLOWED_NAMESPACES_ENV_VAR,
    check_namespace_allowed,
    resolve_allowed_namespaces,
)
from clagentic_loadout.review.errors import ReviewPostError, ReviewVerifyError
from clagentic_loadout.review.forgejo_backend import post_and_verify_comment as _forgejo_post_and_verify_comment
from clagentic_loadout.review.github_backend import post_and_verify_review as _github_post_and_verify_review
from clagentic_loadout.transport.attestation import (
    AttestationError,
    resolve_identity as _resolve_identity,
)
from clagentic_loadout.transport.caller_binding import CallerBindingError, bind_caller
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token as _resolve_token,
)
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
    _resolve_git_host_base,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.transport.readback_envelope import READBACK_ENVELOPE_KEY

# ---------------------------------------------------------------------------
# Exit codes — one reserved range for the merge verb.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_WRONG_PLATFORM = 4
EXIT_NAMESPACE_DENIED = 20
EXIT_AUTHORITY_DENIED = 21
EXIT_STALE_HEAD_SHA = 23
EXIT_GATE_RESULT_BLOCKED = 24
EXIT_PR_TITLE_INVALID = 25
EXIT_MERGE_FAILED = 26
EXIT_GATE_FACT_UNAVAILABLE = 27
EXIT_POST_MERGE_FAILED = _EXIT_POST_MERGE_FAILED
EXIT_CI_STATUS_FAILED = 29
EXIT_COMMIT_SUBJECT_INVALID = 30
EXIT_MERGE_SHAPE_MISMATCH = 31
#: A fresh post-merge readback (merge.merge_readback.verify_merge_landed) did
#: NOT confirm the merge landed (lr-361de3): the mutating merge_pr call
#: itself returned success, but a SEPARATE GET re-reading the PR afterward
#: did not find merged=True with a non-empty merge_commit_sha. Distinct from
#: EXIT_MERGE_FAILED (the merge_pr call itself failed) so a caller can tell
#: "the merge API call succeeded but could not be independently confirmed"
#: apart from "the merge API call itself refused." FAIL-CLOSED, matching
#: --verify-comment's EXIT_VERIFY_FAILED precedent: a caller MUST NOT report
#: success when this fires.
EXIT_MERGE_READBACK_FAILED = 32
#: An EXPLICIT --role value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved
#: (transport.caller_binding.bind_caller, lr-c75c9a -- the same fail-closed
#: binding transport.git_host_api's EXIT_CALLER_INVOKER_MISMATCH already
#: enforced; this verb now enforces it too). FAILS CLOSED BEFORE ANY I/O --
#: no token mint, no authority check, no merge is ever attempted. An
#: OMITTED --role never triggers this (see bind_caller's own docstring) --
#: it is unchanged, existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 33
#: A branch commit subject introduced by the PR matched the deployment's own
#: configured task_id_guard_pattern in mode="block"
#: (task_id_guard.TaskIdGuardViolation, lr-4005f5) -- ONLY reachable on a
#: RESOLVED --merge-method='merge' repo (mirrors EXIT_COMMIT_SUBJECT_INVALID's
#: own merge_method scoping exactly -- see merge.commit_subjects' own
#: docstring) AND only once a repo has configured
#: `push: task_id_guard_pattern:` (no configured pattern is a strict no-op;
#: default mode once a pattern IS set is "block"). See docs/verbs.md's
#: `loadout-merge` section for the full contract.
EXIT_TASK_ID_GUARD_VIOLATION = 36

#: Reviewers required to post a clean verdict before a merge is authorized.
#: A caller wanting a different reviewer roster passes --required-reviewer
#: (repeatable), overriding this default entirely — this is not a partial
#: override, matching push.namespace_guard's own "explicit always wins"
#: precedence rule.
DEFAULT_REQUIRED_REVIEWERS: tuple[str, ...] = ()


class MergeVerbError(Exception):
    """Raised for any merge-gate failure that should terminate the process
    with a specific exit code. Carries the intended exit code as `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise MergeVerbError(message, code)


# ---------------------------------------------------------------------------
# Platform-parameterized backend dispatch (lr-9c69)
#
# _ForgejoMergeBackend / _GithubMergeBackend adapt the two platform backend
# modules' differing gate-fact/merge_pr signatures (forgejo_backend takes an
# explicit api_base on every call; github_backend's endpoints are pinned to
# GITHUB_API_BASE and take none) onto ONE uniform shape, so _run's gate chain
# below drives either platform through the exact same call sites. Neither
# adapter re-implements any HTTP or gate logic -- both are thin pass-throughs
# to their respective backend module's own functions.
# ---------------------------------------------------------------------------


class _ForgejoMergeBackend:
    """Uniform merge-backend adapter over merge.forgejo_backend."""

    def __init__(self, *, git_host_base: str, token: str, opener) -> None:
        self._git_host_base = git_host_base
        self._token = token
        self._opener = opener

    def post_merge_attestation(self, owner: str, repo: str, pr_number: int, *, body: str) -> "str | int":
        """Post the merge-completion attestation via the SAME POST-and-verify
        comment transport review.forgejo_backend already carries (reused, not
        reimplemented -- see that module's post_and_verify_comment). Returns
        the verified comment id. Raises ReviewPostError/ReviewVerifyError on
        any failure -- the caller (merge.verb._run) wraps this call fail-open,
        since the merge itself has already succeeded by the time this fires.
        """
        verified = _forgejo_post_and_verify_comment(
            self._git_host_base, self._token, owner, repo, pr_number, body, opener=self._opener,
        )
        return verified.id

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict:
        return forgejo_backend.get_pr_info(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_comments(self, owner: str, repo: str, pr_number: int) -> list:
        return forgejo_backend.fetch_comments(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_changed_files(self, owner: str, repo: str, pr_number: int) -> list:
        return forgejo_backend.fetch_changed_files(
            self._git_host_base, owner, repo, pr_number, token=self._token, opener=self._opener
        )

    def fetch_ci_status(self, owner: str, repo: str, head_sha: str) -> ci_status.CiStatusResult:
        return forgejo_backend.fetch_ci_status(
            self._git_host_base, owner, repo, head_sha, token=self._token, opener=self._opener
        )

    def fetch_branch_commit_subjects(
        self, owner: str, repo: str, base_branch: str, head_sha: str
    ) -> list[tuple[str, str]]:
        return forgejo_backend.fetch_branch_commit_subjects(
            self._git_host_base, owner, repo, base_branch, head_sha,
            token=self._token, opener=self._opener,
        )

    def merge_pr(
        self, owner: str, repo: str, pr_number: int, *,
        merge_message: str, merge_title: str, merge_method: str,
    ) -> str | None:
        return forgejo_backend.merge_pr(
            self._git_host_base, owner, repo, pr_number,
            token=self._token, merge_message=merge_message, merge_title=merge_title,
            merge_method=merge_method, opener=self._opener,
        )


class _GithubMergeBackend:
    """Uniform merge-backend adapter over merge.github_backend."""

    def __init__(self, *, token: str, opener, caller: str | None = None) -> None:
        self._token = token
        self._opener = opener
        self._caller = caller

    def post_merge_attestation(self, owner: str, repo: str, pr_number: int, *, body: str) -> "str | int":
        """Post the merge-completion attestation via the SAME POST-and-verify
        review transport review.github_backend already carries (reused, not
        reimplemented -- see that module's post_and_verify_review). `caller`
        (the merge role resolved by _resolve_backend) is forwarded for the
        SAME app-slug identity-resolution seam review.github_backend.
        resolve_own_login already consults for every other GitHub post in
        this package. Raises ReviewPostError/ReviewVerifyError on any
        failure -- the caller (merge.verb._run) wraps this call fail-open,
        since the merge itself has already succeeded by the time this fires.
        """
        verified = _github_post_and_verify_review(
            owner, repo, pr_number, body, self._token, caller=self._caller, opener=self._opener,
        )
        return verified.id

    def get_pr_info(self, owner: str, repo: str, pr_number: int) -> dict:
        return github_backend.get_pr_info(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_comments(self, owner: str, repo: str, pr_number: int) -> list:
        return github_backend.fetch_comments(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_changed_files(self, owner: str, repo: str, pr_number: int) -> list:
        return github_backend.fetch_changed_files(owner, repo, pr_number, token=self._token, opener=self._opener)

    def fetch_ci_status(self, owner: str, repo: str, head_sha: str) -> ci_status.CiStatusResult:
        return github_backend.fetch_ci_status(owner, repo, head_sha, token=self._token, opener=self._opener)

    def fetch_branch_commit_subjects(
        self, owner: str, repo: str, base_branch: str, head_sha: str
    ) -> list[tuple[str, str]]:
        return github_backend.fetch_branch_commit_subjects(
            owner, repo, base_branch, head_sha, token=self._token, opener=self._opener,
        )

    def merge_pr(
        self, owner: str, repo: str, pr_number: int, *,
        merge_message: str, merge_title: str, merge_method: str,
    ) -> str | None:
        return github_backend.merge_pr(
            owner, repo, pr_number, token=self._token, merge_message=merge_message,
            merge_title=merge_title, merge_method=merge_method, opener=self._opener,
        )


def _resolve_backend(
    platform: str,
    *,
    owner: str,
    repo: str,
    role: str,
    git_host_base: str,
    token_provider: TokenProvider | None,
    opener,
):
    """Resolve platform guard -> mint/resolve token -> construct the matching
    merge backend adapter. Mirrors review.verb.build_backend exactly (lr-9c69):
    the platform guard ALWAYS runs before token resolution, for both
    platforms -- there is no call path here that reaches _resolve_token
    before the platform has been confirmed to match the selected backend.

    Raises MergeVerbError(code=EXIT_USAGE) for an unrecognized --platform
    value, PlatformMismatchError for a recognized-but-wrong platform (the
    caller translates that to EXIT_WRONG_PLATFORM), and MergeVerbError(code=
    EXIT_TOKEN_FETCH_FAILED) on credential resolution failure.
    """
    if platform == PLATFORM_GITHUB:
        github_backend.assert_platform_is_github(owner, repo, explicit_platform=platform)
    elif platform == PLATFORM_FORGEJO:
        forgejo_backend.assert_platform_is_forgejo(owner, repo, explicit_platform=platform)
    else:
        _fail(
            f"--platform {platform!r} not recognized. Expected "
            f"{PLATFORM_GITHUB!r} or {PLATFORM_FORGEJO!r}.",
            code=EXIT_USAGE,
        )

    print(f"merge: resolving token for role={role!r}", file=sys.stderr)
    active_provider = (
        token_provider if token_provider is not None else resolve_platform_provider(platform)
    )
    try:
        token = _resolve_token(role, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    if platform == PLATFORM_GITHUB:
        return _GithubMergeBackend(token=token, opener=opener, caller=role)
    return _ForgejoMergeBackend(git_host_base=git_host_base, token=token, opener=opener)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="merge",
        description=(
            "merge -- the full gate chain (namespace, authority, stale-SHA, "
            "reviewer verdicts, diff-scope, PR title), then merge via either "
            "the Forgejo or GitHub API. Refuses and exits non-zero on ANY "
            "gate failure; never merges on a partial pass."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  merge --role merger --platform forgejo \\\n"
            "      --repo some-owner/some-repo --pr 42 \\\n"
            "      --expected-head-sha <sha40> --required-reviewer some-reviewer\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"merge {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--platform",
        required=True,
        choices=(PLATFORM_GITHUB, PLATFORM_FORGEJO),
        help="Target platform for the PR (mandatory -- resolved "
        "independently, e.g. from a dispatch envelope's pr_url). Affects "
        "only which backend fetches gate facts and executes the merge; "
        "the gate chain itself runs identically on both platforms.",
    )
    parser.add_argument(
        "--role",
        default=None,
        help=f"Role whose merge authority is checked and whose token is "
        f"resolved via the credential provider (default: {DEFAULT_ROLE!r}). "
        f"Which role may authorize a merge is config (see --authorized-role "
        f"/ an AuthorityProvider), never a hardcoded identity. "
        f"Already-attested, opaque config key downstream (the credential "
        f"provider, the authority check never re-authenticate it "
        f"themselves -- see merge.authority's module docstring). When "
        f"EXPLICITLY supplied, it must ALSO match this process's own "
        f"already attested invoking identity (transport.attestation."
        f"resolve_identity) or the call is refused fail-closed before any "
        f"I/O (transport.caller_binding.bind_caller); omitted, this check "
        f"does not apply.",
    )
    parser.add_argument(
        "--authorized-role",
        action="append",
        dest="authorized_roles",
        default=None,
        help="A role permitted to hold merge authority (repeatable). Used "
        "to build the standalone StaticRoleAuthorityProvider when no "
        "external AuthorityProvider is injected. Required for the merge-"
        "authority gate to ever pass in the standalone configuration.",
    )
    parser.add_argument("--repo", required=True, help="owner/repo to merge in.")
    parser.add_argument("--pr", type=int, required=True, dest="pr_number", help="PR number to merge.")
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR} env "
        f"var, falling back to a configurable compat-alias env var if that "
        f"is unset, or {DEFAULT_GIT_HOST_BASE_URL!r} if neither is set -- see "
        f"transport.git_host_api._resolve_git_host_base). Ignored for the "
        f"GitHub platform.",
    )
    parser.add_argument(
        "--expected-head-sha",
        default="",
        dest="expected_head_sha",
        help="Expected PR head SHA at gate time. When supplied, the merge "
        "is refused if the current head differs. When absent, the "
        "stale-SHA check is a no-op — never invent a SHA.",
    )
    parser.add_argument(
        "--required-reviewer",
        action="append",
        dest="required_reviewers",
        default=None,
        help="A reviewer required to have posted a clean verdict comment "
        "(repeatable). Either a BARE reviewer name (e.g. 'some-reviewer'), "
        "whose expected platform login is DERIVED platform-aware -- "
        "the bare name on --platform forgejo, or "
        "'<resolve_github_app_slug(caller=name)>[bot]' on --platform github, "
        "reusing the SAME github_app.slugs config review.github_backend."
        "resolve_own_login already consults -- or an explicit "
        "'reviewer_name:git_host_login' pair to pin a login directly (override/"
        "back-compat path). A required reviewer with no clean, current-SHA "
        "verdict refuses the merge. Omit entirely to run with no "
        "reviewer-verdict gate (e.g. a deployment gating solely on CI + "
        "authority).",
    )
    parser.add_argument(
        "--max-changed-files",
        type=int,
        default=diff_scope.DEFAULT_MAX_CHANGED_FILES,
        dest="max_changed_files",
        help=f"Maximum changed-file count allowed in the PR diff (default: "
        f"{diff_scope.DEFAULT_MAX_CHANGED_FILES}).",
    )
    parser.add_argument(
        "--skip-title-check",
        action="store_true",
        default=False,
        dest="skip_title_check",
        help="Bypass the Conventional Commits PR title gate. Default: "
        "enforced. Use of this flag is logged to stderr for audit.",
    )
    parser.add_argument(
        "--merge-method",
        default=commit_subjects.REAL_MERGE_METHOD,
        dest="merge_method",
        help="The RESOLVED merge method for this repo/PR (e.g. from repo/"
        f"gate config's allow_squash vs merge_style='merge') -- default: "
        f"{commit_subjects.REAL_MERGE_METHOD!r} (a real, non-squash merge). "
        f"ACTUALLY EXECUTES the requested method: forwarded "
        f"verbatim to the resolved backend's merge_pr as GitHub's "
        f"merge_method / Forgejo's Do field. Gates the branch commit-subject "
        f"check (see --skip-commit-check): the check only fires "
        f"when this resolves to {commit_subjects.REAL_MERGE_METHOD!r} -- on "
        f"any other value (squash, rebase) the check is a no-op, since a "
        f"squash/rebase merge rewrites the resulting commit subject FROM the "
        f"PR title, which the PR-title gate already validated. A "
        f"requested-vs-actual shape mismatch is detected and reported when "
        f"--repo-path is given (see EXIT_MERGE_SHAPE_MISMATCH / merge.merge_shape). "
        f"On the SAME merge_method='merge' condition, an OPTIONAL, "
        f"deployment-configured task-id guard "
        f"(.clagentic/loadout/config.yaml push.task_id_guard_pattern) is also "
        f"checked against each branch commit subject -- a strict no-op with "
        f"no pattern configured; once configured, default mode is BLOCK, "
        f"exit {EXIT_TASK_ID_GUARD_VIOLATION}. See docs/verbs.md's "
        f"`loadout-merge` section for the full contract.",
    )
    parser.add_argument(
        "--skip-commit-check",
        action="store_true",
        default=False,
        dest="skip_commit_check",
        help="Bypass the branch commit-subject Conventional Commits gate. "
        "Default: enforced on --merge-method=merge repos. Use "
        "of this flag is logged to stderr for audit -- intended for an "
        "automation PR whose commits cannot be changed after the fact.",
    )
    parser.add_argument(
        "--merge-message",
        default="",
        dest="merge_message",
        help="Optional merge commit message suffix.",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        dest="task_id",
        help="Opaque work-item ref for this invocation's dispatch envelope "
        "(schemas/common.json's task_id fragment -- an opaque, deployment-"
        "defined string; loadout does not assume a specific tracker or ID "
        "pattern). Rendered as a 'task_id' line on the merge-completion "
        "attestation when supplied; omitted entirely when absent. Never "
        "resolved or validated here -- passed through verbatim.",
    )
    parser.add_argument(
        "--allowed-namespace",
        action="append",
        dest="allowed_namespaces",
        default=None,
        help="Restrict the merge target owner to this namespace (repeatable). "
        f"When omitted, falls back to {ALLOWED_NAMESPACES_ENV_VAR} "
        f"(comma-separated); when neither is set, no namespace restriction "
        f"is enforced.",
    )
    parser.add_argument(
        "--repo-path",
        default=None,
        dest="repo_path",
        help="Local working-tree root the merged repo lives in. When given, "
        "post_merge_steps (see merge.post_merge_config) are read from "
        f"<repo-path>/{DEFAULT_POST_MERGE_CONFIG_RELATIVE_PATH} and, if "
        "present, run in this directory ONLY after the merge in step 8 "
        "actually succeeds. This is an OPTIONAL override, not a required "
        "input -- the caller (a dispatcher with its own project registry) "
        "is expected to supply it whenever a local tree exists; loadout "
        "itself has no project registry to derive one from (see "
        "merge.verb's module docstring). Omitting it entirely "
        "is ONLY a no-op when paired with --no-post-merge-tree; omitting "
        "both is a usage error (EXIT_USAGE) -- see that flag's help.",
    )
    parser.add_argument(
        "--no-post-merge-tree",
        action="store_true",
        default=False,
        dest="no_post_merge_tree",
        help="Explicitly acknowledge that this invocation has NO local "
        "working tree at all (a bare API-only merge) and that skipping "
        "post_merge_steps as a result is intentional. Required whenever "
        "--repo-path is omitted -- omitting --repo-path with neither this "
        "flag nor --skip-post-merge is a USAGE ERROR (EXIT_USAGE), not a "
        "silent skip (omitting a flag must never downgrade a "
        "repo that declares post_merge_steps to a silent no-run exit 0). "
        "Logged to stderr for audit, exactly like --skip-post-merge.",
    )
    parser.add_argument(
        "--skip-post-merge",
        action="store_true",
        default=False,
        dest="skip_post_merge",
        help="Skip post_merge_steps even when --repo-path is given and the "
        "repo's config declares steps. Logged to stderr for audit. Also "
        "satisfies the --repo-path/--no-post-merge-tree usage requirement "
        "when --repo-path is omitted (an explicit blanket skip covers the "
        "no-tree case too).",
    )
    return parser


def _describe_ci_disposition(ci_result: ci_status.CiStatusResult) -> str:
    """Render the CI-status gate's ALREADY-COMPUTED disposition as a short,
    git-host-safe string for both the stderr log line and the merge-completion
    attestation body (merge.attestation.build_attestation_body's
    `ci_disposition` field) -- one rendering, reused, so the two can never
    drift apart from restating the same CiStatusResult two different ways.
    """
    if ci_result.is_empty:
        return "no-runner-by-design (0 commit-status entries at HEAD)"
    return (
        f"combined_state={ci_result.combined_state!r} "
        f"({ci_result.status_count} status(es), {ci_result.run_count} run(s))"
    )


def _parse_required_reviewers(raw: list[str] | None, platform: str) -> dict[str, str]:
    """Parse repeated --required-reviewer values into a {name: login} dict.

    Each entry is either:
      - an explicit 'reviewer_name:git_host_login' pair (override/back-compat
        path) — the login is used verbatim, exactly as before lr-2f1378.
      - a BARE reviewer name (no ':' separator) — its expected login is
        DERIVED platform-aware via merge.reviewer_login.resolve_reviewer_login
        (lr-2f1378): the bare name itself on Forgejo, or the deployment's
        configured GitHub App slug + '[bot]' on GitHub. The derived login
        stays TOOL-AUTHORITATIVE (resolved from config), never trusted from
        a PR comment's claimed identity — see that module's docstring for
        the anti-spoof invariant this preserves (lr-2b3f).

    Raises MergeUsageError on a malformed explicit entry (empty name/login
    around a ':' separator) or when a bare name has no derivable login on
    the target platform (e.g. no github_app.slugs entry configured) — both
    checked BEFORE any network call.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if ":" in entry:
            name, login = entry.split(":", 1)
            name, login = name.strip(), login.strip()
            if not name or not login:
                raise MergeUsageError(
                    f"--required-reviewer {entry!r} must be 'reviewer_name:git_host_login' "
                    f"with both parts non-empty."
                )
            result[name] = login
            continue
        name = entry.strip()
        if not name:
            raise MergeUsageError(
                f"--required-reviewer {entry!r} must be a non-empty reviewer "
                f"name, or 'reviewer_name:git_host_login'."
            )
        try:
            result[name] = resolve_reviewer_login(name, platform)
        except ReviewerLoginNotConfiguredError as exc:
            raise MergeUsageError(str(exc)) from exc
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    authority_provider: AuthorityProvider | None = None,
    opener=None,
    identity_provider=None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable).

    `token_provider`, `authority_provider`, and `opener` are injection
    points for tests and for a deployment wiring its own reference
    providers; all default to the standalone/real path in production use.

    `identity_provider` (lr-c75c9a): a zero-arg callable returning a
    `transport.attestation.Identity` (defaults to
    `transport.attestation.resolve_identity`) -- the injection point for the
    fail-closed --role/attested-invoker binding (transport.caller_binding.
    bind_caller), mirroring the identical parameter transport.git_host_api.main
    already carries for the same purpose.
    """
    if argv is None:
        argv = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    try:
        return _run(
            args,
            token_provider=token_provider,
            authority_provider=authority_provider,
            opener=opener,
            identity_provider=identity_provider,
        )
    except MergeVerbError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return exc.code
    except MergeUsageError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except CallerBindingError as exc:
        print(f"merge: {exc}", file=sys.stderr)
        return EXIT_CALLER_INVOKER_MISMATCH


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    authority_provider: AuthorityProvider | None,
    opener,
    identity_provider=None,
) -> int:
    try:
        owner, repo = parse_owner_repo(args.repo)
    except RemoteResolutionError as exc:
        raise MergeUsageError(str(exc)) from exc

    # lr-ac5c8a: an absent --repo-path must never silently downgrade to "no
    # post-merge run attempted" -- that is the recurring silent-skip defect
    # class (lr-5854ff, lr-4e6f31, clagentic-console PR #365/#366). A caller
    # with genuinely no local tree must say so explicitly (--no-post-merge-tree)
    # or explicitly skip altogether (--skip-post-merge, which already covers
    # the "skip regardless of tree" case). Omitting --repo-path with NEITHER
    # flag set is a usage error, checked BEFORE any credential/network call --
    # same fail-fast placement as the owner/repo parse above.
    if not args.repo_path and not args.no_post_merge_tree and not args.skip_post_merge:
        raise MergeUsageError(
            "--repo-path was omitted but neither --no-post-merge-tree nor "
            "--skip-post-merge was given. A repo may declare post_merge_steps "
            "that would silently never run in this shape -- pass --repo-path "
            "(when a local tree exists), or --no-post-merge-tree (to "
            "explicitly acknowledge a bare API-only merge with no tree to "
            "check), or --skip-post-merge (to skip regardless)."
        )

    # lr-4522a3: when --repo-path points at a real, parseable git tree,
    # refuse a --repo slug that does not match that tree's OWN origin
    # remote -- before any credential mint, so a caller-side argument
    # defect never becomes an opaque platform-API 422 that misleadingly
    # blames the App installation. Compares against the remote, never the
    # directory name (the '.github' org-profile shape is correct and never
    # flagged) -- see merge.repo_path_consistency's module docstring.
    assert_repo_path_consistent(args.repo, args.repo_path)

    role = args.role or DEFAULT_ROLE

    # --role/attested-invoker fail-closed binding (lr-c75c9a, mirrors
    # transport.git_host_api's identical check): checked BEFORE any I/O --
    # before the namespace guard (step 1), before the merge-authority check
    # (step 2), before any token mint. An OMITTED --role (args.role is None)
    # is never checked here -- see transport.caller_binding.bind_caller's own
    # docstring for why (this preserves the pre-existing "omitted --role
    # behaves exactly as before" contract unchanged).
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = resolve_identity_fn()
    except AttestationError as exc:
        _fail(f"attested-identity resolution FAILED -- {exc}", code=EXIT_CALLER_INVOKER_MISMATCH)
    bind_caller(role, caller_explicit=args.role is not None, identity=attested_identity)

    required_reviewers = _parse_required_reviewers(args.required_reviewers, args.platform)
    git_host_base = _resolve_git_host_base(args.git_host_base_url)

    # 1. Namespace guard — runs FIRST, before any credential or network call.
    allowed_namespaces = resolve_allowed_namespaces(
        frozenset(args.allowed_namespaces) if args.allowed_namespaces else None
    )
    try:
        check_namespace_allowed(owner, repo, allowed_namespaces=allowed_namespaces)
    except NamespaceDeniedError as exc:
        _fail(str(exc), code=EXIT_NAMESPACE_DENIED)

    # 2. Merge-authority check — FAIL-CLOSED provider seam. Runs before any
    # credential is minted for an out-of-scope/unauthorized request.
    provider = authority_provider or StaticRoleAuthorityProvider(
        frozenset(args.authorized_roles) if args.authorized_roles else frozenset()
    )
    try:
        check_authority(role, owner, repo, args.pr_number, provider)
    except AuthorityDeniedError as exc:
        _fail(str(exc), code=EXIT_AUTHORITY_DENIED)

    # 3. Platform guard (BOTH directions, fail-closed, BEFORE any credential
    # mint or API call) -> credential resolution -> resolved backend.
    try:
        backend = _resolve_backend(
            args.platform,
            owner=owner,
            repo=repo,
            role=role,
            git_host_base=git_host_base,
            token_provider=token_provider,
            opener=opener,
        )
    except PlatformMismatchError as exc:
        _fail(str(exc), code=EXIT_WRONG_PLATFORM)

    # Read the PR's LIVE current state once, reused by every remaining gate
    # (stale-SHA, verdict SHA-stamp comparison, title gate).
    try:
        pr_info = backend.get_pr_info(owner, repo, args.pr_number)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    current_head_sha = forgejo_backend.get_pr_head_sha(pr_info)
    pr_title = forgejo_backend.get_pr_title(pr_info)

    # 4. Stale-SHA refusal.
    try:
        check_stale_head_sha(
            args.expected_head_sha, current_head_sha, args.pr_number, owner, repo
        )
    except StaleHeadShaError as exc:
        _fail(str(exc), code=EXIT_STALE_HEAD_SHA)

    # 5. Reviewer-verdict fences — for each required reviewer.
    if required_reviewers:
        if not current_head_sha:
            _fail(
                f"reviewer verdict checks FAILED -- current PR head SHA is "
                f"unknown for PR #{args.pr_number} in {owner}/{repo}; cannot "
                f"verify reviewer SHA-stamps without it.",
                code=EXIT_GATE_RESULT_BLOCKED,
            )
        try:
            comments = backend.fetch_comments(owner, repo, args.pr_number)
        except GateFactUnavailableError as exc:
            _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)

        # lr-5260f9: multi-fence refusal for the reviewer-verdict comment
        # body, ENFORCED BY DEFAULT -- see merge.post_merge_config.
        # resolve_enforce_single_verdict_fence's own docstring for the
        # ENFORCE-BY-DEFAULT / CONFIG-GATED OPT-OUT trade-off (the inverse
        # shape of resolve_enforce_merge_shape's warn-by-default precedent;
        # lives in the SAME module as that resolver for the same
        # bootstrap-safety reason -- merge.gate_config is diagnostic-only and
        # must never be imported here; see that module's "BLAST RADIUS"
        # docstring section). Resolved once per invocation, from the same
        # --repo-path config root every other repo-tier gate key here reads.
        try:
            enforce_single_verdict_fence = resolve_enforce_single_verdict_fence(
                args.repo_path
            )
        except PostMergeConfigError as exc:
            _fail(
                f"post-merge config FAILED to load -- {exc}",
                code=EXIT_POST_MERGE_FAILED,
            )

        for reviewer_name, bot_login in required_reviewers.items():
            try:
                verdict_obj = verdict.read_reviewer_verdict(
                    comments,
                    expected_login=bot_login,
                    current_head_sha=current_head_sha,
                    pr_number=args.pr_number,
                    owner=owner,
                    repo=repo,
                    expected_reviewer_name=reviewer_name,
                    enforce_single_fence=enforce_single_verdict_fence,
                )
                verdict.assert_clean_verdict(verdict_obj, reviewer_name)
            except (
                VerdictMissingError,
                VerdictMalformedError,
                VerdictRoleMismatchError,
                VerdictStaleError,
                VerdictBlockingError,
            ) as exc:
                _fail(str(exc), code=EXIT_GATE_RESULT_BLOCKED)
            print(
                f"merge: {reviewer_name!r} verdict PASSED -- "
                f"review_status={verdict_obj.review_status!r}, "
                f"head_sha={verdict_obj.head_sha!r}, "
                f"comment_id={verdict_obj.comment_id}",
                file=sys.stderr,
            )

    # 6. Diff-scope cap.
    try:
        changed_files = backend.fetch_changed_files(owner, repo, args.pr_number)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    print(
        f"merge: diff scope -- PR #{args.pr_number} touches "
        f"{len(changed_files)} file(s) (limit={args.max_changed_files})",
        file=sys.stderr,
    )
    try:
        diff_scope.check_diff_scope(
            changed_files, args.pr_number, owner, repo,
            max_changed_files=args.max_changed_files,
        )
    except DiffScopeExceededError as exc:
        _fail(str(exc), code=EXIT_GATE_RESULT_BLOCKED)

    # 7. PR-title gate.
    if args.skip_title_check:
        print(
            f"merge: PR title gate BYPASSED via --skip-title-check for "
            f"PR #{args.pr_number} in {owner}/{repo} (title={pr_title!r})",
            file=sys.stderr,
        )
    try:
        title_gate.check_pr_title(
            pr_title, args.pr_number, owner, repo, skip=args.skip_title_check
        )
    except TitleInvalidError as exc:
        _fail(str(exc), code=EXIT_PR_TITLE_INVALID)

    # 7b. Branch commit-subject gate (lr-835c57) -- fires ONLY on a resolved
    # merge_method='merge' (real, non-squash) repo: semantic-release reads
    # EACH branch commit's own subject on a real merge, not the PR title
    # (already gated by step 7 above), so a non-conformant 'lr-XXXX: <desc>'
    # commit subject would otherwise silently stop beta cuts even with a
    # clean title. A no-op on any other --merge-method value (squash/rebase
    # rewrite the resulting commit subject FROM the already-gated PR title).
    # See merge.commit_subjects' module docstring for the full rationale and
    # the BLOCK-never-rewrite contract.
    if args.skip_commit_check:
        print(
            f"merge: branch commit-subject gate BYPASSED via "
            f"--skip-commit-check for PR #{args.pr_number} in {owner}/{repo}",
            file=sys.stderr,
        )
    if args.merge_method == commit_subjects.REAL_MERGE_METHOD and not args.skip_commit_check:
        base_branch_for_commits = resolve_base_branch(pr_info)
        try:
            branch_commit_subjects = backend.fetch_branch_commit_subjects(
                owner, repo, base_branch_for_commits, current_head_sha
            )
        except GateFactUnavailableError as exc:
            _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
        print(
            f"merge: branch commit-subject gate -- PR #{args.pr_number} in "
            f"{owner}/{repo} introduces {len(branch_commit_subjects)} "
            f"commit(s) (merge_method={args.merge_method!r})",
            file=sys.stderr,
        )
    else:
        branch_commit_subjects = []
    # TASK-ID GUARD (lr-4005f5, task_id_guard) -- an independent,
    # deployment-config-gated check layered on the SAME branch commit
    # subjects step 7b already fetched: no configured
    # `push: task_id_guard_pattern:` is a strict no-op (see
    # task_id_guard's own module docstring, "NO-OP BY DEFAULT"); once
    # configured, the default mode is "block" (operator-pinned, see that
    # module's own docstring "DEFAULT MODE IS BLOCK"). --repo-path is the
    # SAME config root every other repo-tier gate key here resolves
    # through; None when absent (--no-post-merge-tree/--skip-post-merge
    # paths), which resolves to the disabled default (no file lookup).
    task_id_guard_config = load_task_id_guard_config(args.repo_path)
    try:
        guard_warnings = commit_subjects.check_branch_commit_subjects(
            branch_commit_subjects, args.pr_number, owner, repo,
            merge_method=args.merge_method, skip=args.skip_commit_check,
            task_id_guard_pattern=task_id_guard_config.pattern,
            task_id_guard_mode=task_id_guard_config.mode,
        )
    except CommitSubjectInvalidError as exc:
        _fail(str(exc), code=EXIT_COMMIT_SUBJECT_INVALID)
    except TaskIdGuardViolation as exc:
        _fail(str(exc), code=EXIT_TASK_ID_GUARD_VIOLATION)
    for warning in guard_warnings:
        print(f"merge: WARNING -- {warning}", file=sys.stderr)

    # 8. CI-status gate. An empty result (zero HEAD-scoped commit-status
    # entries) is an EXPLICIT PASS -- no-runner-by-design is a legitimate
    # repo shape (lr-368c), not a missing gate. Emptiness is HEAD-scoped
    # commit-status absence ONLY -- a repo-global signal (e.g. Forgejo's
    # mirror-sync/historical Actions tasks) is explicitly NOT CI evidence
    # and never overrides this (lr-2d2293). See merge.ci_status's module
    # docstring for the full decision.
    try:
        ci_result = backend.fetch_ci_status(owner, repo, current_head_sha)
    except GateFactUnavailableError as exc:
        _fail(str(exc), code=EXIT_GATE_FACT_UNAVAILABLE)
    ci_disposition = _describe_ci_disposition(ci_result)
    if ci_result.is_empty:
        print(
            f"merge: CI-status gate -- no HEAD-scoped CI evidence for PR "
            f"#{args.pr_number} in {owner}/{repo} (0 commit-status entries "
            f"at HEAD); treating as PASS (no-runner-by-design).",
            file=sys.stderr,
        )
    else:
        print(
            f"merge: CI-status gate -- PR #{args.pr_number} in {owner}/{repo} "
            f"{ci_disposition}",
            file=sys.stderr,
        )
    try:
        ci_status.check_ci_status(ci_result, args.pr_number, owner, repo)
    except CiStatusFailedError as exc:
        _fail(str(exc), code=EXIT_CI_STATUS_FAILED)

    # 9. All gates passed -- execute the merge.
    print(
        f"merge: all gates PASSED -- merging PR #{args.pr_number} in "
        f"{owner}/{repo}",
        file=sys.stderr,
    )
    # lr-1953a8: merge_title=pr_title composes the merge commit's SUBJECT
    # from the PR's own (already step-7-gated Conventional Commits) title,
    # rather than each backend's own default (GitHub: "Merge pull request
    # #N from <owner>/<branch>"; Forgejo: an equivalent branch-ref-bearing
    # default) -- a <type>/<task-id>-<slug> branch name would otherwise put
    # the task id straight into the subject with nobody typing it. Pure
    # readability: pr_title is passed through UNMODIFIED, no task-id
    # stripping/matching of any kind -- see merge.github_backend.merge_pr /
    # merge.forgejo_backend.merge_pr's own merge_title docstrings.
    try:
        merged_sha = backend.merge_pr(
            owner, repo, args.pr_number,
            merge_message=args.merge_message, merge_title=pr_title,
            merge_method=args.merge_method,
        )
    except MergeExecutionError as exc:
        _fail(str(exc), code=EXIT_MERGE_FAILED)

    print(f"merge: PR #{args.pr_number} in {owner}/{repo} merged")

    # Post-merge authoritative readback (lr-361de3): merge_pr's own response
    # is NOT a reliable carrier of the merged state (Forgejo returns 200/204
    # with an EMPTY body on success -- see merge.forgejo_backend.merge_pr's
    # docstring; GitHub's response IS checked already, but only at the
    # moment of the call). A FRESH GET, issued now, re-reads the PR and
    # confirms merged==true with a resolvable merge_commit_sha -- the same
    # predicate seq 2 of this task's research pass specified. FAIL-CLOSED:
    # unlike push's own additive-only readback (push.remote_readback), a
    # merge readback failure DOES fail this verb -- a merge gate's whole
    # purpose is deciding what's authorized to land, so reporting success
    # for a mutation this verb cannot independently confirm landed would
    # undermine the gate itself.
    merge_readback = verify_merge_landed(
        lambda: backend.get_pr_info(owner, repo, args.pr_number)
    )
    if not merge_readback.verified:
        _fail(
            f"post-merge readback FAILED for PR #{args.pr_number} in "
            f"{owner}/{repo} -- {merge_readback.detail.get('reason', '')} "
            f"The merge_pr call itself reported success; this independent "
            f"re-read could not confirm it landed. Gate-pass REFUSED.",
            code=EXIT_MERGE_READBACK_FAILED,
        )
    print(
        f"merge: post-merge readback CONFIRMED -- merged_commit_sha="
        f"{merge_readback.detail.get('merged_commit_sha')!r}",
        file=sys.stderr,
    )

    # Merge-completion attestation (lr-20e866): the ONLY git-host-visible mark
    # that THIS tool (as opposed to a human, or an earlier internal merge
    # tool) executed the merge -- see this module's docstring point 5 for
    # why the LORE-COUPLED half of the reference gate-note was never ported, and
    # merge.attestation's own docstring for why this half is pure git-host/
    # product data. FAIL-OPEN BY DESIGN: the merge above already succeeded --
    # a failed attestation POST must never fail this verb or change its exit
    # code, so any failure here is logged to stderr and swallowed, never
    # re-raised. Posted via the SAME POST-and-verify comment transport each
    # backend already carries (review.forgejo_backend.post_and_verify_comment
    # / review.github_backend.post_and_verify_review), not a third
    # implementation.
    # Both work-item IDs (lr-eb22f3): task_id is passed through verbatim from
    # --task-id (the caller's own opaque envelope value, never resolved or
    # validated here); issue_number is parsed back out of the PR body's own
    # `Closes #NN` trailer (git-host-native, never a lore field) -- reusing
    # push.issue_link's single regex rather than a second implementation.
    pr_body = pr_info.get("body") or ""
    issue_number = parse_closes_issue_number(pr_body)
    attestation_body = build_attestation_body(
        gated_head_sha=current_head_sha,
        merged_sha=current_head_sha,
        required_reviewer_logins=list(required_reviewers.values()),
        ci_disposition=ci_disposition,
        task_id=args.task_id,
        issue_number=issue_number,
    )
    verified_comment_id: "str | int | None" = None
    try:
        verified_comment_id = backend.post_merge_attestation(
            owner, repo, args.pr_number, body=attestation_body
        )
    except (
        ReviewPostError,
        ReviewVerifyError,
        urllib.error.URLError,
        TimeoutError,
        OSError,
    ) as exc:
        # FAIL-OPEN (lr-20e866): the merge above already succeeded -- a
        # transport-level failure (network error, non-2xx the backend
        # translated to ReviewPostError/ReviewVerifyError) posting this
        # best-effort attestation must never fail the verb or change its
        # exit code. Named exception types only, never a bare except: a
        # genuine programming error in this call path (e.g. a TypeError from
        # a malformed call) is NOT swallowed here.
        print(
            f"merge: merge-completion attestation POST FAILED (non-fatal, "
            f"merge already succeeded) -- {exc}",
            file=sys.stderr,
        )
    else:
        print(
            f"merge: merge-completion attestation posted -- "
            f"verified_comment_id={verified_comment_id!r}",
            file=sys.stderr,
        )

    print(json.dumps({
        "pr_number": args.pr_number, "owner": owner, "repo": repo,
        READBACK_ENVELOPE_KEY: merge_readback.to_dict(),
    }))

    # 10. Working-tree sync + post-merge steps -- ONLY reached after the
    # merge above actually succeeded (any earlier _fail() call already
    # returned out of this function). Never attempted when --repo-path is
    # absent: a caller with no local working tree (a bare API-only merge) has
    # nowhere to sync a tree or run steps in. lr-ac5c8a: an absent
    # --repo-path reaching this point has ALREADY been required (by the
    # usage-error check earlier in this function) to carry an explicit
    # --no-post-merge-tree or --skip-post-merge acknowledgment -- there is no
    # remaining silent-skip path where --repo-path is simply missing and
    # nothing is logged.
    #
    # lr-173768: a CHECKOUT (git checkout --detach / git checkout -B) is now
    # performed ONLY when something will actually read the checked-out files
    # this invocation -- i.e. at least one post_merge_steps entry will
    # actually run (non-empty list AND --skip-post-merge not given). When
    # nothing will run, this phase still FETCHES and verifies the merged
    # commit is present locally (merge.tree_sync.fetch_merged_sha_object) --
    # so merge.merge_shape.check_merge_shape's local `git log` readback and
    # the merge-completion attestation's SHA claim keep working
    # unconditionally -- but never checks anything out: the working tree,
    # the index, and HEAD are left exactly where the caller had them. This
    # closes the documented contention source of a shared build-agent
    # checkout being yanked out from under other in-flight work on every
    # merge, regardless of whether that merge's own repo even has
    # post_merge_steps configured (see merge.tree_sync's own module
    # docstring, "NO CHECKOUT UNLESS SOMETHING WILL ACTUALLY READ THE TREE",
    # for the full rationale). A per-repo
    # `merge.sync_tree_after_merge` key (default True,
    # merge.post_merge_config.resolve_sync_tree_after_merge) still turns off
    # even the fetch-only phase entirely, unchanged from before this task.
    if args.repo_path:
        try:
            sync_tree_after_merge = resolve_sync_tree_after_merge(args.repo_path)
        except PostMergeConfigError as exc:
            _fail(
                f"post-merge config FAILED to load -- {exc}",
                code=EXIT_POST_MERGE_FAILED,
            )
        if not sync_tree_after_merge:
            print(
                "merge: working-tree sync SKIPPED -- "
                "merge.sync_tree_after_merge: false for this repo",
                file=sys.stderr,
            )
        else:
            base_branch = resolve_base_branch(pr_info)
            # lr-93d718: config discovery (load_post_merge_steps below) and
            # tree_sync's git-tree target are no longer assumed to be the
            # SAME directory. A wrapper-layout repo (config at the wrapper,
            # git tree at a subdirectory) declares an OPTIONAL
            # `merge.git_working_tree` knob
            # (merge.post_merge_config.resolve_git_working_tree) naming that
            # subdirectory relative to the config root; absent (the common,
            # flat-layout case), tree_sync targets --repo-path exactly as
            # before this task -- see that function's own docstring for the
            # full contract and post_merge_config's module docstring for the
            # "CONFIG-ROOT VS GIT-TREE-ROOT" rationale.
            try:
                declared_working_tree = resolve_git_working_tree(args.repo_path)
            except PostMergeConfigError as exc:
                _fail(
                    f"post-merge config FAILED to load -- {exc}",
                    code=EXIT_POST_MERGE_FAILED,
                )
            git_tree_path = (
                str(declared_working_tree) if declared_working_tree is not None else args.repo_path
            )

            # lr-173768: resolve WHETHER any post_merge_steps will actually
            # run this invocation BEFORE deciding whether to check anything
            # out -- a caller-config-load error here is reported exactly as
            # it always was (EXIT_POST_MERGE_FAILED), just moved earlier so
            # the checkout-vs-fetch-only decision itself can be made off a
            # fully-resolved `steps` value.
            if args.skip_post_merge:
                steps: list[dict] = []
            else:
                try:
                    steps = load_post_merge_steps(args.repo_path)
                except PostMergeConfigError as exc:
                    _fail(
                        f"post-merge config FAILED to load -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
            steps_will_run = bool(steps)

            if steps_will_run:
                # lr-7c5540: advance the --repo-path working tree to the
                # merged main SHA BEFORE running any post_merge_steps --
                # backend.merge_pr above was a server-side API merge; it
                # never touched this local tree. Without this, a post-merge
                # step that packages/installs the repo (e.g.
                # `scripts/install.sh` reading pyproject.toml/package source
                # off disk) would silently package whatever ref the caller
                # left checked out (the feature branch HEAD), not what
                # actually landed on main. See merge.tree_sync's module
                # docstring for the full trade-off on how the merged SHA is
                # resolved per backend and why. FAIL LOUD on any inability to
                # verify the tree landed on the merged commit -- never a
                # silent run against the stale ref.
                try:
                    landed_sha = advance_repo_to_merged_sha(
                        git_tree_path,
                        base_branch=base_branch,
                        known_merged_sha=merged_sha,
                    )
                except TreeSyncError as exc:
                    _fail(
                        f"post-merge working-tree sync FAILED -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
                print(
                    f"merge: working tree at {git_tree_path} advanced to "
                    f"merged SHA {landed_sha!r}",
                    file=sys.stderr,
                )
            else:
                # lr-173768: nothing will read the checked-out files this
                # invocation (no post_merge_steps to run) -- fetch and
                # verify the merged commit is present in the local object
                # database WITHOUT checking anything out. Never a `git
                # checkout`, so the working tree/index/HEAD are left exactly
                # as the caller had them.
                try:
                    landed_sha = fetch_merged_sha_object(
                        git_tree_path,
                        base_branch=base_branch,
                        known_merged_sha=merged_sha,
                    )
                except TreeSyncError as exc:
                    _fail(
                        f"post-merge working-tree sync FAILED -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
                print(
                    f"merge: fetched merged SHA {landed_sha!r} into "
                    f"{git_tree_path} (no post_merge_steps to run -- "
                    f"working tree left untouched, no checkout performed)",
                    file=sys.stderr,
                )

            # lr-14f704 item 3: surface a requested-vs-actual merge-shape
            # mismatch loudly rather than silently -- the exact defect class
            # push.remote_readback (lr-4e8a43) closed one layer down, applied
            # here to the merge call itself. Reads the ALREADY-FETCHED local
            # object (see merge.merge_shape's own docstring, "SCOPE") --
            # this readback needs the commit object present, never a
            # checkout, so it runs unconditionally here regardless of
            # whether steps_will_run checked anything out above. A bare
            # API-only merge with no --repo-path has no local object
            # database to read a parent count from, and is not covered by
            # this check.
            try:
                shape_check = check_merge_shape(
                    landed_sha, args.merge_method, git_tree_path
                )
            except MergeShapeCheckError as exc:
                _fail(
                    f"merge-shape readback FAILED -- {exc}",
                    code=EXIT_MERGE_SHAPE_MISMATCH,
                )
            if shape_check.verified and not shape_check.matches:
                mismatch_message = format_mismatch_message(
                    shape_check, pr_number=args.pr_number, owner=owner, repo=repo
                )
                try:
                    enforce_merge_shape = resolve_enforce_merge_shape(args.repo_path)
                except PostMergeConfigError as exc:
                    _fail(
                        f"post-merge config FAILED to load -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
                if enforce_merge_shape:
                    _fail(mismatch_message, code=EXIT_MERGE_SHAPE_MISMATCH)
                print(f"merge: WARNING -- {mismatch_message}", file=sys.stderr)

            if not steps_will_run:
                if args.skip_post_merge:
                    print(
                        "merge: post-merge steps SKIPPED via --skip-post-merge",
                        file=sys.stderr,
                    )
                # else: steps genuinely resolved to an empty list -- nothing
                # to log beyond the fetch-only message already printed above.
            else:
                print(
                    f"merge: running {len(steps)} post-merge step(s) in "
                    f"{args.repo_path}",
                    file=sys.stderr,
                )
                # Deployment-owned env-override seam (lr-52d7): resolved
                # from CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME> env vars
                # and the user-level config file's post_merge_env:
                # section — never from this repo's own (possibly
                # committed) .clagentic/loadout/config.yaml. See
                # merge.post_merge_config.resolve_env_overrides for the
                # full precedence and why this is the correct trust
                # boundary.
                deployment_env_overrides = resolve_env_overrides()
                # lr-d6e52b: repo-tier default bound for any ORDINARY
                # step that does not set its own timeout_seconds -- see
                # merge.post_merge_config's own docstring,
                # "POST_MERGE_STEP_TIMEOUT_SECONDS". None (absent) is a
                # no-op, matching pre-lr-d6e52b unbounded-wait behavior.
                try:
                    default_step_timeout = resolve_post_merge_step_timeout_seconds(
                        args.repo_path
                    )
                except PostMergeConfigError as exc:
                    _fail(
                        f"post-merge config FAILED to load -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
                try:
                    run_post_merge_steps(
                        steps,
                        args.repo_path,
                        deployment_env_overrides=deployment_env_overrides,
                        default_timeout_seconds=default_step_timeout,
                    )
                except (
                    PostMergeStepFailedError,
                    PostMergeStepTimeoutError,
                    PostMergeLivenessError,
                ) as exc:
                    _fail(str(exc), code=EXIT_POST_MERGE_FAILED)

            if steps_will_run:
                # lr-d95cdb: only NOW -- after post_merge_steps have run
                # against the detached, verified tree -- move the tree off
                # that detached HEAD onto base_branch, pointed at the SAME
                # landed_sha advance_repo_to_merged_sha already verified. See
                # merge.tree_sync.land_on_base_branch's own docstring: this
                # is a ref repoint (git checkout -B), never a merge/rebase,
                # so it cannot diverge from the server-side merge result.
                # Runs against git_tree_path (the SAME target
                # advance_repo_to_merged_sha used above), not necessarily
                # --repo-path itself (the wrapper-layout split, lr-93d718).
                # lr-173768: skipped entirely when steps_will_run is False --
                # there is no detached HEAD to move off of in that case (only
                # a fetch happened, never a checkout), and re-pointing the
                # caller's branch ref out from under it with nothing having
                # read the tree would be exactly the unsignaled-mutation
                # class this task removes.
                try:
                    landed_branch_sha = land_on_base_branch(
                        git_tree_path,
                        base_branch=base_branch,
                        landed_sha=landed_sha,
                    )
                except TreeSyncError as exc:
                    _fail(
                        f"post-merge working-tree sync FAILED -- {exc}",
                        code=EXIT_POST_MERGE_FAILED,
                    )
                print(
                    f"merge: working tree at {git_tree_path} landed on "
                    f"{base_branch!r} at {landed_branch_sha!r}",
                    file=sys.stderr,
                )
    elif args.skip_post_merge:
        print("merge: post-merge steps SKIPPED via --skip-post-merge", file=sys.stderr)
    elif args.no_post_merge_tree:
        print(
            "merge: post-merge steps SKIPPED -- --no-post-merge-tree "
            "explicitly acknowledged no local working tree for this "
            "invocation",
            file=sys.stderr,
        )

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
