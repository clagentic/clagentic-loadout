"""push.verb — the push CLI: bot-attributed commit push, issue linking, PR
open (or update), on either Forgejo or GitHub, behind one dispatch shape.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push verb;
the source module stays primary until its separate CUT OVER + RETIRE +
VERIFY-GONE task per the migration plan.

SCOPE (task lr-09ca): push a branch's commits (re-authored to a configured
bot identity), open or update a PR. NEVER merges, NEVER pushes to main —
merge authority is the merge verb's job (a later loadout slice). The
reference module's --merge / post_merge_steps path is not ported here for
that reason; this verb has no merge capability to gate.

IDENTITY / SEAM STRIP FROM THE SOURCE MODULE (see PR body for the full
inventory):
  1. The hardcoded allowed-owner literal (a single fixed brand-org string,
     enforced as a namespace-denied refusal) is now push.namespace_guard —
     a config-driven allowed-namespace set (an env var or an explicit
     caller-supplied set), empty/permissive by default.
  2. Credential mint (a broker self-fetch call plus a separate installation-
     token minting call) is now the SAME transport.credential_provider seam
     Wave B slices 1-2 already established — a TokenProvider resolves a
     token for a caller-supplied role, never a hardcoded broker client.
  3. An operator-specific API host and a broker config path under a
     private dotfile directory are gone; the Forgejo API base this verb
     actually calls (`api_base`) is derived from the git remote URL
     (push.git_coords.parse_forgejo_coords) on every push-path call — see
     "--GIT-HOST-BASE-URL IS CURRENTLY UNROUTED" below for why the flag of
     the same name does not feed into that value — and there is no
     built-in broker-config reader — bot identity is CLI input
     (--bot-name/--bot-email) or a caller-supplied resolver, not read from
     an operator-specific config path this module knows about.
  4. Bare acting-identity literals are gone — --caller is a role/name CLI
     input resolved through the credential provider, exactly like
     review.verb's --caller.
  5. A per-repo doc-body word-count pre-flight from the reference module is
     out of this package's task boundary (a different project's document
     format, not a loadout verb concern) and is not ported.

--GIT-HOST-BASE-URL IS CURRENTLY UNROUTED (lr-cd3113, investigation
finding, not a behavior change): this verb accepts --git-host-base-url and
previously carried its OWN local _resolve_git_host_base(explicit) copy (a
narrower 2-tier chain -- explicit flag, then
CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL, then the localhost placeholder -- vs.
transport.git_host_api's canonical 5-tier chain, which also consults a
compat-alias env var and the user-level config file, plus the forgejo:/
legacy git_host: config-section compat shim from PR #3, 41384e4). Traced
end to end: NEITHER copy's return value ever reached `api_base` in this
module -- api_base is derived exclusively from the git remote URL
(push.git_coords.parse_forgejo_coords) at every push-path call site. The
flag and both resolver copies were dead beyond this docstring and the
--help text describing them. Investigation found no comment, docstring, or
commit message anywhere arguing the narrower chain was a deliberate
push-path credential-safety posture (git history for this module is a
single squashed initial-release commit; the canonical transport copy
documents its own precedence in detail but never mentions this module's
copy at all) -- the divergence reads as copied-and-never-reconciled, not a
considered decision. FIX: the local copy is removed rather than reconciled
with transport.git_host_api's own _resolve_git_host_base (the import
pattern review.verb/merge.verb/merge.close_verb/merge.post_merge_verb/
acquire.verb use) -- a second, un-called implementation of the same-named
function is pure drift with nothing behind it to reconcile. --git-host-
base-url and the module-level GIT_HOST_BASE_URL_ENV_VAR/
DEFAULT_GIT_HOST_BASE_URL constants stay (removing the flag would be its
own behavior change -- an existing caller/test supplying it would newly
hit an argparse "unrecognized arguments" error for a flag that was
previously silently accepted) -- see the flag's own --help text, corrected
in this same fix to say so explicitly rather than describing a resolution
chain that never executes. Wiring a resolved value into an actual API call
is NOT part of this fix -- that would be a behavior change (a config-file-
sourced host would newly affect a push-path credentialed call) outside
this investigation's scope; see this task's own PR body for why that is
named rather than folded in silently.

PRESERVED (load-bearing, not identity):
  - Bot-attributed commit re-authoring + HEAD-author verification
    (push.identity), never silently skipped when the caller marks it
    required (--require-bot-identity).
  - Issue linking (push.issue_link): --issue asserts a matching
    'Closes #NN' trailer; fails closed before any push/PR call.
  - Task-id trailer (push.issue_link, lr-eb22f3): --task-id auto-injects a
    'Task: <task_id>' trailer into the PR body if absent (never fails
    closed -- there is no enforcement analog to --issue's fail-closed
    check, since a missing task_id is a legitimate ad-hoc-mode state).
    Together with --issue, this is the "both IDs on every applicable PR"
    contract: the opaque work-item ref AND the git-host issue number, each
    independently optional, each surfaced as its own PR-body trailer.
  - PR open, and the update-existing-PR path (--update-pr) — no re-
    authoring, no push, on that path.
  - Platform auto-detect via clagentic_loadout.platform_detect.
  - Never pushes to a protected branch (HEAD/main/master); never merges.
  - GIT_ASKPASS token-injection + HOME isolation (push.git_push); no-
    token-in-logs (every raised message here is built from static labels,
    HTTP status codes, and caller-supplied non-secret values only).

SANCTIONED DIAGNOSTIC AFFORDANCE -- --DRY-RUN / --VERBOSE (lr-68039e): a
caller with a failing push previously had NO sanctioned way to see the
full transcript beyond what this verb's own classified failure message
already surfaces -- no --dry-run, no discoverable verbose flag, only an
undiscoverable env var (CLAGENTIC_LOADOUT_PUSH_GIT_TRACE, never mentioned
in --help). Prohibition without affordance guarantees a caller reaches
around the minted-credential path entirely (raw git, an ambient
credential) to get what it needs. --dry-run runs a read-only `git push
--dry-run` through the SAME minted credential, SAME hermeticity
pre-flight, and SAME single git-push call site (push.git_push.
git_push_with_token, test-locked at
tests/test_push_shared_git_push_entrypoint.py) a real push uses --
skipping PR creation/update and the post-push remote readback, since
nothing was actually pushed. --verbose/--trace is the discoverable form of
that same env var: both enable the identical GIT_TRACE passthrough, plus
`git push -v`. Every byte either flag surfaces passes through the same
redaction choke point (push.push_redaction.redact_push_secrets) every
other push-failure field already uses -- one choke point, never a second
implementation.

POST-PUSH HEAD SHA (lr-e36dec; CREATE PATH ONLY as of lr-2500b7 -- see
below): the PR-open (create) success envelope carries "head_sha" -- the
project root's `git rev-parse HEAD` value AFTER any bot-identity
re-authoring has run, validated full-length via sha.validate_sha (reused,
not reimplemented). Bot-identity re-authoring rewrites every commit SHA in
base..HEAD including HEAD itself; this verb is the one caller that knows
the post-rewrite value, so it is the one caller that must report it -- a
SHA captured before push is stale by construction for any downstream
consumer (a dispatching session, a merge gate, a review stamp).

--UPDATE-PR CARRIES NO head_sha AT ALL (lr-2500b7, DEFECT 1 FIX): prior to
this fix, --update-pr's envelope ALSO carried "head_sha" -- a LOCAL `git
rev-parse HEAD` of the caller's own working tree, formatted identically to
the create path's genuinely-pushed value. --update-pr is a metadata-only
verb by construction (see "PR open, and the update-existing-PR path"
above): it never pushes, so that field was a local fact dressed as a
remote one -- a caller reading it reasonably believed a SHA was live on
the remote when this call never touched the remote's git state at all.
Observed against a Forgejo deployment: the PR metadata updated
successfully while an unrelated local commit sat unpushed, and the
envelope's own head_sha was indistinguishable from the create path's. THE
FIX: --update-pr's envelope now carries no sha field whatsoever (`
"pushed": false` instead) rather than performing a remote readback of a
value this call did not itself produce -- a readback here would still
imply this call caused that remote state, which it did not. A best-effort
stderr warning fires when the local branch is ahead of its own remote-
tracking ref (`_warn_if_ahead_of_remote_tracking`) -- the exact situation
a caller is most likely to misread as "the metadata call also pushed my
commits."

--UPDATE-PR BODY MODE IS NOW EXPLICIT, NEVER DEFAULTED (lr-2500b7, DEFECT
2 FIX, OPERATOR DIRECTIVE): --update-pr --body-stdin/--body-env previously
replaced the PR's ENTIRE existing body with no explicit mode -- the only
semantic the verb had. A caller who wanted to append a short follow-up
note had no way to do so short of first reading the existing body out of
band and hand-concatenating it into a new --body-stdin payload; the
observed incident (clagentic-config PR #16) replaced a full measurement
writeup with a two-sentence note using exactly this (the only) code path.
Operator directive: destructive must be OPT-IN, and there is NO default
body mode -- supplying a body on --update-pr without ALSO supplying
exactly one of --replace-body/--append-body is a USAGE ERROR (fails fast,
before any token resolution or network call), never inferred. Omitting a
body entirely on --update-pr is UNCHANGED: the existing body is left
untouched, no mode flag is required (there is nothing to be ambiguous
about). --append-body performs a GET of the CURRENT body immediately
before the update PATCH (push.forgejo_backend.get_pr_body /
push.github_backend.get_pr_body) and concatenates with a blank-line
separator -- update_pr() itself on both backends is UNCHANGED, still a
single unambiguous whole-field PATCH; append is composed at this call
site, not threaded into either backend's update_pr() as a mode parameter.

PR-TITLE GATE (lr-6067): --title (on both PR-open and --update-pr) is
validated against the Conventional Commits grammar via merge.title_gate —
REUSED verbatim, not forked; title_gate.py stays the single source of
grammar truth for both this verb and merge.verb's own pre-merge check. A
non-conformant title is rejected here, at authoring time, instead of only
surfacing later at the merge gate. Opt out with --skip-title-check
(logged to stderr for audit, same semantics as merge.verb's own bypass).
A no-op when --title is not supplied (e.g. an --update-pr body-only edit).

BODY-INPUT ERGONOMICS (lr-df5a11, redesigned lr-e1e2fb): --body-stdin's
JSON-wrapped requirement ({"body": "..."}) is real friction for a caller
whose only tools produce plain text, not escaped JSON -- the observed
failure mode was agents improvising a scratch staging file at a WRONG
location (inside .git/, under /root, or left stray in the working tree)
instead of reaching for a sanctioned mechanism.

FIRST ATTEMPT AND WHY IT WAS REJECTED: a --body-file PATH flag (caller-
supplied path, validated against a scratch-boundary allowlist at read time)
shipped briefly, then was REJECTED after a security audit and an explicit
operator correction: a validated arbitrary path still ACCEPTS a location
parameter, and every containment check is one canonicalization edge case,
one symlink race, one future refactor away from a bypass -- it left the
door open by design rather than removing the location-parameter surface.
The operator's requirement: the caller supplies CONTENT, never a LOCATION.

THE ACTUAL FIX: --body-env (a bare, zero-argument switch, mirroring
transport.git_host_api's own --body-env contract exactly) reads a body a
caller's harness has ALREADY STAGED via `loadout-stage-body --create-branch
<branch>` -- the SAME identity-stamped mechanism (transport.body_env.
stage_caller_body / read_caller_body_bytes) every calling role uses for
the update/comment path, extended (lr-e1e2fb) to also cover PR CREATION.
`<branch>` is the git branch that will open the new PR; this verb resolves
its OWN current branch via `push.git_coords.current_branch` (git
rev-parse) BEFORE reading, and requires the staged stamp's create_branch to
match -- never a caller-typed value on this verb's own argv at all. There
is no filesystem path anywhere in this flow: loadout computes the staging
location, the caller supplies body CONTENT (via `loadout-stage-body`'s own
stdin), and this verb's --body-env is a constant-argv switch.

--body-stdin remains available for a caller that already has the body
JSON-wrapped and prefers a single invocation over the two-step stage/push
sequence. An --body-stdin invalid-JSON error (see _read_body_stdin) points
to --body-env (naming the loadout-stage-body --create-branch invocation
directly) as the sole recommended next step (lr-efbcc6: the raw
printf/echo-redirect staging fallback this pointer used to ALSO name is
retired now that loadout-stage-body is guard-admitted for every calling
role -- see docs/integration.md's "Retired: hand-writing the staged pair"
section).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.commit_subjects import REAL_MERGE_METHOD
from clagentic_loadout.merge.errors import TitleInvalidError
from clagentic_loadout.merge.title_gate import check_pr_title
from clagentic_loadout.platform_detect import (
    PLATFORM_FORGEJO,
    PLATFORM_GITHUB,
    PlatformResolutionError,
    resolve_platform,
)
from clagentic_loadout.push import git_coords, github_backend, identity
from clagentic_loadout.push.branch_commit_check import (
    CommitCheckUnavailableError,
    StrayMergeCommitError,
    check_branch_for_stray_merge_commits,
)
from clagentic_loadout.push.cleanliness_check import (
    CleanlinessCheckError,
    ScratchLitterFoundError,
    check_cleanliness,
)
from clagentic_loadout.push.cleanliness_config import load_scratch_patterns
from clagentic_loadout.push.errors import (
    AuthorMismatchError,
    BodyEmptyError,
    GitPushError,
    HostDeniedError,
    MissingIssueLinkError,
    NamespaceDeniedError,
    PrOpenError,
    PushUsageError,
    RemoteResolutionError,
)
from clagentic_loadout.push.forgejo_backend import create_pr as forgejo_create_pr
from clagentic_loadout.push.forgejo_backend import get_pr_body as forgejo_get_pr_body
from clagentic_loadout.push.forgejo_backend import update_pr as forgejo_update_pr
from clagentic_loadout.push.crew_identity import (
    CrewBotIdentityNotResolvableError,
    is_recognized_crew_caller,
    resolve_crew_bot_identity,
)
from clagentic_loadout.push.git_hermeticity import (
    GitVersionTooOldError,
    RepoLocalConfigHazardError,
)
from clagentic_loadout.push.git_push import git_push_with_token
from clagentic_loadout.push.host_guard import (
    check_host_allowed,
    resolve_allowed_hosts,
)
from clagentic_loadout.push.identity_config import (
    InvalidBuilderIdentityConfigError,
    load_builder_identity,
)
from clagentic_loadout.push.issue_link import (
    enforce_issue_link,
    normalize_closes_trailer,
    normalize_task_trailer,
)
from clagentic_loadout.push.lease_control import resolve_lease
from clagentic_loadout.push.namespace_guard import (
    check_namespace_allowed,
    resolve_allowed_namespaces,
)
from clagentic_loadout.push.remote_readback import (
    RemoteReadbackError,
    read_remote_head,
    verify_remote_authorship,
)
from clagentic_loadout.transport.body_env import (
    BODY_ENV_NOT_EPHEMERAL_NOTE,
    BodyEnvError,
    read_caller_body_bytes,
)
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token_result as _resolve_token_result,
)
from clagentic_loadout.transport.git_host_api import (
    DEFAULT_GIT_HOST_BASE_URL,
    GIT_HOST_BASE_URL_ENV_VAR,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.transport.readback_envelope import (
    READBACK_ENVELOPE_KEY,
    READBACK_SOURCE_GIT_LS_REMOTE,
    READBACK_SOURCE_READ_UNAVAILABLE,
    Readback,
)

# ---------------------------------------------------------------------------
# Exit codes — one reserved range for the push verb.
# ---------------------------------------------------------------------------

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_TOKEN_FETCH_FAILED = 2
EXIT_PUSH_FAILED = 3
EXIT_PR_FAILED = 4
EXIT_REMOTE_ERROR = 7
EXIT_BODY_EMPTY = 10
EXIT_NAMESPACE_DENIED = 20
EXIT_AUTHOR_MISMATCH = 21
EXIT_MISSING_ISSUE_LINK = 24
EXIT_PR_TITLE_INVALID = 25
EXIT_SCRATCH_LITTER_FOUND = 26
#: RETIRED (lr-e1e2fb): previously "--body-file names a path that does not
#: resolve to a readable regular file." --body-file is removed -- no verb in
#: this package accepts a caller-supplied filesystem path for PR-body
#: content. Kept as a named constant (unused, never raised) so the exit-code
#: NUMBER 27 stays reserved rather than being silently repurposed.
EXIT_BODY_FILE_UNREADABLE = 27
#: --body-env was requested but no body is staged for this caller/branch, or
#: the staged body's identity stamp does not match this invocation's
#: resolved branch (transport.body_env.BodyEnvError, lr-e1e2fb). Distinct
#: from EXIT_BODY_EMPTY (the --body-stdin JSON-shape failure): this code is
#: specifically "the sanctioned staging mechanism has nothing valid for this
#: invocation," never a raw filesystem/path failure.
EXIT_BODY_ENV_UNAVAILABLE = 28
#: The branch being pushed carries a commit in <fetched base>..HEAD with a
#: non-Conventional-Commits subject (push.branch_commit_check,
#: StrayMergeCommitError) -- the shape a stray, not-yet-landed merge commit
#: from another PR always has. A push-time backstop for the SAME grammar
#: merge.commit_subjects already enforces at merge time (EXIT_COMMIT_SUBJECT
#: _INVALID=30 on the merge verb), fired here instead, minutes after the
#: offending commit was introduced rather than hours later after build,
#: review, and security audit have already run against it. See --merge-method
#: / --skip-branch-commit-check.
EXIT_STRAY_MERGE_COMMIT = 29
#: The Forgejo API host derived from the live git remote (push.git_coords.
#: parse_forgejo_coords) is not present in the caller-configured
#: allowed-host set (push.host_guard, lr-0e39f9). Fires BEFORE any
#: credential is resolved or git push is attempted -- see
#: push.host_guard.check_host_allowed's own docstring for the full
#: host-anchoring rationale (--allowed-host / CLAGENTIC_LOADOUT_PUSH_
#: ALLOWED_HOSTS; permissive default when unconfigured, mirroring
#: EXIT_NAMESPACE_DENIED's own posture for a different dimension of the
#: same push target).
EXIT_HOST_DENIED = 31
#: The resolved git version on this host is below push.git_hermeticity.
#: MIN_GIT_VERSION, or the target repo's LOCAL .git/config carries a
#: hermeticity hazard (a repo-local credential.* entry, an
#: http.*.extraheader entry, or an includeIf.* directive) that environment
#: isolation alone cannot neutralize (push.git_hermeticity.
#: GitVersionTooOldError / RepoLocalConfigHazardError, lr-a868d2). Fires
#: BEFORE any credentialed git subprocess spawns -- fail-closed, no
#: override flag; see push.git_hermeticity's own module docstring for why.
EXIT_HERMETICITY_FAILED = 32


class PushVerbError(Exception):
    """Raised for any push failure that should terminate the process with a
    specific exit code. Carries the intended exit code as `.code`."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise PushVerbError(message, code)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="push",
        description=(
            "push -- bot-attributed commit push + PR open (or update) on "
            "either Forgejo or GitHub. Never merges, never pushes to main. "
            "Body input: --body-env (a bare switch; reads a body a caller's "
            "harness already staged via `loadout-stage-body --create-branch "
            "<branch>` -- no JSON escaping, no caller-supplied filesystem "
            "path) or --body-stdin (JSON-wrapped "
            "{\"body\": \"...\"} read from stdin) -- exactly one is required "
            "to create a PR; supplying both is a usage error. --title is "
            "gated against the Conventional Commits grammar (see "
            "--skip-title-check; a non-conformant title exits "
            f"{EXIT_PR_TITLE_INVALID}). Before pushing (create-PR path "
            "only), untracked-and-unignored files matching a configurable "
            "scratch-pattern list (.clagentic/loadout/config.yaml "
            "push.scratch_patterns) are WARNED about on stderr; --strict "
            f"fails instead, exit {EXIT_SCRATCH_LITTER_FOUND}. On a "
            "--merge-method='merge' repo (default), every commit in "
            "<fetched --base>..HEAD is also checked against the same "
            "Conventional Commits grammar; a non-conformant subject (e.g. a "
            "stray merge commit from another PR) fails closed, exit "
            f"{EXIT_STRAY_MERGE_COMMIT} -- see --skip-branch-commit-check. "
            "--force-with-lease/--no-force-with-lease explicitly control "
            "whether the push forces with a lease; the resolved value and "
            "its origin are always printed to stderr before the push runs "
            "-- see those flags' own help. --dry-run performs a read-only "
            "push attempt through the same minted credential path, "
            "surfacing the full transcript with no ref updated on the "
            "remote. --verbose/--trace enables git's own verbose push "
            "output plus a GIT_TRACE passthrough (also reachable via the "
            "CLAGENTIC_LOADOUT_PUSH_GIT_TRACE env var) for phase-level "
            "diagnosis -- see those flags' own help."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  echo '{\"body\":\"plain text PR body\"}' | \\\n"
            "    loadout-stage-body --caller builder --create-branch $(git rev-parse --abbrev-ref HEAD)\n"
            "  push --caller builder --title 'feat: add x' --body-env\n"
            "  echo '{\"body\":\"...\"}' | push --caller builder --title "
            "'feat: add x' --body-stdin\n"
            "  push --caller builder --update-pr --pr 42 --title 'new title'\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"push {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help=f"Role/name whose token is resolved via the credential provider "
        f"(default: {DEFAULT_ROLE!r}). MUST be the value the invoking "
        f"harness/guard-hook has already attested for this spawn -- this "
        f"verb consumes it as an opaque config key, never re-authenticates "
        f"it (see transport.credential_provider's module docstring).",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="PR title. Required to create a PR. Validated against the "
        "Conventional Commits grammar (merge.title_gate, the same check "
        "the merge gate applies) before any push or PR call — see "
        "--skip-title-check.",
    )
    parser.add_argument(
        "--body-stdin",
        action="store_true",
        help="Read the PR body as JSON ({\"body\": \"...\"}) from stdin. "
        "Optional for --update-pr — omit to leave the existing body "
        "untouched. Mutually exclusive with --body-env; --body-env is "
        "usually the more ergonomic path when composing valid JSON inline "
        "is awkward -- see that flag's own help.",
    )
    parser.add_argument(
        "--body-env",
        action="store_true",
        dest="body_env",
        help="Read the PR body from a location this deployment already "
        "sanctioned and staged ahead of time -- a bare switch, "
        "no caller-supplied filesystem path anywhere. On the create path, "
        "stage first via `loadout-stage-body --caller <role> "
        "--create-branch <branch>`; this verb resolves its OWN current "
        "branch (git rev-parse --abbrev-ref HEAD) and requires the staged "
        "body's identity stamp to match. On --update-pr, stage via "
        "`loadout-stage-body --caller <role> --target-pr <n>` instead -- "
        "this verb checks the stamp against --pr. A missing stage or a "
        "mismatch exits EXIT_BODY_ENV_UNAVAILABLE. Mutually exclusive with "
        "--body-stdin; supplying both is a usage error. Optional for "
        "--update-pr — omit to leave the existing body untouched. "
        + BODY_ENV_NOT_EPHEMERAL_NOTE,
    )
    parser.add_argument(
        "--update-pr",
        action="store_true",
        dest="update_pr",
        help="Update an existing PR's title/body instead of pushing + "
        "creating a new one. Requires --pr and at least one of --title/"
        "--body-stdin/--body-env. No git push, no re-authoring. Supplying "
        "a body on this path ALSO requires exactly one of --replace-body/"
        "--append-body (see those flags) -- there is no default body mode.",
    )
    parser.add_argument("--pr", type=int, default=None, dest="pr_number", help="PR number to update.")
    body_mode_group = parser.add_mutually_exclusive_group()
    body_mode_group.add_argument(
        "--replace-body",
        action="store_true",
        dest="replace_body",
        help="On --update-pr, REPLACE the PR's existing body wholesale "
        "with the supplied --body-stdin/--body-env content. REQUIRED "
        "(together with --append-body, mutually exclusive with it) "
        "whenever a body is supplied on --update-pr -- there is no "
        "default body mode; supplying a body without one of these two "
        "flags is a usage error. Destructive: the prior body is gone. "
        "Ignored (has no effect) on the PR-create path, where there is no "
        "prior body to replace.",
    )
    body_mode_group.add_argument(
        "--append-body",
        action="store_true",
        dest="append_body",
        help="On --update-pr, APPEND the supplied --body-stdin/--body-env "
        "content to the PR's EXISTING body (read fresh via a GET "
        "immediately before the update PATCH, then joined with a blank-"
        "line separator) rather than replacing it. REQUIRED (together "
        "with --replace-body, mutually exclusive with it) whenever a body "
        "is supplied on --update-pr. Ignored on the PR-create path.",
    )
    parser.add_argument("--base", default="main", help="Base branch for the PR (default: main).")
    parser.add_argument(
        "--platform",
        choices=(PLATFORM_FORGEJO, PLATFORM_GITHUB),
        default=None,
        help="Target platform override. When omitted, auto-detected from "
        "the resolved git remote URL.",
    )
    parser.add_argument(
        "--repo",
        default="",
        help="Explicit owner/repo override. Required for --platform github. "
        "Derived from the git remote on Forgejo when omitted.",
    )
    parser.add_argument(
        "--repo-path",
        default="",
        dest="repo_path",
        help="Local path to the target repo root. Defaults to the git "
        "toplevel of the current working directory.",
    )
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help="Accepted for parity with the other loadout verbs' flag of "
        "the same name, but NOT currently consumed by any push-path API "
        "call in this verb: the Forgejo API base this verb actually calls "
        "is always derived from the git remote URL, regardless of this "
        f"flag. Ignored for GitHub. (default env fallback if it were "
        f"consumed: ${GIT_HOST_BASE_URL_ENV_VAR}, or "
        f"{DEFAULT_GIT_HOST_BASE_URL!r} if unset.)",
    )
    parser.add_argument(
        "--issue",
        type=int,
        default=None,
        dest="issue_number",
        help="Issue number this PR closes. When supplied, a 'Closes #NN' "
        "trailer is auto-injected if absent, and the push fails closed if "
        "the body ends up without a matching trailer. Omit when there is "
        "genuinely no linked issue.",
    )
    parser.add_argument(
        "--task-id",
        default=None,
        dest="task_id",
        help="Opaque work-item ref for this invocation's dispatch envelope "
        "(schemas/common.json's task_id fragment). When supplied, a "
        "'Task: <task_id>' trailer is auto-injected into the PR body if "
        "absent (release.dispatch.parse_trailers reads this same trailer "
        "back out downstream). Never resolved or validated here -- passed "
        "through verbatim as an opaque string. Omit when the invocation "
        "carries no task_id (e.g. an ad-hoc mode).",
    )
    parser.add_argument(
        "--bot-name",
        default=None,
        help="Bot identity name to re-author commits to before push. "
        "Omit to skip re-authoring (see --require-bot-identity).",
    )
    parser.add_argument(
        "--bot-email",
        default=None,
        help="Bot identity email to re-author commits to before push. Must "
        "be supplied together with --bot-name.",
    )
    parser.add_argument(
        "--require-bot-identity",
        action="store_true",
        dest="require_bot_identity",
        help="Fail closed (EXIT_AUTHOR_MISMATCH) if --bot-name/--bot-email "
        "are not both supplied, instead of skipping re-authoring.",
    )
    parser.add_argument(
        "--allowed-namespace",
        action="append",
        dest="allowed_namespaces",
        default=None,
        help="Restrict the push target owner to this namespace. Repeatable. "
        "When omitted, falls back to CLAGENTIC_LOADOUT_ALLOWED_NAMESPACES "
        "(comma-separated); when neither is set, no namespace restriction "
        "is enforced.",
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        dest="allowed_hosts",
        default=None,
        help="Restrict the Forgejo API host this invocation will attach a "
        "credential to (the host derived from the live git remote, NOT the "
        "--git-host-base-url flag -- see that flag's own help). Repeatable; "
        "each value is a bare 'host[:port]' or a full 'scheme://host[:port]' "
        "URL. When omitted, falls back to "
        "CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS (comma-separated); when "
        f"neither is set, no host restriction is enforced. A mismatch exits "
        f"{EXIT_HOST_DENIED} ({EXIT_HOST_DENIED}=EXIT_HOST_DENIED), before "
        "any credential is resolved. Ignored on --platform github (GitHub "
        "coordinate derivation from the git remote is not supported at all "
        "-- see --repo).",
    )
    parser.add_argument(
        "--skip-title-check",
        action="store_true",
        default=False,
        dest="skip_title_check",
        help="Bypass the Conventional Commits PR title gate (--title, on "
        "both create and --update-pr). Default: enforced. Use of this flag "
        "is logged to stderr for audit. On a non-conformant title, exits "
        f"{EXIT_PR_TITLE_INVALID} ({EXIT_PR_TITLE_INVALID}=EXIT_PR_TITLE_INVALID).",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        default=False,
        dest="strict_cleanliness",
        help="Fail the pre-push cleanliness check instead of warning "
        "(create-PR path only). Untracked-and-unignored files matching a "
        "configured scratch pattern "
        "(.clagentic/loadout/config.yaml push.scratch_patterns, default: "
        "pr-body-*, *scratch*, *.diff-check*, HANDOFF.md) fail closed "
        f"before any push, exit {EXIT_SCRATCH_LITTER_FOUND} "
        f"({EXIT_SCRATCH_LITTER_FOUND}=EXIT_SCRATCH_LITTER_FOUND). Default: "
        "warn on stderr and continue.",
    )
    lease_group = parser.add_mutually_exclusive_group()
    lease_group.add_argument(
        "--force-with-lease",
        action="store_true",
        default=None,
        dest="cli_force_with_lease",
        help="Force the push with `git push --force-with-lease`, and "
        "refresh the remote-tracking ref via `git fetch` immediately "
        "before the lease is evaluated (push.lease_control). ALWAYS wins "
        "over the auto-derived default (whether bot-identity re-authoring "
        "rewrote this branch's commits) -- the resolved value and its "
        "origin are always printed to stderr before the push runs, never "
        "inferred silently. Mutually exclusive with --no-force-with-lease.",
    )
    lease_group.add_argument(
        "--no-force-with-lease",
        action="store_false",
        default=None,
        dest="cli_force_with_lease",
        help="Push WITHOUT --force-with-lease, overriding the auto-derived "
        "default. Mutually exclusive with --force-with-lease. Omitting "
        "both flags falls back to the auto-derived default: forced only "
        "when bot-identity re-authoring rewrote this branch's commits "
        "(the ordinary case for a re-authored push against its own "
        "previously-pushed history).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        dest="dry_run",
        help="Read-only push attempt through the SAME minted per-caller "
        "credential path, the SAME hermeticity pre-flight, and the SAME "
        "single git-push call site a real push uses (`git push --dry-run`) "
        "-- no ref is updated on the remote. Surfaces the full transcript "
        "(including any `remote: `-prefixed sideband) on stderr under the "
        "caller's own identity -- the sanctioned substitute for shelling "
        "out to raw git when a push fails opaquely. Skips PR creation/"
        "update and the post-push remote readback (nothing was pushed to "
        "read back). Combine with --verbose/--trace for phase-level "
        "detail. Ignored (has no effect) on --update-pr, which never "
        "pushes.",
    )
    parser.add_argument(
        "--verbose",
        "--trace",
        action="store_true",
        default=False,
        dest="verbose",
        help="Enable git's own verbose push output (`git push -v`) plus "
        "the GIT_TRACE passthrough (packet/hook/transport trace), so a "
        "failed push's phase -- local hook / transport / remote "
        "negotiation / server hook -- is distinguishable without server-"
        "side log access. This is the discoverable form of the "
        "CLAGENTIC_LOADOUT_PUSH_GIT_TRACE environment variable (still "
        "honored as a compat alias -- either turns on the identical "
        "passthrough). All trace output passes through the same "
        "redaction choke point every other push-failure field uses "
        "(push.push_redaction.redact_push_secrets) before it can reach "
        "stdout, stderr, or any raised message -- see docs/"
        "push-failure-reporting.md for exactly what is and is not "
        "redacted.",
    )
    parser.add_argument(
        "--merge-method",
        default=REAL_MERGE_METHOD,
        dest="merge_method",
        help="The RESOLVED merge method this repo/PR will land with -- "
        f"the SAME value the merge gate's own branch commit-subject check "
        f"keys on (merge.commit_subjects.REAL_MERGE_METHOD, default "
        f"{REAL_MERGE_METHOD!r}). Only affects the branch commit-subject "
        "check below; never forwarded to any push or PR-open call. Pass "
        "the value your dispatcher already resolves for this repo's "
        "eventual `loadout-merge --merge-method` call, so the two stay in "
        "sync rather than deriving the signal twice.",
    )
    parser.add_argument(
        "--skip-branch-commit-check",
        action="store_true",
        default=False,
        dest="skip_branch_commit_check",
        help="Bypass the push-time branch commit-subject check (create-PR "
        "path only, --merge-method='merge' repos only). Default: enforced. "
        "Before pushing, every commit in <fetched base>..HEAD is checked "
        "against the same Conventional Commits grammar the merge gate's "
        "commit-subject check (merge.commit_subjects) applies at merge "
        "time -- catching a branch that carries a stray, not-yet-landed "
        "merge commit from another PR minutes after it was introduced "
        "rather than hours later at the merge gate. A CommitCheckUnavailable"
        "Error (the fetch/log itself failing, e.g. origin unreachable) is a "
        "soft-fail: warned on stderr, never blocks the push. A found "
        f"offender exits {EXIT_STRAY_MERGE_COMMIT} "
        f"({EXIT_STRAY_MERGE_COMMIT}=EXIT_STRAY_MERGE_COMMIT). Use of this "
        "flag is logged to stderr for audit.",
    )
    return parser


#: Pointer text appended to every --body-stdin content-validation failure
#: (lr-df5a11, operator-directed; lr-e1e2fb redesign; retired hand-write
#: fallback lr-efbcc6): names a copy-pasteable next step, not just "invalid
#: JSON," so a caller hitting this error comes away knowing exactly what to
#: run instead of improvising its own disk-staging location (the observed
#: failure mode this pointer exists to close -- staging a scratch file
#: inside .git/, under /root, or leaving it stray in the working tree, each
#: caught only by a human denying a permission prompt).
#:
#: --body-env (via loadout-stage-body --create-branch) is named as the
#: SOLE next step: it is the sanctioned mechanism (see this module's own
#: docstring, "BODY-INPUT ERGONOMICS") that removes the escaping step
#: entirely with NO caller-supplied filesystem path anywhere. A prior
#: revision of this pointer ALSO named a raw printf/echo-redirect two-step
#: staging fallback for a caller that "must use --body-stdin directly" --
#: that fallback is retired now that loadout-stage-body is guard-admitted
#: for every calling role this deployment runs (lr-1779c8);
#: naming a retired staging mechanism in this same error text would recreate
#: the exact contradiction this fix exists to close (a caller told to use
#: the sanctioned path AND handed a hand-write alternative in the same
#: breath).
_BODY_STDIN_STAGING_POINTER = (
    "RECOMMENDED: stage the body first via `loadout-stage-body --caller "
    "<role> --create-branch <branch>` (reading plain text from stdin, no "
    "JSON escaping required), then invoke this verb with --body-env instead "
    "of --body-stdin -- see that flag's own help and docs/integration.md."
)


def _unwrap_body_json(raw: bytes, *, source_label: str) -> str:
    """Decode + unwrap a `{"body": "<PR description>"}` JSON envelope,
    shared by every reader of that envelope shape (lr-2b20d2): both
    --body-stdin (typed directly by a caller) and --body-env (staged via
    `loadout-stage-body`, which documents this same {"body": ...} wrapper
    as its stdin contract) hand this function raw bytes and get back the
    unwrapped body string, or a BodyEmptyError naming exactly which
    reader's flag failed. Extracted from what was _read_body_stdin's
    inline logic so the two readers cannot drift out of agreement on what
    "the envelope" means -- see this module's BODY-INPUT ERGONOMICS
    docstring section for why the envelope exists at all, and this task's
    own history for why letting the two readers diverge is the defect
    class being closed here: *source_label* is only used for the
    caller-visible flag name in an error message (e.g. "--body-stdin" or
    "--body-env"), never to change unwrap behavior -- there is no
    content-sniffing here, the envelope is unconditionally required.
    """
    if len(raw) == 0:
        raise BodyEmptyError(
            f"{source_label} received empty input (0 bytes). The git host would "
            f"reject an empty PR body. {_BODY_STDIN_STAGING_POINTER}"
        )
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise BodyEmptyError(
            f"{source_label} does not contain valid JSON: {exc}. Expected "
            f'{{"body": "<PR description>"}}. {_BODY_STDIN_STAGING_POINTER}'
        ) from exc
    if not isinstance(parsed, dict):
        raise BodyEmptyError(
            f"{source_label} must contain a JSON OBJECT with a 'body' key; "
            f"got {type(parsed).__name__}. {_BODY_STDIN_STAGING_POINTER}"
        )
    body_value = parsed.get("body")
    if not isinstance(body_value, str) or not body_value.strip():
        raise BodyEmptyError(
            f"{source_label} has no non-empty 'body' string field "
            f"(got {body_value!r}). {_BODY_STDIN_STAGING_POINTER}"
        )
    return body_value


def _read_body_stdin() -> str:
    """Read + validate --body-stdin content. Raises BodyEmptyError (the
    shared push.errors vocabulary) on any validation failure; main()
    translates that to EXIT_BODY_EMPTY at the CLI boundary, mirroring how
    review.verb translates ReviewBodyStdinEmptyError."""
    raw = sys.stdin.buffer.read()
    return _unwrap_body_json(raw, source_label="--body-stdin")


def _read_body_env(
    *, caller: str, create_branch: str | None = None, target_pr: int | None = None
) -> str:
    """Read + validate --body-env content (lr-e1e2fb): a body a caller's
    harness already staged via `loadout-stage-body`, using the SAME
    identity-stamped read-and-consume API
    (transport.body_env.read_caller_body_bytes) reviewer roles already use
    for the update/comment path. No filesystem path is ever supplied by
    this verb's own caller -- EXACTLY ONE of *create_branch* / *target_pr*
    is THIS VERB's own resolved binding (the current git branch for the
    create path, or --pr for --update-pr), never a value read from a
    caller-facing flag whose value could vary the staging LOCATION.

    UNWRAPS the staged {"body": "..."} JSON envelope via the same
    _unwrap_body_json helper --body-stdin uses (lr-2b20d2): loadout-stage-body
    stages JSON-wrapped content -- that is its documented stdin contract, the
    same one review.verb's and transport.git_host_api's own staged-body
    readers already parse -- so a verbatim-bytes read here was the sole
    outlier reader disagreeing with that contract, shipping the literal
    wrapper (escaped newlines, braces and all) as the PR body. There is no
    content-sniffing: the envelope is unconditionally required, exactly as
    it is for --body-stdin.

    Raises PushVerbError(code=EXIT_BODY_ENV_UNAVAILABLE) if no body is
    staged for this caller/binding (missing stage, or a mismatched
    identity stamp); raises BodyEmptyError if the staged content is missing,
    not valid JSON, or reads back empty/whitespace-only, mirroring
    --body-stdin's own refusals (the git host would reject an empty PR body
    either way).
    """
    try:
        raw = read_caller_body_bytes(
            caller=caller, expect_create_branch=create_branch, expect_target_pr=target_pr
        )
    except BodyEnvError as exc:
        _fail(str(exc), code=EXIT_BODY_ENV_UNAVAILABLE)
    return _unwrap_body_json(raw, source_label="--body-env")


def _check_title_gate(args: argparse.Namespace, owner: str, repo: str) -> None:
    """Run the shared Conventional Commits title gate (merge.title_gate,
    reused verbatim — lr-6067) against --title when one is supplied, on
    BOTH the PR-open and --update-pr paths: a retitle via --update-pr can
    introduce an invalid title exactly as an open can.

    A no-op when --title was not supplied (e.g. an --update-pr call that
    only changes the body) — there is nothing to validate. No PR number
    exists yet at PR-open time, so `pr_number=None`; the merge gate's own
    call (merge.verb, unchanged by this) always has a real PR number, since
    a merge only ever targets an existing PR.

    Raises merge.errors.TitleInvalidError, caught at the CLI boundary in
    main() and mapped to EXIT_PR_TITLE_INVALID; a no-op when
    --skip-title-check was passed (logged here for audit, mirroring
    merge.verb's own bypass logging).
    """
    if args.title is None:
        return
    if args.skip_title_check:
        print(
            f"push: PR title gate BYPASSED via --skip-title-check for "
            f"{owner}/{repo} (title={args.title!r})",
            file=sys.stderr,
        )
    pr_number = args.pr_number if args.update_pr else None
    check_pr_title(args.title, pr_number, owner, repo, skip=args.skip_title_check)


def _run_cleanliness_check(project_root: Path, *, strict: bool) -> None:
    """Pre-push scratch-litter check (lr-d7a8, push.cleanliness_check).

    The push verb never runs `git add` (safe by design), so it can push
    silently with unignored, untracked scratch litter still sitting in the
    tree unless something flags it. This is that flag: a config-driven
    pattern list (push.cleanliness_config, .clagentic/loadout/config.yaml
    `push.scratch_patterns`) matched against
    `git ls-files --others --exclude-standard` (untracked AND unignored
    only — a gitignored scratch file is silent, per task requirement).

    Default: WARN to stderr, listing every matched file and which pattern
    matched, and continue. `strict=True`: the SAME matches raise
    ScratchLitterFoundError, caught at the CLI boundary in main() and
    mapped to EXIT_SCRATCH_LITTER_FOUND — no push, no PR call.

    TOOL-ALTITUDE (lr-d7a8): this decision belongs to the verb itself, not
    a guard hook or the merge gate — it never touches the working tree.

    A CleanlinessCheckError (the underlying `git ls-files` call itself
    failing) is treated as a soft-fail: warned to stderr and otherwise
    ignored, never blocking a push over the check's own execution failure
    (a --strict caller who wants a hard failure here can already read the
    warning and treat it as a signal in CI).
    """
    patterns = load_scratch_patterns(project_root).patterns
    try:
        matches = check_cleanliness(project_root, patterns=patterns, strict=strict)
    except CleanlinessCheckError as exc:
        print(f"push: pre-push cleanliness check could not run -- {exc}", file=sys.stderr)
        return

    if matches:
        listed = "\n".join(f"  {path!r} (matched pattern {pattern!r})" for path, pattern in matches)
        print(
            f"push: WARNING -- untracked, unignored scratch litter found "
            f"before push:\n{listed}",
            file=sys.stderr,
        )


def _run_branch_commit_check(
    project_root: Path, *, base_branch: str, remote: str, merge_method: str, skip: bool
) -> None:
    """Push-time branch commit-subject check (lr-dd1742,
    push.branch_commit_check) -- catches a branch carrying a stray,
    not-yet-landed merge commit from another PR at push time instead of
    hours later at the merge gate (merge.commit_subjects' own
    EXIT_COMMIT_SUBJECT_INVALID). No-op for any *merge_method* other than
    "merge" (see push.branch_commit_check's own docstring for why this
    reuses merge.commit_subjects.REAL_MERGE_METHOD rather than a second
    signal), and when *skip* is True (logged here for audit, mirroring
    _check_title_gate's own bypass logging).

    A CommitCheckUnavailableError (the underlying git fetch/log failing --
    unreachable origin, no such base branch) is a SOFT-FAIL: warned on
    stderr, never blocks the push -- this check's own inability to run must
    never refuse a push that would otherwise be clean, exactly like
    _run_cleanliness_check's own CleanlinessCheckError handling.

    Raises push.branch_commit_check.StrayMergeCommitError when at least one
    offending commit is found; caught at the CLI boundary in main() and
    mapped to EXIT_STRAY_MERGE_COMMIT.
    """
    if skip:
        print(
            f"push: branch commit-subject check BYPASSED via "
            f"--skip-branch-commit-check for {project_root} "
            f"(base={base_branch!r}, remote={remote!r})",
            file=sys.stderr,
        )
        return
    try:
        check_branch_for_stray_merge_commits(
            project_root, base_branch, merge_method=merge_method, remote=remote, skip=False,
        )
    except CommitCheckUnavailableError as exc:
        print(
            f"push: branch commit-subject check could not run -- {exc}",
            file=sys.stderr,
        )


def _resolve_repo_root(repo_path_override: str) -> Path:
    if repo_path_override:
        p = Path(repo_path_override).resolve()
        if not p.is_dir():
            raise PushUsageError(
                f"--repo-path {repo_path_override!r} does not exist or is not a directory"
            )
        return p
    import subprocess

    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    if probe.returncode == 0 and probe.stdout.strip():
        return Path(probe.stdout.strip()).resolve()
    return Path.cwd()


def _resolve_effective_bot_identity(
    bot_name: str | None,
    bot_email: str | None,
    *,
    caller: str,
    platform: str,
    config_root: str | Path | None = None,
    provider_verified_app_slug: str | None = None,
) -> tuple[str | None, str | None, str]:
    """Resolve the (name, email) to re-author commits to, PLUS a source label
    for diagnostics. Precedence (lr-f145d2, most-specific first; lr-43c8d7
    inserts tier 2 below tier 1's old position):

      1. --bot-name/--bot-email (CLI, always wins when BOTH are supplied on
         this invocation's own argv).
      2. PROVIDER-VERIFIED (lr-43c8d7 ADDITION): when *caller* is a
         recognized crew caller on GitHub (same gate as the tier below) AND
         *provider_verified_app_slug* is non-empty, the bot identity is
         derived from THAT value -- a credential-minting provider's own
         broker-verified App slug (transport.credential_provider.
         ResolvedToken.app_slug), checked against reality at token-mint
         time, rather than an operator-typed `github_app.slugs.<caller>`
         config entry naming the SAME fact. See "PROVIDER-VERIFIED TIER"
         below for the full rationale and non-goals.
      3. Derived from *caller* + config (push.crew_identity), when *caller*
         is a recognized crew caller (present in this deployment's declared
         `github_app.callers` registry) AND *platform* is `PLATFORM_GITHUB`
         -- see below. This is now the STANDALONE FALLBACK within the
         recognized-crew-caller gate: reached when tier 2 has nothing to
         offer (no provider, or a provider that supplied an empty
         app_slug -- both real, non-error states).
      4. push.identity_config.load_builder_identity (the deployment-tier
         `builder_identity:` user-config section, lr-4e8a43 task ADDITION 2).
      5. Neither (source "none") -- ONLY reachable when *caller*/*platform*
         is NOT a recognized crew caller on GitHub; see FAIL-CLOSED below.

    DERIVED-FROM-CALLER, UNCONDITIONAL FOR A RECOGNIZED CREW CALLER ON
    GITHUB (lr-f145d2, restating lr-0902ba remediation item 1): lr-4e8a43
    wired load_builder_identity into this verb, but shipped OPT-IN -- with
    no `builder_identity:` section configured, re-authoring silently
    no-ops and this verb inherits ambient git config. Verified live: a
    deployment that had ALREADY declared a caller -> GitHub App slug
    mapping (`github_app.slugs`/`github_app.callers`, the SAME config
    `review.github_backend.resolve_own_login` and
    `merge.reviewer_login.resolve_reviewer_login` already derive bot LOGINS
    from) still produced commits attributed to the invoking human's own
    personal account, because nothing derived a bot COMMIT identity from
    that already-declared mapping. This tier closes that gap: for a
    recognized crew caller pushing to GitHub, the bot identity is derived
    from data the deployment already provided for token minting -- no
    second identity string to configure, no new required config key.

    PLATFORM IS PART OF THE GATE, NOT THE RESOLVER'S FAILURE PATH
    (lr-f145d2 follow-up, post-review correction): an earlier revision
    gated only on caller here and let resolve_crew_bot_identity raise for
    a non-GitHub platform, which converted EVERY Forgejo push from a
    recognized crew caller -- the overwhelming majority of this
    deployment's actual push traffic -- into a hard EXIT_AUTHOR_MISMATCH
    failure with no fix available. is_recognized_crew_caller now takes
    *platform* directly and returns False for any non-GitHub platform,
    so a Forgejo push from a recognized caller never enters tier 2 at all
    and falls through to tier 3/4 exactly as it did before this module
    existed -- see is_recognized_crew_caller's own docstring for the full
    account.

    FAIL-CLOSED FOR A RECOGNIZED CREW CALLER ON GITHUB (task requirement,
    NOT additive): when *caller*+*platform* is_recognized_crew_caller but
    resolve_crew_bot_identity cannot resolve an identity for it (no
    github_app.slugs.<caller> entry configured), this function raises
    CrewBotIdentityNotResolvableError rather than falling through to tier 3
    or tier 4 -- today's ambient-git-config inherit fallback is
    UNREACHABLE for a recognized crew caller on GitHub. This is the one
    FAIL path in this function that is not "config present but malformed"
    (that is InvalidBuilderIdentityConfigError, tier 3, unchanged from
    lr-4e8a43).

    EXTERNAL-USER SAFETY (task non-negotiable constraint, argued explicitly
    in this task's own PR body): tier 2/3 is gated on
    is_recognized_crew_caller, which itself requires *platform* to be
    GitHub AND a NON-EMPTY `github_app.callers` list to be configured at
    all. A deployment with no `github_app` section (the out-of-the-box,
    external-consumer state) -- or a caller string that is simply not in
    that list, or any push to a non-GitHub platform -- gets
    is_recognized_crew_caller() == False unconditionally, so this function
    falls through past tier 2/3 exactly as it did before this change, all
    the way to tier 4/5 with ZERO behavior change from lr-4e8a43's shipped
    shape. Only a deployment that has ALREADY told loadout who its crew
    callers are, pushing to GitHub specifically (exactly the deployments
    this bug was observed against), sees tier 2/3 activate at all. A
    deployment with no credential-provider identity integration (the common
    external case, and every deployment before this task) always has
    *provider_verified_app_slug*=None, so tier 2 never fires and tier 3
    behaves exactly as tier 2 did before this task.

    PROVIDER-VERIFIED TIER, NON-GOALS (lr-43c8d7): this tier does NOT widen
    is_recognized_crew_caller's own gate -- a caller absent from
    `github_app.callers`, or a push to Forgejo, never reaches tier 2
    regardless of what *provider_verified_app_slug* carries (the platform
    gate stays exactly where lr-f145d2 put it, see
    is_recognized_crew_caller's own docstring). This tier only changes
    WHICH SLUG resolve_crew_bot_identity uses once that gate has already
    passed -- it is a slug-SOURCE precedence change within an
    already-existing tier, not a new activation path. It also does NOT
    rescue a caller this function would otherwise raise
    CrewBotIdentityNotResolvableError for: *this function itself* still
    fails closed on that condition even when *provider_verified_app_slug*
    is None (unresolvable via config, provider not yet consulted) -- see
    `_run_create_pr`'s own call-site comment for WHY that ordering is
    preserved (a token mint is not spent ahead of a push that config alone
    already proves cannot proceed) and how the provider-verified override
    is actually applied there: as a SECOND call to this function, made
    AFTER the token has been minted, only when the FIRST call (made before
    minting, with *provider_verified_app_slug* omitted) already resolved
    via `"caller-derived"` -- i.e. this tier OVERRIDES an already-resolvable
    config-derived identity's slug source, it does not open a new path to
    resolvability a bare config lookup did not already have. A future task
    that wants the provider tier to also RESCUE an otherwise-unresolvable
    caller would need to change `_run_create_pr`'s ordering (mint before
    the fail-closed gate), which is a bigger behavior change than this task
    was scoped for and is named here as a deliberate non-goal, not an
    oversight.

    --bot-name/--bot-email STILL WIN when BOTH are explicitly supplied on
    this invocation's own argv -- an explicit per-call override is never
    silently replaced by a derived or deployment-wide default.

    Returns (name, email, source) where source is one of "cli",
    "provider-verified", "caller-derived", "config", or "none" (diagnostic
    label only, never part of the JSON success envelope).

    Raises:
        identity_config.InvalidBuilderIdentityConfigError: the
            builder_identity: section is present but malformed (tier 4,
            unchanged from lr-4e8a43) -- fails closed rather than silently
            falling back to no identity, since a malformed config the
            operator believes is active is exactly the false-assurance
            class that wiring closes.
        CrewBotIdentityNotResolvableError: *caller* is a recognized crew
            caller but neither tier 2 nor tier 3 could resolve an identity
            for it (see FAIL-CLOSED above).
    """
    if bot_name and bot_email:
        return bot_name, bot_email, "cli"

    if is_recognized_crew_caller(caller, platform, config_root=config_root):
        derived_name, derived_email = resolve_crew_bot_identity(
            caller, platform, config_root=config_root,
            provider_verified_app_slug=provider_verified_app_slug,
        )
        source = (
            "provider-verified"
            if provider_verified_app_slug and provider_verified_app_slug.strip()
            else "caller-derived"
        )
        return derived_name, derived_email, source

    config_name, config_email = load_builder_identity(config_root=config_root)
    if config_name and config_email:
        return config_name, config_email, "config"

    return bot_name, bot_email, "none"


def _perform_remote_readback(
    *,
    remote: str,
    branch: str,
    project_root: Path,
    expected_bot_email: str | None,
) -> dict:
    """Post-push authoritative remote state (lr-4e8a43): read the branch's
    HEAD back FROM THE REMOTE via `git ls-remote` (push.remote_readback,
    never a local `git rev-parse` re-read) and, when a bot identity was
    supplied for this push, verify the AUTHOR of the commit the remote now
    holds -- not merely that the ref advanced (task ADDITION 1).

    ADDITIVE-ONLY, NEVER A NEW HARD FAILURE (task scope: build the RETURN,
    not the ENFORCEMENT): a `git push` that already returned success has, by
    definition, landed on the remote -- a subsequent transient failure of
    THIS diagnostic re-read (e.g. a network blip in the seconds after a
    successful push) must never turn an already-successful push+PR-open into
    a hard failure. On any RemoteReadbackError this returns a degraded
    result (`remote_head_sha=None`) and prints a stderr warning naming the
    exact failure -- the caller still gets EXIT_OK, and the envelope
    honestly reports that the remote fact could not be confirmed rather than
    silently omitting the field or substituting a local value (which would
    reintroduce the exact defect this task exists to close).

    Returns a dict merged directly into the JSON success envelope:
      remote_head_sha: the SHA `git ls-remote` reported, or None on failure.
      remote_head_sha_source: "git_ls_remote" on success, None on failure --
        the provenance tag a downstream consumer keys validation off (see
        push.remote_readback module docstring).
      readback (lr-361de3): the SAME stable {verified, source, detail} shape
        every other remote-mutating verb's envelope now carries (see
        transport.readback_envelope) -- verified=True with
        source="git_ls_remote" on success; verified=False with
        source="read_unavailable" on an unconfirmed readback. Rendered
        ALONGSIDE the pre-existing remote_head_sha/remote_head_sha_source
        keys above (back-compat: an existing caller reading those two keys
        directly is unaffected) rather than replacing them -- this task's
        cross-verb stability requirement (ONE predicate a consumer can apply
        to every verb's envelope) is satisfied by the new `readback` key
        without breaking a caller who already reads the older, push-specific
        keys.
      authorship_checked / authorship_matches: present only when
        expected_bot_email was supplied AND the readback itself succeeded --
        a caller with no configured bot identity sees neither key rather
        than a misleading `false`.

    STILL ADDITIVE, NOT FAIL-CLOSED (explicit decision, lr-361de3): unlike
    the merge/close readbacks this task also adds (which DO fail the verb on
    an unconfirmed readback), this function's own failure mode is UNCHANGED
    from lr-4e8a43's original, already-adjudicated design -- a `git push`
    that already returned success has, by construction, landed; a subsequent
    read failure here is diagnostic, not evidence the push itself failed.
    Merge/close had NO readback at all before this task and are net-new
    fail-closed gates; push already had an intentionally additive readback
    this task is not asked to relitigate. See this task's PR body for the
    full rationale.
    """
    try:
        readback = read_remote_head(remote, branch, project_root)
    except RemoteReadbackError as exc:
        print(
            f"push: WARNING -- post-push remote readback could not confirm "
            f"the pushed branch on the remote: {exc}. The push and PR-open "
            f"calls already succeeded; this is a diagnostic re-read only. "
            f"remote_head_sha is omitted (never substituted with a local "
            f"value) so a caller cannot mistake this for a confirmed remote "
            f"fact.",
            file=sys.stderr,
        )
        return {
            "remote_head_sha": None,
            "remote_head_sha_source": None,
            READBACK_ENVELOPE_KEY: Readback(
                verified=False,
                source=READBACK_SOURCE_READ_UNAVAILABLE,
                detail={"reason": str(exc)},
            ).to_dict(),
        }

    envelope = {
        "remote_head_sha": readback.remote_head_sha,
        "remote_head_sha_source": readback.source,
        READBACK_ENVELOPE_KEY: Readback(
            verified=True,
            source=READBACK_SOURCE_GIT_LS_REMOTE,
            detail={"remote_head_sha": readback.remote_head_sha},
        ).to_dict(),
    }

    if expected_bot_email:
        authorship = verify_remote_authorship(
            readback.remote_head_sha, expected_bot_email, project_root
        )
        envelope["authorship_checked"] = authorship.checked
        envelope["authorship_matches"] = authorship.matches
        if not authorship.matches:
            print(
                f"push: WARNING -- the commit the remote confirms at "
                f"{readback.remote_head_sha!r} is authored by "
                f"{authorship.actual_email!r}, not the expected bot identity "
                f"{authorship.expected_email!r}. ADDITIVE-ONLY: this push is "
                f"NOT failed over the mismatch (enforcement is a separate, "
                f"config-gated follow-up) -- reported in the envelope for "
                f"the caller to act on.",
                file=sys.stderr,
            )

    return envelope


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    builder_identity_config_root: str | Path | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable).

    `builder_identity_config_root` (lr-4e8a43): a TEST-ONLY injection seam,
    mirroring `token_provider`/`opener` above -- overrides the user-level
    config root `push.identity_config.load_builder_identity` reads
    `builder_identity:` from (see `_resolve_effective_bot_identity`). A real
    CLI invocation never passes this; it exists so a test can point the
    lookup at an isolated `tmp_path` instead of the real machine's
    `~/.config/clagentic/loadout/config.yaml` -- there is no corresponding
    CLI flag, since the config ROOT itself is deployment-fixed, not a
    per-invocation choice (mirrors identity_config.load_builder_identity's
    own `config_root` parameter, which is documented there as
    primarily-for-tests too).
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
            opener=opener,
            builder_identity_config_root=builder_identity_config_root,
        )
    except PushVerbError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return exc.code
    except PushUsageError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except BodyEmptyError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return EXIT_BODY_EMPTY
    except TitleInvalidError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return EXIT_PR_TITLE_INVALID
    except ScratchLitterFoundError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return EXIT_SCRATCH_LITTER_FOUND
    except StrayMergeCommitError as exc:
        print(f"push: {exc}", file=sys.stderr)
        return EXIT_STRAY_MERGE_COMMIT


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    builder_identity_config_root: str | Path | None = None,
) -> int:
    # 1. Argument-shape validation, before any I/O.
    if args.body_env and args.body_stdin:
        raise PushUsageError(
            "--body-env and --body-stdin are mutually exclusive body "
            "sources -- supply exactly one."
        )

    has_body_input = args.body_env or args.body_stdin
    if args.update_pr:
        if args.pr_number is None:
            raise PushUsageError("--update-pr requires --pr <n>.")
        if args.title is None and not has_body_input:
            raise PushUsageError(
                "--update-pr requires at least one of --title, --body-env, "
                "or --body-stdin."
            )
        # Explicit body-mode requirement (lr-2500b7, operator directive):
        # a body supplied on --update-pr with NO stated mode is a usage
        # error -- there is no default, and none is inferred. Only the
        # caller knows whether this edit is a deliberate revision (replace)
        # or an addition (append); the tool guessing at that intent is the
        # exact defect class this closes (see this module's own
        # "BODY-MODE" section below for the full rationale).
        if has_body_input and not (args.replace_body or args.append_body):
            raise PushUsageError(
                "--update-pr with a body (--body-env/--body-stdin) requires "
                "exactly one of --replace-body or --append-body -- there is "
                "no default body mode. State whether this edit REPLACES the "
                "PR's existing body or APPENDS to it; guessing is refused."
            )
    elif args.title is None:
        raise PushUsageError("--title is required to create a PR.")

    if bool(args.bot_name) != bool(args.bot_email):
        raise PushUsageError("--bot-name and --bot-email must be supplied together.")

    # 2. caller/project_root resolved BEFORE body reading: --body-env on the
    # create path binds to THIS process's own current branch
    # (push.git_coords.current_branch), which requires project_root to
    # compute -- so branch resolution must happen before the read, not after.
    caller = args.caller or DEFAULT_ROLE
    project_root = _resolve_repo_root(args.repo_path)

    # 3. Body resolution + issue-link enforcement (create path, and update
    # when --body-env or --body-stdin was passed). --body-env (lr-e1e2fb, the
    # RECOMMENDED path -- no caller-supplied filesystem path anywhere) is
    # checked first so a caller supplying it never pays for a stdin read at
    # all. On the create path it binds to this verb's own resolved current
    # branch; on --update-pr it binds to --pr instead (an existing PR, the
    # same binding loadout-stage-body's own --target-pr mode uses).
    #
    # PROTECTED-BRANCH CHECK BEFORE CONSUMING (security re-audit nit,
    # follow-up): on the create path, --body-env READ-AND-CONSUMES the
    # staged body
    # (transport.body_env.read_caller_body_bytes) -- _run_create_pr's own
    # protected-branch refusal (detached HEAD resolves to the literal
    # "HEAD", which IS in git_coords.PROTECTED_BRANCHES) fires LATER than
    # this point, so without this check here a caller on a detached HEAD
    # would stage a body, have it consumed by this read, THEN hit the
    # protected-branch refusal deep in _run_create_pr with nothing left to
    # retry -- a re-stage-from-scratch tax for a failure this verb could
    # have caught before ever touching the staged body. Checked here,
    # BEFORE the read, using the SAME current_branch value _run_create_pr
    # itself resolves moments later (git_coords.current_branch is a cheap,
    # idempotent `git rev-parse --abbrev-ref HEAD` -- recomputing it is not
    # a second source of truth, just an earlier call to the same one).
    body: str | None = None
    if args.body_env:
        if args.update_pr:
            body = _read_body_env(caller=caller, target_pr=args.pr_number)
        else:
            current_branch = git_coords.current_branch(project_root)
            if current_branch in git_coords.PROTECTED_BRANCHES:
                _fail(
                    f"refusing to push from detached HEAD or protected "
                    f"branch: {current_branch!r}. Nothing was consumed -- "
                    f"the staged --body-env content (if any) is untouched "
                    f"and does not need to be re-staged once you are on a "
                    f"real branch.",
                    code=EXIT_PUSH_FAILED,
                )
            body = _read_body_env(caller=caller, create_branch=current_branch)
    elif args.body_stdin:
        body = _read_body_stdin()
    elif not args.update_pr:
        raise BodyEmptyError(
            f"PR body is required to create a PR. Pass --body-env (stage "
            f"first via loadout-stage-body, RECOMMENDED) or --body-stdin "
            f"(JSON-wrapped). {_BODY_STDIN_STAGING_POINTER}"
        )

    if body is not None and args.task_id:
        body = normalize_task_trailer(body, args.task_id)

    if body is not None and args.issue_number is not None:
        body = normalize_closes_trailer(body, args.issue_number)
        try:
            enforce_issue_link(body, args.issue_number)
        except MissingIssueLinkError as exc:
            _fail(str(exc), code=EXIT_MISSING_ISSUE_LINK)

    allowed_namespaces = resolve_allowed_namespaces(
        frozenset(args.allowed_namespaces) if args.allowed_namespaces else None
    )
    allowed_hosts = resolve_allowed_hosts(
        frozenset(args.allowed_hosts) if args.allowed_hosts else None
    )

    # 4. Platform resolution.
    raw_remote_url = git_coords.read_remote_url_best_effort(project_root)
    try:
        resolved_platform = resolve_platform(args.platform, raw_remote_url)
    except PlatformResolutionError:
        _fail(
            "cannot auto-detect platform: no --platform flag given and no "
            "git remote URL is available. Pass --platform explicitly.",
            code=EXIT_REMOTE_ERROR,
        )
    args.platform = resolved_platform

    if args.update_pr:
        return _run_update_pr(
            args, body=body, caller=caller, project_root=project_root,
            allowed_namespaces=allowed_namespaces, allowed_hosts=allowed_hosts,
            token_provider=token_provider, opener=opener,
        )

    return _run_create_pr(
        args, body=body, caller=caller, project_root=project_root,
        allowed_namespaces=allowed_namespaces, allowed_hosts=allowed_hosts,
        token_provider=token_provider, opener=opener,
        builder_identity_config_root=builder_identity_config_root,
    )


def _resolve_owner_repo_for_update(args: argparse.Namespace, project_root: Path) -> tuple[str, str, str]:
    """Returns (owner, repo, api_base) — api_base is "" on the GitHub path
    (github_backend hardcodes its own public API base)."""
    if args.platform == PLATFORM_GITHUB:
        if not args.repo:
            raise RemoteResolutionError(
                "--repo is required for --platform github (e.g. --repo "
                "some-owner/some-repo); GitHub coordinate derivation from "
                "git remote is not supported."
            )
        owner, repo = git_coords.parse_owner_repo(args.repo)
        return owner, repo, ""

    if args.repo:
        owner, repo = git_coords.parse_owner_repo(args.repo)
        remote_name = git_coords.tracking_remote(git_coords.current_branch(project_root), project_root)
        raw_url = git_coords.remote_url(remote_name, project_root)
        api_base, _o, _r = git_coords.parse_forgejo_coords(raw_url)
    else:
        remote_name = git_coords.tracking_remote(git_coords.current_branch(project_root), project_root)
        raw_url = git_coords.remote_url(remote_name, project_root)
        api_base, owner, repo = git_coords.parse_forgejo_coords(raw_url)
    return owner, repo, api_base


#: Separator joined between the PR's existing body and newly-appended
#: content on --append-body (lr-2500b7). A blank line, matching ordinary
#: markdown paragraph separation -- neither string is re-wrapped or
#: otherwise transformed, so this is the only formatting choice made here.
_APPEND_BODY_SEPARATOR = "\n\n"


def _warn_if_ahead_of_remote_tracking(project_root: Path) -> None:
    """Warn on stderr when the current local branch is AHEAD of its own
    remote-tracking ref (lr-2500b7): the exact situation the originating
    incident was misdiagnosed as -- a caller ran --update-pr (a metadata-
    only verb that never pushes) while local commits sat unpushed, and
    reasonably assumed the metadata call meant those commits had landed too.

    Best-effort only: never raises, never affects the exit code. `git
    rev-list --left-right --count <upstream>...HEAD` failing (e.g. no
    upstream configured) is silently skipped -- this is a diagnostic nicety
    for the common case, not a new precondition on every --update-pr call.
    """
    import subprocess

    upstream = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        cwd=str(project_root), capture_output=True, text=True,
    )
    if upstream.returncode != 0 or not upstream.stdout.strip():
        return
    counts = subprocess.run(
        ["git", "rev-list", "--left-right", "--count", "@{u}...HEAD"],
        cwd=str(project_root), capture_output=True, text=True,
    )
    if counts.returncode != 0:
        return
    parts = counts.stdout.split()
    if len(parts) != 2:
        return
    _behind, ahead = parts
    if ahead.isdigit() and int(ahead) > 0:
        print(
            f"push: WARNING -- the local branch is {ahead} commit(s) ahead of "
            f"its remote-tracking ref {upstream.stdout.strip()!r}. --update-pr "
            f"NEVER pushes (metadata-only: title/body PATCH) -- those local "
            f"commits are NOT on the remote after this call. Use the create "
            f"path (push without --update-pr) to push them.",
            file=sys.stderr,
        )


def _run_update_pr(
    args: argparse.Namespace,
    *,
    body: str | None,
    caller: str,
    project_root: Path,
    allowed_namespaces: frozenset[str],
    allowed_hosts: frozenset[str],
    token_provider: TokenProvider | None,
    opener,
) -> int:
    try:
        owner, repo, api_base = _resolve_owner_repo_for_update(args, project_root)
    except RemoteResolutionError as exc:
        _fail(str(exc), code=EXIT_REMOTE_ERROR)

    try:
        check_namespace_allowed(owner, repo, allowed_namespaces=allowed_namespaces)
    except NamespaceDeniedError as exc:
        _fail(str(exc), code=EXIT_NAMESPACE_DENIED)

    # Host anchoring (lr-0e39f9): api_base is "" on the GitHub path
    # (github_backend hardcodes its own public API base, see
    # _resolve_owner_repo_for_update's own docstring) -- an empty string
    # never legitimately matches a configured allowed-host entry, so this
    # check is skipped unconditionally for GitHub rather than requiring
    # every deployment's allowlist to also carry an empty-string entry.
    if args.platform != PLATFORM_GITHUB:
        try:
            check_host_allowed(api_base, allowed_hosts=allowed_hosts)
        except HostDeniedError as exc:
            _fail(str(exc), code=EXIT_HOST_DENIED)

    _check_title_gate(args, owner, repo)

    print(f"push: resolving token for caller={caller!r} (PR update)", file=sys.stderr)
    active_provider = (
        token_provider
        if token_provider is not None
        else resolve_platform_provider(args.platform)
    )
    try:
        token = _resolve_token_result(caller, active_provider, repo=f"{owner}/{repo}").token
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    # Append mode (lr-2500b7): GET the CURRENT body immediately before the
    # update PATCH, then concatenate -- update_pr()'s own PATCH contract on
    # both backends stays a single unambiguous whole-field replace; append
    # is composed here, at the call site, not as a mode flag threaded into
    # either backend's update_pr().
    effective_body = body
    if body is not None and args.append_body:
        try:
            if args.platform == PLATFORM_GITHUB:
                current_body = github_backend.get_pr_body(owner, repo, args.pr_number, token=token, opener=opener)
            else:
                current_body = forgejo_get_pr_body(api_base, owner, repo, args.pr_number, token=token, opener=opener)
        except PrOpenError as exc:
            _fail(str(exc), code=EXIT_PR_FAILED)
        effective_body = (
            f"{current_body}{_APPEND_BODY_SEPARATOR}{body}" if current_body else body
        )

    try:
        if args.platform == PLATFORM_GITHUB:
            github_backend.update_pr(owner, repo, args.pr_number, token=token, title=args.title, body=effective_body, opener=opener)
            pr_url = f"https://github.com/{owner}/{repo}/pull/{args.pr_number}"
        else:
            forgejo_update_pr(api_base, owner, repo, args.pr_number, token=token, title=args.title, body=effective_body, opener=opener)
            pr_url = f"{api_base}/{owner}/{repo}/pulls/{args.pr_number}"
    except PrOpenError as exc:
        _fail(str(exc), code=EXIT_PR_FAILED)

    # NO head_sha ON THIS PATH (lr-2500b7, defect 1): --update-pr never
    # pushes (see this module's own docstring, "PR open, and the
    # update-existing-PR path") -- a LOCAL `git rev-parse HEAD` value
    # reported here, formatted identically to the create path's genuinely-
    # pushed remote fact, is exactly the defect a caller reasonably reads as
    # "this SHA is on the remote" when it never went anywhere. Rather than
    # perform a remote readback of a value this verb did not itself produce
    # (misleading in the OTHER direction -- implying this call caused that
    # remote state), this path reports NO sha field at all and an explicit
    # `pushed: false` marker, plus a best-effort stderr warning when the
    # local branch is ahead of its tracking ref (the exact scenario the
    # originating incident was misdiagnosed as).
    _warn_if_ahead_of_remote_tracking(project_root)

    print(f"push: PR #{args.pr_number} updated: {pr_url}")
    print(json.dumps({
        "pr_number": args.pr_number, "pr_url": pr_url, "owner": owner, "repo": repo,
        "pushed": False,
    }))
    return EXIT_OK


def _run_create_pr(
    args: argparse.Namespace,
    *,
    body: str,
    caller: str,
    project_root: Path,
    allowed_namespaces: frozenset[str],
    allowed_hosts: frozenset[str],
    token_provider: TokenProvider | None,
    opener,
    builder_identity_config_root: str | Path | None = None,
) -> int:
    branch = git_coords.current_branch(project_root)
    if branch in git_coords.PROTECTED_BRANCHES:
        _fail(f"refusing to push from detached HEAD or protected branch: {branch!r}", code=EXIT_PUSH_FAILED)

    if args.platform == PLATFORM_GITHUB:
        if not args.repo:
            _fail(
                "--repo is required for --platform github (e.g. --repo "
                "some-owner/some-repo); GitHub coordinate derivation from "
                "git remote is not supported.",
                code=EXIT_REMOTE_ERROR,
            )
        owner, repo = git_coords.parse_owner_repo(args.repo)
        api_base = ""
        remote_name = git_coords.tracking_remote(branch, project_root)
    else:
        if args.repo:
            owner, repo = git_coords.parse_owner_repo(args.repo)
            remote_name = git_coords.tracking_remote(branch, project_root)
            raw_url = git_coords.remote_url(remote_name, project_root)
            api_base, _o, _r = git_coords.parse_forgejo_coords(raw_url)
        else:
            remote_name = git_coords.tracking_remote(branch, project_root)
            raw_url = git_coords.remote_url(remote_name, project_root)
            api_base, owner, repo = git_coords.parse_forgejo_coords(raw_url)

    try:
        check_namespace_allowed(owner, repo, allowed_namespaces=allowed_namespaces)
    except NamespaceDeniedError as exc:
        _fail(str(exc), code=EXIT_NAMESPACE_DENIED)

    # Host anchoring (lr-0e39f9): api_base is "" on the GitHub path (a fixed
    # literal set two branches above, never derived from the git remote) --
    # skipped unconditionally for GitHub, mirroring _run_update_pr's own
    # identical guard (see that function's own comment for the full
    # rationale).
    if args.platform != PLATFORM_GITHUB:
        try:
            check_host_allowed(api_base, allowed_hosts=allowed_hosts)
        except HostDeniedError as exc:
            _fail(str(exc), code=EXIT_HOST_DENIED)

    _check_title_gate(args, owner, repo)

    # Builder-identity resolution happens BEFORE token resolution -- UNCHANGED
    # ordering from lr-4e8a43 (cheap, purely local YAML validation +
    # caller-derivation, no credential mint or network call yet): a
    # malformed builder_identity: config, or a recognized crew caller with
    # NO resolvable config-tier slug, must fail loud before this invocation
    # spends a token mint on a push it is about to refuse anyway. This
    # pass resolves with provider_verified_app_slug=None (the provider has
    # not been consulted yet) -- see below for how a provider's verified
    # slug is folded in AFTER minting, as an OVERRIDE on an already-resolved
    # identity, never as a way to rescue a failure this pass raises.
    try:
        effective_bot_name, effective_bot_email, identity_source = _resolve_effective_bot_identity(
            args.bot_name, args.bot_email,
            caller=caller, platform=args.platform,
            config_root=builder_identity_config_root,
        )
    except InvalidBuilderIdentityConfigError as exc:
        _fail(
            f"deployment builder_identity config is present but invalid -- {exc} "
            f"Refusing to fall back to no bot identity silently: a malformed "
            f"config an operator believes is active must fail loud, not "
            f"produce false assurance.",
            code=EXIT_AUTHOR_MISMATCH,
        )
    except CrewBotIdentityNotResolvableError as exc:
        _fail(
            f"caller {caller!r} is a recognized crew caller but its bot "
            f"commit identity could not be derived -- {exc} Refusing to "
            f"fall back to ambient git config for a recognized crew caller "
            f"-- a mis-attributed commit is unrecoverable once merged.",
            code=EXIT_AUTHOR_MISMATCH,
        )

    print(f"push: resolving token for caller={caller!r}", file=sys.stderr)
    active_provider = (
        token_provider
        if token_provider is not None
        else resolve_platform_provider(args.platform)
    )
    try:
        resolved = _resolve_token_result(caller, active_provider, repo=f"{owner}/{repo}")
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)
    token = resolved.token

    # Provider-verified OVERRIDE (lr-43c8d7), applied AFTER the identity gate
    # above already succeeded: when the resolved provider supplied a
    # verified app_slug for this mint AND the identity that gate resolved
    # was itself "caller-derived" (i.e. it came from
    # github_app.slugs.<caller> config, the tier this override sits above --
    # see _resolve_effective_bot_identity's own docstring), re-derive the
    # SAME (name, email) shape using the provider's slug instead of the
    # config one. This can only ever RUN when tier 2/3's gate already
    # accepted the caller as resolvable -- it never rescues a
    # CrewBotIdentityNotResolvableError the gate above already raised (that
    # ordering constraint -- resolving identity before spending a token
    # mint -- is preserved exactly as lr-4e8a43 established it; see this
    # task's own PR body for the trade-off this decision makes explicit).
    # --bot-name/--bot-email (identity_source == "cli") and
    # builder_identity: config (identity_source == "config") are NEVER
    # touched by this override -- both already won at a higher or
    # independent precedence tier.
    if identity_source == "caller-derived" and resolved.app_slug and resolved.app_slug.strip():
        effective_bot_name, effective_bot_email = resolve_crew_bot_identity(
            caller, args.platform, config_root=builder_identity_config_root,
            provider_verified_app_slug=resolved.app_slug,
        )
        identity_source = "provider-verified"

    if identity_source == "provider-verified":
        print(
            f"push: derived bot commit identity from caller={caller!r} via "
            f"the credential provider's VERIFIED app_slug "
            f"(name={effective_bot_name!r}) -- this took precedence over "
            f"github_app.slugs config, --bot-name/--bot-email, and "
            f"builder_identity: config.",
            file=sys.stderr,
        )
    elif identity_source == "caller-derived":
        print(
            f"push: derived bot commit identity from caller={caller!r} via "
            f"this deployment's github_app.slugs mapping "
            f"(name={effective_bot_name!r}) -- --bot-name/--bot-email and "
            f"builder_identity: config were not consulted.",
            file=sys.stderr,
        )
    elif identity_source == "config":
        print(
            f"push: using deployment builder_identity from config "
            f"(name={effective_bot_name!r}) -- --bot-name/--bot-email were "
            f"not supplied on this invocation.",
            file=sys.stderr,
        )

    try:
        history_rewritten = identity.pin_commits_to_bot_identity(
            effective_bot_name, effective_bot_email, args.base, project_root,
            fail_closed_on_missing=args.require_bot_identity,
        )
    except AuthorMismatchError as exc:
        _fail(str(exc), code=EXIT_AUTHOR_MISMATCH)

    _run_cleanliness_check(project_root, strict=args.strict_cleanliness)

    _run_branch_commit_check(
        project_root,
        base_branch=args.base,
        remote=remote_name,
        merge_method=args.merge_method,
        skip=args.skip_branch_commit_check,
    )

    # LEASE CONTROL (lr-f57f13, D5 DECIDED): never derive force_with_lease
    # silently from history_rewritten alone -- resolve_lease applies the
    # explicit CLI override first, refreshes the remote-tracking ref before
    # trusting a forced lease evaluation, and returns the origin label this
    # call prints below BEFORE the push runs (see push.lease_control's own
    # module docstring for the full defect this closes: loadout re-authoring
    # commits on essentially every push silently forced a lease evaluation
    # against a STALE remote-tracking ref, converting an ordinary conflict
    # into git's least-explained rejection shape, "(stale info)"). `token`
    # is passed through so the pre-lease fetch runs via the SAME
    # credentialed envelope as the push itself (pre-merge security review
    # finding: the first shipped version fetched via an ambient credential
    # helper instead of the minted token) -- see push.lease_control's own
    # docstring, "CREDENTIALED FETCH, NOT AN AMBIENT ONE".
    # HERMETICITY PRE-FLIGHT (lr-a868d2): resolve_lease's own pre-lease fetch
    # (when a lease is being forced) runs through the SAME credentialed,
    # hermetic envelope git_push_with_token itself uses -- a
    # GitVersionTooOldError/RepoLocalConfigHazardError raised there is a
    # fail-closed hermeticity refusal, NOT an ordinary fetch failure (which
    # resolve_lease already degrades to a printed warning on its own, via a
    # narrower `except GitFetchError` that does not catch either of these) --
    # so it must terminate this invocation rather than silently letting the
    # push proceed. The push call below performs the SAME pre-flight
    # independently (it does not skip this check just because resolve_lease
    # already ran it -- see git_push_with_token's own docstring), so a
    # no-lease-forced invocation (which never calls resolve_lease's fetch at
    # all) is still covered.
    try:
        lease = resolve_lease(
            cli_force_with_lease=args.cli_force_with_lease,
            history_rewritten=history_rewritten,
            remote=remote_name,
            branch=branch,
            project_root=project_root,
            token=token,
        )
    except (GitVersionTooOldError, RepoLocalConfigHazardError) as exc:
        _fail(str(exc), code=EXIT_HERMETICITY_FAILED)
    print(
        f"push: force-with-lease={lease.force_with_lease} "
        f"(origin={lease.origin!r}, pre-lease fetch attempted={lease.fetch_attempted})",
        file=sys.stderr,
    )
    if lease.fetch_warning:
        print(f"push: WARNING -- {lease.fetch_warning}", file=sys.stderr)

    other_platform_label = PLATFORM_FORGEJO if args.platform == PLATFORM_GITHUB else PLATFORM_GITHUB
    try:
        git_push_with_token(
            remote_name, branch, token, project_root,
            force_with_lease=lease.force_with_lease,
            lease_origin=lease.origin,
            platform=args.platform,
            other_platform_label=other_platform_label,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
    except (GitVersionTooOldError, RepoLocalConfigHazardError) as exc:
        _fail(str(exc), code=EXIT_HERMETICITY_FAILED)
    except GitPushError as exc:
        _fail(str(exc), code=EXIT_PUSH_FAILED)

    if args.dry_run:
        # --DRY-RUN STOPS HERE (lr-68039e): nothing was pushed to the
        # remote (git_push_with_token already printed the full transcript
        # to stderr) -- a PR create/update call and the post-push remote
        # readback below both assume a real push landed, which this
        # invocation deliberately did not perform. EXIT_OK: a dry-run that
        # completed its git-push --dry-run attempt (whether or not the
        # attempt itself would have succeeded -- a non-zero dry-run exit
        # already raised GitPushError above and exited EXIT_PUSH_FAILED)
        # is a successful DIAGNOSTIC run.
        print(
            f"push: --dry-run complete for {owner}/{repo}#{branch} -- no ref "
            f"was updated on the remote; see the transcript above.",
            file=sys.stderr,
        )
        return EXIT_OK

    # Post-push authoritative remote readback (lr-4e8a43) -- performed BEFORE
    # the PR-open call so its result is available to enrich a PR-open
    # failure message too (see the KNOWN TRAP handling below: a redundant
    # PR-create 409 after a successful push is exactly the case where a
    # caller most needs to know the push landed).
    remote_readback_envelope = _perform_remote_readback(
        remote=remote_name, branch=branch, project_root=project_root,
        expected_bot_email=effective_bot_email,
    )

    try:
        if args.platform == PLATFORM_GITHUB:
            pr_number = github_backend.create_pr(
                owner, repo, token=token, head=branch, base=args.base,
                title=args.title, body=body, opener=opener,
            )
            pr_url = f"https://github.com/{owner}/{repo}/pull/{pr_number}"
        else:
            pr_number = forgejo_create_pr(
                api_base, owner, repo, token=token, head=branch, base=args.base,
                title=args.title, body=body, opener=opener,
            )
            pr_url = f"{api_base}/{owner}/{repo}/pulls/{pr_number}"
    except PrOpenError as exc:
        # KNOWN TRAP (task requirement 5): a redundant PR-create call can
        # 409/422 ("PR already exists for this head/base pair") even though
        # the git push immediately above ALREADY LANDED SUCCESSFULLY -- the
        # documented, observed failure mode (exit 4) that taught callers to
        # distrust this verb's exit code and reach for their own local
        # reads instead. ADDITIVE-ONLY (task scope): this still exits
        # EXIT_PR_FAILED -- fixing the exit code itself is an enforcement-
        # shaped behavior change (silently returning EXIT_OK for a call that
        # did not open/return a PR would be its own new failure mode for any
        # caller relying on the current contract) -- but the message is
        # unambiguous about WHICH part failed, and reports the confirmed
        # remote_head_sha the readback above already obtained, so a caller
        # is never left re-deriving "did the push actually land" from exit
        # code 4 alone.
        remote_sha_note = (
            f" The push itself already landed -- remote_head_sha "
            f"{remote_readback_envelope.get('remote_head_sha')!r} was "
            f"confirmed via a fresh {remote_readback_envelope.get('remote_head_sha_source')!r} "
            f"readback before this PR-open call was attempted."
            if remote_readback_envelope.get("remote_head_sha")
            else ""
        )
        likely_redundant = (
            f" HTTP {exc.status_code} may mean a PR for this head/base pair "
            f"already exists (a REDUNDANT create, not a failed push) -- check "
            f"the target repo for an existing open PR from this branch before "
            f"retrying create."
            if exc.status_code in (409, 422)
            else ""
        )
        _fail(f"{exc}{remote_sha_note}{likely_redundant}", code=EXIT_PR_FAILED)

    # NO bare local head_sha (lr-361de3, FALSE-REPORT FIX): prior to this
    # fix, the envelope carried BOTH a bare `head_sha` (this verb's own local
    # `git rev-parse HEAD`, via _resolve_head_sha) AND
    # `remote_readback_envelope`'s `remote_head_sha` (the SAME commit,
    # confirmed via a genuine `git ls-remote` round-trip) -- a caller had no
    # way to tell which of the two same-shaped SHA fields was the
    # authoritative remote fact and which was a local read formatted
    # identically to it. Verified real and unfixed on origin/main (this
    # task's own research pass, seq 2 item (c) instance 1) -- the in-flight
    # lr-2500b7 branch touched ONLY _run_update_pr, never this create path.
    # THE FIX: drop the bare local value entirely. remote_readback_envelope's
    # `remote_head_sha` (and the new `readback` key below) is now the SOLE
    # SHA this envelope reports -- there is exactly one field a caller can
    # read as "the pushed commit," and it is always the remote-confirmed
    # one (or explicitly absent/unverified when the readback itself could
    # not confirm it -- see _perform_remote_readback's own docstring).
    envelope = {
        "pr_number": pr_number, "pr_url": pr_url, "owner": owner, "repo": repo,
    }
    envelope.update(remote_readback_envelope)

    print(f"push: PR #{pr_number} opened: {pr_url}")
    print(json.dumps(envelope))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
