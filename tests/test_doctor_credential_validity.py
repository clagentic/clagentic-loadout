"""test_doctor_credential_validity.py — coverage for
clagentic_loadout.doctor.credential_validity (lr-0eeb0c).

No real network call is ever made — every probe function is exercised via
an injected `opener` callable, the SAME pattern
tests/test_transport_git_host_api.py already established for
git_host_api.request(). Every fixture below asserts against the FIVE
states this module exists to distinguish, never collapsing malformed vs
rejected vs insufficient-scope vs unreachable vs ok (task acceptance
criteria, lr-0eeb0c).
"""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error

import pytest

from clagentic_loadout.doctor.credential_validity import (
    CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
    CREDENTIAL_STATE_MALFORMED,
    CREDENTIAL_STATE_OK,
    CREDENTIAL_STATE_REJECTED,
    CREDENTIAL_STATE_UNKNOWN,
    CREDENTIAL_STATE_UNREACHABLE,
    CREDENTIAL_STATES,
    probe_forgejo_credential,
    probe_github_credential,
)
from clagentic_loadout.push.push_failure_labels import SUB_CAUSE_MALFORMED_TOKEN


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


def _fake_opener_200(login: str = "some-bot"):
    def opener(req, timeout=15):
        return _FakeResponse(200, json.dumps({"login": login}).encode("utf-8"))

    return opener


def _fake_opener_http_error(status: int, body: bytes):
    def opener(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, status, "err", {}, io.BytesIO(body))

    return opener


def _fake_opener_network_error():
    def opener(req, timeout=15):
        raise urllib.error.URLError("connection refused")

    return opener


class TestCredentialStateVocabularyReused:
    def test_malformed_state_reuses_push_failure_label(self):
        """MALFORMED-TOKEN VOCABULARY, REUSED NOT REINVENTED (task
        instruction): this module's own MALFORMED state literal IS
        push_failure_labels.SUB_CAUSE_MALFORMED_TOKEN, not a second,
        parallel string for the identical failure class."""
        assert CREDENTIAL_STATE_MALFORMED == SUB_CAUSE_MALFORMED_TOKEN

    def test_all_states_enumerated(self):
        assert CREDENTIAL_STATES == {
            CREDENTIAL_STATE_OK,
            CREDENTIAL_STATE_MALFORMED,
            CREDENTIAL_STATE_REJECTED,
            CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
            CREDENTIAL_STATE_UNREACHABLE,
            CREDENTIAL_STATE_UNKNOWN,
        }


class TestForgejoProbe:
    def test_well_formed_accepted_credential_reports_ok(self):
        result = probe_forgejo_credential(
            "http://git-host.example.com",
            "tok-well-formed",
            opener=_fake_opener_200(login="amos"),
        )
        assert result.state == CREDENTIAL_STATE_OK
        assert "amos" in result.detail
        assert result.platform == "forgejo"

    def test_malformed_token_shape_reports_malformed_not_expiry_not_permission(self):
        """The exact evidence quote from the task: 'token is malformed:
        token contains an invalid number of segments' -- this must report
        MALFORMED, never REJECTED (expiry-shaped) and never
        INSUFFICIENT_SCOPE (permission-shaped)."""
        body = (
            b"token is malformed: token contains an invalid number of segments\n"
            b"access token does not exist [sha: 03cf31aea57b77ed0f89d14f5d4f22ed7f97ba4b]\n"
            b"authorized integration: parse JWT error: token is malformed"
        )
        result = probe_forgejo_credential(
            "http://git-host.example.com",
            "tok-malformed",
            opener=_fake_opener_http_error(401, body),
        )
        assert result.state == CREDENTIAL_STATE_MALFORMED
        assert result.state != CREDENTIAL_STATE_REJECTED
        assert result.state != CREDENTIAL_STATE_INSUFFICIENT_SCOPE

    def test_expired_or_revoked_credential_reports_rejected_distinct_from_malformed(self):
        result = probe_forgejo_credential(
            "http://git-host.example.com",
            "tok-dead",
            opener=_fake_opener_http_error(401, b"Credentials are incorrect or have expired"),
        )
        assert result.state == CREDENTIAL_STATE_REJECTED
        assert result.state != CREDENTIAL_STATE_MALFORMED

    def test_insufficient_scope_reports_distinctly_from_auth_failure(self):
        result = probe_forgejo_credential(
            "http://git-host.example.com",
            "tok-scoped",
            opener=_fake_opener_http_error(403, b"Forbidden"),
        )
        assert result.state == CREDENTIAL_STATE_INSUFFICIENT_SCOPE
        assert result.state != CREDENTIAL_STATE_REJECTED
        assert result.state != CREDENTIAL_STATE_MALFORMED

    def test_unreachable_host_reports_infrastructure_not_credential_fault(self):
        result = probe_forgejo_credential(
            "http://unreachable.example.com",
            "tok-anything",
            opener=_fake_opener_network_error(),
        )
        assert result.state == CREDENTIAL_STATE_UNREACHABLE
        assert result.state not in (
            CREDENTIAL_STATE_MALFORMED,
            CREDENTIAL_STATE_REJECTED,
            CREDENTIAL_STATE_INSUFFICIENT_SCOPE,
        )

    def test_unrecognized_200_body_degrades_to_unknown_not_false_green(self):
        def opener(req, timeout=15):
            return _FakeResponse(200, b"not json")

        result = probe_forgejo_credential(
            "http://git-host.example.com", "tok-weird", opener=opener
        )
        assert result.state == CREDENTIAL_STATE_UNKNOWN

    def test_never_carries_token_material(self):
        result = probe_forgejo_credential(
            "http://git-host.example.com",
            "super-secret-token-value",
            opener=_fake_opener_200(),
        )
        assert "super-secret-token-value" not in result.detail
        assert "super-secret-token-value" not in str(result.resolved)
        assert result.token_sha256 == hashlib.sha256(
            b"super-secret-token-value"
        ).hexdigest()


class TestGithubProbe:
    def test_well_formed_accepted_credential_reports_ok(self):
        result = probe_github_credential("tok-well-formed", opener=_fake_opener_200(login="amos"))
        assert result.state == CREDENTIAL_STATE_OK
        assert result.platform == "github"

    def test_malformed_token_shape_reports_malformed(self):
        body = json.dumps({"message": "Bad credentials: invalid token"}).encode("utf-8")
        result = probe_github_credential("tok-malformed", opener=_fake_opener_http_error(401, body))
        assert result.state == CREDENTIAL_STATE_MALFORMED

    def test_expired_or_revoked_credential_reports_rejected(self):
        body = json.dumps({"message": "Bad credentials"}).encode("utf-8")
        result = probe_github_credential("tok-dead", opener=_fake_opener_http_error(401, body))
        assert result.state == CREDENTIAL_STATE_REJECTED

    def test_app_installation_token_403_reports_insufficient_scope(self):
        """GET /user 403 for a GitHub App installation token is documented,
        expected behavior (review.github_backend.resolve_own_login's own
        rationale) -- this probe reports it as INSUFFICIENT_SCOPE, never as
        a broken credential."""
        body = json.dumps({"message": "Must have admin rights to Repository."}).encode("utf-8")
        result = probe_github_credential("tok-app", opener=_fake_opener_http_error(403, body))
        assert result.state == CREDENTIAL_STATE_INSUFFICIENT_SCOPE

    def test_unreachable_host_reports_infrastructure_fault(self):
        result = probe_github_credential("tok-anything", opener=_fake_opener_network_error())
        assert result.state == CREDENTIAL_STATE_UNREACHABLE

    def test_never_carries_token_material(self):
        result = probe_github_credential("super-secret-gh-token", opener=_fake_opener_200())
        assert "super-secret-gh-token" not in result.detail
        assert "super-secret-gh-token" not in str(result.resolved)
