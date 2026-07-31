"""transport — authenticated git-host API verbs.

Wave B slice 1 (lr-3ba8, tome #688). Ported from the reference git-host-API
transport; the source copy stays primary until its separate CUT OVER +
RETIRE + VERIFY-GONE task per the migration plan.

    git_host_api         — authenticated Forgejo REST call (GET + write
                           methods), post-and-verify comment gate,
                           --body-stdin as the sole body path,
                           parse_json_body() (the shared tolerant raw-bytes
                           -> dict parse every Forgejo backend's write-path
                           callers need). Renamed from forge_api.py
                           (lr-9ade, folded into lr-39f8; internal
                           identifiers completed lr-9fdbed).
    github_client        — the shared, redirect-hardened GitHub REST request
                           primitive (post-Wave-B extraction, lr-e1f9): the
                           transport shaping review/push/merge's
                           github_backend.py modules build on. See its
                           docstring for exactly what stayed local per-verb.
    credential_provider — the credential-resolution seam: resolve_token(),
                           with a static/config provider (standalone), a
                           command/exec provider (CommandTokenProvider,
                           lr-af6e), and a pluggable reference-provider
                           protocol (e.g. a gatekeeper-style credential
                           minting service).
    provider_config      — per-platform provider SELECTION (lr-af6e):
                           resolve_platform_provider(platform) reads env/
                           config to decide which TokenProvider a given
                           platform (forgejo/github) uses. The one place
                           every verb's token resolution goes through.
    redirect_guard       — the one no-redirect urllib opener every
                           bearer-token-carrying call in this transport
                           layer (Forgejo AND GitHub) builds through
                           (lr-412f security review finding).
"""

from __future__ import annotations

from clagentic_loadout.transport.credential_provider import (
    CommandTokenProvider,
    CredentialProviderError,
    StaticTokenProvider,
    TokenProvider,
)
from clagentic_loadout.transport.git_host_api import (
    EXIT_BODY_STDIN_EMPTY,
    EXIT_CURL_FAILED,
    EXIT_OK,
    EXIT_OWNER_REPO_NOT_FOUND,
    EXIT_STALE_PR,
    EXIT_TOKEN_FETCH_FAILED,
    EXIT_VERIFY_COMMENT_REQUIRED,
    EXIT_VERIFY_FAILED,
    GitHostApiError,
    build_request,
    main,
    parse_json_body,
    request,
)
from clagentic_loadout.transport.github_client import (
    GITHUB_API_BASE,
    GITHUB_API_VERSION,
    request_json,
)
from clagentic_loadout.transport.provider_config import resolve_platform_provider
from clagentic_loadout.transport.redirect_guard import (
    NoRedirectHandler,
    no_redirect_opener,
)

__all__ = [
    "EXIT_BODY_STDIN_EMPTY",
    "EXIT_CURL_FAILED",
    "EXIT_OK",
    "EXIT_OWNER_REPO_NOT_FOUND",
    "EXIT_STALE_PR",
    "EXIT_TOKEN_FETCH_FAILED",
    "EXIT_VERIFY_COMMENT_REQUIRED",
    "EXIT_VERIFY_FAILED",
    "GITHUB_API_BASE",
    "GITHUB_API_VERSION",
    "CommandTokenProvider",
    "CredentialProviderError",
    "GitHostApiError",
    "NoRedirectHandler",
    "StaticTokenProvider",
    "TokenProvider",
    "build_request",
    "main",
    "no_redirect_opener",
    "parse_json_body",
    "request",
    "request_json",
    "resolve_platform_provider",
]
