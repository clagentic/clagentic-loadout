"""git_host_api.py — authenticated Forgejo REST call verb.

Wave B slice 1 (lr-3ba8, tome #688). Ported from the reference git-host-API
transport; the source copy stays primary until its separate CUT OVER +
RETIRE + VERIFY-GONE task per the migration plan.

GitHub-target routing (lr-104a): PATH is ordinarily a path relative to the
resolved git-host (Forgejo) base -- but when PATH is itself an ABSOLUTE
http(s) URL whose host is GitHub (see _is_github_target), this verb resolves
the caller's GitHub reader token instead of the Forgejo token, and issues the
request against that URL directly with NO git-host base prepended. Before
this fix, every PATH -- including an already-absolute GitHub URL -- had the
git-host base blindly prepended, producing a malformed
'http://<git-host-base><https://api.github.com/...>' URL (lr-10d8
EXIT_CURL_FAILED) and leaving no working way to target GitHub through this
shared transport. A relative Forgejo path is completely unaffected: this is
strictly an additional routing branch, not a change to the existing
git-host-base-prepend path.

--verify-comment / --pr-sha (the Forgejo-shaped post-and-verify machinery
below) apply only to Forgejo's own /api/v1/repos/... path shape and never
match an absolute GitHub URL, so a GitHub-target GET simply passes through
that machinery as a no-op -- this fix is scoped to routing/auth, not to
reimplementing the Forgejo verify-comment contract for GitHub.

Renamed from forge_api.py (lr-9ade, folded into lr-39f8; internal identifiers
completed lr-9fdbed): the vendor-neutral "forge" noun for the
git-hosting-platform concept this module talks to has been fully replaced
with "git_host" -- module filename, cross-file references (entry_points,
cli.py import, other modules' prose), AND internal identifiers
(GitHostApiError, git_host_base params, etc.) all use the git-host
vocabulary now. Vendor identifiers (PLATFORM_FORGEJO, 'forgejo' literals,
FORGEJO_BASE_URL compat alias) are UNCHANGED — this module still talks to
the Forgejo REST API specifically.

--body-stdin / --body-env are the TWO body paths for a write method
(POST/PATCH/PUT/DELETE); exactly one is required. The reference transport
carried a legacy --body-file staging flag as back-compat; this port does
not — that is the whole point of building fresh (lr-3ba8 task description).
--body-env (lr-10a996, BODY-TRANSPORT half) is NOT a revival of that
back-compat flag: it takes no caller-supplied path at all and reads a
FIXED, CALLER-NAMESPACED location (transport.body_env.
resolve_caller_body_path, lr-3a7ae8: $TMPDIR/clagentic-loadout/
body.<caller>.json, keyed off this verb's own --caller value, never a
caller-typed path) that a caller's own harness stages before invoking this
verb — see transport.body_env's module docstring for why this closes the
"body data on argv/pipe" gap --body-stdin alone leaves open (a piped/echoed
JSON body still puts the varying payload on the shell command line every
call, which a static per-invocation argv analyzer can never allowlist)
without reopening the caller-chosen-arbitrary-path class the legacy
--body-file flag was, and why the path is namespaced per --caller rather
than shared (two concurrent same-TMPDIR callers can no longer collide on
one staged file).

Auth: the caller's git-host token is ALWAYS resolved through the configured
credential_provider.TokenProvider (see that module) — never read from an
inherited environment variable. There is no non-forgeable discriminator
between "this process is the real caller" and "this process inherited a
token from something else", so an ambient token in the environment is never
trusted, regardless of any acting-as marker. Self-fetch-per-invocation is
correct on every dispatch path because a relay/orchestration layer that
injects credentials does so from the same provider, yielding the identical
token.

--caller/attested-invoker fail-closed binding (lr-82c385, tome #700): an
EXPLICIT --caller value is bound, in this module, to the ATTESTED invoking
identity resolved via transport.attestation.resolve_identity (configured
provider > sidecar adapter > built-in OS-user fallback) — see bind_caller.
A mismatch is refused BEFORE any I/O: an identity may use only its OWN
credential, and this refusal happens unconditionally, even where a
named-agent allowlist configured elsewhere would otherwise admit the
mismatched role. This is layer (1)->(2) of the three-layer trust model
(attested invoking identity -> crew role/--caller -> credential grantor);
credential_provider.resolve_token and merge.authority.check_authority
remain layer (2)->(3), consuming --caller/--role as the pre-existing,
already-attested opaque value they have always treated it as (lr-e5eeab) —
this task does not change either of those seams. An OMITTED --caller is
never checked against the attested identity (see bind_caller's own
docstring for why) — this preserves the existing default-to-DEFAULT_ROLE
behavior byte-for-byte.

Repo context (lr-ea28, GitHub-URL extraction added lr-5f7971): a repo-scoped
minting provider (e.g. a GitHub-App-style installation-token mint) needs to
know which owner/repo it is minting for. For a relative Forgejo path, this
module derives that "owner/repo" string via _REPOS_PATH_RE (the same regex
the known-bad-owner check already uses). For an absolute GitHub URL (see
GitHub-target routing above), _REPOS_PATH_RE never matches -- GitHub's REST
API has no /api/v1/ prefix -- so a dedicated _GITHUB_REPOS_URL_RE extracts
owner/repo from the URL's own /repos/{owner}/{repo}/... shape instead. Both
paths feed the same resolve_token(role, provider, repo=...) call; call_repo
is None for a target that is not repo-scoped (e.g. /api/v1/user, or a
GitHub /user, /orgs/... URL). A provider with no notion of repo scoping
(StaticTokenProvider, and CommandTokenProvider configured with no {repo}
placeholder) is unaffected either way.

--verify-comment + --pr-sha post-and-verify gate (load-bearing safety
property, preserved unchanged in spirit from the source):
  After a successful POST to issues/<pr>/comments, perform a mandatory
  readback: (a) GET /api/v1/user to resolve the caller's own bot login from
  the token, (b) GET issues/<pr>/comments on the SAME pr_number extracted
  from the POST path, (c) confirm a comment authored by that login exists
  with the posted body AND a created_at at or after the moment immediately
  before this POST was issued (freshness anchor, small clock-skew
  tolerance) -- this closes the gap where a bare login+body substring match
  could be satisfied by a stale/pre-existing comment. (d) return the
  comment id from the READBACK response, never from the POST alone.
  A POST to issues/<pr>/comments that OMITS --verify-comment is a hard
  refusal BEFORE any I/O: a comments POST can never complete
  fire-and-forget.

--expect-verdict-block <reviewer> (lr-30c0d0): a reviewer role's PR verdict
is a fenced ```review-result``` block (merge-gate hard requirement,
merge.verdict). A guard-bash-style argv scanner that forbids a backtick in
any --body-stdin producer argument cannot tell a markdown fence apart from
shell command substitution -- it has to deny both, which left reviewers with
no way to post the one thing the merge gate requires without hand-authoring
the fence through a shell producer. This flag moves fence CONSTRUCTION into
this tool: the caller's --body-stdin JSON carries structured fields
(`review_status`, `head_sha`, plus the ordinary `body` prose) with ZERO
backticks anywhere in the argv or the piped bytes; this module appends the
fence itself via clagentic_loadout.merge.verdict.build_verdict_block (the
SAME function the merge gate's own re-parse -- merge.verdict.
read_reviewer_verdict -- treats as the fence's one authoring source), then
posts the combined body. Requires --caller (the fence's `reviewer` field),
--verify-comment (this is always a comments POST) and --pr-sha (the fence's
`head_sha` field is the caller's own evaluated SHA, not a second value to
keep in sync). `pr_number` in the fence is taken from the POST path itself
-- never re-declared in the stdin JSON -- so there is no way for the fenced
`pr_number` to disagree with the PR this is actually posted to. After the
ordinary --verify-comment readback confirms the comment landed, this mirrors
that verification one step further: the verified comment's OWN body is
re-parsed via merge.verdict.parse_verdict_block and checked field-for-field
against what was requested, so a fence that landed truncated or mangled in
transit is caught here rather than surfacing later as an opaque merge-gate
refusal.

--caller-tracking-id <id> (lr-10a996, BODY-TRANSPORT half -- pairs with the
emit-and-verify readback, lr-482c20, a separate task): the general-purpose
counterpart to --expect-verdict-block for a caller that wants to carry an
opaque work-item tracking reference alongside a comment body without
authoring shell-hostile constructs. Before this flag, a caller needing to
stamp a comment with its own tracking id (e.g. a lore task id) had no way to
do so other than a hand-authored fenced block (the same backtick-in-argv
trap --expect-verdict-block fixes for the reviewer-verdict case) or staging
a state note via a `cat >> $HOME/... << EOF` heredoc with a
$VAR-substituted redirect target -- neither of which a static shell-argv
guard can analyze, so both fail closed with a false block and a manual
operator approval prompt. This flag moves note
CONSTRUCTION into this tool exactly like --expect-verdict-block moves fence
construction in: clagentic_loadout.transport.note_compose.build_composed_body
appends a fenced ```loadout-note``` block (distinct fence-language tag from
```review-result```, and carrying no merge-gate enforcement semantics of its
own) to the --body-stdin 'body' prose, in-process, with zero backticks ever
required in the argv or the piped bytes. Requires POST to
issues/<pr>/comments and --verify-comment; composes on top of whatever
--expect-verdict-block already produced when both flags are supplied
together (the tracking-id note always trails the verdict fence). The
tracking id itself is caller-opaque -- this module never interprets it
(CLAUDE.md rule 6a: no lore/LORE_* dependency in product code).

--delete-own-comment (lr-e2ce66): self-delete-own-comment capability,
mirroring --verify-comment's belt-and-suspenders posture but PRE-flighted
before the mutating call rather than after it. ADMISSIBLE OPERATION
(operator-agreed): delete a comment IFF (author login ==
the caller's OWN bot identity, resolved from the token via the same
resolve_bot_login GET /api/v1/user this module's --verify-comment readback
already uses) AND (comment body contains NO fenced ```review-result```
block, re-parsed via merge.verdict.parse_verdict_block -- the SAME
single-source-of-truth parser the merge gate itself uses, never a bespoke
regex). No crew agent may delete another author's comment -- cross-author
delete is an audit-tampering/censorship surface and is refused
unconditionally, with no override; human-comment removal stays an operator
action outside this tool entirely. Even a self-authored comment carrying a
landed verdict fence is refused, so a caller can never game the merge-gate
re-read by posting clean, letting it be read, deleting it, and reposting.
Before issuing DELETE issues/comments/<id>, this tool: (a) GETs the comment
(get_comment), (b) asserts its author login matches the caller's own
resolved bot login (refuses otherwise), (c) asserts its body has no verdict
fence (refuses otherwise), then (d) issues the DELETE. Both platforms
independently gate delete on authorship-or-admin at the API layer
(identity-of-token) -- this tool-side check is defense-in-depth on top of
that platform enforcement, not a replacement for it. A DELETE to
issues/comments/<id> that OMITS --delete-own-comment is a hard refusal
BEFORE any I/O, exactly like a comments POST omitting --verify-comment.

Content-Type ownership: git_host_api sets 'Content-Type: application/json'
itself whenever a JSON body is being sent and the caller has not already
supplied their own Content-Type header.

Never use curl --netrc. An inherited token is never used. The token is
passed to the HTTP layer as a header value, never interpolated into a shell
string.

Redirect hardening (security review finding): every call in this module
carries the live git-host bearer token in its Authorization header. urllib's default
HTTPRedirectHandler follows a 3xx response and RE-ISSUES the request
(original headers intact, including Authorization) against the redirect's
Location target -- if git_host_base is compromised, misconfigured, or sits
behind a misbehaving reverse proxy, that Location can be an
attacker-controlled host, and the live token would be replayed to it.
request() therefore builds its opener via the shared
clagentic_loadout.transport.redirect_guard.no_redirect_opener() rather than
falling back to urllib.request.urlopen's redirect-following default; a 3xx
surfaces as an HTTPError, handled by the ordinary error path like any other
failure, without ever dispatching a second request. This module's own
_NoRedirectHandler was previously a local copy of this handler (matching the
equivalent pattern shipped in clagentic_loadout.release.dispatch and
telemetry.sink); it is now re-exported from redirect_guard (lr-412f
pre-merge security review finding) -- the class was extracted into a shared
module once a THIRD transport call site (review.github_backend) needed the
identical protection for a bearer/App-installation token header, at which
point three independent local copies stopped being decoupling and started
being a place for the protection to go missing (exactly what happened when
github_backend.py shipped a bespoke urlopen with none of it).

Exit codes (module constants EXIT_*, see below for the full table).

git_host_api --help / -h prints usage and exits EXIT_OK immediately, before any
argument is treated as PATH.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

from clagentic_loadout._version import get_version
from clagentic_loadout.merge.verdict import (
    VERDICT_FENCE,
    build_verdict_block,
    find_all_verdict_blocks,
    parse_verdict_block,
)
from clagentic_loadout.platform_detect import (
    DEFAULT_GITHUB_HOSTNAME,
    PLATFORM_FORGEJO,
    PLATFORM_GITHUB,
)
from clagentic_loadout.transport import redirect_guard
from clagentic_loadout.transport.attestation import (
    AttestationError,
    Identity,
    resolve_identity as _resolve_identity,
)
from clagentic_loadout.transport.body_env import (
    BODY_ENV_NOT_EPHEMERAL_NOTE,
    BodyEnvError,
    read_body_bytes,
)
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    DEFAULT_ROLE,
    TokenProvider,
    resolve_token as _resolve_token,
)
from clagentic_loadout.transport.note_compose import build_composed_body
from clagentic_loadout.transport.provider_config import (
    load_user_config_section,
    resolve_platform_provider,
)

# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------

#: Success (and, with --verify-comment, readback confirmed own-bot comment
#: on the correct PR).
EXIT_OK = 0
#: Generic usage error (bad arguments, missing PATH).
EXIT_USAGE = 1
#: Token resolution failed (provider raised CredentialProviderError).
EXIT_TOKEN_FETCH_FAILED = 2
#: Comment readback mismatch: no own-bot comment found on the target PR
#: after POST, or the match failed the freshness anchor. Callers MUST NOT
#: report clean when this fires -- the gate is not verified.
EXIT_VERIFY_FAILED = 3
#: PR SHA mismatch: the PR head SHA differs from the SHA the caller
#: evaluated. The caller MUST report 'stale' and NOT claim gate-pass.
EXIT_STALE_PR = 4
#: owner/repo not found: the /api/v1/repos/{owner}/{repo} path does not
#: exist. Distinct from EXIT_STALE_PR: a 404 on the PR endpoint may mean the
#: REPO itself is gone/wrong, not just that the PR is stale.
EXIT_OWNER_REPO_NOT_FOUND = 6
#: --body-stdin content is empty / not valid JSON / has no non-empty 'body'
#: string field. Fails BEFORE the HTTP call is made so a truncated/racy/
#: empty stdin source never reaches the git host as a silent empty POST (which
#: rejects with an opaque 422 "[Body]: Required").
EXIT_BODY_STDIN_EMPTY = 8
#: A POST to issues/<pr>/comments omitted --verify-comment. Fails BEFORE any
#: I/O: a comments POST can no longer complete fire-and-forget with no
#: readback -- a bare 2xx only proves the HTTP transaction completed, not
#: that the comment is confirmed present.
EXIT_VERIFY_COMMENT_REQUIRED = 9
#: The HTTP request itself failed (network error, or a write method
#: returned a non-2xx status). A DEDICATED code, distinct from
#: EXIT_VERIFY_FAILED, so a transport/URL failure is never indistinguishable
#: from a verify-readback failure by exit code alone.
EXIT_CURL_FAILED = 10
#: --expect-verdict-block was supplied but a precondition for constructing
#: the fence is missing/invalid BEFORE any I/O: not a comments POST, missing
#: --verify-comment, missing --pr-sha, or --body-stdin JSON missing/
#: malformed review_status. Distinct from EXIT_USAGE so a caller can tell
#: "the verdict-block contract was violated" apart from an ordinary
#: argument-shape mistake.
EXIT_VERDICT_BLOCK_USAGE = 11
#: The comment landed and passed the ordinary --verify-comment readback, but
#: re-parsing the fenced ```review-result``` block from the VERIFIED
#: comment's own body (never the locally-constructed string) did not match
#: what --expect-verdict-block requested -- the fence was lost, truncated,
#: or mangled in transit. The gate is not verified when this fires.
EXIT_VERDICT_BLOCK_MISMATCH = 12
#: --caller-tracking-id was supplied but a precondition is missing/invalid
#: BEFORE any I/O: not a comments POST, missing --verify-comment, or the
#: value itself is empty/whitespace-only or contains a control character.
#: Distinct from EXIT_USAGE so a caller can tell "the caller-tracking-id
#: contract was violated" apart from an ordinary argument-shape mistake
#: (lr-10a996, mirrors EXIT_VERDICT_BLOCK_USAGE's precedent).
EXIT_CALLER_TRACKING_ID_USAGE = 13
#: --body-env and --body-stdin are mutually exclusive, or NEITHER was
#: supplied for a write method that needs a body -- exactly one
#: body-ingestion flag is required. Distinct from EXIT_USAGE so a caller can
#: tell "the body-ingestion contract was violated" apart from an ordinary
#: argument-shape mistake (lr-10a996 BODY-TRANSPORT half).
EXIT_BODY_INGESTION_USAGE = 14
#: --body-env was supplied but the caller-namespaced staged-body path
#: (transport.body_env.resolve_caller_body_path, lr-3a7ae8) is missing, not
#: a regular file, or unreadable -- the caller's harness never staged a
#: body under this --caller's own namespace before invoking this verb.
#: Distinct from EXIT_BODY_STDIN_EMPTY so a caller can tell "the fixed path
#: itself is the problem" apart from "stdin content failed validation"
#: (lr-10a996 BODY-TRANSPORT half).
EXIT_BODY_ENV_UNREADABLE = 15
#: --delete-own-comment refused BEFORE the DELETE was issued (lr-e2ce66):
#: either (a) the belt-and-suspenders GET could not resolve the comment /
#: the caller's own bot login, (b) the comment's author login does not match
#: the caller's own resolved bot login (cross-author delete -- an
#: audit-tampering/censorship surface, refused unconditionally), or (c) the
#: comment body carries a fenced ```review-result``` block (deleting a
#: landed verdict could game the merge gate: post clean, get read, delete,
#: repost). Distinct from EXIT_VERIFY_FAILED/EXIT_CURL_FAILED so a caller can
#: tell "the delete-own-comment authorship/verdict contract was violated"
#: apart from an ordinary transport failure -- mirrors the
#: EXIT_VERDICT_BLOCK_USAGE / EXIT_CALLER_TRACKING_ID_USAGE precedent (a
#: dedicated code per belt-and-suspenders contract, never folded into
#: EXIT_USAGE or an unrelated existing code).
EXIT_DELETE_OWN_COMMENT_REFUSED = 16
#: An absolute http(s) URL PATH targeted the Forgejo branch (i.e.
#: _is_github_target said "not GitHub") but its host:port does NOT match the
#: resolved git-host base (lr-69af67, closing a gap flagged non-blocking on
#: lr-8f7d4e/#77 by both review passes). Fails BEFORE any I/O: the git-host
#: bearer token is never attached to a request whose absolute-URL host was
#: not verified to be the configured git-host, and the base is never
#: silently prepended in front of a foreign-host absolute URL either (that
#: would just reintroduce the double-authority defect lr-8f7d4e fixed, this
#: time against an attacker/typo host instead of the correct one). Distinct
#: from EXIT_CURL_FAILED so a caller can tell "this tool refused to route an
#: absolute URL to an unverified host" apart from an ordinary transport
#: failure.
EXIT_ABSOLUTE_URL_HOST_MISMATCH = 17
#: The resolved target platform's repo-path pattern failed to match PATH,
#: but PATH matches the OTHER platform's repo-path shape instead (lr-aa4e3c:
#: a Forgejo-shaped /api/v1/repos/... path fat-fingered against a GitHub
#: target, or a bare GitHub-shaped /repos/... path -- missing the /api/v1
#: prefix -- fat-fingered against a Forgejo target). Fails BEFORE any I/O and
#: BEFORE any token mint is attempted: the caller gets a one-line corrective
#: error naming the exact fixed-up URL instead of a mint-time refusal that
#: reads like an environment defect (see the merge-gate role's lr-2ea2a7
#: misdiagnosis this closes). Distinct from a bare "no repo context" case (call_repo stays
#: None and the call proceeds unscoped) -- this code fires ONLY when the
#: OTHER platform's shape actually matches, i.e. there is a specific,
#: nameable fix to suggest.
EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH = 18
#: An EXPLICIT --caller value does not match the ATTESTED invoking identity
#: this process's own attestation-provider chain resolved (lr-82c385, tome
#: #700). FAILS CLOSED BEFORE ANY I/O -- no token mint, no request is ever
#: issued -- an identity may only ever use ITS OWN credential; a caller who
#: presents a role other than its own attested identity is refused
#: unconditionally, with no override. Distinct from EXIT_TOKEN_FETCH_FAILED
#: (a resolvable identity whose credential provider itself then refuses) and
#: from EXIT_USAGE (an ordinary argument-shape mistake) so an operator can
#: tell "this process attested as X but tried to act as Y" apart from either
#: of those. An OMITTED --caller never triggers this check (see
#: bind_caller's own docstring) -- it is unchanged, existing behavior.
EXIT_CALLER_INVOKER_MISMATCH = 19

# HTTP methods that mutate server state and require fail-on-HTTP-error
# enforcement. GET/HEAD are read-only.
_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})

# Freshness-anchor clock-skew tolerance: the readback in verify_comment_on_pr
# requires a matched comment's created_at to be at or after the pre-post
# timestamp, minus this tolerance. Absorbs ordinary clock drift between this
# process and the git-host server without opening a window wide enough to match
# a genuinely stale/pre-existing comment.
_FRESHNESS_SKEW_TOLERANCE_SECONDS = 5

# Bare role/caller name pattern. Anchored with \A...\Z, not ^...$ (lr-3e3318,
# sibling fix alongside transport.credential_provider._SAFE_ROLE_RE /
# _SAFE_REPO_RE): '$' without re.MULTILINE also matches just before a
# trailing newline in Python, so 'caller\n' would otherwise pass -- \A/\Z
# anchor strictly to start/end of string with no such tolerance.
_SAFE_CALLER_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")

# --caller-tracking-id pattern: an opaque work-item reference (CLAUDE.md
# rule 6a -- "task_id is an opaque work-item ref (pattern configurable)").
# Deliberately broader than _SAFE_CALLER_RE (a tracking id may carry a
# deployment-specific prefix/separator, e.g. a lore task id's "lr-" prefix)
# but still rejects control characters and whitespace so the value can never
# smuggle a newline or other structural character into the JSON metadata
# fence this module constructs.
_SAFE_CALLER_TRACKING_ID_RE = re.compile(r"^[\x21-\x7e]{1,128}$")

# Regex to extract owner from any /api/v1/repos/{owner}/{repo}/... path.
# Anchored to the Forgejo path shape (/api/v1/repos/...) -- this is the
# ONLY shape it matches. It does NOT match a GitHub absolute URL, which has
# no /api/v1/ prefix (see _GITHUB_REPOS_URL_RE below, lr-5f7971).
_REPOS_PATH_RE = re.compile(r"^/api/v1/repos/([^/]+)/([^/]+)")

# Regex to extract owner/repo from an ABSOLUTE GitHub URL of the shape
# https://<host>/repos/{owner}/{repo}/... (e.g.
# https://api.github.com/repos/some-owner/some-repo/pulls/1). A GitHub
# target's path_arg is the full URL, not a bare path, and GitHub's REST API
# has no /api/v1/ prefix -- so _REPOS_PATH_RE (Forgejo-anchored) never
# matches it, leaving call_repo=None and a {repo}-templated GitHub token
# helper unable to mint (lr-5f7971, the last mile of lr-104a: that task
# wired absolute-URL routing + Forgejo repo-threading but never GitHub-URL
# repo extraction). This pattern is applied ONLY when _is_github_target(...)
# is already true, so it is never reached for a Forgejo path -- Forgejo
# behavior is unaffected. A non-repo-scoped GitHub URL (e.g. /user,
# /orgs/...) does not match here either, and call_repo stays None -- fail-
# closed on a {repo}-templated helper is correct for those, exactly as it
# already is for a non-repo-scoped Forgejo path.
_GITHUB_REPOS_URL_RE = re.compile(r"^https?://[^/]+/repos/([^/]+)/([^/]+)")

# Pattern matching GitHub's bare /repos/{owner}/{repo}/... path shape with NO
# scheme/host and NO /api/v1 prefix (lr-aa4e3c cross-platform URL-shape
# check): a caller who fat-fingers a Forgejo-target PATH by typing GitHub's
# shape directly (omitting both the absolute GitHub host AND the Forgejo
# /api/v1/ prefix) produces a relative path that would otherwise silently
# fail _REPOS_PATH_RE's owner/repo extraction with no clue why. Anchored to
# the ABSENCE of /api/v1 so it never matches an ordinary, correctly-shaped
# Forgejo path (_REPOS_PATH_RE, which is checked first and always wins when
# it matches).
_BARE_REPOS_PATH_RE = re.compile(r"^/repos/([^/]+)/([^/]+)")

# Pattern matching the issues/<pr>/comments endpoint. Captures owner, repo,
# pr_number. Accepts an optional single trailing slash and/or query string
# (both route identically at the HTTP layer) without swallowing a distinct
# sub-resource such as .../comments/123.
_ISSUE_COMMENTS_RE = re.compile(
    r"^/api/v1/repos/([^/]+)/([^/]+)/issues/(\d+)/comments/?(?:\?.*)?$"
)

# Pattern matching the single-comment sub-resource endpoint (lr-e2ce66):
# .../issues/comments/<comment_id> -- Forgejo's DELETE-a-comment shape (note:
# NOT nested under a specific issue/PR number; Forgejo scopes comment
# mutation by comment id alone). Captures owner, repo, comment_id. Deliberately
# a SEPARATE pattern from _ISSUE_COMMENTS_RE (which matches the plural
# .../issues/<pr>/comments collection endpoint used for listing/posting) --
# conflating the two would make a DELETE target ambiguously match the POST
# collection endpoint's precondition checks.
_ISSUE_COMMENT_ID_RE = re.compile(
    r"^/api/v1/repos/([^/]+)/([^/]+)/issues/comments/(\d+)/?(?:\?.*)?$"
)

#: Default Forgejo API base URL. Overridable via --git-host-base-url or the
#: CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL env var -- no operator-host default is
#: baked in beyond this generic placeholder-free localhost fallback, which
#: only matters for --help/argument-parsing smoke tests; any real invocation
#: must supply a base URL.
DEFAULT_GIT_HOST_BASE_URL = "http://127.0.0.1:3000"

#: Env var carrying the git-host base URL when --git-host-base-url is
#: omitted.
GIT_HOST_BASE_URL_ENV_VAR = "CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL"

#: Env var naming the COMPAT-ALIAS env var consulted when
#: GIT_HOST_BASE_URL_ENV_VAR is unset (lr-87bb). A deployment migrating from a pre-CLAGENTIC_LOADOUT_*
#: base-URL var (e.g. a spawn env that already exports a differently-named
#: var pointing at the real git host) sets this to that var's NAME -- the
#: value itself is read from os.environ[<that name>], never hardcoded here.
#: This mirrors CLAUDE.md rule 6a: "Identity env/sidecar names are
#: CLAGENTIC_LOADOUT_*-branded with configurable compat aliases." The
#: branded var (GIT_HOST_BASE_URL_ENV_VAR) always wins when set; the alias is
#: consulted only as a fallback, never a co-equal source.
GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR = (
    "CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL_COMPAT_ALIAS"
)

#: Default name of the compat-alias env var itself, used when
#: GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR is unset. This is the ONLY
#: alias name product code knows about by default; deployments needing a
#: different legacy var name repoint
#: GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR rather than this module
#: growing a second hardcoded name.
DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS = "FORGEJO_BASE_URL"

#: Top-level config-file section this module owns within the USER-LEVEL
#: <config_root>/config.yaml (lr-e570 seq-2: a released tool must not force
#: a hand-exported env var -- mirrors transport.provider_config's
#: `credentials:` section/DEFAULT_USER_CONFIG_ROOT/config_root convention
#: exactly, REUSING that same loader rather than a second YAML parser or a
#: different config path).
#:
#: RENAMED (lr-08b451, operator-decided shape): this key is consulted ONLY
#: on the Forgejo path -- see `_resolve_git_host_base`'s call site in
#: `main`/every verb that imports it, and the unconditional discard at the
#: GitHub-platform branch (`if target_platform == PLATFORM_GITHUB:
#: git_host_base = ""`) in each of those callers. The old name `git_host`
#: reads as "the git host for this deployment" when it is really "the
#: Forgejo base URL, nothing else" -- a documented trap (see docs/
#: integration.md, "Forgejo-only plumbing") that once nearly caused an
#: agent to repoint this SHARED, USER-LEVEL key at github.com for a
#: GitHub-hosted repo, which would have done nothing on the GitHub path
#: while breaking every OTHER Forgejo-hosted repo on the same box (this key
#: has no per-repo tier -- see post_merge_config.py's "USER-LEVEL only"
#: rationale for the identical trust-boundary argument applied to a
#: different section). Renamed to `forgejo`, matching the `_forgejo`/
#: `_github` split the credentials section (`token_provider_forgejo`/
#: `token_provider_github`) already uses.
#:
#: The env var (`GIT_HOST_BASE_URL_ENV_VAR`), its compat alias
#: (`DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS`), and the `--git-host-base-url`
#: CLI flag are explicitly NOT renamed here -- see this module's own
#: `_resolve_git_host_base` docstring, tier 2's cross-reference to the
#: scorched-earth `FORGE_BASE_URL` removal this repo already regrets doing
#: once. Renaming a RELEASED env var/flag a second time is a bigger blast
#: radius than the naming mismatch it would fix.
GIT_HOST_CONFIG_SECTION = "forgejo"

#: The PRE-rename section name (lr-08b451). `_resolve_git_host_base`'s
#: config-file tier reads `GIT_HOST_CONFIG_SECTION` (`forgejo`) FIRST; when
#: that section has no `base_url` value, it falls back to reading this
#: legacy section name -- so an existing install that has only ever seeded
#: `git_host:` (e.g. via a not-yet-upgraded `scripts/install.sh`, or a
#: hand-edited config file predating this rename) keeps resolving exactly
#: as before, with no forced re-seed. New name wins whenever BOTH sections
#: carry a value (see `_resolve_git_host_base`'s config-file tier for the
#: exact precedence and its own test coverage).
LEGACY_GIT_HOST_CONFIG_SECTION = "git_host"

#: Key within the config section carrying the base URL -- unchanged by the
#: lr-08b451 section rename; only the top-level section name moved.
GIT_HOST_CONFIG_KEY_BASE_URL = "base_url"


class GitHostApiError(Exception):
    """Raised for any git_host_api failure that should terminate the process
    with a specific exit code. Carries the intended exit code as `.code` so
    both the CLI (`main`) and library callers can translate it uniformly."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise GitHostApiError(message, code)


# ---------------------------------------------------------------------------
# --body-stdin content validation
# ---------------------------------------------------------------------------


def validate_body_stdin_content(raw_bytes: bytes) -> None:
    """Validate that --body-stdin's content is non-empty JSON with a
    non-empty 'body' string field, BEFORE the HTTP call is made.

    Every --body-stdin caller in the comment-posting contract posts
    {"body": "<comment text>"}. Some other JSON shape without a 'body' key
    is not a sanctioned use of this flag.

    Raises GitHostApiError(code=EXIT_BODY_STDIN_EMPTY) on:
      - zero-byte input
      - content that is not valid JSON
      - valid JSON that is not an object
      - valid JSON object missing a 'body' key, or where 'body' is not a
        non-empty string (after stripping whitespace)
    """
    if len(raw_bytes) == 0:
        _fail(
            "--body-stdin received empty input (0 bytes). The git host would "
            "reject an empty request body with a generic '[Body]: Required' "
            "422 that does not name stdin as the cause.",
            code=EXIT_BODY_STDIN_EMPTY,
        )
    try:
        parsed = json.loads(raw_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"--body-stdin does not contain valid JSON: {exc}. Expected "
            f'{{"body": "<comment text>"}}.',
            code=EXIT_BODY_STDIN_EMPTY,
        )
    if not isinstance(parsed, dict):
        _fail(
            f"--body-stdin must contain a JSON OBJECT with a 'body' key; "
            f"got {type(parsed).__name__}.",
            code=EXIT_BODY_STDIN_EMPTY,
        )
    body_value = parsed.get("body")
    if not isinstance(body_value, str) or not body_value.strip():
        _fail(
            f"--body-stdin has no non-empty 'body' string field "
            f"(got {body_value!r}). The git host requires a non-empty comment body.",
            code=EXIT_BODY_STDIN_EMPTY,
        )


def build_expected_verdict_body(
    stdin_bytes: bytes,
    *,
    reviewer: str,
    pr_number: int,
    expected_head_sha: str,
) -> str:
    """Construct the POST body for an --expect-verdict-block invocation.

    --body-stdin's JSON carries the ordinary 'body' prose PLUS 'review_status'
    (the ONLY field this tool cannot derive on the caller's behalf -- 'clean'
    or 'blocking' is the reviewer's actual finding). 'head_sha' and
    'pr_number' are NOT read from stdin: head_sha is *expected_head_sha* (the
    same --pr-sha value this invocation already checked the PR against, so
    there is exactly one place a SHA is typed, not two that could drift) and
    pr_number is parsed from the POST path itself. No backtick ever appears
    in stdin_bytes or in any argv -- the fence is appended here, in-process,
    via merge.verdict.build_verdict_block (the single source of truth for the
    ```review-result``` shape the merge gate re-parses).

    PRE-EMBEDDED-FENCE REFUSAL (lr-5260f9, observed against a Forgejo
    deployment -- live production evidence): 'body' MUST NOT already
    contain a fenced ```review-result``` block. Before this check, this
    function unconditionally APPENDED a fence regardless of what 'body'
    already carried -- a caller (reviewer) that had already hand-embedded its
    own fence in 'body' got a SECOND, duplicate fence, and the pre-existing
    last-fence-wins parse (merge.verdict.parse_verdict_block) validated the
    resulting two-fence comment cleanly, silently hiding the malformed shape
    from --verify-comment's own self-check. DESIGN CHOICE, argued explicitly
    (the task named this a genuine fork): REJECT the pre-staged fence rather
    than silently respect it (e.g. skip the append and post the caller's own
    fence verbatim). Silently respecting it would mean this function's own
    'reviewer'/'review_status'/'head_sha'/'pr_number' fields -- the ones this
    invocation just validated and the ones --pr-sha/--caller/the POST path
    guarantee cannot drift -- could silently disagree with whatever the
    caller hand-typed into its own fence, reopening exactly the drift this
    flag exists to prevent (this module's own docstring, '--expect-verdict-
    block'). Rejecting makes the malformed shape UNREACHABLE from this
    sanctioned path: a caller with a pre-embedded fence gets a same-shaped,
    resolved-values usage error instead of a comment silently landing with
    two.

    Raises GitHostApiError(code=EXIT_VERDICT_BLOCK_USAGE) when the JSON is
    malformed, is not an object, is missing 'body'/'review_status',
    'review_status' is not 'clean'/'blocking', or 'body' already contains a
    fenced ```review-result``` block.

    Returns the combined body string (prose + appended fence) -- this is
    what is actually POSTed and, later, what --verify-comment's readback
    compares against.
    """
    try:
        parsed = json.loads(stdin_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"--expect-verdict-block: --body-stdin does not contain valid "
            f"JSON: {exc}. Expected "
            f'{{"body": "<prose>", "review_status": "clean"|"blocking"}}.',
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    if not isinstance(parsed, dict):
        _fail(
            f"--expect-verdict-block: --body-stdin must contain a JSON "
            f"OBJECT; got {type(parsed).__name__}.",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    prose = parsed.get("body")
    if not isinstance(prose, str) or not prose.strip():
        _fail(
            f"--expect-verdict-block: --body-stdin has no non-empty 'body' "
            f"string field (got {prose!r}).",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    pre_embedded = find_all_verdict_blocks(prose)
    if pre_embedded:
        _fail(
            f"--expect-verdict-block: --body-stdin 'body' already contains "
            f"{len(pre_embedded)} fenced ```{VERDICT_FENCE}``` block(s). "
            f"This flag CONSTRUCTS the verdict fence itself -- 'body' must "
            f"be plain prose with no pre-embedded fence, or the posted "
            f"comment would carry two. Remove the hand-authored fence from "
            f"'body' and let --expect-verdict-block build it from "
            f"'review_status' instead.",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    review_status = parsed.get("review_status")
    if review_status not in ("clean", "blocking"):
        _fail(
            f"--expect-verdict-block: --body-stdin 'review_status' must be "
            f"'clean' or 'blocking', got {review_status!r}.",
            code=EXIT_VERDICT_BLOCK_USAGE,
        )
    try:
        fence = build_verdict_block(reviewer, review_status, expected_head_sha, pr_number)
    except ValueError as exc:
        _fail(f"--expect-verdict-block: {exc}", code=EXIT_VERDICT_BLOCK_USAGE)
    return f"{prose}\n{fence}"


def _validate_owner(owner: str, *, known_bad_owners: frozenset[str]) -> None:
    """Reject configured known-bad owner names before any network call.

    known_bad_owners is caller-supplied config (e.g. a git-remote/
    service-account name that is never a valid org owner in a given
    deployment) -- empty by default; this module bakes in no such names
    itself.
    """
    if owner in known_bad_owners:
        _fail(
            f"owner {owner!r} is a configured known-bad owner (git-remote/"
            f"service-account name, not a valid git-host org owner). Check the "
            f"repo PATH in the dispatch envelope.",
            code=EXIT_OWNER_REPO_NOT_FOUND,
        )


# ---------------------------------------------------------------------------
# HTTP helpers (urllib only -- no subprocess, no curl dependency)
# ---------------------------------------------------------------------------


#: Re-exported from transport.redirect_guard (lr-412f pre-merge security
#: review finding) -- this WAS a local copy of the handler ("duplicated
#: locally ... so transport stays decoupled" per the module's own prior
#: docstring), extracted into a shared module once a THIRD transport call
#: site (review.github_backend) needed the identical protection for a
#: bearer/App-installation token header and a bespoke local urlopen call
#: shipped without it. The name _NoRedirectHandler is kept as an alias so
#: existing test/CLI references into this module keep working unchanged.
_NoRedirectHandler = redirect_guard.NoRedirectHandler


def _default_opener():
    """Build a urllib opener that never follows redirects (see
    transport.redirect_guard.NoRedirectHandler). Constructed lazily so a
    test-injected opener never has to go through this at all."""
    return redirect_guard.no_redirect_opener()


def _auth_headers(token: str, *, content_type: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"token {token}"}
    if content_type is not None:
        headers["Content-Type"] = content_type
    return headers


def build_request(
    git_host_base: str,
    method: str,
    path: str,
    token: str,
    *,
    body_bytes: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> urllib.request.Request:
    """Build the urllib Request for one git-host API call.

    Content-Type ownership: when a JSON body is being sent and the caller
    has not already supplied their own Content-Type in extra_headers, this
    adds 'Content-Type: application/json' itself (case-insensitive check).
    """
    headers = _auth_headers(token)
    if extra_headers:
        headers.update(extra_headers)
    if body_bytes is not None and not any(
        k.lower() == "content-type" for k in headers
    ):
        headers["Content-Type"] = "application/json"
    url = f"{git_host_base}{path}"
    return urllib.request.Request(url, data=body_bytes, headers=headers, method=method)


def request(
    git_host_base: str,
    method: str,
    path: str,
    token: str,
    *,
    body_bytes: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
    timeout: int = 15,
    opener=None,
) -> tuple[int, bytes]:
    """Issue one authenticated git-host API call. Returns (status_code, raw_body_bytes).

    Write methods (POST/PATCH/PUT/DELETE) raise GitHostApiError(code=
    EXIT_CURL_FAILED) on any non-2xx status or network error -- callers must
    be able to distinguish a silently-accepted bad request from a real
    success. GET/HEAD callers receive the response verbatim (including
    non-2xx) so they can parse an error body themselves; this mirrors the
    reference transport's --fail-with-body-on-write-methods-only contract.

    `opener` injects a urllib opener's .open callable for tests -- no real
    network call is ever made when a fake opener is supplied.

    Redirect hardening (security review finding): when no opener is
    injected, this builds its own no-redirect opener (see _NoRedirectHandler) rather than
    calling urllib.request.urlopen directly -- urlopen's default redirect
    handler would replay the Authorization header (the live bearer token)
    to whatever host a 3xx Location names.
    """
    req = build_request(
        git_host_base, method, path, token, body_bytes=body_bytes, extra_headers=extra_headers
    )
    urlopen = opener if opener is not None else _default_opener().open
    try:
        with urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = getattr(resp, "status", None) or resp.getcode()
            return status, raw
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        if 300 <= exc.code < 400:
            # A 3xx here means _NoRedirectHandler refused to follow it (see
            # above) -- this is a redirect refusal, not an ordinary HTTP
            # error response. Always fail closed, for GET too: a redirect
            # must never be treated as a completed call whose body a caller
            # goes on to parse, and its Location is never named here so it
            # cannot leak into a log alongside a request that carried the
            # live bearer token.
            _fail(
                f"git-host API {method} {path} received HTTP {exc.code} "
                f"(redirect) -- refused to follow; a redirect could replay "
                f"the request's Authorization header to a different host.",
                code=EXIT_CURL_FAILED,
            )
        if method in _WRITE_METHODS:
            _fail(
                f"git-host API {method} {path} returned HTTP {exc.code}: "
                f"{raw.decode('utf-8', errors='replace')[:500]}",
                code=EXIT_CURL_FAILED,
            )
        return exc.code, raw
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _fail(f"git-host API {method} {path} unreachable: {exc}", code=EXIT_CURL_FAILED)


def parse_json_body(raw: bytes) -> dict[str, Any]:
    """Parse request()'s raw response bytes as a JSON object, tolerantly.

    Post-Wave-B extraction (lr-e1f9): request() deliberately returns raw
    bytes rather than a parsed body (see its own docstring — read-only
    callers stream the response verbatim). Every write-response caller in
    this package's Forgejo backends (push.forgejo_backend, merge.
    forgejo_backend) then needs the SAME tolerant parse of that raw body —
    an empty body or a body that fails to decode/parse is never a caller
    error worth raising over; it is treated as "no fields", and each
    caller's own field-presence check (e.g. a missing 'number' on a PR
    create) is what fails closed, not a JSON parse exception here. Each
    backend module independently defined this identical fallback until this
    extraction (same duplication class the redirect_guard extraction
    addressed for the Forgejo opener, one level lower in the stack).

    Returns {} on an empty body, invalid JSON, or a body that decodes to
    something other than a JSON object (a list/string/etc — every caller of
    this helper expects a dict-shaped Forgejo API response).
    """
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


# ---------------------------------------------------------------------------
# Post-and-verify helpers
# ---------------------------------------------------------------------------


def resolve_bot_login(git_host_base: str, token: str, *, opener=None) -> str:
    """Fetch the authenticated user's login from GET /api/v1/user.

    Raises GitHostApiError(code=EXIT_VERIFY_FAILED) on any failure.
    """
    status, raw = request(git_host_base, "GET", "/api/v1/user", token, opener=opener)
    if status != 200:
        _fail(
            f"verify-comment FAILED -- GET /api/v1/user returned HTTP {status}. "
            f"Cannot resolve caller bot login.",
            code=EXIT_VERIFY_FAILED,
        )
    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"verify-comment FAILED -- GET /api/v1/user returned unparseable JSON: {exc}.",
            code=EXIT_VERIFY_FAILED,
        )
    login = body.get("login", "")
    if not login:
        _fail(
            "verify-comment FAILED -- GET /api/v1/user returned no 'login' field. "
            "Cannot confirm comment authorship without knowing the caller's bot login.",
            code=EXIT_VERIFY_FAILED,
        )
    return login


def check_repo_exists(git_host_base: str, token: str, owner: str, repo: str, *, opener=None) -> bool:
    """GET /api/v1/repos/{owner}/{repo}; True on HTTP 200, False ONLY on 404.

    Non-404 HTTP errors and network errors propagate as GitHostApiError so a
    transient server error is never misdiagnosed as owner-not-found.
    """
    status, _raw = request(
        git_host_base, "GET", f"/api/v1/repos/{owner}/{repo}", token, opener=opener
    )
    return status == 200


def check_pr_sha(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: str,
    expected_sha: str,
    *,
    known_bad_owners: frozenset[str] = frozenset(),
    opener=None,
) -> None:
    """GET /api/v1/repos/{owner}/{repo}/pulls/{pr_number} and confirm
    head.sha == expected_sha.

    On HTTP 404: distinguishes owner/repo-not-found (EXIT_OWNER_REPO_NOT_FOUND)
    from PR-not-found/stale (EXIT_STALE_PR) via a secondary repo-existence
    check, so a wrong owner is never silently rationalized as a stale PR.
    """
    _validate_owner(owner, known_bad_owners=known_bad_owners)

    status, raw = request(
        git_host_base, "GET", f"/api/v1/repos/{owner}/{repo}/pulls/{pr_number}", token, opener=opener
    )
    if status == 404:
        repo_exists = check_repo_exists(git_host_base, token, owner, repo, opener=opener)
        if not repo_exists:
            _fail(
                f"owner/repo {owner!r}/{repo!r} not found at "
                f"{git_host_base}/api/v1/repos -- verify the OWNER (org).",
                code=EXIT_OWNER_REPO_NOT_FOUND,
            )
        _fail(
            f"pr-sha check FAILED -- GET pulls/{pr_number} returned HTTP 404 "
            f"(PR not found; repo {owner!r}/{repo!r} exists). PR may be closed, "
            f"merged, or deleted.",
            code=EXIT_STALE_PR,
        )
    if status != 200:
        _fail(
            f"pr-sha check FAILED -- GET pulls/{pr_number} returned HTTP {status}.",
            code=EXIT_STALE_PR,
        )

    try:
        body = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"pr-sha check FAILED -- GET pulls/{pr_number} returned unparseable JSON: {exc}.",
            code=EXIT_STALE_PR,
        )
    actual_sha = body.get("head", {}).get("sha", "")
    if not actual_sha:
        _fail(
            f"pr-sha check FAILED -- GET pulls/{pr_number} returned no head.sha field.",
            code=EXIT_STALE_PR,
        )
    if actual_sha != expected_sha:
        _fail(
            f"pr-sha MISMATCH -- PR #{pr_number} head is now {actual_sha!r} but "
            f"the caller evaluated {expected_sha!r}. The caller reviewed a stale "
            f"snapshot -- gate-pass REFUSED. Re-review at the current head SHA "
            f"before posting.",
            code=EXIT_STALE_PR,
        )
    print(
        f"git_host_api: pr-sha confirmed -- PR #{pr_number} head={actual_sha!r} "
        f"matches evaluated SHA.",
        file=sys.stderr,
    )


def _parse_git_host_timestamp(raw: str) -> "datetime | None":
    """Parse a git-host API timestamp (RFC 3339 / ISO 8601) into an aware UTC
    datetime. Returns None if missing/unparseable -- callers must treat that
    as "cannot confirm freshness", not as a pass."""
    if not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def verify_comment_on_pr(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    pr_number: str,
    posted_body: str,
    bot_login: str,
    *,
    not_before: "datetime",
    opener=None,
) -> dict[str, Any]:
    """Read back comments on issues/<pr_number> and confirm the caller's own
    bot comment is present with the posted body AND was created at or after
    `not_before` (freshness anchor).

    `not_before` is the timestamp captured immediately before the POST was
    issued. Requiring the matched comment's created_at to be at or after
    that instant closes the gap where a substring-only match (login + body)
    could be satisfied by a stale/pre-existing comment with overlapping body
    text rather than the comment this invocation just posted.

    Returns the verified comment dict (from the readback, NOT the POST
    response). Raises GitHostApiError(code=EXIT_VERIFY_FAILED) if no own-bot,
    sufficiently-fresh comment with the expected body is found.

    Body matching uses substring: the posted body must appear verbatim
    within the readback comment body (tolerates markdown/whitespace
    normalization).
    """
    status, raw = request(
        git_host_base,
        "GET",
        f"/api/v1/repos/{owner}/{repo}/issues/{pr_number}/comments",
        token,
        opener=opener,
    )
    if status != 200:
        _fail(
            f"verify-comment FAILED -- GET issues/{pr_number}/comments returned "
            f"HTTP {status}. Cannot confirm comment landed on the correct PR.",
            code=EXIT_VERIFY_FAILED,
        )
    try:
        comments = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"verify-comment FAILED -- GET issues/{pr_number}/comments returned "
            f"unparseable JSON: {exc}.",
            code=EXIT_VERIFY_FAILED,
        )
    if not isinstance(comments, list):
        _fail(
            f"verify-comment FAILED -- GET issues/{pr_number}/comments returned "
            f"non-list.",
            code=EXIT_VERIFY_FAILED,
        )

    not_before_with_tolerance = not_before.timestamp() - _FRESHNESS_SKEW_TOLERANCE_SECONDS
    stale_candidates: list[Any] = []
    # Scan newest-first so a retry produces the most-recent match.
    for comment in reversed(comments):
        comment_login = comment.get("user", {}).get("login", "")
        comment_body = comment.get("body", "")
        if not (comment_login == bot_login and posted_body in comment_body):
            continue
        created_at = _parse_git_host_timestamp(comment.get("created_at", ""))
        if created_at is None or created_at.timestamp() < not_before_with_tolerance:
            stale_candidates.append(comment.get("id"))
            continue
        comment_id = comment.get("id")
        comment_url = comment.get("html_url", "")
        print(
            f"git_host_api: verify-comment confirmed -- comment id={comment_id} "
            f"by {bot_login!r} on PR #{pr_number}, created_at="
            f"{comment.get('created_at')!r} (freshness anchor satisfied, "
            f"not_before={not_before.isoformat()!r}).",
            file=sys.stderr,
        )
        return {"id": comment_id, "html_url": comment_url, "login": bot_login, "body": comment_body}

    if stale_candidates:
        _fail(
            f"verify-comment MISMATCH -- {len(stale_candidates)} comment(s) by "
            f"{bot_login!r} matched the posted body but FAILED the freshness "
            f"anchor (created_at older than not_before="
            f"{not_before.isoformat()!r}, stale comment ids={stale_candidates}). "
            f"A stale/pre-existing comment with overlapping body text cannot "
            f"satisfy this post's verify-comment. Gate-pass REFUSED.",
            code=EXIT_VERIFY_FAILED,
        )

    _fail(
        f"verify-comment MISMATCH -- No comment by {bot_login!r} with the posted "
        f"body was found in GET issues/{pr_number}/comments on "
        f"{git_host_base}/repos/{owner}/{repo}. The comment may have landed on the "
        f"wrong PR, failed silently, or was posted under a different identity. "
        f"Gate-pass REFUSED.",
        code=EXIT_VERIFY_FAILED,
    )


def verify_verdict_block(
    verified_comment_body: str,
    *,
    reviewer: str,
    expected_review_status: str,
    expected_head_sha: str,
    expected_pr_number: int,
) -> None:
    """Re-parse the fenced ```review-result``` block from the VERIFIED
    comment's own body (the readback --verify-comment already confirmed
    landed, fresh, on the correct PR) and confirm every field matches what
    --expect-verdict-block requested.

    This is a defense-in-depth check on top of --verify-comment's own
    substring match: --verify-comment already confirms the posted body
    string appears in the readback; this additionally confirms the fence
    WITHIN that body still parses to the exact fields this invocation built,
    using merge.verdict.parse_verdict_block -- the identical parser the
    merge gate itself uses, so "this tool thinks it posted a valid fence"
    and "the merge gate will accept this fence" can never silently disagree.

    Raises GitHostApiError(code=EXIT_VERDICT_BLOCK_MISMATCH) if no fence is
    found, or any field does not match.
    """
    parsed = parse_verdict_block(verified_comment_body)
    if parsed is None:
        _fail(
            "--expect-verdict-block: verify-comment confirmed the comment "
            "landed, but re-parsing its own body found NO fenced "
            "```review-result``` block. Gate-pass REFUSED.",
            code=EXIT_VERDICT_BLOCK_MISMATCH,
        )
    mismatches = []
    if parsed.get("reviewer") != reviewer:
        mismatches.append(f"reviewer: expected {reviewer!r}, got {parsed.get('reviewer')!r}")
    if parsed.get("review_status") != expected_review_status:
        mismatches.append(
            f"review_status: expected {expected_review_status!r}, got "
            f"{parsed.get('review_status')!r}"
        )
    if parsed.get("head_sha") != expected_head_sha:
        mismatches.append(
            f"head_sha: expected {expected_head_sha!r}, got {parsed.get('head_sha')!r}"
        )
    if parsed.get("pr_number") != expected_pr_number:
        mismatches.append(
            f"pr_number: expected {expected_pr_number!r}, got {parsed.get('pr_number')!r}"
        )
    if mismatches:
        _fail(
            "--expect-verdict-block MISMATCH -- the verified comment's own "
            "fenced ```review-result``` block does not match what was "
            "requested: " + "; ".join(mismatches) + ". Gate-pass REFUSED.",
            code=EXIT_VERDICT_BLOCK_MISMATCH,
        )


def get_comment(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    comment_id: str,
    *,
    opener=None,
) -> dict[str, Any]:
    """GET the single comment at issues/comments/<comment_id> (lr-e2ce66).

    Belt-and-suspenders step (a) for --delete-own-comment: reads the
    comment's current author login and body BEFORE any DELETE is issued, so
    the authorship and verdict-fence checks below are evaluated against the
    live comment state, never a caller-supplied guess.

    Raises GitHostApiError(code=EXIT_DELETE_OWN_COMMENT_REFUSED) if the GET
    does not return HTTP 200 with a parseable JSON object -- a comment that
    cannot be read cannot be safely deleted under this contract.
    """
    status, raw = request(
        git_host_base,
        "GET",
        f"/api/v1/repos/{owner}/{repo}/issues/comments/{comment_id}",
        token,
        opener=opener,
    )
    if status != 200:
        _fail(
            f"delete-own-comment REFUSED -- GET issues/comments/{comment_id} "
            f"returned HTTP {status}. Cannot confirm authorship/verdict-fence "
            f"status of a comment that cannot be read.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )
    try:
        comment = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        _fail(
            f"delete-own-comment REFUSED -- GET issues/comments/{comment_id} "
            f"returned unparseable JSON: {exc}.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )
    if not isinstance(comment, dict):
        _fail(
            f"delete-own-comment REFUSED -- GET issues/comments/{comment_id} "
            f"returned a non-object JSON body.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )
    return comment


def delete_own_comment(
    git_host_base: str,
    token: str,
    owner: str,
    repo: str,
    comment_id: str,
    *,
    opener=None,
) -> None:
    """Belt-and-suspenders self-delete-own-comment (lr-e2ce66), mirroring
    --verify-comment's post-and-verify posture but pre-flighted BEFORE the
    mutating call rather than after it.

    ADMISSIBLE OPERATION (operator-agreed): delete a
    comment IFF (author login == the caller's OWN bot identity, resolved
    from the token) AND (comment body contains NO fenced
    ```review-result``` block). No crew agent may delete another author's
    comment (cross-author delete is an audit-tampering/censorship surface --
    refused unconditionally, never an override). Even a self-authored
    comment carrying a landed verdict fence is refused, so deleting a
    verdict can never be used to game the merge gate's re-read
    (post-clean, get-read, delete, repost).

    Order of operations, BEFORE issuing DELETE:
      (a) GET the comment (get_comment) -- resolves its live author login
          and body.
      (b) Resolve the caller's OWN bot login (resolve_bot_login, the same
          function --verify-comment's readback already uses) and assert the
          comment's author login matches it exactly. Refuse otherwise.
      (c) Re-parse the comment body for a fenced ```review-result``` block
          via merge.verdict.parse_verdict_block -- the SAME single-source-
          of-truth parser the merge gate and --expect-verdict-block's own
          readback use, never a bespoke regex. Refuse if a fence is found.
      (d) Only then issue DELETE.

    Both platforms independently gate delete on authorship-or-admin at the
    API layer (identity-of-token) -- this tool-side check is defense-in-
    depth on top of that platform enforcement, not a replacement for it.

    Raises GitHostApiError(code=EXIT_DELETE_OWN_COMMENT_REFUSED) on any
    refusal (unreadable comment, cross-author, verdict-fence-present, or a
    non-2xx DELETE response -- request() itself raises EXIT_CURL_FAILED for
    a non-2xx DELETE, which propagates unchanged since a distinct code
    there would blur "the belt-and-suspenders checks passed but the DELETE
    itself failed" into the same bucket as a refused-before-any-I/O case).
    """
    comment = get_comment(git_host_base, token, owner, repo, comment_id, opener=opener)
    comment_login = comment.get("user", {}).get("login", "")
    comment_body = comment.get("body", "")

    bot_login = resolve_bot_login(git_host_base, token, opener=opener)
    if comment_login != bot_login:
        _fail(
            f"delete-own-comment REFUSED -- comment {comment_id!r} on "
            f"{owner}/{repo} is authored by {comment_login!r}, not the "
            f"caller's own bot login {bot_login!r}. No crew agent may "
            f"delete another author's comment (cross-author delete is an "
            f"audit-tampering/censorship surface). DELETE not issued.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )

    if parse_verdict_block(comment_body) is not None:
        _fail(
            f"delete-own-comment REFUSED -- comment {comment_id!r} on "
            f"{owner}/{repo} carries a fenced ```review-result``` block. "
            f"Deleting a landed verdict could game the merge-gate re-read "
            f"(post clean, get read, delete, repost) -- even a caller's own "
            f"verdict comment is never eligible for delete. DELETE not "
            f"issued.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )

    request(
        git_host_base,
        "DELETE",
        f"/api/v1/repos/{owner}/{repo}/issues/comments/{comment_id}",
        token,
        opener=opener,
    )
    print(
        f"git_host_api: delete-own-comment confirmed -- comment id={comment_id} "
        f"by {bot_login!r} on {owner}/{repo} deleted (author-verified, "
        f"no verdict fence).",
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Argument parsing / CLI
# ---------------------------------------------------------------------------


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-git-host-api",
        description=(
            "loadout-git-host-api -- authenticated Forgejo REST call. "
            "--body-stdin or --body-env is the body path for every write "
            "method (exactly one required); a comments POST requires "
            "--verify-comment. --body-env reads a FIXED, statically-"
            "analyzable path a caller's harness stages before invoking this "
            "verb, so the invoking command line carries zero per-invocation "
            "body data on argv or a pipe. A reviewer role "
            "posting a merge-gate verdict uses --expect-verdict-block "
            "<reviewer> INSTEAD OF hand-authoring the fenced "
            "```review-result``` block: this tool builds/appends the fence "
            "itself from the body-ingestion flag's structured review_status "
            "field, so "
            "no backtick ever has to cross the shell. Any caller can attach "
            "an opaque work-item tracking id to a comment via "
            "--caller-tracking-id INSTEAD OF a hand-authored fence or a "
            "heredoc-staged state note: this tool composes the body "
            "in-process. PATH may also be an absolute GitHub API URL (e.g. "
            "https://api.github.com/...), in which case the reader's GitHub "
            "token is used and no git-host base is prepended. A DELETE to "
            "issues/comments/<id> requires --delete-own-comment: belt-and-"
            "suspenders self-delete that GETs the comment first and refuses "
            "unless the caller's own bot login authored it and its body "
            "carries no review-result verdict fence."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  loadout-git-host-api /api/v1/repos/some-owner/some-repo/pulls/42.diff\n"
            "  loadout-git-host-api POST /api/v1/repos/some-owner/some-repo/issues/42/comments \\\n"
            "      --verify-comment --pr-sha abc123 < body.json\n"
            "  echo '{\"body\":\"...\"}' | loadout-git-host-api --caller some-role --body-stdin "
            "--verify-comment --pr-sha abc123 POST "
            "/api/v1/repos/some-owner/some-repo/issues/42/comments\n"
            "  loadout-git-host-api GET https://api.github.com/repos/some-owner/some-repo/pulls/42/reviews\n"
            "\n"
            "Reviewer route -- tool-owned verdict fence, NO backticks cross the shell:\n"
            "  echo '{\"body\":\"LGTM, no issues found.\",\"review_status\":\"clean\"}' | \\\n"
            "    loadout-git-host-api --caller some-reviewer --body-stdin --verify-comment \\\n"
            "      --pr-sha abc123 --expect-verdict-block some-reviewer \\\n"
            "      POST /api/v1/repos/some-owner/some-repo/issues/42/comments\n"
            "\n"
            "Caller-tracking-id route -- tool-owned metadata note, NO heredoc, NO $VAR redirect:\n"
            "  echo '{\"body\":\"Status update: build green.\"}' | \\\n"
            "    loadout-git-host-api --caller some-role --body-stdin --verify-comment \\\n"
            "      --caller-tracking-id some-tracking-id-123 \\\n"
            "      POST /api/v1/repos/some-owner/some-repo/issues/42/comments\n"
            "\n"
            "Body-off-argv route -- a harness stages the body at the FIXED\n"
            "path first (e.g. its own Write tool), then invokes with a CONSTANT argv --\n"
            "no per-invocation body substring, no pipe, no producer:\n"
            "    loadout-git-host-api --caller some-role --body-env --verify-comment \\\n"
            "      --pr-sha abc123 POST /api/v1/repos/some-owner/some-repo/issues/42/comments\n"
            "\n"
            "Self-delete-own-comment route -- belt-and-suspenders: refuses\n"
            "unless the caller's own bot login authored the comment and it carries no\n"
            "review-result verdict fence:\n"
            "    loadout-git-host-api --caller some-role --delete-own-comment \\\n"
            "      DELETE /api/v1/repos/some-owner/some-repo/issues/comments/123\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-git-host-api {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        default=None,
        help=f"Role/name whose token is resolved via the credential provider "
        f"(default: {DEFAULT_ROLE!r}). An already attested value, consumed "
        f"as an opaque config key -- when EXPLICITLY supplied, it must match "
        f"this process's own attested invoking identity (transport."
        f"attestation.resolve_identity) or the call is refused fail-closed "
        f"before any I/O; omitted, this check does not apply.",
    )
    parser.add_argument(
        "--body-stdin",
        action="store_true",
        help="Read the request body from stdin. Content is validated before "
        "the request is issued. Mutually exclusive with --body-env (exactly "
        "one body-ingestion flag is required for a write method).",
    )
    parser.add_argument(
        "--body-env",
        action="store_true",
        help="BODY-OFF-ARGV-AND-PIPE route: read the request "
        "body from a FIXED, statically-analyzable, CALLER-NAMESPACED path "
        "($TMPDIR/clagentic-loadout/body.<caller>.json, transport.body_env."
        "resolve_caller_body_path) that the caller's own harness "
        "(e.g. its Write tool) stages BEFORE invoking this verb -- see "
        "docs/integration.md. Takes NO value, so the invoking command line "
        "is a CONSTANT string with no per-invocation body substring, unlike "
        "--body-stdin piped from an argv producer (e.g. `echo '{...}' |`), "
        "which puts the varying JSON body on the shell command line every "
        "call. Namespaced by --caller so two concurrent same-TMPDIR callers "
        "can never collide on one staged file. Content is validated "
        "identically to --body-stdin, from the same call site. Mutually "
        "exclusive with --body-stdin. " + BODY_ENV_NOT_EPHEMERAL_NOTE,
    )
    parser.add_argument(
        "--verify-comment",
        action="store_true",
        help="Mandatory for a POST to issues/<pr>/comments: after posting, "
        "read back and confirm the caller's own comment landed, freshly, on "
        "the correct PR.",
    )
    parser.add_argument(
        "--pr-sha",
        default=None,
        help="Confirm the PR head SHA matches before posting (requires "
        "--verify-comment to take effect). Also supplies the fenced "
        "verdict block's head_sha field when --expect-verdict-block is "
        "used -- one value, never a second SHA to keep in sync.",
    )
    parser.add_argument(
        "--expect-verdict-block",
        metavar="REVIEWER",
        default=None,
        help="TOOL-OWNED fence construction for a reviewer's merge-gate "
        "verdict: builds and appends a fenced ```review-result``` block "
        "(clagentic_loadout.merge.verdict.build_verdict_block) to the "
        "--body-stdin 'body' prose before posting, using this flag's value "
        "as the fence's 'reviewer' field. --body-stdin's JSON must also "
        "carry a 'review_status' field ('clean' or 'blocking') -- the only "
        "field this tool cannot derive on the caller's behalf. No backtick "
        "ever needs to appear in --body-stdin's JSON or in any argv. "
        "Requires POST to issues/<pr>/comments, --body-stdin, "
        "--verify-comment, and --pr-sha (supplies head_sha); pr_number is "
        "taken from PATH. After the ordinary --verify-comment readback, "
        "the verified comment's own body is re-parsed and checked "
        "field-for-field against what was requested.",
    )
    parser.add_argument(
        "--caller-tracking-id",
        metavar="ID",
        default=None,
        help="TOOL-OWNED metadata composition: appends a "
        "fenced ```loadout-note``` block (transport.note_compose."
        "build_composed_body) carrying this opaque work-item tracking id "
        "to the posted comment body, entirely in-process -- no backtick, "
        "heredoc, or $VAR-substituted redirect target ever has to cross "
        "the shell to carry a tracking reference alongside a comment. "
        "Opaque to this tool: a lore task id for a LORE-integrated "
        "deployment, or any other deployment's own tracking-id shape. "
        "Requires a POST to issues/<pr>/comments and --verify-comment.",
    )
    parser.add_argument(
        "--delete-own-comment",
        action="store_true",
        help="MANDATORY for a DELETE to issues/comments/<id>: belt-and-"
        "suspenders self-delete, mirroring --verify-comment's posture. "
        "BEFORE issuing DELETE, GETs the comment and refuses unless (a) its "
        "author login matches the caller's own resolved bot login "
        "(cross-author delete is refused unconditionally -- no override) "
        "and (b) its body carries no fenced ```review-result``` block "
        "(deleting a landed verdict is refused even for the caller's own "
        "comment). A DELETE to issues/comments/<id> that OMITS this flag is "
        "a hard refusal BEFORE any I/O.",
    )
    parser.add_argument(
        "--git-host-base-url",
        default=None,
        help=f"Forgejo API base URL (default: ${GIT_HOST_BASE_URL_ENV_VAR} env "
        f"var, falling back to the compat-alias env var "
        f"named by ${GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR} (default "
        f"alias name {DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS!r}) if unset, "
        f"then the {GIT_HOST_CONFIG_SECTION!r}.{GIT_HOST_CONFIG_KEY_BASE_URL!r} "
        f"key in ~/.config/clagentic/loadout/config.yaml if unset (falling "
        f"back to the legacy {LEGACY_GIT_HOST_CONFIG_SECTION!r} section name "
        f"there for an existing install that has not migrated), or "
        f"{DEFAULT_GIT_HOST_BASE_URL!r} if none is set). This value is "
        f"Forgejo-only plumbing: it is unused on a GitHub-targeted call.",
    )
    parser.add_argument(
        "method_or_path",
        help="HTTP method (GET/POST/PATCH/PUT/DELETE, defaults to GET when "
        "the first positional is the path) or the API PATH itself.",
    )
    parser.add_argument(
        "path_if_method",
        nargs="?",
        default=None,
        help="API PATH when a method was given as the first positional.",
    )
    return parser


def _split_method_and_path(args: argparse.Namespace) -> tuple[str, str]:
    methods = ("GET", "POST", "PATCH", "PUT", "DELETE")
    first = args.method_or_path
    if first.upper() in methods and args.path_if_method:
        return first.upper(), args.path_if_method
    if first.upper() in methods and args.path_if_method is None:
        _fail(f"PATH argument required after method {first.upper()!r}.", code=EXIT_USAGE)
    return "GET", first


def _is_github_target(path_arg: str, *, github_hostname: str = DEFAULT_GITHUB_HOSTNAME) -> bool:
    """True iff *path_arg* targets GitHub rather than the configured Forgejo/
    git-host base (lr-104a).

    A relative path (e.g. "/api/v1/repos/o/r/pulls/1.diff", the ordinary
    Forgejo shape) is never a GitHub target -- only an ABSOLUTE http(s) URL
    whose host matches *github_hostname* (default DEFAULT_GITHUB_HOSTNAME,
    'github.com' -- the SAME sentinel platform_detect.detect_platform_from_url
    uses, no second hardcoded hostname introduced here) is routed to the
    GitHub read path. This mirrors platform_detect's own "full URL is the
    only valid platform signal" rule: a bare relative path has no host to
    inspect at all, so it always keeps the existing Forgejo/git-host-base
    behavior byte-for-byte.

    Uses urllib.parse rather than a substring test on the raw string so a
    Forgejo path that happens to CONTAIN the literal text "github.com"
    somewhere in a query string or PR title is never misrouted -- only the
    URL's own host component is inspected.

    The host match accepts *github_hostname* itself OR any subdomain of it
    (e.g. the real API host "api.github.com" for the default 'github.com'
    sentinel) -- a dot-boundary suffix check, not a raw substring, so a host
    like "evil-github.com" can never spoof a match against "github.com".
    """
    parsed = urllib.parse.urlsplit(path_arg)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname
    if hostname is None:
        return False
    hostname = hostname.lower()
    sentinel = github_hostname.lower()
    return hostname == sentinel or hostname.endswith(f".{sentinel}")


def _resolve_git_host_base(
    explicit: str | None,
    *,
    env: dict[str, str] | None = None,
    config_root: str | None = None,
) -> str:
    """Resolve the git-host (Forgejo) API base URL.

    Precedence (lr-87bb, config-file tier added lr-e570; the deprecated
    pre-rename FORGE_BASE_URL env-var fallback removed lr-9fdbed -- scorched
    earth, zero 'forge' env-var strings remain, BREAKING for any deployment
    still exporting the old var):
      1. *explicit* (--git-host-base-url) -- always wins when non-empty.
      2. GIT_HOST_BASE_URL_ENV_VAR (CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL) --
         the branded var, PRIMARY source once no explicit flag is given.
      3. The compat-alias env var -- name taken from
         GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR if set, else
         DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS ("FORGEJO_BASE_URL").
         Consulted ONLY when the branded var above is unset/empty -- a
         fallback, never a co-equal source. Lets a deployment whose spawn
         env already exports a differently-named legacy base-URL var
         (pointing at the real git host) work without an env-export change
         on that side.
      4. The USER-LEVEL config file, <config_root>/config.yaml (default
         transport.provider_config.DEFAULT_USER_CONFIG_ROOT --
         ~/.config/clagentic/loadout/config.yaml), `forgejo:` section,
         `base_url` key -- read via provider_config.load_user_config_section,
         the SAME loader/config-root convention the credential-provider
         tier already uses (no second YAML parser, no second config path).
         This is the RELEASED, no-hand-export mechanism: an installer or a
         one-time `clagentic-loadout` config write lands this once, and no
         deployment has to hand-export an env var to point loadout at its
         git host. Consulted ONLY when every env-var tier above is
         unset/empty.

         COMPAT SHIM (lr-08b451): the section was renamed from `git_host:`
         to `forgejo:` to make its Forgejo-only scope honest (see
         GIT_HOST_CONFIG_SECTION's own docstring for why). When `forgejo:`
         has no `base_url` value, the LEGACY `git_host:` section
         (LEGACY_GIT_HOST_CONFIG_SECTION) is read as a fallback, so an
         existing install that has only ever seeded the old name keeps
         resolving exactly as before -- no forced re-seed, no breakage. The
         NEW name wins whenever both sections carry a value.
      5. DEFAULT_GIT_HOST_BASE_URL (the localhost placeholder) -- final
         fallback; never an operator host (CLAUDE.md rule 1).

    *env* overrides os.environ for tests; defaults to the real process
    environment. *config_root* overrides the user-level config root the
    config-file tier reads from (mainly for tests), mirroring
    provider_config.resolve_platform_provider's own `config_root` parameter.
    """
    if explicit:
        return explicit.rstrip("/")
    import os

    active_env = env if env is not None else os.environ

    branded = active_env.get(GIT_HOST_BASE_URL_ENV_VAR)
    if branded:
        return branded.rstrip("/")

    alias_name = (
        active_env.get(GIT_HOST_BASE_URL_COMPAT_ALIAS_NAME_ENV_VAR)
        or DEFAULT_GIT_HOST_BASE_URL_COMPAT_ALIAS
    )
    alias_value = active_env.get(alias_name)
    if alias_value:
        return alias_value.rstrip("/")

    config_section = load_user_config_section(GIT_HOST_CONFIG_SECTION, config_root=config_root)
    config_value = config_section.get(GIT_HOST_CONFIG_KEY_BASE_URL)
    if isinstance(config_value, str) and config_value:
        return config_value.rstrip("/")

    # COMPAT SHIM (lr-08b451): fall back to the pre-rename `git_host:`
    # section when the new `forgejo:` section has no value, so an install
    # that has only ever seeded the old name keeps working unchanged.
    legacy_section = load_user_config_section(
        LEGACY_GIT_HOST_CONFIG_SECTION, config_root=config_root
    )
    legacy_value = legacy_section.get(GIT_HOST_CONFIG_KEY_BASE_URL)
    if isinstance(legacy_value, str) and legacy_value:
        return legacy_value.rstrip("/")

    return DEFAULT_GIT_HOST_BASE_URL.rstrip("/")


def _absolute_url_host_matches_git_host_base(path_arg: str, git_host_base: str) -> bool:
    """True iff *path_arg* is an absolute http(s) URL whose host:port matches
    *git_host_base*'s own host:port (lr-69af67, closing a gap flagged
    non-blocking on lr-8f7d4e/#77: the Forgejo absolute-URL branch had no
    equivalent to _is_github_target's hostname-anchor check).

    Mirrors _is_github_target's "urlsplit the host component, never a raw
    substring test" approach, but the comparison target here is the
    RESOLVED git-host base (a full URL, e.g. http://127.0.0.1:3000 or
    http://forgejo.example.com:3000) rather than a bare hostname sentinel --
    so both host AND port must match; _is_github_target has no equivalent
    port dimension because GitHub's hostname sentinel is scheme/port-
    agnostic by design (any port on api.github.com or a subdomain routes),
    while the git-host base is a single concrete authority a deployment
    configures once.

    *path_arg* is assumed to already be an absolute http(s) URL (callers
    check urllib.parse.urlsplit(path_arg).scheme first, exactly as the
    existing path_arg_is_absolute_url check does) -- this function only
    answers "does its host:port match", not "is it absolute" a second time.
    """
    target_netloc = urllib.parse.urlsplit(path_arg).netloc.lower()
    base_netloc = urllib.parse.urlsplit(git_host_base).netloc.lower()
    return target_netloc == base_netloc


def _check_cross_platform_url_shape_mistake(
    path_arg: str,
    target_platform: str,
    *,
    git_host_base: str,
) -> None:
    """Detect a Forgejo-shaped path fat-fingered against a GitHub target, or
    a bare GitHub-shaped path fat-fingered against a Forgejo target
    (lr-aa4e3c), and fail closed with a corrective error naming the exact
    fixed-up URL.

    Evidence (lr-aa4e3c task description): a merge-gate caller issued
    'GET https://github.com/api/v1/repos/o/r/pulls/336' -- a Forgejo path
    shape (/api/v1/repos/...) on the github.com host. Platform detection
    correctly resolved PLATFORM_GITHUB, but _GITHUB_REPOS_URL_RE only
    matches the canonical api.github.com/repos/{owner}/{repo}/... shape, so
    repo extraction silently returned None and a {repo}-templated token
    helper refused with no clue why. That refusal read exactly like an
    unrelated environment defect -- this check gives the caller a one-line
    self-recoverable correction instead.

    Called ONLY after the resolved platform's own repo-path pattern has
    already failed to match path_arg (i.e. call_repo is None) -- a
    successful match on the resolved platform's own shape never reaches
    here, so an ordinary, correctly-shaped call is completely unaffected.

    Fail-closed with guidance ONLY: this never rewrites path_arg and never
    retries the call under a corrected URL -- silently rewriting a caller's
    URL would hide the caller-side bug this check exists to surface, and
    would widen this tool's behavior beyond "validate and report." Raises
    GitHostApiError(code=EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH) BEFORE any
    token mint is attempted when (and only when) the OTHER platform's shape
    actually matches; otherwise returns None (call_repo simply stays None,
    the pre-existing fail-open-to-unscoped behavior for a non-repo-scoped
    call).
    """
    if target_platform == PLATFORM_GITHUB:
        # A GitHub target's path_arg is the full absolute URL (see
        # _is_github_target) -- _REPOS_PATH_RE is anchored to a bare path
        # starting with /api/v1/repos/..., so it is matched against the
        # URL's own path component, never the scheme+host-prefixed string.
        url_path = urllib.parse.urlsplit(path_arg).path
        forgejo_shaped_match = _REPOS_PATH_RE.match(url_path)
        if forgejo_shaped_match:
            owner, repo = forgejo_shaped_match.group(1), forgejo_shaped_match.group(2)
            _fail(
                f"cross-platform URL-shape mistake -- PATH {path_arg!r} targets a "
                f"GitHub host but uses the Forgejo path shape (/api/v1/repos/...). "
                f"GitHub's REST API has no /api/v1/ prefix. Detected owner/repo: "
                f"{owner}/{repo}. Use "
                f"https://{DEFAULT_GITHUB_HOSTNAME}/repos/{owner}/{repo}/... "
                f"instead (e.g. https://api.{DEFAULT_GITHUB_HOSTNAME}/repos/"
                f"{owner}/{repo}/...).",
                code=EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH,
            )
    else:
        github_shaped_match = _BARE_REPOS_PATH_RE.match(path_arg)
        if github_shaped_match:
            owner, repo = github_shaped_match.group(1), github_shaped_match.group(2)
            _fail(
                f"cross-platform URL-shape mistake -- PATH {path_arg!r} targets a "
                f"Forgejo host but uses the GitHub path shape (/repos/... with no "
                f"/api/v1 prefix). Detected owner/repo: {owner}/{repo}. Use "
                f"{git_host_base}/api/v1/repos/{owner}/{repo}/... instead.",
                code=EXIT_CROSS_PLATFORM_URL_SHAPE_MISMATCH,
            )


def bind_caller(
    caller: str,
    *,
    caller_explicit: bool,
    identity: "Identity",
) -> None:
    """Enforce the layer (1)->(2) binding: an identity may act ONLY as
    ITS OWN attested value (lr-82c385, tome #700).

    `identity` is whatever `transport.attestation.resolve_identity` (or an
    injected equivalent) resolved for THIS process -- the configured
    provider, the sidecar adapter, or the built-in OS-user fallback, in that
    fixed order. `caller` is the value `--caller` resolved to (already
    defaulted to DEFAULT_ROLE when omitted, by the call site).

    FAIL-CLOSED, BEFORE ANY I/O: `caller != identity.subject` on an
    EXPLICIT --caller raises GitHostApiError(code=
    EXIT_CALLER_INVOKER_MISMATCH) -- no token mint is ever attempted, no
    request is ever issued. There is no override, no allowlist that admits
    a mismatch: even a role an operator-configured named-agent allowlist
    would otherwise grant is refused here if it does not match this
    process's own attested identity, because this check runs BEFORE (and
    independently of) whatever role-entitlement decision a
    TokenProvider/AuthorityProvider would make downstream -- it answers a
    different question ("is this process who it claims to be") than those
    seams do ("is this claimed role entitled to X").

    `caller_explicit=False` (an OMITTED --caller, defaulted to
    DEFAULT_ROLE by the call site) is NEVER checked against `identity` --
    this preserves the pre-existing, unchanged "omitted --caller behaves
    exactly as before" contract (this task's own test-matrix requirement).
    An omitted --caller is not an identity CLAIM at all; there is nothing
    to bind.

    This is INDEPENDENT of, and runs strictly BEFORE,
    `transport.credential_provider.resolve_token` and
    `merge.authority.check_authority` -- neither of those seams is changed
    by this function, and neither of them re-verifies what this function
    already confirmed. See this module's own docstring and
    `transport.attestation`'s module docstring for the full three-layer
    trust-model statement this function is layer (1)->(2) of.
    """
    if not caller_explicit:
        return
    if caller != identity.subject:
        _fail(
            f"--caller {caller!r} does not match the ATTESTED invoking "
            f"identity {identity.subject!r} (resolved via the "
            f"{identity.source!r} attestation layer). An identity may act "
            f"ONLY as its own attested value -- this is refused BEFORE any "
            f"network I/O and before any credential is resolved, "
            f"unconditionally, with no override (even a role a named-agent "
            f"allowlist would otherwise admit is denied here).",
            code=EXIT_CALLER_INVOKER_MISMATCH,
        )


def main(
    argv: list[str] | None = None,
    *,
    token_provider: TokenProvider | None = None,
    opener=None,
    known_bad_owners: frozenset[str] = frozenset(),
    identity_provider=None,
) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable) -- the `if __name__` guard below
    is the one place that translates the return value into a real exit.

    `token_provider` and `opener` are injection points for tests; both
    default to the real provider/urllib path in production use.
    `identity_provider` is a zero-arg callable returning a
    `transport.attestation.Identity` (defaults to
    `transport.attestation.resolve_identity`) -- the injection point for the
    fail-closed --caller/attested-invoker binding (lr-82c385, see
    `bind_caller`).
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
            known_bad_owners=known_bad_owners,
            identity_provider=identity_provider,
        )
    except GitHostApiError as exc:
        print(f"git-host-api: {exc}", file=sys.stderr)
        return exc.code


def _run(
    args: argparse.Namespace,
    *,
    token_provider: TokenProvider | None,
    opener,
    known_bad_owners: frozenset[str],
    identity_provider=None,
) -> int:
    method, path_arg = _split_method_and_path(args)

    path_owner_match = _REPOS_PATH_RE.match(path_arg)
    if path_owner_match:
        _validate_owner(path_owner_match.group(1), known_bad_owners=known_bad_owners)

    # --verify-comment is MANDATORY for a POST to issues/<pr>/comments.
    # Checked BEFORE any I/O -- a comments POST missing the flag is refused
    # for free, with no wasted token-resolution round trip.
    if method == "POST" and _ISSUE_COMMENTS_RE.match(path_arg) and not args.verify_comment:
        _fail(
            f"POST to {path_arg!r} (a PR/issue comments endpoint) requires "
            f"--verify-comment. A comments POST can no longer complete "
            f"fire-and-forget: a bare 2xx only proves the HTTP transaction "
            f"completed, not that the comment is confirmed present. Add "
            f"--verify-comment (and --pr-sha <sha> if available) to this "
            f"invocation.",
            code=EXIT_VERIFY_COMMENT_REQUIRED,
        )

    # --delete-own-comment preconditions, checked BEFORE any I/O (same
    # fail-fast posture as the --verify-comment check above): a DELETE to
    # issues/comments/<id> that omits this flag is refused for free -- a
    # comment delete can never complete fire-and-forget without the
    # belt-and-suspenders authorship/verdict-fence checks running first.
    delete_comment_match = _ISSUE_COMMENT_ID_RE.match(path_arg)
    if method == "DELETE" and delete_comment_match and not args.delete_own_comment:
        _fail(
            f"DELETE to {path_arg!r} (a single-comment endpoint) requires "
            f"--delete-own-comment. A comment delete can no longer complete "
            f"fire-and-forget: belt-and-suspenders (GET the comment, assert "
            f"caller-own authorship, assert no review-result verdict fence) "
            f"must run BEFORE the DELETE is issued. Add --delete-own-comment "
            f"to this invocation.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )
    if args.delete_own_comment and not (method == "DELETE" and delete_comment_match):
        _fail(
            f"--delete-own-comment requires a DELETE to an "
            f"issues/comments/<id> endpoint, got {method} {path_arg!r}.",
            code=EXIT_DELETE_OWN_COMMENT_REFUSED,
        )

    # --expect-verdict-block preconditions, checked BEFORE any I/O (same
    # fail-fast posture as the --verify-comment check above): a reviewer
    # verdict fence only makes sense on a comments POST, with a body-
    # ingestion flag (--body-stdin or --body-env), --verify-comment, and
    # --pr-sha all present -- --pr-sha supplies the fence's head_sha, so it
    # is not optional the way it is for an ordinary comment.
    if args.expect_verdict_block is not None:
        if method != "POST" or not _ISSUE_COMMENTS_RE.match(path_arg):
            _fail(
                f"--expect-verdict-block requires a POST to an "
                f"issues/<pr>/comments endpoint, got {method} {path_arg!r}.",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
        if not (args.body_stdin or args.body_env):
            _fail(
                "--expect-verdict-block requires a body-ingestion flag "
                "(--body-stdin or --body-env; the fence's prose + "
                "review_status are supplied there, never via a "
                "shell-visible argv).",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
        if not args.verify_comment:
            _fail(
                "--expect-verdict-block requires --verify-comment (the "
                "posted fence is re-parsed from the verified comment's own "
                "readback body, never from the locally-constructed string).",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
        if not args.pr_sha:
            _fail(
                "--expect-verdict-block requires --pr-sha (supplies the "
                "fence's head_sha field -- the caller's own evaluated SHA, "
                "not a second value to keep in sync).",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )
        if not _SAFE_CALLER_RE.match(args.expect_verdict_block):
            _fail(
                f"--expect-verdict-block {args.expect_verdict_block!r} "
                f"contains invalid characters (only alphanumeric, hyphen, "
                f"underscore; no path separators or traversal).",
                code=EXIT_VERDICT_BLOCK_USAGE,
            )

    # --caller-tracking-id preconditions, checked BEFORE any I/O (lr-10a996,
    # same fail-fast posture as --expect-verdict-block above): the tracking
    # id is composed into a comment body, so it only makes sense on a
    # comments POST with --verify-comment (the composed body must be
    # confirmed to have actually landed).
    if args.caller_tracking_id is not None:
        if method != "POST" or not _ISSUE_COMMENTS_RE.match(path_arg):
            _fail(
                f"--caller-tracking-id requires a POST to an "
                f"issues/<pr>/comments endpoint, got {method} {path_arg!r}.",
                code=EXIT_CALLER_TRACKING_ID_USAGE,
            )
        if not args.verify_comment:
            _fail(
                "--caller-tracking-id requires --verify-comment (the "
                "composed body, including the appended loadout-note fence, "
                "is confirmed via the ordinary post-and-verify readback).",
                code=EXIT_CALLER_TRACKING_ID_USAGE,
            )
        if not _SAFE_CALLER_TRACKING_ID_RE.match(args.caller_tracking_id):
            _fail(
                f"--caller-tracking-id {args.caller_tracking_id!r} is "
                f"empty, contains whitespace, or contains a control "
                f"character (only printable non-whitespace ASCII, max 128 "
                f"chars).",
                code=EXIT_CALLER_TRACKING_ID_USAGE,
            )

    # --caller is resolved and validated HERE, before body ingestion
    # (moved up from its original post-body-ingestion position, lr-3a7ae8):
    # --body-env now reads a CALLER-NAMESPACED staged path (see below), so
    # the validated caller identity has to exist before that read happens,
    # not after.
    caller = args.caller or DEFAULT_ROLE
    if not _SAFE_CALLER_RE.match(caller):
        _fail(
            f"--caller {caller!r} contains invalid characters (only "
            f"alphanumeric, hyphen, underscore; no path separators or "
            f"traversal).",
            code=EXIT_USAGE,
        )

    # --caller/attested-invoker fail-closed binding (lr-82c385, tome #700),
    # checked BEFORE any I/O -- same fail-fast posture as every other
    # precondition above. An OMITTED --caller (args.caller is None) is never
    # checked here: bind_caller's own docstring is the single source of
    # truth for why (it is not an identity claim, there is nothing to
    # bind) -- this preserves the pre-existing "omitted --caller behaves
    # exactly as before" contract unchanged.
    resolve_identity_fn = identity_provider if identity_provider is not None else _resolve_identity
    try:
        attested_identity = resolve_identity_fn()
    except AttestationError as exc:
        _fail(
            f"attested-identity resolution FAILED -- {exc}",
            code=EXIT_CALLER_INVOKER_MISMATCH,
        )
    bind_caller(caller, caller_explicit=args.caller is not None, identity=attested_identity)

    # PR number, extracted EARLY (lr-becdef): a pure regex match against
    # path_arg, no I/O, no dependency on --verify-comment -- moved ahead of
    # the body-ingestion block below so a --body-env read can be bound to
    # the PR it is actually being read for (see body_env_comments_match's
    # use at the read call site). This does not replace the existing
    # comments_match computed further below (still gated on
    # args.verify_comment for that use) -- it is a second, unconditional
    # match against the same path shape, purely to make pr_number available
    # earlier for the stale-read provenance check.
    body_env_comments_match = _ISSUE_COMMENTS_RE.match(path_arg)
    body_env_target_pr = (
        int(body_env_comments_match.group(3)) if body_env_comments_match else None
    )

    # Body ingestion: exactly one of --body-stdin / --body-env for a write
    # method that actually sends a body. Checked BEFORE any I/O, same
    # fail-fast posture as every other precondition above -- a caller that
    # supplies both, or neither, on a write method gets a resolved-values
    # usage error rather than an ambiguous silent choice between the two
    # sources. --delete-own-comment is the ONE write-method path exempted
    # from this requirement (lr-e2ce66): a comment DELETE sends no request
    # body at all -- delete_own_comment() (called below) never reads
    # stdin_bytes, so requiring a body-ingestion flag here would force a
    # caller to supply meaningless body content for an operation that has
    # none.
    #
    # --body-env reads a CALLER-NAMESPACED staged path (lr-3a7ae8:
    # <TMPDIR>/clagentic-loadout/body.<caller>.json, not the single shared
    # fixed path) -- two concurrent same-TMPDIR callers with different
    # --caller values can never collide on one physical file, and a caller
    # that never staged its own body fails closed rather than risking a
    # foreign caller's staged content (root cause per a structured
    # diagnosis on a real incident, lr-f00c6f). The read is ALSO bound to
    # body_env_target_pr (and args.pr_sha, when supplied) and consumes the
    # staged file on success (lr-becdef): a leftover body from a prior,
    # unrelated PR/invocation can no longer be silently re-read and
    # re-posted -- see transport.body_env's module docstring.
    stdin_bytes: bytes | None = None
    if method in _WRITE_METHODS and not args.delete_own_comment:
        if args.body_stdin and args.body_env:
            _fail(
                "--body-stdin and --body-env are mutually exclusive -- "
                "supply exactly one body-ingestion flag.",
                code=EXIT_BODY_INGESTION_USAGE,
            )
        if args.body_stdin:
            stdin_bytes = sys.stdin.buffer.read()
        elif args.body_env:
            if body_env_target_pr is None:
                _fail(
                    f"--body-env requires a POST/PATCH/PUT/DELETE to an "
                    f"issues/<pr>/comments endpoint so the staged body can "
                    f"be bound to the PR it was staged for "
                    f"(stale-read guard); {path_arg!r} does not match that "
                    f"shape.",
                    code=EXIT_BODY_ENV_UNREADABLE,
                )
            try:
                stdin_bytes = read_body_bytes(
                    caller=caller,
                    expect_target_pr=body_env_target_pr,
                    expect_head_sha=args.pr_sha,
                )
            except BodyEnvError as exc:
                _fail(str(exc), code=EXIT_BODY_ENV_UNREADABLE)
        if stdin_bytes is not None and args.expect_verdict_block is None:
            validate_body_stdin_content(stdin_bytes)
        # else (expect_verdict_block set): build_expected_verdict_body
        # (called below, once pr_number is known from the path) performs the
        # equivalent validation plus the review_status field check --
        # calling validate_body_stdin_content here too would duplicate that
        # work for no benefit.

    # git-host-aware target routing (lr-104a): an ABSOLUTE URL whose host is
    # GitHub is read via the reader-role GitHub token, with NO git-host base
    # prepended -- path_arg is already the full URL. Every other path shape
    # (a relative Forgejo path, the overwhelming majority of calls) keeps
    # the existing behavior byte-for-byte: PLATFORM_FORGEJO token + the
    # resolved git-host base prepended in front of the path. See
    # _is_github_target's own docstring for why this is a host-component
    # check, not a raw substring test.
    target_platform = PLATFORM_GITHUB if _is_github_target(path_arg) else PLATFORM_FORGEJO

    # Repo context (lr-ea28, GitHub-URL extraction added lr-5f7971): derive
    # "owner/repo" from the request target so a repo-scoped minting provider
    # (e.g. a GitHub-App-style installation-token mint, whose command is
    # templated with a {repo} placeholder) can resolve. A GitHub target's
    # path_arg is an ABSOLUTE URL with no /api/v1/ prefix, so _REPOS_PATH_RE
    # (the Forgejo path shape already matched above for the known-bad-owner
    # check) never matches it -- _GITHUB_REPOS_URL_RE is used instead,
    # scoped to ONLY the GitHub branch so Forgejo's existing
    # _REPOS_PATH_RE-derived behavior is byte-for-byte unchanged. None for a
    # target that is not repo-scoped (e.g. /api/v1/user, or a GitHub
    # /user, /orgs/... URL) -- a repo-scoped minting provider configured
    # with a {repo} placeholder will fail closed on such a call (see
    # CommandTokenProvider), which is correct: a non-repo-scoped call has no
    # owner/repo to mint a scoped token for.
    if target_platform == PLATFORM_GITHUB:
        github_repo_match = _GITHUB_REPOS_URL_RE.match(path_arg)
        call_repo = (
            f"{github_repo_match.group(1)}/{github_repo_match.group(2)}"
            if github_repo_match
            else None
        )
    else:
        call_repo = (
            f"{path_owner_match.group(1)}/{path_owner_match.group(2)}"
            if path_owner_match
            else None
        )

    # Cross-platform URL-shape mistake (lr-aa4e3c): the resolved platform's
    # own repo-path pattern just failed to match (call_repo is None) -- BEFORE
    # any token mint is attempted, check whether PATH actually matches the
    # OTHER platform's repo-path shape instead. If it does, this is a
    # nameable caller-side URL-shape mistake, not an ordinary non-repo-scoped
    # call -- fail closed with a corrective error naming the exact fixed-up
    # URL rather than proceeding to a token mint that will refuse opaquely.
    if call_repo is None:
        _check_cross_platform_url_shape_mistake(
            path_arg, target_platform, git_host_base=_resolve_git_host_base(args.git_host_base_url)
        )

    print(
        f"git-host-api: resolving token for caller={caller!r} platform={target_platform!r}",
        file=sys.stderr,
    )
    active_provider = (
        token_provider
        if token_provider is not None
        else resolve_platform_provider(target_platform)
    )
    try:
        token = _resolve_token(caller, active_provider, repo=call_repo)
    except CredentialProviderError as exc:
        _fail(f"token resolution FAILED -- {exc}", code=EXIT_TOKEN_FETCH_FAILED)

    # A GitHub target's path_arg IS the full URL -- git_host_base is empty so
    # request()'s f"{git_host_base}{path}" join reduces to the URL itself,
    # rather than prepending the Forgejo/git-host base in front of an
    # already-absolute GitHub URL (the exact malformed-URL defect lr-104a
    # fixed: 'http://<git-host-base><https://api.github.com/...>').
    #
    # lr-8f7d4e: the SAME malformed-URL class also happens on the Forgejo
    # branch when path_arg is already an absolute http(s) URL -- a caller
    # contract that passes a full git-host URL as PATH rather than a
    # relative one (see the caller-habit case already named in
    # _is_github_target's own docstring). _is_github_target correctly says
    # "not GitHub" for a non-GitHub host, but that left git_host_base still
    # being prepended in front of an already-absolute URL, producing
    # 'http://127.0.0.1:3000http://forgejo.example.com:3000/...', a
    # malformed authority urllib cannot resolve ("Name or service not
    # known"). Mirrors the GitHub branch above: when path_arg is itself an
    # absolute http(s) URL AND its host:port matches the resolved git-host
    # base (see below), git_host_base is emptied so request()'s join always
    # reduces to the URL verbatim. A relative Forgejo path (the overwhelming
    # majority of calls, and forgejo-curl's own convention) is unaffected --
    # it has no scheme/host for urlsplit to find, so this stays False and
    # the existing base-prepend behavior is byte-for-byte unchanged.
    #
    # lr-69af67 (closing a gap flagged non-blocking on lr-8f7d4e/#77 by both
    # review passes): the fix above emptied git_host_base for ANY absolute
    # http(s) URL on the Forgejo branch, with no host-anchor check
    # equivalent to _is_github_target's hostname-suffix match -- so the live
    # Forgejo bearer token would be attached and sent to WHATEVER host
    # path_arg named, not only the configured git-host. The git-host base is
    # resolved unconditionally now (previously only inside the relative-path
    # branch) so an absolute-URL PATH can be checked against it via
    # _absolute_url_host_matches_git_host_base BEFORE git_host_base is emptied.
    # A mismatch fails closed (EXIT_ABSOLUTE_URL_HOST_MISMATCH) rather than
    # either (a) silently attaching the git-host token to a foreign host, or
    # (b) falling through to the double-prepend the base-cased fix above
    # exists to prevent -- refuse-with-a-clear-error is chosen over silent
    # host-substitution/normalization so a caller-contract violation (an
    # absolute URL pointed somewhere other than the configured git host) is
    # never masked as either a routing success or an opaque transport
    # failure.
    path_arg_is_absolute_url = urllib.parse.urlsplit(path_arg).scheme in ("http", "https")
    resolved_git_host_base = _resolve_git_host_base(args.git_host_base_url)
    if target_platform == PLATFORM_GITHUB:
        git_host_base = ""
    elif path_arg_is_absolute_url:
        if _absolute_url_host_matches_git_host_base(path_arg, resolved_git_host_base):
            git_host_base = ""
        else:
            offending_host = urllib.parse.urlsplit(path_arg).netloc
            _fail(
                f"absolute URL PATH {path_arg!r} targets host {offending_host!r}, "
                f"which does not match the resolved git-host base "
                f"{resolved_git_host_base!r}. Refusing to attach the git-host "
                f"token to an unverified host, and refusing to prepend the "
                f"git-host base in front of an already-absolute URL "
                f"(malformed double-authority). Pass an absolute URL on the "
                f"configured git host, or a relative path.",
                code=EXIT_ABSOLUTE_URL_HOST_MISMATCH,
            )
    else:
        git_host_base = resolved_git_host_base

    # --delete-own-comment (lr-e2ce66): dispatches to the dedicated
    # belt-and-suspenders helper and returns immediately -- this is a
    # DIFFERENT shape from every other write method below (no request body,
    # no --verify-comment readback-after-POST; the verification here is a
    # GET-before-DELETE, already fully encapsulated in delete_own_comment()).
    # Short-circuiting here keeps the ordinary comments-POST body-building/
    # verify-comment machinery below completely unaware this flag exists.
    if args.delete_own_comment:
        owner, repo, comment_id = (
            delete_comment_match.group(1),
            delete_comment_match.group(2),
            delete_comment_match.group(3),
        )
        delete_own_comment(git_host_base, token, owner, repo, comment_id, opener=opener)
        print(json.dumps({"deleted_comment_id": int(comment_id)}))
        return EXIT_OK

    comments_match = _ISSUE_COMMENTS_RE.match(path_arg) if args.verify_comment else None
    is_comment_post = method == "POST" and comments_match is not None

    if args.pr_sha and is_comment_post:
        owner, repo, pr_number = comments_match.group(1), comments_match.group(2), comments_match.group(3)
        check_pr_sha(
            git_host_base, token, owner, repo, pr_number, args.pr_sha,
            known_bad_owners=known_bad_owners, opener=opener,
        )

    # --expect-verdict-block: build the combined (prose + fence) body IN
    # PROCESS, after check_pr_sha has already confirmed args.pr_sha matches
    # the PR's live head -- the fence's head_sha is exactly the SHA the
    # caller just had confirmed current, never re-typed. pr_number comes
    # from the PATH match, not from stdin, so the fenced pr_number can never
    # disagree with the PR this is actually posted to. stdin_bytes is
    # already sourced from whichever body-ingestion flag was supplied
    # (--body-stdin or --body-env) -- this line does not care which.
    post_body_bytes = stdin_bytes if method in _WRITE_METHODS else None
    if args.expect_verdict_block is not None:
        assert stdin_bytes is not None and is_comment_post  # enforced by the preflight checks above
        pr_number_int = int(comments_match.group(3))
        combined_body = build_expected_verdict_body(
            stdin_bytes,
            reviewer=args.expect_verdict_block,
            pr_number=pr_number_int,
            expected_head_sha=args.pr_sha,
        )
        post_body_bytes = json.dumps({"body": combined_body}).encode("utf-8")

    # --caller-tracking-id (lr-10a996): compose the final body IN PROCESS,
    # after any --expect-verdict-block fence has already been appended above
    # -- the tracking-id note is appended LAST so it always trails the
    # verdict fence when both are requested, and note_compose never has to
    # know about the verdict-fence shape. Operates on whatever
    # post_body_bytes already holds (the plain --body-stdin body, or the
    # verdict-combined body) so the two composition steps stack cleanly
    # rather than requiring a caller to choose one or the other.
    if args.caller_tracking_id is not None:
        assert post_body_bytes is not None and is_comment_post  # enforced by the preflight checks above
        current_body = json.loads(post_body_bytes.decode("utf-8")).get("body", "")
        composed_body = build_composed_body(
            current_body, caller_tracking_id=args.caller_tracking_id
        )
        post_body_bytes = json.dumps({"body": composed_body}).encode("utf-8")

    captured_body = ""
    if is_comment_post and post_body_bytes is not None:
        try:
            stdin_json = json.loads(post_body_bytes.decode("utf-8"))
            captured_body = stdin_json.get("body", "")
        except (json.JSONDecodeError, UnicodeDecodeError):
            captured_body = post_body_bytes.decode("utf-8", errors="replace")

    pre_post_utc = datetime.now(timezone.utc) if is_comment_post and args.verify_comment else None

    status, raw = request(
        git_host_base, method, path_arg, token,
        body_bytes=post_body_bytes,
        opener=opener,
    )

    if method not in _WRITE_METHODS or method == "GET":
        # Read-only calls stream the response straight to stdout, exactly
        # like the reference transport's curl passthrough, so existing
        # callers parsing diffs/files continue to work unchanged.
        sys.stdout.buffer.write(raw)
    else:
        print(
            f"git-host-api: {method} {path_arg} -> HTTP {status}",
            file=sys.stderr,
        )

    if is_comment_post and args.verify_comment:
        owner, repo, pr_number = comments_match.group(1), comments_match.group(2), comments_match.group(3)
        bot_login = resolve_bot_login(git_host_base, token, opener=opener)
        assert pre_post_utc is not None
        verified = verify_comment_on_pr(
            git_host_base, token, owner, repo, pr_number, captured_body, bot_login,
            not_before=pre_post_utc, opener=opener,
        )

        result = {
            "verified_comment_id": verified["id"],
            "verified_comment_url": verified["html_url"],
            "verified_by_login": verified["login"],
            "pr_number": int(pr_number),
        }

        if args.expect_verdict_block is not None:
            # Mirror step: re-parse the fence from the VERIFIED comment's own
            # body (the readback, not the locally-constructed combined_body
            # string) via the SAME merge.verdict.parse_verdict_block the
            # merge gate itself uses -- confirms the fence this tool
            # constructed and posted landed byte-identical, never trusting
            # the pre-POST string alone.
            verify_verdict_block(
                verified["body"],
                reviewer=args.expect_verdict_block,
                expected_review_status=json.loads(stdin_bytes.decode("utf-8"))["review_status"],
                expected_head_sha=args.pr_sha,
                expected_pr_number=int(pr_number),
            )
            result["verdict_block_verified"] = True

        print(json.dumps(result))

    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
