"""test_workflow_dispatch.py -- loadout-workflow-dispatch.

No real network and no real credential: every call goes through an injected
opener, token provider and identity provider, with synthetic repos, workflows
and roles. The refusal tests use a transport and a provider that fail the test
on any use, proving the allow-list check runs before either.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clagentic_loadout.transport.attestation import Identity
from clagentic_loadout.transport.credential_provider import CredentialProviderError
from clagentic_loadout.workflow import dispatch_config, dispatch_verb

_REPO = "some-owner/some-repo"
_CONFIG = """\
workflow_dispatch:
  - workflow: build.yml
    refs: [main, release/1.x]
  - workflow: deploy.yaml
    refs: [main]
"""


class _FailOnAnyCall:
    def __call__(self, req, timeout=15):
        raise AssertionError(f"no network call may happen, got {req.get_method()} {req.full_url}")


class _FailOnAnyToken:
    def resolve_token(self, role: str, repo: str | None = None) -> str:
        raise AssertionError(f"no credential may be minted, got role={role!r}")


class _Token:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None]] = []

    def resolve_token(self, role: str, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return "tok-synthetic"


class _Response:
    def __init__(self, status: int, body: object = None) -> None:
        self.status = status
        self._raw = b"" if body is None else json.dumps(body).encode("utf-8")
        self.headers = {}

    def read(self):
        return self._raw

    def getcode(self):
        return self.status

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _Host:
    """Scripted GitHub: records every request, answers dispatch and runs reads."""

    def __init__(self, *, dispatch=None, runs=None, runs_error=False) -> None:
        self.dispatch = dispatch if dispatch is not None else (204, None)
        self.runs = runs
        self.runs_error = runs_error
        self.requests: list[tuple[str, str, dict | None, str]] = []

    def __call__(self, req, timeout=15):
        body = json.loads(req.data) if req.data else None
        auth = req.get_header("Authorization")
        self.requests.append((req.get_method(), req.full_url, body, auth))
        if req.get_method() == "POST":
            status, payload = self.dispatch
            if status >= 400:
                raise urllib.error.HTTPError(
                    req.full_url, status, "err", {}, _Response(status, payload)
                )
            return _Response(status, payload)
        if self.runs_error:
            raise urllib.error.URLError("runs read failed")
        return _Response(200, {"workflow_runs": self.runs or []})


def _identity(subject: str):
    return lambda: Identity(subject, "builtin")


def _git_origin(path: Path, url: str) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "remote", "add", "origin", url], check=True)


def _repo(
    tmp_path: Path,
    config: str | None = _CONFIG,
    origin: str | None = f"https://github.com/{_REPO}.git",
) -> Path:
    if origin is not None:
        _git_origin(tmp_path, origin)
    if config is not None:
        cfg = tmp_path / ".clagentic" / "loadout"
        cfg.mkdir(parents=True)
        (cfg / "config.yaml").write_text(config, encoding="utf-8")
    return tmp_path


def _argv(repo_path: Path, *extra: str, workflow="build.yml", ref="main") -> list[str]:
    return [
        "--caller", "role-a", "--workflow", workflow, "--ref", ref,
        "--repo-path", str(repo_path), *extra,
    ]


def _run(argv, *, host=None, tokens=None, caller="role-a"):
    return dispatch_verb.main(
        argv,
        token_provider=tokens if tokens is not None else _Token(),
        opener=host if host is not None else _Host(),
        identity_provider=_identity(caller),
    )


def _now_iso(offset_seconds: int = 0) -> str:
    return datetime.fromtimestamp(
        datetime.now(timezone.utc).timestamp() + offset_seconds, timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- success path ---------------------------------------------------------


def test_dispatch_posts_ref_and_inputs_and_reports_newest_run(tmp_path, capsys):
    repo = _repo(tmp_path)
    run = {"id": 42, "html_url": "https://example.test/runs/42", "created_at": _now_iso(2)}
    host, tokens = _Host(runs=[run]), _Token()

    rc = _run(_argv(repo, "--input", "channel=stable", "--input", "note=a=b"), host=host, tokens=tokens)

    assert rc == 0
    method, url, body, auth = host.requests[0]
    assert method == "POST"
    assert url == (
        "https://api.github.com/repos/some-owner/some-repo/actions/workflows/build.yml/dispatches"
    )
    assert body == {"ref": "main", "inputs": {"channel": "stable", "note": "a=b"}}
    assert auth == "token tok-synthetic"
    assert tokens.calls == [("role-a", _REPO)]
    out = json.loads(capsys.readouterr().out)
    assert out["workflow"] == "build.yml" and out["ref"] == "main"
    assert out["dispatch"] == {"accepted": True, "status": 204}
    assert out["run"] == {"id": 42, "url": "https://example.test/runs/42"}
    assert out["run_lookup"] == "newest_run"


def test_no_inputs_sends_no_inputs_key(tmp_path):
    host = _Host()
    assert _run(_argv(_repo(tmp_path)), host=host) == 0
    assert host.requests[0][2] == {"ref": "main"}


def test_at_most_one_read_after_dispatch(tmp_path):
    host = _Host(runs=[])
    assert _run(_argv(_repo(tmp_path)), host=host) == 0
    assert [r[0] for r in host.requests] == ["POST", "GET"]
    assert "event=workflow_dispatch" in host.requests[1][1]
    assert "branch=main" in host.requests[1][1]
    assert "per_page=1" in host.requests[1][1]


def test_run_details_in_dispatch_response_skip_the_read(tmp_path, capsys):
    body = {"workflow_run_id": 7, "html_url": "https://example.test/runs/7"}
    host = _Host(dispatch=(200, body))
    assert _run(_argv(_repo(tmp_path)), host=host) == 0
    assert [r[0] for r in host.requests] == ["POST"]
    out = json.loads(capsys.readouterr().out)
    assert out["run"] == {"id": 7, "url": "https://example.test/runs/7"}
    assert out["run_lookup"] == "dispatch_response"


def test_stale_run_is_not_reported_as_this_dispatch(tmp_path, capsys):
    old = {"id": 1, "html_url": "https://example.test/runs/1", "created_at": "2020-01-01T00:00:00Z"}
    assert _run(_argv(_repo(tmp_path)), host=_Host(runs=[old])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"] is None and out["run_lookup"] == "not_yet_visible"


def test_run_created_just_before_the_dispatch_is_not_attributed(tmp_path, capsys):
    # Inside the old five-second skew window: an earlier dispatch must not be
    # named as this one.
    earlier = {"id": 2, "html_url": "https://example.test/runs/2", "created_at": _now_iso(-2)}
    assert _run(_argv(_repo(tmp_path)), host=_Host(runs=[earlier])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"] is None and out["run_lookup"] == "not_yet_visible"
    assert "not yet visible" in out["note"]


def test_run_created_after_the_dispatch_is_attributed_without_a_note(tmp_path, capsys):
    later = {"id": 3, "html_url": "https://example.test/runs/3", "created_at": _now_iso(2)}
    assert _run(_argv(_repo(tmp_path)), host=_Host(runs=[later])) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["run"] == {"id": 3, "url": "https://example.test/runs/3"}
    assert "note" not in out


def test_failed_run_read_does_not_fail_an_accepted_dispatch(tmp_path, capsys):
    assert _run(_argv(_repo(tmp_path)), host=_Host(runs_error=True)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["dispatch"]["accepted"] is True
    assert out["run"] is None and out["run_lookup"] == "unavailable"


def test_listed_slash_ref_is_accepted(tmp_path):
    host = _Host()
    assert _run(_argv(_repo(tmp_path), ref="release/1.x"), host=host) == 0
    assert host.requests[0][2]["ref"] == "release/1.x"


# --- refusal path: no network call, no credential ---------------------------


@pytest.mark.parametrize(
    "config,workflow,ref,expected_code,fragment",
    [
        (_CONFIG, "other.yml", "main", dispatch_verb.EXIT_NOT_LISTED, "other.yml"),
        (_CONFIG, "build.yml", "dev", dispatch_verb.EXIT_NOT_LISTED, "'dev'"),
        (_CONFIG, "deploy.yaml", "release/1.x", dispatch_verb.EXIT_NOT_LISTED, "release/1.x"),
        (None, "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "no repo config file"),
        ("roles: {}\n", "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "workflow_dispatch"),
        ("workflow_dispatch: build.yml\n", "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "must be a list"),
        ("workflow_dispatch: [x]\n", "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "workflow_dispatch[0]"),
        (
            "workflow_dispatch:\n  - {workflow: build.yml, refs: []}\n",
            "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "workflow_dispatch[0].refs",
        ),
        (
            "workflow_dispatch:\n  - {workflow: sub/build.yml, refs: [main]}\n",
            "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "workflow_dispatch[0].workflow",
        ),
        (
            "workflow_dispatch:\n  - {workflow: build.yml, refs: [main, 'a b']}\n",
            "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "refs[1]",
        ),
        (
            "workflow_dispatch:\n  - {workflow: build.yml, refs: [main], extra: 1}\n",
            "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "unknown key",
        ),
        ("workflow_dispatch: [\n", "build.yml", "main", dispatch_verb.EXIT_CONFIG_INVALID, "YAML"),
    ],
)
def test_unlisted_or_bad_config_refuses_before_any_call(
    tmp_path, capsys, config, workflow, ref, expected_code, fragment
):
    repo = _repo(tmp_path, config)

    rc = _run(
        _argv(repo, workflow=workflow, ref=ref),
        host=_FailOnAnyCall(),
        tokens=_FailOnAnyToken(),
    )

    assert rc == expected_code
    assert fragment in capsys.readouterr().err


@pytest.mark.parametrize(
    "origin",
    [
        "git@github.com:Some-Owner/Some-Repo.git",
        f"https://github.com/{_REPO}",
        f"https://github.com/{_REPO}/",
        f"ssh://git@github.com/{_REPO}.git",
        f"https://GitHub.com/{_REPO}.git",
    ],
)
def test_github_origin_forms_are_accepted_and_target_the_origin(tmp_path, origin):
    host = _Host()
    assert _run(_argv(_repo(tmp_path, origin=origin)), host=host) == 0
    assert "/repos/some-owner/some-repo/actions/workflows/" in host.requests[0][1].lower()


def test_post_url_owner_and_repo_come_from_origin_only(tmp_path):
    host, tokens = _Host(), _Token()
    repo = _repo(tmp_path, origin="https://github.com/origin-owner/origin-repo.git")
    assert _run(_argv(repo), host=host, tokens=tokens) == 0
    assert host.requests[0][1] == (
        "https://api.github.com/repos/origin-owner/origin-repo/actions/workflows/build.yml/dispatches"
    )
    assert tokens.calls == [("role-a", "origin-owner/origin-repo")]


def test_subdirectory_of_the_checkout_uses_the_top_level(tmp_path):
    repo = _repo(tmp_path)
    sub = repo / "pkg" / "inner"
    sub.mkdir(parents=True)
    host = _Host()
    assert _run(_argv(sub), host=host) == 0
    assert "/repos/some-owner/some-repo/" in host.requests[0][1]


@pytest.mark.parametrize(
    "origin",
    [
        "https://git-host.example.com/some-owner/some-repo.git",
        "https://github.com@git-host.example.com/some-owner/some-repo.git",
        "https://notgithub.com/some-owner/some-repo.git",
        "https://github.com.example.com/some-owner/some-repo.git",
        "https://api.github.com/some-owner/some-repo.git",
    ],
)
def test_origin_on_another_host_is_refused_with_no_call(tmp_path, capsys, origin):
    repo = _repo(tmp_path, origin=origin)
    rc = _run(_argv(repo), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_UNSUPPORTED_PLATFORM
    err = capsys.readouterr().err
    assert "GitHub only" in err and "Nothing was sent" in err


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "not-a-remote-url",
        "git@github.com@git-host.example.com:some-owner/some-repo.git",
        "https://github.com/only-owner",
        "https://github.com/a/b/c",
        "https://github.com:8443/some-owner/some-repo.git",
        "https://github.com/some-owner/../some-repo.git",
        "file:///srv/some-owner/some-repo.git",
    ],
)
def test_underivable_origin_is_refused_with_no_call(tmp_path, capsys, origin):
    repo = _repo(tmp_path, origin=origin)
    rc = _run(_argv(repo), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_USAGE
    assert "Nothing was sent" in capsys.readouterr().err


def test_no_git_tree_is_refused_with_no_call(tmp_path, capsys):
    rc = _run(_argv(tmp_path), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_USAGE
    assert "not inside a git working tree" in capsys.readouterr().err


def test_tracking_remote_on_github_does_not_rescue_a_foreign_origin(tmp_path, capsys):
    repo = _repo(tmp_path, origin="https://git-host.example.com/some-owner/some-repo.git")
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "upstream", f"https://github.com/{_REPO}.git"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "work"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "branch.work.remote", "upstream"], check=True)
    rc = _run(_argv(repo), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_UNSUPPORTED_PLATFORM
    assert "git-host.example.com" in capsys.readouterr().err


def test_config_only_in_the_wrapper_parent_is_not_used(tmp_path, capsys):
    wrapper_cfg = tmp_path / ".clagentic" / "loadout"
    wrapper_cfg.mkdir(parents=True)
    (wrapper_cfg / "config.yaml").write_text(_CONFIG, encoding="utf-8")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    _repo(checkout, config=None)
    rc = _run(_argv(checkout), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_CONFIG_INVALID
    err = capsys.readouterr().err
    assert "outside" in err and "Nothing was sent" in err


def test_repo_and_platform_flags_no_longer_exist(tmp_path):
    repo = _repo(tmp_path)
    for extra in (["--repo", _REPO], ["--platform", "github"]):
        assert _run(_argv(repo, *extra), host=_FailOnAnyCall(), tokens=_FailOnAnyToken()) != 0


def test_caller_mismatch_is_refused_before_credential(tmp_path, capsys):
    rc = _run(
        _argv(_repo(tmp_path)),
        host=_FailOnAnyCall(),
        tokens=_FailOnAnyToken(),
        caller="someone-else",
    )
    assert rc == dispatch_verb.EXIT_CALLER_INVOKER_MISMATCH
    assert "role-a" in capsys.readouterr().err


# --- host responses ---------------------------------------------------------


def test_403_names_the_missing_permission(tmp_path, capsys):
    host = _Host(dispatch=(403, {"message": "Resource not accessible by integration"}))
    rc = _run(_argv(_repo(tmp_path)), host=host)
    assert rc == dispatch_verb.EXIT_PERMISSION_DENIED
    err = capsys.readouterr().err
    assert "actions: write" in err
    assert "role-a" in err and _REPO in err
    assert "Resource not accessible by integration" in err
    assert "tok-synthetic" not in err


@pytest.mark.parametrize("status", [401, 404, 422, 500])
def test_other_failures_exit_dispatch_failed_with_resolved_values(tmp_path, capsys, status):
    host = _Host(dispatch=(status, {"message": "host said no"}))
    rc = _run(_argv(_repo(tmp_path)), host=host)
    assert rc == dispatch_verb.EXIT_DISPATCH_FAILED
    err = capsys.readouterr().err
    assert str(status) in err and "build.yml" in err and "'main'" in err and "host said no" in err


def test_network_failure_exits_dispatch_failed(tmp_path, capsys):
    def _down(req, timeout=15):
        raise urllib.error.URLError("unreachable")

    rc = _run(_argv(_repo(tmp_path)), host=_down)
    assert rc == dispatch_verb.EXIT_DISPATCH_FAILED
    assert "unreachable" in capsys.readouterr().err


def test_token_failure_exits_token_fetch_failed(tmp_path):
    class _NoCreds:
        def resolve_token(self, role, repo=None):
            raise CredentialProviderError("no credential configured")

    rc = _run(_argv(_repo(tmp_path)), host=_FailOnAnyCall(), tokens=_NoCreds())
    assert rc == dispatch_verb.EXIT_TOKEN_FETCH_FAILED


# --- argument handling --------------------------------------------------------


@pytest.mark.parametrize("bad", ["novalue", "=x", "1bad=x", "a b=x"])
def test_malformed_input_is_a_usage_error(tmp_path, capsys, bad):
    rc = _run(_argv(_repo(tmp_path), "--input", bad), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_USAGE
    assert repr(bad) in capsys.readouterr().err


def test_duplicate_input_key_is_a_usage_error(tmp_path, capsys):
    rc = _run(
        _argv(_repo(tmp_path), "--input", "k=1", "--input", "k=2"),
        host=_FailOnAnyCall(),
        tokens=_FailOnAnyToken(),
    )
    assert rc == dispatch_verb.EXIT_USAGE
    assert "'k'" in capsys.readouterr().err


@pytest.mark.parametrize("workflow", ["../x.yml", "dir/x.yml", "x.sh", "x.yml?a=b"])
def test_bad_workflow_name_is_a_usage_error(tmp_path, workflow):
    rc = _run(_argv(_repo(tmp_path), workflow=workflow), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_USAGE


def test_missing_required_argument_is_a_usage_error():
    assert dispatch_verb.main(["--workflow", "build.yml"]) != 0


def test_allowlist_matching_is_exact_not_glob(tmp_path):
    repo = _repo(tmp_path, "workflow_dispatch:\n  - {workflow: build.yml, refs: ['release/*']}\n")
    rc = _run(_argv(repo, ref="release/1.x"), host=_FailOnAnyCall(), tokens=_FailOnAnyToken())
    assert rc == dispatch_verb.EXIT_NOT_LISTED


def test_loader_returns_entries(tmp_path):
    entries = dispatch_config.load_dispatch_allowlist(_repo(tmp_path))
    assert [(e.workflow, e.refs) for e in entries] == [
        ("build.yml", ("main", "release/1.x")),
        ("deploy.yaml", ("main",)),
    ]
