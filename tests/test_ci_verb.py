"""CLI-level coverage for `clagentic-loadout ci rerun` and `ci cancel`.

No real network: every request goes through a fake opener that answers from a
route table and records what was asked. Each behaviour is one table; a row is a
case.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path

import pytest

from clagentic_loadout.ci import verb as ci_verb
from clagentic_loadout.ci.exits import (
    EXIT_CALLER_INVOKER_MISMATCH,
    EXIT_FORBIDDEN,
    EXIT_HOST_ERROR,
    EXIT_NOT_SUPPORTED,
    EXIT_OK,
    EXIT_RUN_NOT_FOUND,
    EXIT_STATE_REFUSED,
    EXIT_TOKEN_FETCH_FAILED,
    EXIT_USAGE,
)
from clagentic_loadout.provisioning.allowlist import generate_role_fragment
from clagentic_loadout.provisioning.roles import (
    DEFAULT_ROLE_VERBS,
    KNOWN_VERBS,
    load_role_verbs,
    roles_holding_verb,
)
from clagentic_loadout.transport import attestation
from clagentic_loadout.transport.credential_provider import CredentialProviderError

_REPO = "some-owner/some-repo"
_RUN = 4242
_HEAD = "a" * 40
_TOKEN = "tok-secret-value"
_FORGEJO_BASE = "http://git-host.example.test"
_GH_RUN = f"https://api.github.com/repos/{_REPO}/actions/runs/{_RUN}"
_FJ_RUN = f"{_FORGEJO_BASE}/api/v1/repos/{_REPO}/actions/runs/{_RUN}"


class _Response:
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


class _Host:
    """Answers (method, url) from a table. A status of 300 or more is raised as
    the HTTPError a real host response would be."""

    def __init__(self, routes: dict[tuple[str, str], tuple[int, object]]):
        self.routes = routes
        self.requests: list[tuple[str, str, str | None]] = []

    def __call__(self, req, timeout=None):
        method, url = req.get_method(), req.full_url
        self.requests.append((method, url, req.get_header("Authorization")))
        assert (method, url) in self.routes, f"unexpected request {method} {url}"
        status, body = self.routes[(method, url)]
        raw = json.dumps(body).encode() if body is not None else b""
        if status >= 300:
            raise urllib.error.HTTPError(url, status, "err", {}, io.BytesIO(raw))
        return _Response(status, raw)

    def posts(self) -> list[str]:
        return [url for method, url, _auth in self.requests if method == "POST"]


class _Tokens:
    def __init__(self, token: str = _TOKEN):
        self.token = token
        self.roles: list[str] = []

    def resolve_token(self, role, *, repo=None):
        self.roles.append(role)
        return self.token


class _NoTokens:
    def resolve_token(self, role, *, repo=None):
        raise CredentialProviderError("no credential for this role")


class _Sink:
    def __init__(self):
        self.records: list[dict] = []

    def emit(self, record):
        self.records.append(record)
        return True


def _gh_routes(post_status: int, post_body=None, *, suffix: str, head=_HEAD):
    return {
        ("GET", _GH_RUN): (200, {"head_sha": head}),
        ("POST", f"{_GH_RUN}/{suffix}"): (post_status, post_body),
    }


def _fj_routes(post_status: int, post_body=None):
    return {
        ("GET", _FJ_RUN): (200, {"commit_sha": _HEAD}),
        ("POST", f"{_FJ_RUN}/cancel"): (post_status, post_body),
    }


def _call(
    verb: str, extra: list[str], host: _Host, *, platform: str = "github", caller: str = "some-role",
    tokens=None, sink=None, repo: str = _REPO, run: str = str(_RUN),
) -> int:
    argv = ["--caller", caller, "--platform", platform, "--repo", repo, "--run", run, *extra]
    if platform == "forgejo":
        argv += ["--git-host-base-url", _FORGEJO_BASE]
    return ci_verb.main(
        argv,
        operation=verb,
        token_provider=tokens if tokens is not None else _Tokens(),
        opener=host,
        sink=sink if sink is not None else _Sink(),
    )


# --- success: one POST to the documented path, exit 0, run and head printed ---

_SUCCESS_ROWS = [
    # id, platform, verb, extra argv, operation, suffix, status
    ("gh-rerun-failed", "github", "rerun", ["--failed-jobs"], "rerun-failed-jobs", "rerun-failed-jobs", 201),
    ("gh-rerun", "github", "rerun", [], "rerun", "rerun", 201),
    ("gh-cancel", "github", "cancel", [], "cancel", "cancel", 202),
    ("fj-cancel", "forgejo", "cancel", [], "cancel", "cancel", 204),
]


@pytest.mark.parametrize(
    "platform,verb,extra,operation,suffix,status",
    [row[1:] for row in _SUCCESS_ROWS],
    ids=[row[0] for row in _SUCCESS_ROWS],
)
def test_success_makes_one_authenticated_post_and_prints_run_and_head(
    platform, verb, extra, operation, suffix, status, capsys
):
    routes = _gh_routes(status, None, suffix=suffix) if platform == "github" else _fj_routes(status)
    host = _Host(routes)
    sink = _Sink()

    rc = _call(verb, extra, host, platform=platform, sink=sink)

    assert rc == EXIT_OK
    run_url = _GH_RUN if platform == "github" else _FJ_RUN
    assert host.posts() == [f"{run_url}/{suffix}"]
    assert all(auth == f"token {_TOKEN}" for _m, _u, auth in host.requests)
    printed = json.loads(capsys.readouterr().out)
    assert printed["run"] == _RUN and printed["head_sha"] == _HEAD
    assert printed["operation"] == operation and printed["status"] == status
    assert _TOKEN not in json.dumps(printed)


# --- Forgejo operations its API does not offer -------------------------------

@pytest.mark.parametrize(
    "extra,operation", [([], "rerun"), (["--failed-jobs"], "rerun-failed-jobs")],
    ids=["rerun", "rerun-failed-jobs"],
)
def test_forgejo_rerun_is_not_supported_and_makes_no_request(extra, operation, capsys):
    host, tokens = _Host({}), _Tokens()

    rc = _call("rerun", extra, host, platform="forgejo", tokens=tokens)

    assert rc == EXIT_NOT_SUPPORTED
    assert host.requests == [] and tokens.roles == []
    err = capsys.readouterr().err
    assert "forgejo" in err and operation in err


# --- host errors: one write request, named code, no token printed -------------

_ERROR_ROWS = [
    # id, platform, status, body, expected code
    ("gh-404", "github", 404, {"message": "Not Found"}, EXIT_RUN_NOT_FOUND),
    ("gh-403", "github", 403, {"message": "Resource not accessible"}, EXIT_FORBIDDEN),
    ("gh-409", "github", 409, {"message": "cannot be rerun"}, EXIT_STATE_REFUSED),
    ("gh-422", "github", 422, {"message": "Unprocessable"}, EXIT_STATE_REFUSED),
    ("gh-500", "github", 500, {"message": "boom"}, EXIT_HOST_ERROR),
    ("gh-302", "github", 302, None, EXIT_HOST_ERROR),
    ("fj-404", "forgejo", 404, {"message": "not found"}, EXIT_RUN_NOT_FOUND),
    ("fj-403", "forgejo", 403, {"message": "forbidden"}, EXIT_FORBIDDEN),
    ("fj-500", "forgejo", 500, {"message": "boom"}, EXIT_HOST_ERROR),
]


@pytest.mark.parametrize(
    "platform,status,body,code", [row[1:] for row in _ERROR_ROWS], ids=[row[0] for row in _ERROR_ROWS]
)
def test_host_error_exits_with_its_named_code_after_one_post(platform, status, body, code, capsys):
    # A host that echoes the credential back must still not get it printed.
    echoing = dict(body, message=f"{body['message']} {_TOKEN}") if body else None
    host = _Host(
        _gh_routes(status, echoing, suffix="cancel")
        if platform == "github"
        else _fj_routes(status, echoing)
    )

    rc = _call("cancel", [], host, platform=platform, caller="some-role")

    assert rc == code
    assert len(host.posts()) == 1
    captured = capsys.readouterr()
    assert _TOKEN not in captured.err and _TOKEN not in captured.out
    if status != 302:
        assert str(status) in captured.err
    if code == EXIT_FORBIDDEN:
        assert "some-role" in captured.err
    if code == EXIT_STATE_REFUSED:
        assert "ci cancel" in captured.err and "ci rerun" in captured.err


def test_unreadable_run_does_not_stop_the_operation():
    routes = _gh_routes(202, None, suffix="cancel")
    routes[("GET", _GH_RUN)] = (404, {"message": "Not Found"})
    host, sink = _Host(routes), _Sink()

    rc = _call("cancel", [], host, sink=sink)

    assert rc == EXIT_OK
    assert sink.records[0]["head_sha"] is None


def test_no_credential_exits_before_any_request():
    host = _Host({})

    assert _call("cancel", [], host, tokens=_NoTokens()) == EXIT_TOKEN_FETCH_FAILED
    assert host.requests == []


_USAGE_ROWS = [
    ("bad-repo", {"repo": "no-slash"}),
    ("zero-run", {"run": "0"}),
    ("non-numeric-run", {"run": "abc"}),
]


@pytest.mark.parametrize("override", [row[1] for row in _USAGE_ROWS], ids=[row[0] for row in _USAGE_ROWS])
def test_usage_errors_exit_usage_without_a_request(override):
    host = _Host({})

    assert _call("cancel", [], host, **override) == EXIT_USAGE
    assert host.requests == []


def test_caller_that_is_not_the_attested_identity_is_refused_before_any_request():
    host = _Host({})
    argv = ["--caller", "some-role", "--platform", "github", "--repo", _REPO, "--run", "1"]

    rc = ci_verb.main(
        argv, operation="cancel", token_provider=_Tokens(), opener=host,
        identity_provider=lambda: attestation.Identity("another-role", attestation.SOURCE_CONFIGURED),
    )

    assert rc == EXIT_CALLER_INVOKER_MISMATCH
    assert host.requests == []


# --- recording ----------------------------------------------------------------

_EVENT_FIELDS = {
    "schema", "verb", "operation", "caller", "platform", "repo", "run_id", "head_sha",
    "http_status", "outcome", "ts",
}

_RECORD_ROWS = [
    # id, platform, verb, extra, host status, expected operation, http status, outcome
    ("ok", "github", "rerun", ["--failed-jobs"], 201, "rerun-failed-jobs", 201, "ok"),
    ("forbidden", "github", "cancel", [], 403, "cancel", 403, "forbidden"),
    ("state-refused", "github", "rerun", [], 409, "rerun", 409, "state_refused"),
    ("not-supported", "forgejo", "rerun", [], None, "rerun", 0, "not_supported"),
]


@pytest.mark.parametrize(
    "platform,verb,extra,status,operation,http_status,outcome",
    [row[1:] for row in _RECORD_ROWS],
    ids=[row[0] for row in _RECORD_ROWS],
)
def test_every_call_emits_one_complete_record_without_the_token(
    platform, verb, extra, status, operation, http_status, outcome
):
    routes = {} if status is None else _gh_routes(status, {"message": "x"}, suffix=operation)
    sink = _Sink()

    _call(verb, extra, _Host(routes), platform=platform, sink=sink)

    assert len(sink.records) == 1
    record = sink.records[0]
    assert set(record) == _EVENT_FIELDS
    assert record["verb"] == "ci" and record["operation"] == operation
    assert record["caller"] == "some-role" and record["platform"] == platform
    assert record["repo"] == _REPO and record["run_id"] == _RUN
    assert record["http_status"] == http_status and record["outcome"] == outcome
    assert record["head_sha"] == (None if status is None else _HEAD)
    assert record["ts"].endswith("Z")
    assert _TOKEN not in json.dumps(record)


def test_record_of_a_missing_credential_has_no_status():
    sink = _Sink()

    _call("cancel", [], _Host({}), tokens=_NoTokens(), sink=sink)

    assert sink.records[0]["outcome"] == "token_failed" and sink.records[0]["http_status"] == 0


def test_default_sink_writes_nothing_and_the_call_succeeds(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", raising=False)
    monkeypatch.setenv("CLAGENTIC_LOADOUT_TELEMETRY_DIR", str(tmp_path / "telemetry"))
    host = _Host(_gh_routes(202, None, suffix="cancel"))

    rc = ci_verb.main(
        ["--caller", "some-role", "--platform", "github", "--repo", _REPO, "--run", str(_RUN)],
        operation="cancel", token_provider=_Tokens(), opener=host,
    )

    assert rc == EXIT_OK
    assert not (tmp_path / "telemetry").exists()


# --- which role holds the verb --------------------------------------------------

def test_only_the_merger_role_holds_the_verb_by_default():
    assert "ci" in KNOWN_VERBS
    assert roles_holding_verb("ci") == frozenset({"merger"})
    assert all("ci" not in DEFAULT_ROLE_VERBS[role] for role in ("builder", "reviewer", "lead"))


def test_a_repo_roles_section_moves_the_verb(tmp_path: Path):
    config = tmp_path / ".clagentic" / "loadout"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text("roles:\n  zorbnaut:\n    - ci\n", encoding="utf-8")

    assert load_role_verbs(tmp_path) == {"zorbnaut": ("ci",)}
    assert roles_holding_verb("ci", repo_root=tmp_path) == frozenset({"zorbnaut"})


def test_the_allowlist_fragment_for_the_verb_names_the_umbrella_command():
    assert generate_role_fragment("merger", ("ci",)) == [
        "Bash(clagentic-loadout ci *)",
        "Bash(clagentic-loadout ci:*)",
    ]
