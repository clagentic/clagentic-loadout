"""test_merge_close_verb.py — CLI-level coverage for
clagentic_loadout.merge.close_verb (loadout-close-pr, lr-2ba5e1).

Mirrors test_merge_verb_platform_dispatch.py's discipline (NO real network
call, NO real git; everything driven through an injected opener + injected
token/authority providers):
  - --help / --version exit EXIT_OK without needing any other argument.
  - --platform is mandatory (argparse enforces it).
  - Namespace guard runs BEFORE any credential mint or authority check.
  - Merge-authority check runs BEFORE any credential mint (a
    _RefusingTokenProvider proves this).
  - The platform guard fires BEFORE any credential mint, for BOTH
    wrong-platform directions.
  - A --platform forgejo invocation dispatches to forgejo_backend.close_pr
    (PATCH .../issues/{n}); a --platform github invocation dispatches to
    github_backend.close_pr (PATCH .../pulls/{n}) -- proven by an opener
    that only understands one platform's URL shape.
  - A non-2xx close response surfaces as EXIT_CLOSE_FAILED, never a false
    success.
"""

from __future__ import annotations

import io
import json
import urllib.error

from clagentic_loadout.merge import close_verb
from clagentic_loadout.transport.credential_provider import CredentialProviderError

_OWNER = "some-owner"
_REPO = "some-repo"


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise AssertionError(
            f"token provider must not be called before the namespace/"
            f"authority/platform guards refuse (role={role!r})"
        )


class _MissingCredsTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise CredentialProviderError("no credentials configured for this role")


class _AllowingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return True


class _DenyingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        return False


class _RefusingAuthorityProvider:
    def authority_allows(self, role, owner, repo, pr_number) -> bool:
        raise AssertionError("authority provider must not be called")


class _FakeResponse:
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


def _forgejo_opener(status: int = 200, *, readback_confirms: bool = True):
    """Understands the Forgejo issues/{n} PATCH shape (the close itself) AND
    the pulls/{n} GET shape (lr-361de3's post-close readback,
    merge.merge_readback.verify_pr_closed re-reading get_pr_info) -- an
    unexpected (e.g. GitHub-shaped) call raises AssertionError, proving the
    forgejo backend was the one actually dispatched to.

    A non-2xx *status* raises HTTPError -- transport.git_host_api.request()
    fails fast (raises GitHostApiError, translated by forgejo_backend.
    close_pr into MergeExecutionError) for ANY non-2xx response on a write
    method; it never returns a plain (status, body) tuple for one, so the
    fixture must raise to reach that path, exactly like a real non-2xx
    response would.

    readback_confirms (default True): the GET .../pulls/{n} response's
    'state' field reads "closed" when True (the confirming case), "open"
    when False (TestPostCloseReadback exercises the failure path).
    """

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET":
            assert "/api/v1/repos/" in url and "/pulls/" in url, (
                f"expected a Forgejo pulls GET (post-close readback), got {url!r}"
            )
            return _FakeResponse(
                200, json.dumps({"state": "closed" if readback_confirms else "open"}).encode("utf-8")
            )
        assert "/api/v1/repos/" in url and "/issues/" in url, (
            f"expected a Forgejo issues PATCH, got {url!r}"
        )
        assert method == "PATCH"
        assert json.loads(req.data.decode("utf-8")) == {"state": "closed"}
        if status >= 300:
            raise urllib.error.HTTPError(
                url, status, "err", {}, io.BytesIO(b'{"message": "not found"}')
            )
        return _FakeResponse(status, b"{}")

    return opener


def _github_opener(status: int = 200, *, readback_confirms: bool = True):
    """Understands the GitHub pulls/{n} PATCH shape (the close itself) AND
    the same endpoint's GET shape (lr-361de3's post-close readback) -- an
    unexpected (e.g. Forgejo-shaped) call raises AssertionError, proving the
    github backend was the one actually dispatched to."""

    def opener(req, timeout=30):
        assert req.full_url == f"https://api.github.com/repos/{_OWNER}/{_REPO}/pulls/42"
        method = req.get_method()
        if method == "GET":
            return _FakeResponse(
                200, json.dumps({"state": "closed" if readback_confirms else "open"}).encode("utf-8")
            )
        assert method == "PATCH"
        assert json.loads(req.data.decode("utf-8")) == {"state": "closed"}
        return _FakeResponse(status, b'{"state": "closed"}')

    return opener


class TestHelpAndVersion:
    def test_help_exits_ok_with_no_other_args(self):
        assert close_verb.main(["--help"]) == close_verb.EXIT_OK

    def test_version_exits_ok(self):
        assert close_verb.main(["--version"]) == close_verb.EXIT_OK


class TestUsage:
    def test_missing_platform_exits_argparse_usage_code(self):
        # argparse itself raises SystemExit(2) for a missing required
        # argument (its own convention, distinct from this verb's own
        # EXIT_USAGE=1 for a caller-input-shape error caught downstream of
        # argparse) -- main() propagates that code verbatim, matching
        # merge.verb's own --platform-missing behavior exactly.
        code = close_verb.main(["--repo", f"{_OWNER}/{_REPO}", "--pr", "42"])
        assert code == 2

    def test_malformed_repo_is_usage_error(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", "not-owner-slash-repo", "--pr", "42"]
        )
        assert code == close_verb.EXIT_USAGE


class TestNamespaceGuard:
    def test_denied_namespace_refuses_before_any_credential_or_authority_check(self):
        code = close_verb.main(
            [
                "--platform", "forgejo",
                "--repo", f"{_OWNER}/{_REPO}",
                "--pr", "42",
                "--allowed-namespace", "a-different-owner",
            ],
            token_provider=_RefusingTokenProvider(),
            authority_provider=_RefusingAuthorityProvider(),
        )
        assert code == close_verb.EXIT_NAMESPACE_DENIED


class TestAuthorityGuard:
    def test_denied_authority_refuses_before_any_credential_mint(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RefusingTokenProvider(),
            authority_provider=_DenyingAuthorityProvider(),
        )
        assert code == close_verb.EXIT_AUTHORITY_DENIED

    def test_allowed_authority_proceeds_to_credential_resolution(self):
        recording = _RecordingTokenProvider()
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=recording,
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(),
        )
        assert code == close_verb.EXIT_OK
        assert recording.resolved_for  # token WAS resolved once authority passed


class TestCredentialFailure:
    def test_missing_credentials_is_token_fetch_failed(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_MissingCredsTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
        )
        assert code == close_verb.EXIT_TOKEN_FETCH_FAILED


class TestPlatformDispatch:
    def test_forgejo_platform_dispatches_to_forgejo_backend(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(),
        )
        assert code == close_verb.EXIT_OK

    def test_github_platform_dispatches_to_github_backend(self):
        code = close_verb.main(
            ["--platform", "github", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(),
        )
        assert code == close_verb.EXIT_OK


class TestCloseFailure:
    def test_non_2xx_close_response_is_close_failed(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(status=404),
        )
        assert code == close_verb.EXIT_CLOSE_FAILED


class TestPostCloseReadback:
    """lr-361de3: close_verb performs a FRESH post-close GET .../pulls/{n}
    readback (merge.merge_readback.verify_pr_closed) AFTER the close PATCH's
    own response reports success -- fail-closed, distinct
    EXIT_CLOSE_READBACK_FAILED, and the SAME stable {verified, source,
    detail} 'readback' envelope key merge.verb's own readback uses.

    NON-VACUITY (task acceptance criterion 4): the confirmed-close test below
    proves the readback path is actually reached (source == 'api_get',
    detail carries state == 'closed'); the two failure tests below prove a
    PATCH that reports success but is NOT confirmed by the readback GET
    (readback_confirms=False) FAILS the verb with a distinct exit code --
    reverting close_verb's own fail-closed check would make either failure
    test go green on EXIT_OK instead, demonstrating this is not vacuous.
    """

    def test_confirmed_close_reports_verified_in_envelope_forgejo(self, capsys):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(),  # readback_confirms=True (default)
        )
        assert code == close_verb.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["readback"]["verified"] is True
        assert payload["readback"]["source"] == "api_get"
        assert payload["readback"]["detail"]["state"] == "closed"

    def test_unconfirmed_close_fails_closed_forgejo(self):
        code = close_verb.main(
            ["--platform", "forgejo", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_forgejo_opener(readback_confirms=False),
        )
        assert code == close_verb.EXIT_CLOSE_READBACK_FAILED

    def test_unconfirmed_close_fails_closed_github(self):
        code = close_verb.main(
            ["--platform", "github", "--repo", f"{_OWNER}/{_REPO}", "--pr", "42"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_github_opener(readback_confirms=False),
        )
        assert code == close_verb.EXIT_CLOSE_READBACK_FAILED
