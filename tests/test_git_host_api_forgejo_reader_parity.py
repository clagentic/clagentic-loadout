"""test_git_host_api_forgejo_reader_parity.py — Forgejo reader-surface
code-parity gate, the READER-SURFACE TWIN of the push-side GitHub-source
parity gate (see test_push_github_mint_parity.py).

The reference agent-orchestration deployment wants to promote
loadout-git-host-api to a builder role's PRIMARY reader surface for Forgejo
(forgejo-curl demoted to FALLBACK). Before that flip, loadout-side
code-parity between loadout-git-host-api's Forgejo authenticated read/API
path and the reference `forgejo-curl --caller <agent>` tool must be proven,
exactly as the push-side gate proved the GitHub auth/mint/attest seam
before that flip.

Properties asserted here, each read directly from
scripts/forgejo-curl's own module docstring/code (the reference contract)
and cross-checked against loadout's implementation:

  1. AUTH SOURCE: forgejo-curl "always self-fetches" the caller's PAT from
     an internal secret broker, keyed by --caller, and NEVER trusts an
     inherited FORGEJO_TOKEN from the process environment (forgejo-curl
     module docstring, "Auth contract" section). loadout's equivalent seam
     is transport.credential_provider's TokenProvider protocol, wired via
     transport.provider_config.resolve_platform_provider("forgejo") to a
     CommandTokenProvider whose configured command performs the SAME kind
     of self-fetch -- proven here with a SYNTHETIC command this test
     controls end to end (no real broker present, no hardcoded agent
     names), never a hardcoded broker import.
     transport.git_host_api.request()'s own opener-injection contract
     (proven in test_transport_git_host_api.py::TestMainGet::
     test_get_uses_injected_provider_not_env) already proves an ambient
     env var has zero effect on the token actually used; this file adds
     the reader-specific assertion that the CommandTokenProvider seam
     itself is reached with the caller's role, not a hardcoded identity.

  2. CALLER-ATTESTATION BINDING: forgejo-curl's explicit --caller is bound
     to the attested invoking identity (resolve_attested_identity(), a
     4-path chain topped by a per-spawn sidecar file and falling back to
     an ambient env var) -- a mismatch REFUSES fail-closed, BEFORE any
     self-fetch or network I/O (EXIT_CALLER_MISMATCH=11); an unattested
     invocation (chain resolves nothing) is not a mismatch and proceeds.
     loadout's transport.git_host_api.bind_caller enforces the identical
     shape against transport.attestation.resolve_identity's own 3-layer
     chain (configured env -> sidecar file -> built-in OS-user fallback).
     This EXACT property (explicit mismatch fails closed before I/O,
     omitted caller never checked) is ALREADY fully proven by
     tests/test_git_host_api_caller_attested_invoker_binding.py -- this
     file does not re-prove it, it CITES it as the reader-parity evidence
     (see docs/reader-parity.md) and adds only the one property that file
     does not cover: that bind_caller/resolve_identity are reached on an
     ordinary READ (GET), not only on the write/comment-post paths that
     file's own fixtures happen to exercise most heavily.

  3. GET SEMANTICS: both tools issue an authenticated GET with the token in
     an Authorization header (never in the URL/query string), and BOTH
     return the response body verbatim on a non-200 GET (forgejo-curl's
     _check_repo_exists/_check_pr_sha re-raise on non-404; git_host_api's
     request() returns (status, raw) unchanged for any non-write method,
     letting the caller parse an error body itself) -- proven here against
     the shared request()/build_request() functions already covered
     generically in test_transport_git_host_api.py; this file adds the
     forgejo-curl-equivalence framing (same three properties named
     side-by-side).

  4. FAIL-CLOSED FAILURE CLASSES on a read: forgejo-curl's
     _EXIT_TOKEN_FETCH_FAILED (self-fetch broker failure) and
     EXIT_CURL_FAILED-equivalent (network/transport failure) both surface
     as a distinct, non-zero, non-silent exit -- no partial/open-fail read
     ever returns a 2xx-shaped result on a failure. loadout's
     EXIT_TOKEN_FETCH_FAILED / EXIT_CURL_FAILED mirror this exactly.

  5. SCOPING: forgejo-curl's --caller selects a per-role secret-broker
     path (~/.config/crew/agents/<caller>.env) -- the token is always
     scoped to the CALLING role, never a blanket credential. loadout's
     CommandTokenProvider substitutes the SAME role string into its
     configured command's argv ({role} placeholder / trailing-arg
     convention) -- the resolved token is provably tied to the *role
     argument the caller supplied*, not a fixed value the seam always
     returns regardless of caller.

What is explicitly NOT claimed (see docs/reader-parity.md's own "not
claimed" section): this file does not exercise a real Forgejo server, a
real secret broker, or forgejo-curl's own Python process -- both "the
reference tool's documented contract" and "loadout's implementation" are
compared at the CONTRACT-SHAPE level, using forgejo-curl's own
module-docstring/code as the read-only source of truth for what its
contract promises (mirroring test_push_github_mint_parity.py's approach
for the push-side GitHub gate).
"""

from __future__ import annotations

import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

from clagentic_loadout.transport import git_host_api
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.attestation import Identity
from clagentic_loadout.transport.credential_provider import CommandTokenProvider


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Same isolation rationale as test_transport_git_host_api.py's own
    autouse fixture (lr-396f) -- a real ~/.config/clagentic/loadout/
    config.yaml on this host must never leak into a parity assertion."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


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


def _write_fake_self_fetch_script(tmp_path: Path, *, behavior: str) -> list[str]:
    """A tiny synthetic command standing in for forgejo-curl's own
    self-fetch-from-broker mechanism -- this test's conformance seam:
    no real broker import, an invented per-test role.

    *behavior*:
      "success"        -- prints a role-scoped fake token, exits 0. The
                           token DETERMINISTICALLY encodes the role it was
                           called for (f"tok-for-{role}"), so a test can
                           assert the resolved credential is actually
                           SCOPED to the calling role, not a fixed value.
      "broker_failure"  -- exits non-zero, mirroring forgejo-curl's
                           _self_fetch_token dying with
                           _EXIT_TOKEN_FETCH_FAILED on a broker error.
    """
    script = tmp_path / f"fake_self_fetch_{behavior}.py"
    if behavior == "success":
        body = (
            "import sys\n"
            "role = sys.argv[-1]\n"
            "print(f'tok-for-{role}')\n"
            "sys.exit(0)\n"
        )
    elif behavior == "broker_failure":
        body = (
            "import sys\n"
            "print('self-fetch FAILED: broker unreachable', file=sys.stderr)\n"
            "sys.exit(2)\n"
        )
    else:
        raise ValueError(f"unknown behavior {behavior!r}")
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script)]


# ---------------------------------------------------------------------------
# 1. AUTH SOURCE -- CommandTokenProvider self-fetch seam, role-scoped,
#    reached via the SAME provider-config seam a Forgejo reader configures
#    (never a hardcoded broker import).
# ---------------------------------------------------------------------------


class TestForgejoReaderAuthSourceParity:
    def test_get_resolves_token_via_configured_command_provider_not_env(
        self, monkeypatch, tmp_path
    ):
        """Mirrors forgejo-curl's own auth contract statement verbatim
        ("An inherited FORGEJO_TOKEN in the environment is NEVER trusted or
        used"): an ambient env var with a plausible-looking token must have
        ZERO effect on the credential actually attached to the GET -- the
        token must come from the configured self-fetch command's stdout,
        every time, on every invocation."""
        monkeypatch.setenv("FORGEJO_TOKEN", "ambient-should-never-be-used")
        argv = _write_fake_self_fetch_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)

        captured = {}

        def fake_opener(req, timeout=15):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(200, b'{"ok": true}')

        rc = git_host_api.main(
            ["--caller", "some-reader-role", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider,
            opener=fake_opener,
            identity_provider=lambda: Identity("some-reader-role", "configured"),
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["headers"]["Authorization"] == "token tok-for-some-reader-role"
        assert "ambient-should-never-be-used" not in captured["headers"]["Authorization"]

    def test_token_is_scoped_to_the_calling_role_not_a_fixed_value(self, tmp_path):
        """forgejo-curl's --caller selects a PER-ROLE secret-broker path
        (~/.config/crew/agents/<caller>.env) -- the resolved token is tied
        to which role asked, never a blanket credential shared across
        roles. The fake self-fetch command here deterministically encodes
        its own role argument into the returned token, so this test can
        assert loadout's CommandTokenProvider actually forwards the calling
        role through to the credential-resolution command rather than
        resolving a fixed value regardless of caller."""
        argv = _write_fake_self_fetch_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)

        for role, expected_token in (("reader-alpha", "tok-for-reader-alpha"),
                                       ("reader-beta", "tok-for-reader-beta")):
            captured = {}

            def fake_opener(req, timeout=15, _captured=captured):
                _captured["headers"] = dict(req.header_items())
                return _FakeResponse(200, b"{}")

            rc = git_host_api.main(
                ["--caller", role, "/api/v1/repos/o/r/pulls/1.diff"],
                token_provider=provider,
                opener=fake_opener,
                identity_provider=lambda r=role: Identity(r, "configured"),
            )
            assert rc == git_host_api.EXIT_OK
            assert captured["headers"]["Authorization"] == f"token {expected_token}"

    def test_broker_self_fetch_failure_is_fail_closed_token_fetch_failed(self, tmp_path):
        """forgejo-curl: a broker self-fetch failure dies with
        EXIT_TOKEN_FETCH_FAILED -- no partial/open-fail read is ever
        attempted with no credential. loadout: the SAME synthetic
        self-fetch command failing surfaces as EXIT_TOKEN_FETCH_FAILED,
        and the injected opener (standing in for the network call) is
        NEVER reached -- the failure happens before any GET is issued."""
        argv = _write_fake_self_fetch_script(tmp_path, behavior="broker_failure")
        provider = CommandTokenProvider(argv)
        opener_called = {"n": False}

        def fake_opener(req, timeout=15):
            opener_called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["--caller", "some-reader-role", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider,
            opener=fake_opener,
            identity_provider=lambda: Identity("some-reader-role", "configured"),
        )
        assert rc == git_host_api.EXIT_TOKEN_FETCH_FAILED
        assert opener_called["n"] is False

    def test_provider_seam_never_imports_a_hardcoded_broker(self):
        """Conformance check: the credential_provider module itself must
        carry no import of a real secret-broker client -- the seam is
        satisfied by ANY command a deployment configures, proven by this
        file's own synthetic command above, never by a hardcoded client
        this module imports."""
        import ast
        from clagentic_loadout.transport import credential_provider as cp_module

        tree = ast.parse(
            Path(cp_module.__file__).read_text(encoding="utf-8"),
            filename=cp_module.__file__,
        )
        imported_names: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported_names.add((alias.asname or alias.name).lower())
        forbidden_substrings = ("bao", "vault", "gatekeeper", "openbao")
        hits = [
            name
            for name in imported_names
            for forbidden in forbidden_substrings
            if forbidden in name
        ]
        assert not hits, (
            f"transport.credential_provider imports a hardcoded broker "
            f"client: {hits} -- the seam must stay provider-agnostic"
        )


# ---------------------------------------------------------------------------
# 2. CALLER-ATTESTATION BINDING ON A READ -- cites the existing exhaustive
#    coverage in test_git_host_api_caller_attested_invoker_binding.py (this
#    file does not re-prove that contract) and adds the one property that
#    file's fixtures do not isolate: the binding fires on an ordinary GET,
#    reached via the SAME CommandTokenProvider self-fetch seam this file
#    exercises above, end to end.
# ---------------------------------------------------------------------------


class TestForgejoReaderCallerAttestationBindingOnGet:
    def test_explicit_caller_mismatch_on_get_denied_before_self_fetch(self, tmp_path):
        """forgejo-curl: an explicit --caller that disagrees with the
        attested chain is refused BEFORE _self_fetch_token is ever called
        -- no broker round trip for a mismatched identity. loadout: the
        SAME refusal, proven here with the self-fetch CommandTokenProvider
        seam wired in (rather than the bare _RecordingTokenProvider fixture
        test_git_host_api_caller_attested_invoker_binding.py uses) so the
        parity claim covers the REAL seam a Forgejo reader would run
        against, not just an in-memory stub."""
        argv = _write_fake_self_fetch_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)
        opener_called = {"n": False}

        def fake_opener(req, timeout=15):
            opener_called["n"] = True
            return _FakeResponse(200, b"{}")

        rc = git_host_api.main(
            ["--caller", "reader-role", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider,
            opener=fake_opener,
            identity_provider=lambda: Identity("a-different-attested-identity", "configured"),
        )
        assert rc == git_host_api.EXIT_CALLER_INVOKER_MISMATCH
        assert opener_called["n"] is False

    def test_omitted_caller_on_get_unaffected_by_attestation(self, tmp_path):
        """forgejo-curl: an unattested/omitted --caller is not a mismatch
        and proceeds using the resolved ambient default. loadout: an
        omitted --caller on an ordinary GET proceeds to DEFAULT_ROLE
        regardless of what the attested identity resolves to -- the same
        "nothing to bind" contract test_git_host_api_caller_attested_
        invoker_binding.py::TestOmittedCallerUnchanged already proves at
        the fixture level; this asserts it survives end-to-end through the
        real self-fetch command seam."""
        argv = _write_fake_self_fetch_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)

        captured = {}

        def fake_opener(req, timeout=15):
            captured["headers"] = dict(req.header_items())
            return _FakeResponse(200, b"{}")

        from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE

        rc = git_host_api.main(
            ["/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider,
            opener=fake_opener,
            identity_provider=lambda: Identity("someone-else-entirely", "configured"),
        )
        assert rc == git_host_api.EXIT_OK
        assert captured["headers"]["Authorization"] == f"token tok-for-{DEFAULT_ROLE}"


# ---------------------------------------------------------------------------
# 3. GET SEMANTICS -- non-200 read returns the response verbatim (never
#    raises), matching forgejo-curl's _check_repo_exists/_check_pr_sha
#    re-raise-on-non-404 contract: a GET caller parses the body/status
#    itself rather than the transport silently swallowing or fabricating a
#    result.
# ---------------------------------------------------------------------------


class TestForgejoReaderGetSemanticsParity:
    def test_non_200_get_returns_status_and_body_verbatim(self):
        """forgejo-curl's GET helpers (_check_repo_exists) distinguish a
        definitive 404 from other statuses and let the caller decide what
        it means -- non-200 is never silently treated as success or
        silently discarded. git_host_api.request() for a GET returns
        (status, raw) verbatim for ANY status, including non-200 -- proven
        generically in test_transport_git_host_api.py::TestRequest::
        test_get_method_non_2xx_does_not_raise; this asserts the same
        shape framed as the forgejo-curl-equivalence property this gate
        needs."""
        def fake_opener(req, timeout=15):
            raise urllib.error.HTTPError(
                req.full_url, 404, "Not Found", {}, io.BytesIO(b'{"message":"not found"}')
            )

        status, body = git_host_api.request(
            "http://git-host.example.com", "GET", "/api/v1/repos/o/r", "tok-1",
            opener=fake_opener,
        )
        assert status == 404
        assert json.loads(body) == {"message": "not found"}

    def test_token_carried_in_authorization_header_never_in_url(self):
        """Both tools carry the token as an Authorization header value,
        never as a query parameter or URL-embedded credential -- proven
        generically in test_transport_git_host_api.py::TestBuildRequest::
        test_token_never_in_url; restated here as an explicit reader-parity
        property since it is exactly the property forgejo-curl's own
        module docstring calls out ('Token never appears in process
        command line...')."""
        req = git_host_api.build_request(
            "http://git-host.example.com", "GET", "/api/v1/repos/o/r/pulls/1.diff",
            "super-secret-reader-tok",
        )
        assert "super-secret-reader-tok" not in req.full_url
        assert req.get_header("Authorization") == "token super-secret-reader-tok"


# ---------------------------------------------------------------------------
# 4. FAIL-CLOSED FAILURE CLASSES -- a network/transport failure on a read
#    surfaces as a distinct, non-zero exit, never a silent success.
# ---------------------------------------------------------------------------


class TestForgejoReaderFailClosedFailureClasses:
    def test_network_failure_on_get_is_curl_failed_not_silent(self, tmp_path):
        """forgejo-curl: an unreachable broker/host on a GET dies with a
        non-zero exit naming the failure; curl's own non-zero return is
        never silently swallowed. loadout: the equivalent network failure
        on request() surfaces as EXIT_CURL_FAILED -- proven end-to-end
        through main() with the real self-fetch CommandTokenProvider seam
        wired in (token resolution succeeds; the SUBSEQUENT network call
        is what fails), not just at the bare request()-function level
        already covered in test_transport_git_host_api.py."""
        argv = _write_fake_self_fetch_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)

        def fake_opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        rc = git_host_api.main(
            ["--caller", "reader-role", "/api/v1/repos/o/r/pulls/1.diff"],
            token_provider=provider,
            opener=fake_opener,
            identity_provider=lambda: Identity("reader-role", "configured"),
        )
        assert rc == git_host_api.EXIT_CURL_FAILED


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
