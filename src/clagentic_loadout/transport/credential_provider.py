"""credential_provider.py — git-host-token resolution seam.

Wave B slice 1 (lr-3ba8, tome #688). The reference git-host-API transport
self-fetches the caller's token from a specific internal secret broker at a
hardcoded per-caller path and NEVER trusts an inherited token from the
process environment. That "never trust inherited, always resolve fresh"
SECURITY POSTURE is preserved here — but the *mechanism* (which broker, what
path) is not loadout's to bake in. This module defines the seam: a
``TokenProvider`` protocol with one method, ``resolve_token(role, *,
repo=None)`` (the optional ``repo`` context is lr-ea28 — see below), plus two
concrete implementations that need no external minting service imported into
this package:

  - ``StaticTokenProvider`` — reads a role-scoped ``.env`` file on disk. The
    standalone fallback, unchanged default behavior.
  - ``CommandTokenProvider`` (lr-af6e) — the git-credential-helper pattern: a
    deployment-configured argv is exec'd (never a shell), the token is read
    from its stdout, and any minting process a deployment already runs (an
    internal secret-broker self-fetch, a GitHub-App-style installation-token
    mint, or anything else that can print a token to stdout) becomes a
    TokenProvider with zero product code — see that class's docstring for
    the full contract.

A gatekeeper-style credential-minting service is the REFERENCE provider for
the command-exec shape above — it is named here only as a docstring example
of *a* command a deployment might configure; it is never imported. loadout
ships the protocol and these two concrete providers, not a hardcoded client
for any one broker.

THE `role` PARAMETER IS AN ALREADY-ATTESTED VALUE, NEVER A FREE ARG (tome
#700 correction 3, lr-e5eeab): every verb's `--caller`/`--role` flag is
consumed HERE, at this seam, as an OPAQUE CONFIG KEY — a string used to
select which role-scoped credential/App-slug/authority entry applies — this
seam itself never re-authenticates it, and NEITHER does
`transport.github_app_config.resolve_github_app_slug` or
`merge.authority.check_authority`, the two other seams downstream of it.
This module remains orchestration-agnostic (this repo's CLAUDE.md rule 2):
it has no spawn-side visibility into how a harness decided which role a
given process is allowed to act as, and it does not itself acquire that
visibility.

THIS IS NOT THE WHOLE STORY ANY MORE (lr-c75c9a correction — the doctrine
above predates this fix and, read alone, is stale): the "some layer outside
loadout attests --caller/--role before it reaches a verb's argv" story used
to be an EXTERNAL, unenforced assumption for every verb except
`transport.git_host_api` — the one call site lr-82c385 originally wired a
real, in-package check into. lr-c75c9a closes that gap: EVERY mutating verb
(`push`, `merge`, `merge close-pr`, `merge post-merge`, `review`, `acquire`,
`git-host-api`) now calls `transport.caller_binding.bind_caller` — which
compares an EXPLICIT `--caller`/`--role` against
`transport.attestation.resolve_identity`'s result — BEFORE ever reaching
this seam. That binding does NOT violate the "MUST NOT reach into a
harness-specific identity sidecar" posture above: `transport.attestation`'s
sidecar adapter is a config-driven seam (`dir`/`file_prefix`/
`session_id_env` are all deployment-supplied, see that module's own
docstring) with no hardcoded harness shape baked in, which is exactly what
CLAUDE.md rule 2 requires — a capability agents act through (an attestation
mechanism) is in scope for this package by default; the rule constrains WHO
owns orchestration, not whether loadout may resolve identity via its own
configurable seam. By the time `role`/`--caller` reaches THIS module's
`resolve_token`, it has therefore already been checked, when explicit,
against this process's own attested identity — this seam still treats it as
opaque config (that has not changed and will not), but it is no longer true
to say the check happens only outside this package. See
`transport.caller_binding`'s own module docstring for the full binding
contract, including the "omitted --caller/--role is never checked"
carve-out this seam has always relied on and which is unchanged.

The actual identity-vs-role entitlement check (which attested identity may
act as which role) remains a MINTING-TIME concern, layered in front of this
seam by whatever `TokenProvider`/`AuthorityProvider` a deployment wires in —
see `clagentic_loadout.merge.authority`'s module docstring for the parallel
statement on the authority side, and this package's reference deployment (a
gatekeeper-style minting service) for where that entitlement check actually
lives. That is a DIFFERENT question ("is this claimed role entitled to X")
than `bind_caller`'s ("is this process who it claims to be") — the two are
independent and `bind_caller` runs first. Passing an unvalidated,
caller-chosen role string straight through to `CommandTokenProvider`'s argv
is still guarded structurally at THIS seam (see `_SAFE_ROLE_RE` below)
against argv-level option injection — that is a shell/argv-safety property,
not an identity-authentication one, and the two must not be conflated.

Optional repo context (lr-ea28): a repo-scoped minting provider (e.g. a
GitHub App installation-token mint, which is scoped to one owner/repo and
cannot be minted correctly without knowing which) needs more than the role
to resolve a token. ``resolve_token`` gains a KEYWORD-ONLY ``repo`` parameter,
defaulting to ``None``, on both the ``TokenProvider`` protocol method and the
module-level ``resolve_token`` function.

Trade-off named (protocol-signature change vs. a separate context object):
a keyword-only parameter with a default is chosen over introducing a new
"call context" object. A context object would need its own versioning
story the moment a second piece of context shows up, and would force every
existing custom ``TokenProvider`` implementation — including ones outside
this package that this seam cannot see — to accept it or break.

Compatibility mechanics (the actual property under test): the module-level
``resolve_token()`` function inspects the resolved provider's
``resolve_token`` SIGNATURE (`inspect.signature`, a one-time, deterministic
check — see `_provider_accepts_repo_kwarg`) to decide whether it can accept
a `repo` keyword at all, and forwards `repo=` ONLY when it can. Every
existing custom ``TokenProvider`` implementation with the old
`resolve_token(self, role)` signature — including every verb call site's
pre-lr-ea28 test fixtures, and any third-party provider this seam cannot
see — is therefore called EXACTLY as before this feature existed:
`provider.resolve_token(role)`, zero keyword arguments added, REGARDLESS of
whether the calling verb happens to have repo context to offer. A verb that
now resolves `owner/repo` before calling `resolve_token` (push, review,
merge — see their own modules) does not need a signature-aware branch of
its own: it always passes `repo=f"{owner}/{repo}"`, and this function is
the single place that decides whether the resolved provider can use it.

This was chosen over a signature-blind unconditional `resolve_token(role,
repo=repo)` (which raises `TypeError` against ANY legacy provider the
moment a caller has repo context — breaking exactly the "existing custom
providers implementing the old signature must not break" requirement) and
over a fragile try/except-TypeError-and-retry-without-repo (which cannot
distinguish "this provider's signature does not accept repo" from "this
provider's resolve_token body raised an unrelated TypeError while
executing" — the retry would silently swallow the latter and report success
without repo scoping instead of surfacing the real bug). `StaticTokenProvider`
accepts and ignores `repo` (it never needed repo-scoping) precisely because
it ships in this package and was updated in lockstep; `CommandTokenProvider`
is the first provider that acts on it (`{repo}` template placeholder,
fail-closed when the configured command needs it but the call site supplied
none — see that class's docstring).

No hardcoded broker host, secret-storage path, or organization name lives
here. Config/env names follow CLAGENTIC_LOADOUT_* (CLI-NAMING-STANDARD.md).
Reuses clagentic_loadout.release.secrets_config.read_role_env_file for the
standalone provider's on-disk format — one role-scoped .env reader for the
whole package, not a second copy of the mode-600 / path-traversal logic.

PROVIDER-SUPPLIED VERIFIED IDENTITY (lr-43c8d7): a minting provider MAY know
something about the resolved credential beyond the bare token string
itself — the reference motivating case is a GitHub App installation-token
mint that verifies the App's own slug against the broker at mint time
(fail-closed on mismatch) and can report that BROKER-VERIFIED slug back to
the caller. That value is strictly more trustworthy than an operator-typed
config entry (`github_app.slugs.<caller>`, see transport.github_app_config)
naming the SAME fact, because the provider's copy was checked against
reality at credential-mint time and the config copy was never checked
against anything.

THE MECHANISM, decided explicitly (see this task's own PR body for the
trade-offs against the two rejected alternatives): a provider's
`resolve_token` method may return EITHER a bare `str` (every existing
provider, unchanged) OR a `ResolvedToken` instance carrying `token` plus an
OPTIONAL `app_slug`. `resolve_token_result()` (module-level, mirrors
`resolve_token()`'s own shape) normalizes either return value into a
`ResolvedToken` — a provider that returns a bare string yields
`ResolvedToken(token=that_string, app_slug=None)`, so a caller reading only
`.token` sees ZERO behavior change regardless of which shape the configured
provider returns. This is a return-type union on the EXISTING seam, not a
new provider protocol method and not stdout content-sniffing: nothing here
inspects a string's contents to guess whether it "looks like" JSON — see
`CommandTokenProvider`'s own docstring for how structured stdout is
produced, which is an explicit per-instance opt-in flag, never inferred
from what a command happens to print.

`resolve_token()` (the pre-existing, still-supported function) is
UNCHANGED in return shape (`str`) and behavior — it now resolves via
`resolve_token_result()` internally and returns just `.token`, so every
existing call site (git_host_api, review.verb, merge.verb, and push.verb's
own bare-token uses) keeps working with no code change at all. A caller
that wants the provider-supplied `app_slug` (push.verb's new identity tier,
lr-43c8d7) calls `resolve_token_result()` instead.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from clagentic_loadout.release.secrets_config import (
    DEFAULT_ROLE,
    SecretEnvError,
    read_role_env_file,
)

#: Hard ceiling on a command provider's stdout size, in bytes. A git-host PAT
#: or an installation token is always small; anything beyond this is treated
#: as a misconfigured/misbehaving command (e.g. printing a help page or a log
#: dump to stdout instead of a token) and rejected fail-closed rather than
#: accepted and truncated silently.
COMMAND_PROVIDER_MAX_OUTPUT_BYTES = 8192

#: The .env key a standalone role-scoped secret file must carry the git-host
#: token under. Distinct from release.dispatch's STATUS_HOOK_SECRET key —
#: same file FORMAT, different file and different key, by design (a role's
#: git-host token and its status-hook HMAC secret are different credentials
#: with different blast radii).
GIT_HOST_TOKEN_ENV_KEY = "CLAGENTIC_LOADOUT_GIT_HOST_TOKEN"

#: JSON key names CommandTokenProvider reads when `emit_structured_output`
#: is enabled (lr-43c8d7) — mirrors the reference minting provider's own
#: `--json` output shape ({token, expires_at, app_slug}), consuming only
#: the two keys this seam has a use for. `expires_at` is intentionally not
#: read here: this seam has no expiry-tracking concept on either shape
#: (bare-token or structured) and adding one is out of this task's scope.
STRUCTURED_OUTPUT_TOKEN_KEY = "token"
STRUCTURED_OUTPUT_APP_SLUG_KEY = "app_slug"


@dataclass(frozen=True)
class ResolvedToken:
    """A resolved git-host token, plus whatever the provider itself could
    verify about the identity it was minted for (lr-43c8d7).

    `app_slug` is OPTIONAL and is the PROVIDER-VERIFIED value when present —
    e.g. a GitHub App installation-token mint that checked the App's slug
    against its own broker at mint time, not an operator-typed config
    string. `None` means the provider did not supply one at all (a
    bare-token provider, or a structured provider whose role has no
    App-slug binding configured for this mint — both real, reachable
    states, not error conditions): a caller falls back to its own
    config-derived identity source exactly as it did before this type
    existed. See this module's own docstring, "PROVIDER-SUPPLIED VERIFIED
    IDENTITY", for the full precedence this feeds into push.verb's
    `_resolve_effective_bot_identity`.
    """

    token: str
    app_slug: str | None = None


class CredentialProviderError(ValueError):
    """Raised on any token-resolution failure — missing file, insecure
    permissions, empty/missing token, or a provider-specific minting
    failure. Callers translate this to their own exit-code convention; this
    module never calls sys.exit so it stays exit-code-agnostic and testable
    in isolation."""


@runtime_checkable
class TokenProvider(Protocol):
    """The credential-resolution seam every git_host_api caller resolves a
    token through.

    A single method, ``resolve_token(role, *, repo=None)``, returning a
    git-host API token as a plain string. Implementations own their own
    minting/fetch mechanism entirely — loadout's transport code depends only
    on this signature, never on how a given deployment obtains the token.
    Raise CredentialProviderError (or a subclass) on any failure;
    git_host_api treats that uniformly as EXIT_TOKEN_FETCH_FAILED regardless
    of which provider is configured.

    ``repo`` (lr-ea28) is OPTIONAL, keyword-only, and defaults to None — a
    repo-scoped minting provider (e.g. a GitHub App installation-token mint)
    uses it to mint a token scoped to the right owner/repo; a provider with
    no notion of repo scoping (StaticTokenProvider) ignores it entirely. See
    this module's docstring for the backward-compatibility trade-off this
    signature shape was chosen for.

    RETURN TYPE (lr-43c8d7): `resolve_token` returns either a bare `str`
    (every pre-existing provider, and any provider with nothing further to
    report) or a `ResolvedToken` instance (a provider that can ALSO report a
    provider-verified `app_slug` alongside the token). `resolve_token_result()`
    (module-level) normalizes either shape; `resolve_token()` (module-level,
    pre-existing) unwraps to just the token string either way, so an existing
    provider implementation needs no change at all.
    """

    def resolve_token(self, role: str, *, repo: str | None = None) -> str | ResolvedToken:
        """Return a git-host API token scoped to *role* (and, for a
        repo-scoped provider, *repo* — an "owner/repo" string), either as a
        bare string or as a ResolvedToken carrying additional
        provider-verified identity (lr-43c8d7 — see this class's own
        docstring). Never returns an empty token — raise
        CredentialProviderError instead."""
        ...


class StaticTokenProvider:
    """Standalone reference TokenProvider: reads a role-scoped .env file on
    disk (same format/security properties as
    clagentic_loadout.release.secrets_config.read_role_env_file — mode-600
    enforcement, path-traversal-safe role token, never logs/echoes values).

    This is the "no external minting service configured" fallback — a
    deployment with no gatekeeper-equivalent broker can still operate by
    dropping a git-host token into
    ``~/.config/clagentic/loadout/roles/<role>.env`` under
    GIT_HOST_TOKEN_ENV_KEY. A deployment that DOES have a minting service wires
    its own TokenProvider implementation instead; this class is not the only
    way to satisfy the protocol.
    """

    def __init__(self, *, config_root=None) -> None:
        self._config_root = config_root

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        # repo is accepted (per the TokenProvider protocol, lr-ea28) and
        # deliberately ignored -- this provider's on-disk role-scoped .env
        # file has no notion of repo scoping.
        try:
            kvs = read_role_env_file(
                role, (GIT_HOST_TOKEN_ENV_KEY,), config_root=self._config_root
            )
        except SecretEnvError as exc:
            raise CredentialProviderError(
                f"static token provider REFUSED — {exc}"
            ) from exc
        return kvs[GIT_HOST_TOKEN_ENV_KEY]


#: Placeholder token in a configured command's argv template, substituted
#: with *role* when present. When absent, *role* is appended as the command's
#: final argument instead (see CommandTokenProvider's docstring for the
#: trade-off between these two shapes).
COMMAND_ROLE_PLACEHOLDER = "{role}"

#: Placeholder token in a configured command's argv template, substituted
#: with *repo* ("owner/repo") when present (lr-ea28). Unlike
#: COMMAND_ROLE_PLACEHOLDER, there is no append-as-final-arg fallback for
#: repo: a command with no {repo} placeholder simply never receives repo
#: context (byte-identical behavior to before this task). A command that
#: DOES contain {repo} but is called with no repo context is a fail-closed
#: misconfiguration, not a silent no-op — see CommandTokenProvider.resolve_token.
COMMAND_REPO_PLACEHOLDER = "{repo}"

#: Bare role/name token, mirroring git_host_api._SAFE_CALLER_RE and
#: secrets_config._SAFE_NAME_RE (lr-ea28 security finding: PR #26 review
#: comment 12757). CommandTokenProvider substitutes *role* into an
#: already-split argv element (never a shell string), so shell metacharacter
#: injection is structurally impossible -- but a LEADING '-' is not a shell
#: metacharacter, it is a normal, valid argv byte that the deployment's own
#: minting-command CLI (getopt/argparse-style) parses as the START OF A FLAG.
#: A role value the deployment never validated (e.g. one derived from an
#: attacker-influenced --caller/--role CLI argument on a verb that does not
#: itself pre-validate it, unlike git_host_api's _SAFE_CALLER_RE) could inject an
#: unintended option into the exec'd command's own argument parsing --
#: argv-level option injection, distinct from and not mitigated by
#: shell=False. Enforced HERE, at the seam, rather than trusting every verb
#: call site to have already validated it (defense at the provider, not just
#: the callers) -- this is the SAME bare-token grammar
#: secrets_config._SAFE_NAME_RE already enforces for StaticTokenProvider's
#: role, so CommandTokenProvider and StaticTokenProvider now share one
#: security posture for the value that names "which role".
#:
#: Anchored with \A...\Z, not ^...$ (lr-3e3318): in Python, without
#: re.MULTILINE, '$' matches at end-of-string OR just before a trailing
#: newline, so 'role\n' would otherwise pass this validator -- inert today
#: (this value only ever reaches an already-split, shell=False argv element,
#: where a trailing newline byte cannot split arguments or introduce a
#: command), but a validator whose whole job is to constrain a value before
#: it reaches an exec'd argv should assert exactly what it appears to
#: assert. \A and \Z anchor strictly to the start/end of the string with no
#: trailing-newline tolerance.
_SAFE_ROLE_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")

#: owner/repo grammar for the {repo} substitution (lr-ea28 security finding,
#: PR #26 review comment 12757; narrowed lr-fdc6ec). Each segment is
#: [A-Za-z0-9._-]+ EXCEPT for two unconditional exclusions, each protecting
#: a DIFFERENT, real property:
#:   - a segment may never START WITH '-' -- the actual argv-injection
#:     vector. getopt/argparse-style parsers treat a leading '-' as
#:     introducing an option; this is enforced regardless of what follows
#:     the hyphen.
#:   - a segment may never BE (as a WHOLE segment) '.' or '..' -- the actual
#:     path-traversal vector. '.' and '..' have no legitimate owner/repo
#:     meaning and this seam has no reason to accept them.
#: A leading '.' is otherwise ALLOWED (lr-fdc6ec): GitHub mandates a repo
#: literally named '.github' for the org-profile README
#: (github.com/<owner>/.github), so rejecting every leading '.' rejected a
#: legitimate, unavoidable repo name. This is safe because a leading '.' is
#: not, and has never been, the injection vector this regex defends
#: against -- no getopt/argparse-style parser treats a leading '.' as
#: introducing a flag, so admitting it does not reopen the argv-injection
#: property. It also does not reopen the traversal vector: '.github' is not
#: '.' or '..' as a whole segment, and the exclusion below is anchored to
#: the FULL segment (not just its start), so '..' embedded via a leading
#: '..' followed by more characters (e.g. '..github', which is not '..' as
#: a whole segment and was never a traversal token in a single owner/repo
#: segment anyway) still passes on the same principle -- this seam does
#: owner/repo=SLASH-separated substitution, not filesystem path resolution,
#: so a single segment is never re-joined with '..' to walk anywhere.
#:
#: lr-fdc6ec verification note: an earlier draft of this pattern used an
#: UNANCHORED '$' in the negative lookahead (`(?!\.\.?$)`), which let
#: 'REJECT' cases like '../x' slip through -- the lookahead only checked
#: the segment ending at THAT position in the SOURCE STRING (which
#: includes the '/' and the next segment), not the end of the currently
#: matched segment, so `..` followed by `/x` did not match `\.\.?$` (there
#: is more string after it) and the exclusion silently failed to fire. The
#: lookahead is anchored per-segment here by checking for '.'/'..' followed
#: by EITHER '/' or end-of-string (`(?:/|$)`), which correctly excludes a
#: whole '.' or '..' segment regardless of which segment position (owner or
#: repo) it occupies. Proven against the full accept/reject matrix in
#: tests/test_transport_command_token_provider.py.
#:
#: Two segments, one '/' separator -- an owner/repo string with zero or
#: multiple '/', or an empty segment, does not match and is rejected the
#: same as any other malformed value.
#:
#: lr-3e3318: the per-segment lookahead's own internal '$' (excluding a
#: whole '.'/'..' segment immediately before a '/' or end-of-string) is
#: UNCHANGED and still correct -- it is anchored per-segment via the
#: `(?:/|$)` alternation, not to the end of the overall match, so it is not
#: the anchor this fix addresses. The OUTER anchor is: this pattern
#: previously used '^' ... '$' for the whole-string match, which (without
#: re.MULTILINE) also matches just before a trailing newline in Python --
#: so 'owner/repo\n' passed validation. Anchored with \A...\Z instead (see
#: _SAFE_ROLE_RE's docstring comment for the identical rationale): strictly
#: whole-string, no trailing-newline tolerance.
_SAFE_REPO_SEGMENT = r"(?!-)(?!\.\.?(?:/|$))[A-Za-z0-9._-]+"
_SAFE_REPO_RE = re.compile(
    rf"\A{_SAFE_REPO_SEGMENT}/{_SAFE_REPO_SEGMENT}\Z"
)


class CommandTokenProvider:
    """TokenProvider that execs a deployment-configured argv and reads a
    git-host token from its stdout — the git-credential-helper pattern.

    This is the seam a deployment wires its own credential-minting process
    into (an internal secret-broker self-fetch, a GitHub-App-style
    installation-token mint, or anything else that can print a token to
    stdout) without loadout importing or hardcoding that process anywhere.
    [clagentic: gatekeeper](https://github.com/clagentic/clagentic-gatekeeper)
    is the reference implementation of such a minting command — named here
    only as a docstring example; this module never imports it.

    Role substitution (trade-off, named per lr-af6e): the configured argv is
    a fixed list of strings. If any element equals COMMAND_ROLE_PLACEHOLDER
    ("{role}"), that element is replaced with *role* at call time (template
    substitution) — this lets a command receive the role as a flag value
    (e.g. ["mint-token", "--role", "{role}"]). If no element matches, *role*
    is appended as the command's final argument instead (simpler, no
    placeholder needed for the common "argv + one trailing positional"
    shape). Only one placeholder occurrence is substituted; a command
    needing the role in more than one position, or in a different shape
    entirely, should use the append form and parse its own final argv
    element. This is simpler than full shell-style templating and covers
    every command shape this seam needs today.

    Repo-context substitution (lr-ea28): a repo-scoped minting provider (e.g.
    a GitHub App installation-token mint: `mint --role <role> --repo
    <owner>/<repo>`) needs the target owner/repo in its argv, not just the
    role. If any configured argv element equals COMMAND_REPO_PLACEHOLDER
    ("{repo}"), that element is replaced with the call's *repo* argument
    ("owner/repo") at call time — e.g. ["mint", "--role", "{role}", "--repo",
    "{repo}"]. Unlike the role placeholder, there is NO append-as-final-arg
    fallback for repo: a configured command with no "{repo}" element behaves
    BYTE-IDENTICALLY to before this feature existed, regardless of whether a
    caller happens to pass a repo argument — repo context only ever reaches
    the child process through an explicit "{repo}" placeholder the
    deployment opted into. Conversely, a command that DOES contain "{repo}"
    but is invoked with no repo context (the call site passed none) is a
    FAIL-CLOSED misconfiguration: resolve_token raises
    CredentialProviderError naming the configured command shape and role
    (never inventing or substituting a placeholder value) rather than
    exec'ing the command with a literal unsubstituted "{repo}" string, which
    would silently mint a token against the wrong scope (or a minting
    command's own confusing "no such repo" error two layers removed from the
    actual cause).

    Security contract:
      - shell=False always — argv is passed as a list, never interpolated
        into a shell string. No shell metacharacter in a role or a
        configured argv element can escape into command injection.
      - The token is read only from stdout; a trailing newline is stripped
        (the conventional shape for a credential-helper-style command).
        stdout is capped at COMMAND_PROVIDER_MAX_OUTPUT_BYTES — a command
        that emits more than that is treated as misconfigured/misbehaving,
        not as "the token plus some noise to strip."
      - Fail-closed on: nonzero exit, empty stdout (after strip), or stdout
        exceeding the size cap. Every failure raises CredentialProviderError
        with the CONFIGURED COMMAND SHAPE and role (never the token) —
        stderr, if any, is included as a diagnostic (a minting command's own
        stderr is expected to be non-secret operational text; a deployment
        whose command shape logs a token to stderr is instructed to fix that
        contract, not something this provider can inspect for).
      - The token never appears in this process's own argv beyond what the
        deployment configured (the configured command line + the resolved
        role/placeholder substitution), never in a log line this provider
        emits, and is never written to the child process's environment
        beyond what it already inherits from this process (no env
        manipulation here at all).

    STRUCTURED OUTPUT, OPT-IN ONLY (lr-43c8d7): `emit_structured_output`
    defaults to `False` — stdout is read as a bare token string, byte-
    identical to every behavior documented above, REGARDLESS of what the
    configured command actually prints. This is the non-negotiable
    zero-behavior-change guarantee for a deployment whose minting command
    prints a bare token: nothing in this class ever inspects stdout's
    CONTENT to decide how to parse it (no "try JSON, fall back to bare
    string" sniffing — a bare token is not JSON, but guessing at unstated
    caller intent by content-sniffing is exactly the tool shape this
    codebase has repeatedly refused, see this module's own PROVIDER-
    SUPPLIED VERIFIED IDENTITY section). The DEPLOYMENT decides which shape
    its own configured command emits and sets this flag to match — never
    inferred from the output itself.

    When `emit_structured_output=True`, stdout is instead parsed as a JSON
    object with a required `"token"` string key and an optional `"app_slug"`
    string key (STRUCTURED_OUTPUT_TOKEN_KEY / STRUCTURED_OUTPUT_APP_SLUG_KEY
    — mirrors the reference minting provider's own `--json` output shape,
    named here only as the shape this seam consumes, never imported).
    `resolve_token` then returns a `ResolvedToken(token=..., app_slug=...)`
    instead of a bare string. Fail-closed on: invalid JSON, a non-object top
    level, a missing/non-string/empty `"token"` key, or a present-but-
    non-string `"app_slug"` key — each raises CredentialProviderError naming
    the configured command shape and role, never inventing a value. A
    present `"app_slug"` that is an empty string is normalized to `None`
    (the same "empty means not configured" contract
    `transport.github_app_config` already applies to its own slug values) —
    this is the real, reachable case where a minting provider's role has no
    App-slug binding configured for this particular mint (see this
    class's own PR body / module docstring: gatekeeper's own `--json`
    contract returns an empty `app_slug` for exactly this state, not an
    absent key).
    """

    def __init__(
        self,
        argv: list[str],
        *,
        timeout: float = 15.0,
        emit_structured_output: bool = False,
    ) -> None:
        if not argv:
            raise CredentialProviderError(
                "command token provider REFUSED — configured argv is empty. "
                "A CommandTokenProvider requires at least one argv element "
                "(the command to exec)."
            )
        self._argv = list(argv)
        self._timeout = timeout
        self._emit_structured_output = emit_structured_output

    def _build_argv(self, role: str, *, repo: str | None) -> list[str]:
        # Validate BEFORE any substitution -- an unvalidated role or repo
        # value substituted into an already-split argv element is still
        # exposed to argv-level OPTION INJECTION against the exec'd
        # command's own argument parser: a leading '-' is not a shell
        # metacharacter (shell=False already neutralizes those), but it IS a
        # normal, valid argv byte that a getopt/argparse-style CLI parses as
        # the start of a flag. Enforced HERE, at the seam, rather than
        # trusting every verb call site to have pre-validated its own
        # role/caller value (PR #26 review comment 12757 -- some verbs, e.g.
        # push/review/merge's --caller/--role, do not pre-validate the way
        # git_host_api's _SAFE_CALLER_RE does; this is defense at the provider,
        # not just the callers).
        if not _SAFE_ROLE_RE.match(role):
            raise CredentialProviderError(
                f"command token provider REFUSED — role {role!r} contains "
                f"invalid characters (only alphanumeric, hyphen, underscore; "
                f"no leading hyphen). A role/caller value substituted into a "
                f"configured command's argv must be a bare token: an "
                f"unvalidated value (e.g. one starting with '-') could be "
                f"parsed as a FLAG by the exec'd command's own argument "
                f"parser, not the shell (shell=False already prevents shell "
                f"metacharacter injection separately)."
            )

        # {repo} substitution happens first and ALWAYS on the already-split
        # argv tokens -- never on a shell string. A configured command with
        # no {repo} placeholder is untouched by this step (byte-identical to
        # before this feature existed), regardless of whether *repo* was
        # supplied.
        if COMMAND_REPO_PLACEHOLDER in self._argv:
            if repo is None:
                raise CredentialProviderError(
                    f"command token provider REFUSED — configured command "
                    f"{self._argv!r} contains the {COMMAND_REPO_PLACEHOLDER!r} "
                    f"placeholder for role {role!r}, but no repo context was "
                    f"supplied by the call site. A repo-scoped minting "
                    f"command cannot resolve a correctly-scoped token "
                    f"without an owner/repo — pass repo= to resolve_token(), "
                    f"or remove {COMMAND_REPO_PLACEHOLDER!r} from the "
                    f"configured command if repo scoping is not needed."
                )
            if not _SAFE_REPO_RE.match(repo):
                raise CredentialProviderError(
                    f"command token provider REFUSED — repo {repo!r} does "
                    f"not match the required 'owner/repo' grammar (each "
                    f"segment must not start with a hyphen, must not BE '.' "
                    f"or '..' as a whole segment, and may otherwise contain "
                    f"only letters, digits, '.', '_', '-'). A repo value "
                    f"substituted into a configured command's argv must be a "
                    f"safe bare token: an unvalidated leading '-' could be "
                    f"parsed as a FLAG by the exec'd command's own argument "
                    f"parser (argv-level option injection), not the shell "
                    f"(shell=False already prevents shell metacharacter "
                    f"injection separately)."
                )
            argv = [
                repo if part == COMMAND_REPO_PLACEHOLDER else part
                for part in self._argv
            ]
        else:
            argv = list(self._argv)

        if COMMAND_ROLE_PLACEHOLDER in argv:
            return [
                role if part == COMMAND_ROLE_PLACEHOLDER else part
                for part in argv
            ]
        return [*argv, role]

    def resolve_token(self, role: str, *, repo: str | None = None) -> str | ResolvedToken:
        argv = self._build_argv(role, repo=repo)
        # Resolved-values error reporting below names the CONFIGURED COMMAND
        # SHAPE and role for diagnosis -- never the token, which is only
        # ever read from stdout and never echoed back into any exception
        # message or log line this provider emits.
        try:
            proc = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                timeout=self._timeout,
            )
        except FileNotFoundError as exc:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} not found for role {role!r}: {exc}."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} timed out after {self._timeout}s for role "
                f"{role!r}."
            ) from exc
        except OSError as exc:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} failed to start for role {role!r}: {exc}."
            ) from exc

        if proc.returncode != 0:
            stderr_diag = proc.stderr.decode("utf-8", errors="replace").strip()
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} exited {proc.returncode} for role "
                f"{role!r}."
                + (f" stderr: {stderr_diag[:500]!r}" if stderr_diag else "")
            )

        if len(proc.stdout) > COMMAND_PROVIDER_MAX_OUTPUT_BYTES:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} produced {len(proc.stdout)} bytes of "
                f"stdout for role {role!r}, exceeding the "
                f"{COMMAND_PROVIDER_MAX_OUTPUT_BYTES}-byte cap. A token is "
                f"never this large; this is treated as a misconfigured or "
                f"misbehaving command."
            )

        raw_stdout = proc.stdout.decode("utf-8", errors="replace").rstrip("\n").rstrip("\r")
        if not raw_stdout:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} exited 0 but produced empty stdout for "
                f"role {role!r}."
            )

        if not self._emit_structured_output:
            return raw_stdout

        return self._parse_structured_output(raw_stdout, role=role)

    def _parse_structured_output(self, raw_stdout: str, *, role: str) -> ResolvedToken:
        """Parse `emit_structured_output=True` stdout into a ResolvedToken
        (lr-43c8d7). Fail-closed on any shape mismatch -- see this class's
        own docstring, "STRUCTURED OUTPUT, OPT-IN ONLY", for the full
        contract and why this is never attempted unless the deployment
        opted in via the constructor flag."""
        try:
            parsed = json.loads(raw_stdout)
        except json.JSONDecodeError as exc:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} was configured with emit_structured_output=True "
                f"for role {role!r}, but its stdout is not valid JSON: {exc}."
            ) from exc

        if not isinstance(parsed, dict):
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} (emit_structured_output=True) produced a "
                f"JSON {type(parsed).__name__}, not an object, for role "
                f"{role!r}. Expected an object with a "
                f"{STRUCTURED_OUTPUT_TOKEN_KEY!r} key."
            )

        token_value = parsed.get(STRUCTURED_OUTPUT_TOKEN_KEY)
        if not isinstance(token_value, str) or not token_value:
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} (emit_structured_output=True) produced no "
                f"non-empty {STRUCTURED_OUTPUT_TOKEN_KEY!r} string for role "
                f"{role!r}."
            )

        app_slug_value = parsed.get(STRUCTURED_OUTPUT_APP_SLUG_KEY)
        if app_slug_value is not None and not isinstance(app_slug_value, str):
            raise CredentialProviderError(
                f"command token provider REFUSED — configured command "
                f"{self._argv!r} (emit_structured_output=True) produced a "
                f"non-string {STRUCTURED_OUTPUT_APP_SLUG_KEY!r} value for "
                f"role {role!r}: {app_slug_value!r}."
            )
        # Empty string normalized to None (lr-43c8d7): the reference minting
        # provider returns an empty app_slug for a role with no App-slug
        # binding configured -- a real, reachable "not supplied" state, not
        # an error, mirroring github_app_config's own empty-means-unset
        # convention for the same fact.
        resolved_app_slug = app_slug_value if app_slug_value else None

        return ResolvedToken(token=token_value, app_slug=resolved_app_slug)


def _provider_accepts_repo_kwarg(provider: object) -> bool:
    """Return True iff *provider*'s resolve_token method will accept a
    `repo=` keyword argument without raising TypeError.

    Uses `inspect.signature` (a one-time, deterministic signature check) —
    NOT a try/except-and-retry-without-repo, which the task calls out as
    fragile: a retry-on-TypeError could mask an unrelated TypeError raised
    from *inside* a provider's own resolve_token body (e.g. a bug in the
    provider's minting logic that happens to also raise TypeError) as if it
    were this compatibility gap, silently dropping repo context and masking
    the real error. Inspecting the signature up front distinguishes "this
    provider's resolve_token cannot accept repo" from "this provider's
    resolve_token raised TypeError while executing" with certainty, before
    any call is made.

    A provider whose resolve_token accepts `**kwargs` (VAR_KEYWORD) is
    treated as repo-capable — the same rule Python itself uses to decide
    whether a keyword argument binds.
    """
    try:
        sig = inspect.signature(provider.resolve_token)
    except (TypeError, ValueError):
        # No signature could be determined (e.g. a builtin/C-implemented
        # callable) -- fail closed on the side of NOT passing repo, matching
        # this function's pre-lr-ea28 unconditional `resolve_token(role)`
        # call shape.
        return False
    for param in sig.parameters.values():
        if param.name == "repo" or param.kind is inspect.Parameter.VAR_KEYWORD:
            return True
    return False


def resolve_token_result(
    role: str,
    provider: TokenProvider | None = None,
    *,
    repo: str | None = None,
) -> ResolvedToken:
    """Resolve a git-host token for *role* via *provider*, as a
    `ResolvedToken` carrying whatever the provider itself could verify
    about the identity it minted for (lr-43c8d7).

    Defaults to StaticTokenProvider() when no provider is supplied — same
    default as `resolve_token()` below. Normalizes EITHER return shape a
    provider's own `resolve_token` may produce: a bare `str` becomes
    `ResolvedToken(token=that_string, app_slug=None)`; a `ResolvedToken`
    already returned by the provider is passed through unchanged. This is a
    RETURN-TYPE check (`isinstance`), never content-sniffing a string to
    guess its shape — see `credential_provider`'s own module docstring,
    "PROVIDER-SUPPLIED VERIFIED IDENTITY", for why that distinction matters
    here.

    ``repo`` (lr-ea28) forwarding rule is UNCHANGED from `resolve_token()` —
    see that function's own docstring for the full `_provider_accepts_repo_kwarg`
    rationale; this function shares the exact same forwarding logic.

    Raises CredentialProviderError when the provider raises, or when the
    resolved token (either shape) is empty.
    """
    active_provider = provider if provider is not None else StaticTokenProvider()
    raw_result = (
        active_provider.resolve_token(role, repo=repo)
        if _provider_accepts_repo_kwarg(active_provider)
        else active_provider.resolve_token(role)
    )
    result = (
        raw_result
        if isinstance(raw_result, ResolvedToken)
        else ResolvedToken(token=raw_result)
    )
    if not result.token:
        raise CredentialProviderError(
            f"token provider {active_provider!r} returned an empty token for "
            f"role {role!r}."
        )
    return result


def resolve_token(
    role: str,
    provider: TokenProvider | None = None,
    *,
    repo: str | None = None,
) -> str:
    """Resolve a git-host API token for *role* via *provider*.

    Defaults to StaticTokenProvider() when no provider is supplied — a
    deployment integrating a reference credential-minting service (e.g.
    gatekeeper) passes its own TokenProvider implementation here instead.
    Never reads an inherited token from the process environment: an
    ambient/inherited token is not a resolved credential and this function
    has no fallback path to one. Raises CredentialProviderError on failure.

    ``repo`` (lr-ea28) is OPTIONAL, keyword-only. It is forwarded to the
    provider's own ``resolve_token`` only when the provider's signature can
    accept a `repo` keyword (see `_provider_accepts_repo_kwarg` — a
    deterministic `inspect.signature` check, not a fragile try/except
    retry). A pre-existing custom TokenProvider implementation with the old
    `resolve_token(self, role)` signature is therefore called exactly as
    before this feature existed — `provider.resolve_token(role)`, no `repo`
    keyword at all — REGARDLESS of whether the caller supplied a `repo`
    value; a caller with real repo context and a provider that cannot accept
    it simply never receives that context (the provider was never told
    about it, so there is nothing to fail closed over at this layer — the
    provider's own minting logic is what would fail to scope correctly, the
    same as if this feature did not exist for that provider). A provider
    that DOES declare a `repo` parameter (or accepts `**kwargs`) receives it
    verbatim, including when `repo` is None.

    RETURN SHAPE UNCHANGED (lr-43c8d7): this function still returns a bare
    `str`, byte-identical for every existing caller, regardless of whether
    the resolved provider is capable of returning a `ResolvedToken`
    internally — it simply discards `.app_slug`. A caller that wants the
    provider-supplied `app_slug` uses `resolve_token_result()` instead.
    """
    return resolve_token_result(role, provider, repo=repo).token


__all__ = [
    "COMMAND_PROVIDER_MAX_OUTPUT_BYTES",
    "COMMAND_REPO_PLACEHOLDER",
    "COMMAND_ROLE_PLACEHOLDER",
    "DEFAULT_ROLE",
    "GIT_HOST_TOKEN_ENV_KEY",
    "STRUCTURED_OUTPUT_APP_SLUG_KEY",
    "STRUCTURED_OUTPUT_TOKEN_KEY",
    "CommandTokenProvider",
    "CredentialProviderError",
    "ResolvedToken",
    "StaticTokenProvider",
    "TokenProvider",
    "resolve_token",
    "resolve_token_result",
]
