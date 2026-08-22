"""test_role_invoker_mismatch.py — behavioral role/invoker matching tests
(tome #700 T4-test, lr-e5c1bd; test sibling of T4-lo, lr-e5eeab).

lr-e5eeab (merged, PR #81) locked in that every verb's `--caller`/`--role`
flag is consumed as an ALREADY-ATTESTED, opaque config-key value -- proven
there via a STRUCTURAL guard (an AST identifier scan showing no
sidecar/side-channel ingestion, plus a --help-text assertion). That is a
proof about *shape* (no second identity source exists), not about
*behavior* (what actually happens when the attested role does/doesn't match
what a provider allows, or is omitted entirely). This module is the
behavioral complement, proving the three properties tome #700 asks for
directly through each verb's `main()` entrypoint:

  1. An explicit --role/--caller that matches what the injected provider
     treats as "this is the invoker" succeeds -- the resolved role reaches
     the provider verbatim and the provider's own allow decision is
     what determines the outcome, not some default.
  2. An explicit --role/--caller that does NOT match what the provider
     authorizes is DENIED -- fail-closed, the standard StaticRoleAuthority
     Provider/CredentialProviderError shapes already proven at the unit
     level (test_merge_authority.py, credential_provider's own tests), but
     exercised here end-to-end through the CLI dispatch path so this
     module proves the value that reaches the provider is the CLI's own
     --role/--caller, not something else.
  3. An OMITTED --role/--caller falls back to DEFAULT_ROLE (transport.
     credential_provider.DEFAULT_ROLE, "release-dispatcher") -- the
     "defaults to the invoker identity" half of the contract: no value
     supplied means "whatever role this spawn's own default identity is",
     never an empty string, never a hardcoded agent name, and never a
     silent skip of the authority/credential seam.

merge.verb is the richest case (role flows through BOTH merge.authority AND
transport.credential_provider); push.verb and review.verb each cover the
single-seam (credential-provider-only) shape for --caller, proving the same
match/mismatch/default properties hold on every verb that exposes the flag
-- not just the one with the most gates. No lore dependency (CLAUDE.md rule
6a); every provider here is an in-process test double, no network, no real
credential broker.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport.credential_provider import (
    DEFAULT_ROLE,
    CredentialProviderError,
)

_FULL_SHA = "a" * 40


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": "application/json"}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


# ---------------------------------------------------------------------------
# merge.verb -- role flows through BOTH merge.authority.check_authority AND
# transport.credential_provider.resolve_token. Recording providers below
# capture the exact role string each seam was called with, so a test can
# assert the CLI's --role (or its DEFAULT_ROLE fallback) is what actually
# reached both seams -- not merely that main() returned some exit code.
# ---------------------------------------------------------------------------


class _RecordingAuthorityProvider:
    """Authorizes only roles in *allowed*; records every role it was asked
    about, so a test can assert the invoker-attested value actually reached
    this seam (not some other string)."""

    def __init__(self, allowed: frozenset[str]) -> None:
        self._allowed = allowed
        self.checked_roles: list[str] = []

    def authority_allows(self, role: str, owner: str, repo: str, pr_number: int) -> bool:
        self.checked_roles.append(role)
        return role in self._allowed


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123") -> None:
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _MismatchedTokenProvider:
    """Only resolves a token for one specific role -- every other role is a
    CredentialProviderError, exactly like a real role-scoped secret file
    that simply has no entry for an unrecognized role."""

    def __init__(self, only_role: str, token: str = "tok-123") -> None:
        self._only_role = only_role
        self._token = token
        self.resolved_for: list[str] = []

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        if role != self._only_role:
            raise CredentialProviderError(
                f"no credentials configured for role {role!r}"
            )
        return self._token


def _merge_opener():
    """Minimal opener for a merge that reaches the actual merge_pr call --
    no reviewer-verdict fence, no diff-scope, no title issue, empty CI
    (no-runner-by-design pass). Only the namespace/authority/credential
    gates are under test in this file; every later gate is deliberately
    permissive so a role-mismatch/match test's outcome is never masked by
    an unrelated gate."""

    # lr-361de3: merge.verb now performs a FRESH post-merge GET .../pulls/{n}
    # readback (merge.merge_readback.verify_merge_landed) -- see
    # test_merge_verb.py's _make_opener for the identical overlay rationale.
    _merge_landed = [False]

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith("/merge"):
            _merge_landed[0] = True
            return _FakeResponse(200, b"{}")
        if method == "POST" and "/comments" in url:
            return _json_resp(201, {"id": 9001})
        if method == "GET" and url.endswith("/user"):
            return _json_resp(200, {"login": "loadout-merger"})
        if method == "GET" and url.endswith("/files"):
            return _json_resp(200, [{"filename": "a.py"}])
        if method == "GET" and url.endswith("/comments"):
            return _json_resp(200, [])
        if method == "GET" and url.endswith("/status"):
            return _json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/actions/tasks"):
            return _json_resp(200, {"total_count": 0})
        if method == "GET" and "/compare/" in url:
            # lr-835c57: empty branch-commit list -- this file exercises
            # role/invoker matching, not the commit-subject gate.
            return _json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            if _merge_landed[0]:
                return _json_resp(
                    200,
                    {"head": {"sha": _FULL_SHA}, "title": "feat: a change",
                     "merged": True, "merge_commit_sha": "e" * 40},
                )
            return _json_resp(200, {"head": {"sha": _FULL_SHA}, "title": "feat: a change"})
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener


def _merge_base_args(**overrides) -> list[str]:
    args = {
        "--platform": "forgejo",
        "--repo": "some-owner/some-repo",
        "--pr": "1",
    }
    args.update(overrides)
    argv: list[str] = []
    for key, value in args.items():
        if value is None:
            continue
        argv.extend([key, str(value)])
    # lr-ac5c8a: this file's fixtures carry no local working tree --
    # --no-post-merge-tree satisfies the now-mandatory --repo-path/
    # --no-post-merge-tree/--skip-post-merge acknowledgment (see
    # test_merge_verb.py's _base_args for the identical rationale).
    if "--repo-path" not in argv and "--skip-post-merge" not in argv:
        argv.append("--no-post-merge-tree")
    return argv


class TestMergeVerbRoleMatchesAuthorizedInvoker:
    """(1) An explicit --role that matches what the AuthorityProvider
    authorizes for this invoker succeeds -- and the SAME role string reaches
    both merge.authority and transport.credential_provider (one attested
    value, two consumers, never re-derived per seam)."""

    def test_explicit_role_authorized_succeeds_and_reaches_both_seams(self):
        authority = _RecordingAuthorityProvider(allowed=frozenset({"merger"}))
        tokens = _RecordingTokenProvider()
        argv = _merge_base_args(**{"--role": "merger", "--authorized-role": "merger"})
        code = merge_verb.main(
            argv, token_provider=tokens, authority_provider=authority, opener=_merge_opener()
        )
        assert code == merge_verb.EXIT_OK
        assert authority.checked_roles == ["merger"]
        assert tokens.resolved_for == ["merger"]


class TestMergeVerbRoleMismatchDenied:
    """(2) An explicit --role that the AuthorityProvider does NOT authorize
    for this invoker is DENIED, fail-closed, BEFORE any credential is
    resolved -- a role mismatch must never fall through to a token mint."""

    def test_unauthorized_role_denied_before_token_resolution(self):
        # The provider authorizes ONLY "merger" -- this invocation attests a
        # DIFFERENT role ("intern"), which the provider has never granted
        # merge authority to.
        authority = _RecordingAuthorityProvider(allowed=frozenset({"merger"}))

        def _refuse_if_called(role: str) -> str:
            raise AssertionError(
                f"token provider must not be called on an authority mismatch "
                f"(role={role!r})"
            )

        class _RefusingTokenProvider:
            resolve_token = staticmethod(_refuse_if_called)

        argv = _merge_base_args(**{"--role": "intern", "--authorized-role": "merger"})
        code = merge_verb.main(
            argv, token_provider=_RefusingTokenProvider(), authority_provider=authority
        )
        assert code == merge_verb.EXIT_AUTHORITY_DENIED
        assert authority.checked_roles == ["intern"]

    def test_credential_provider_scoped_to_a_different_role_denied(self):
        """A role the AUTHORITY provider allows can still be denied at the
        CREDENTIAL seam if that role has no resolvable token -- the two
        seams are independent fail-closed gates, not one combined check."""
        authority = _RecordingAuthorityProvider(allowed=frozenset({"merger"}))
        tokens = _MismatchedTokenProvider(only_role="someone-else")
        argv = _merge_base_args(**{"--role": "merger", "--authorized-role": "merger"})
        code = merge_verb.main(argv, token_provider=tokens, authority_provider=authority)
        assert code == merge_verb.EXIT_TOKEN_FETCH_FAILED
        assert tokens.resolved_for == ["merger"]


class TestMergeVerbOmittedRoleDefaultsToInvoker:
    """(3) Omitting --role entirely falls back to DEFAULT_ROLE
    ("release-dispatcher") -- never an empty string, never a hardcoded agent
    name -- and that default is what reaches BOTH seams."""

    def test_omitted_role_resolves_to_default_role_in_both_seams(self):
        authority = _RecordingAuthorityProvider(allowed=frozenset({DEFAULT_ROLE}))
        tokens = _RecordingTokenProvider()
        argv = _merge_base_args(**{"--authorized-role": DEFAULT_ROLE})  # no --role at all
        code = merge_verb.main(
            argv, token_provider=tokens, authority_provider=authority, opener=_merge_opener()
        )
        assert code == merge_verb.EXIT_OK
        assert authority.checked_roles == [DEFAULT_ROLE]
        assert tokens.resolved_for == [DEFAULT_ROLE]

    def test_omitted_role_is_denied_when_default_role_not_authorized(self):
        """The default role is exactly as subject to the authority gate as
        any explicit role -- omitting --role is not an implicit bypass."""
        authority = _RecordingAuthorityProvider(allowed=frozenset({"merger"}))
        argv = _merge_base_args(**{"--authorized-role": "merger"})  # no --role
        code = merge_verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=authority,
        )
        assert code == merge_verb.EXIT_AUTHORITY_DENIED
        assert authority.checked_roles == [DEFAULT_ROLE]


# ---------------------------------------------------------------------------
# push.verb -- single-seam case (--caller flows only through
# transport.credential_provider; there is no authority gate on this verb).
# Covers the same match/mismatch/default trio with an --update-pr call
# (skips git/push machinery entirely -- this file is about role resolution,
# not push mechanics, which test_push_verb.py already covers).
# ---------------------------------------------------------------------------


def _push_update_opener(*, pr_number=42):
    def opener(req, timeout=15):
        if req.get_method() == "PATCH" and req.full_url.endswith(f"/pulls/{pr_number}"):
            return _json_resp(200, {"number": pr_number})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


class TestPushVerbCallerRoleContract:
    def test_explicit_caller_matching_provider_succeeds(self):
        tokens = _RecordingTokenProvider()
        argv = [
            "--caller", "builder",
            "--update-pr", "--pr", "42",
            "--title", "fix: something",
            "--repo", "some-owner/some-repo",
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
        ]
        code = push_verb.main(argv, token_provider=tokens, opener=_push_update_opener())
        assert code == push_verb.EXIT_OK
        assert tokens.resolved_for == ["builder"]

    def test_caller_with_no_resolvable_token_denied(self):
        tokens = _MismatchedTokenProvider(only_role="someone-else")
        argv = [
            "--caller", "builder",
            "--update-pr", "--pr", "42",
            "--title", "fix: something",
            "--repo", "some-owner/some-repo",
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
        ]
        code = push_verb.main(argv, token_provider=tokens, opener=_push_update_opener())
        assert code == push_verb.EXIT_TOKEN_FETCH_FAILED
        assert tokens.resolved_for == ["builder"]

    def test_omitted_caller_defaults_to_default_role(self):
        tokens = _RecordingTokenProvider()
        argv = [
            "--update-pr", "--pr", "42",
            "--title", "fix: something",
            "--repo", "some-owner/some-repo",
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
        ]  # no --caller
        code = push_verb.main(argv, token_provider=tokens, opener=_push_update_opener())
        assert code == push_verb.EXIT_OK
        assert tokens.resolved_for == [DEFAULT_ROLE]


# ---------------------------------------------------------------------------
# review.verb -- single-seam case (--caller flows only through
# transport.credential_provider). Uses an ordinary --body-stdin post (no
# verdict route) so the test is about role resolution, not the verdict-fence
# machinery test_review_verb.py already covers in depth.
# ---------------------------------------------------------------------------


def _review_success_opener(*, pr_number=42, comment_id=9):
    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(200, {"id": comment_id})
        if url.endswith("/api/v1/user"):
            return _json_resp(200, {"login": "some-role"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            return _json_resp(
                200,
                [
                    {
                        "id": comment_id,
                        "user": {"login": "some-role"},
                        "body": "LGTM",
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://readback",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


class TestReviewVerbCallerRoleContract:
    def _run(self, argv, tokens, monkeypatch):
        # --body-env is now the DEFAULT body-ingestion route when neither
        # body-ingestion flag is passed (lr-9ca25a) -- this class's tests
        # exercise the caller/role contract, not body-ingestion, so
        # --body-stdin is added explicitly to keep driving the mocked stdin
        # content exactly as before the default flip.
        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: b'{"body": "LGTM"}'
        )
        if "--body-stdin" not in argv and "--body-env" not in argv:
            argv = [*argv, "--body-stdin"]
        return review_verb.main(argv, token_provider=tokens, opener=_review_success_opener())

    def test_explicit_caller_matching_provider_succeeds(self, monkeypatch):
        tokens = _RecordingTokenProvider()
        argv = [
            "--caller", "reviewer",
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
            "some-owner/some-repo", "42",
        ]
        code = self._run(argv, tokens, monkeypatch)
        assert code == review_verb.EXIT_OK
        assert tokens.resolved_for == ["reviewer"]

    def test_caller_with_no_resolvable_token_denied(self, monkeypatch):
        tokens = _MismatchedTokenProvider(only_role="someone-else")
        argv = [
            "--caller", "reviewer",
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
            "some-owner/some-repo", "42",
        ]
        code = self._run(argv, tokens, monkeypatch)
        assert code == review_verb.EXIT_TOKEN_FETCH_FAILED
        assert tokens.resolved_for == ["reviewer"]

    def test_omitted_caller_defaults_to_default_role(self, monkeypatch):
        tokens = _RecordingTokenProvider()
        argv = [
            "--platform", "forgejo",
            "--git-host-base-url", "https://forgejo.example",
            "some-owner/some-repo", "42",
        ]  # no --caller
        code = self._run(argv, tokens, monkeypatch)
        assert code == review_verb.EXIT_OK
        assert tokens.resolved_for == [DEFAULT_ROLE]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
