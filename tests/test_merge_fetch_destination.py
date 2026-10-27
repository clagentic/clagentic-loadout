# ruff: noqa: F811
# (the shared fixtures are imported into this namespace for pytest to register)
"""The merge verbs refuse a steered tracking remote and carry no ambient credential.

loadout-merge and loadout-post-merge fetch from the base branch's tracking remote,
which repo-local git config names. Each case runs the real entrypoint against a
real git repo, a throwaway HOME whose global config holds a credential helper, and
a local HTTP listener that answers 401 Basic and records every request and
Authorization header. The fetch primitives are covered in
`tests/test_merge_fetch_destination_units.py`.

  M1  GitHub / M2  Forgejo: a steered tracking remote (its url, a second fetch url,
      the branch's remote, or a repo-local insteadOf) refuses loadout-merge before
      the merge call and before any request
  M3  a repo-local insteadOf onto the listener (the `insteadof` case of M1/M2)
  M4  a GLOBAL url.insteadOf onto the listener is not applied: no request
  M5  loadout-post-merge with a steered remote is refused, no request
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.merge import post_merge_verb, verb
from tests._gate_repo import CONFIRMABLE_GITHUB_ORIGIN, git, init_gate_repo, make_origin_confirmable
from tests._support.gate_merge import run_gate_merge
from tests.test_merge_post_merge_verb import (
    _base_args as post_merge_args,
    _init_repo_with_origin as init_post_merge_repo,
    _make_github_opener as post_merge_github_opener,
    _make_opener as post_merge_forgejo_opener,
)
from tests.test_merge_verb_post_merge import (  # noqa: F401  (the autouse fixtures register here)
    _AllowingAuthorityProvider,
    _RecordingTokenProvider,
    _base_args,
    _init_repo_with_origin as init_merge_repo,
    _isolate_user_config_root,
    _make_github_opener,
    _make_opener,
    _reset_seeded_base,
)
from tests.test_push_guard_fetch_hermetic import AMBIENT, ambient_home, listener  # noqa: F401

_REPO_PATH_SUFFIX = "some-owner/some-repo.git"
#: What the merge verbs anchor to by default, per platform.
_ANCHOR_PREFIX = {"github": "https://github.com/", "forgejo": "http://127.0.0.1:3000/"}
_PLATFORMS = ["github", "forgejo"]
#: Ways a repo-local config entry steers the base branch's fetch to the listener.
_STEERINGS = ["url", "second-fetch-url", "branch-remote", "insteadof"]


def _steer(tree, kind: str, listener, platform: str) -> None:
    prefix = _ANCHOR_PREFIX[platform]
    if kind == "url":
        git(tree, "config", "remote.mirror.url", listener.url)
    elif kind == "second-fetch-url":
        git(tree, "remote", "set-url", "--add", "mirror", listener.url)
    elif kind == "branch-remote":
        git(tree, "config", "branch.main.remote", listener.url)
    elif kind == "insteadof":
        git(tree, "config", "remote.mirror.url", f"{prefix}{_REPO_PATH_SUFFIX}")
        git(tree, "config", f"url.{listener.base}.insteadOf", prefix)
    else:
        raise AssertionError(kind)


def _no_credential_reached(listener) -> bool:
    return listener.requests == [] and listener.authorizations == []


class _RecordingOpener:
    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list[tuple[str, str]] = []

    def __call__(self, req, timeout=30):
        self.calls.append((req.get_method(), req.full_url))
        return self._inner(req, timeout)

    def merged(self) -> bool:
        return any(url.endswith("/merge") and method in ("PUT", "POST") for method, url in self.calls)


def _merge_repo(tmp_path, platform: str):
    init_merge_repo(tmp_path)
    if platform == "github":
        make_origin_confirmable(tmp_path, url=CONFIRMABLE_GITHUB_ORIGIN)
    return tmp_path


def _run_merge(tree, platform: str):
    inner = _make_github_opener() if platform == "github" else _make_opener()
    opener = _RecordingOpener(inner)
    provider = _RecordingTokenProvider()
    code = verb.main(
        _base_args(**{"--repo-path": str(tree), "--platform": platform}),
        token_provider=provider,
        authority_provider=_AllowingAuthorityProvider(),
        opener=opener,
    )
    return code, opener, provider


@pytest.mark.parametrize("kind", _STEERINGS)
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_m1_m2_m3_a_steered_tracking_remote_refuses_the_merge_before_any_request(
    platform, kind, tmp_path, listener, ambient_home, capsys
):
    tree = _merge_repo(tmp_path, platform)
    _steer(tree, kind, listener, platform)

    code, opener, provider = _run_merge(tree, platform)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_USAGE, err
    assert not opener.merged()
    assert _no_credential_reached(listener)
    # The platform API was read to learn the base branch, nothing else.
    assert len(provider.resolved_for) == 1
    assert listener.url not in out + err and AMBIENT not in out + err
    assert "refusing before any git network call" in err


@pytest.mark.parametrize("kind", ["url", "branch-remote"])
def test_a_steered_remote_on_the_pre_checks_clone_path_exits_as_a_refused_destination(
    kind, tmp_path, listener, ambient_home, scratch_tmp, capsys
):
    """The PR head is not local, so the pre_checks merge-result clone must fetch
    it: a steered remote there is a refused destination (usage exit), not an
    unbuildable merge result (pre_checks exit)."""
    gate = {"required_reviewer_roles": [], "pre_checks": [{"cmd": ["true"], "on_failure": "fail"}]}
    repo = init_gate_repo(tmp_path / "shared", tracked_gate=gate)
    git(repo.path, "branch", "-q", "-D", "pr")
    git(repo.path, "reflog", "expire", "--expire=now", "--all")
    git(repo.path, "gc", "-q", "--prune=now")
    if kind == "url":
        git(repo.path, "remote", "add", "mirror", listener.url)
        git(repo.path, "config", "branch.main.remote", "mirror")
    else:
        git(repo.path, "config", "branch.main.remote", listener.url)
    merge_calls: list[str] = []

    code = run_gate_merge(repo, merge_calls)

    out, err = capsys.readouterr()
    assert code == verb.EXIT_USAGE, err
    assert code != verb.EXIT_PRE_CHECKS_FAILED
    assert "refusing before any git network call" in err
    assert "could not build the merge result" not in err
    assert merge_calls == []
    assert _no_credential_reached(listener)
    assert listener.url not in out + err and AMBIENT not in out + err


def test_a_differing_pushurl_is_not_a_fetch_destination(tmp_path, listener, ambient_home, capsys):
    """The merge verbs never push: a pushurl on the listener (or an ssh pushurl,
    the common shape) must neither refuse the merge nor be contacted."""
    tree = _merge_repo(tmp_path, "forgejo")
    git(tree, "config", "remote.mirror.pushurl", listener.url)

    code, _opener, _provider = _run_merge(tree, "forgejo")

    assert code == verb.EXIT_OK, capsys.readouterr().err
    assert _no_credential_reached(listener)


def test_m4_a_global_insteadof_onto_the_listener_is_not_applied(
    tmp_path, listener, ambient_home, capsys
):
    tree = _merge_repo(tmp_path, "github")
    git(tree, "config", "remote.mirror.url", f"https://github.com/{_REPO_PATH_SUFFIX}")
    # Keeps the run offline: a dead proxy scoped to the anchored host fails any
    # fetch of it fast, and does not match the rewritten listener url, so a fetch
    # that applied the rewrite would not be hidden.
    git(tree, "config", "http.https://github.com/.proxy", "http://127.0.0.1:1")
    subprocess.run(
        ["git", "config", "--file", str(ambient_home / ".gitconfig"),
         f"url.{listener.base}.insteadOf", "https://github.com/"],
        check=True,
    )

    code, opener, _provider = _run_merge(tree, "github")

    out, err = capsys.readouterr()
    # The destination is the anchored host (the fetch fails offline), not refused.
    assert opener.merged()
    assert code == verb.EXIT_POST_MERGE_FAILED, err
    assert _no_credential_reached(listener)
    assert AMBIENT not in out + err


@pytest.mark.parametrize("kind", _STEERINGS)
@pytest.mark.parametrize("platform", _PLATFORMS)
def test_m5_post_merge_with_a_steered_remote_is_refused_before_any_request(
    platform, kind, tmp_path, listener, ambient_home, capsys
):
    init_post_merge_repo(tmp_path)
    if platform == "github":
        make_origin_confirmable(tmp_path, url=CONFIRMABLE_GITHUB_ORIGIN)
    _steer(tmp_path, kind, listener, platform)
    head_before = git(tmp_path, "rev-parse", "HEAD")
    opener = post_merge_github_opener() if platform == "github" else post_merge_forgejo_opener()

    code = post_merge_verb.main(
        post_merge_args(str(tmp_path), **{"--platform": platform}),
        token_provider=_RecordingTokenProvider(),
        authority_provider=_AllowingAuthorityProvider(),
        opener=opener,
    )

    out, err = capsys.readouterr()
    assert code == post_merge_verb.EXIT_USAGE, err
    assert _no_credential_reached(listener)
    assert git(tmp_path, "rev-parse", "HEAD") == head_before
    assert git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
    assert listener.url not in out + err and AMBIENT not in out + err
