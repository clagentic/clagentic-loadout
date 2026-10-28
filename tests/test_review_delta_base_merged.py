"""A base merged into the branch since the last review, end to end through
`loadout-review run` on both platforms: the range from the last reviewed head
then carries the base's own changes, so the run reviews the pull request's own
diff in full and names the reason. A plain fast-forward still reviews a delta."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import Env, identity_provider, make_diff, prompts
from tests._support.review_host import PRIOR_HEAD, ReviewHost

#: What the pull request itself changes, and what the base brought in.
_OWN_FILE = "own.py"
_BASE_FILE = "from_base.py"


def _run(env: Env, host: ReviewHost, capsys):
    argv = [
        "run", "--caller", "reviewer", "--repo", "some-owner/some-repo",
        *host.platform_args(), "--repo-path", str(env.repo),
        "--prior-head-sha", PRIOR_HEAD,
    ]
    capsys.readouterr()
    code = review_cli.main(
        argv,
        token_provider=env.token_provider,
        opener=host.opener,
        identity_provider=identity_provider("reviewer"),
        config_root=env.cfg,
        run_root=env.runs,
    )
    out = capsys.readouterr().out
    lines = [line for line in out.strip().splitlines() if line.startswith("{")]
    return code, json.loads(lines[-1])


def _host(platform: str, env: Env, *, base_merged: bool) -> ReviewHost:
    return ReviewHost(
        platform,
        diff=make_diff({_OWN_FILE: 2}),
        base_sha=env.base_sha,
        files=[_OWN_FILE],
        # Merged, the range from the prior head holds the base's file as well.
        range_diff=make_diff({_OWN_FILE: 1, _BASE_FILE: 5} if base_merged else {_OWN_FILE: 1}),
        base_merged=base_merged,
    )


@pytest.fixture(params=["github", "forgejo"])
def platform(request, tmp_path, monkeypatch) -> str:
    monkeypatch.setenv("STUB_DIR", str(tmp_path / "stubs"))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return request.param


def _stage(result: dict, name: str) -> dict:
    return next(s for s in result["stages"] if s["stage"] == name)


def test_a_base_merged_into_the_branch_reviews_the_prs_own_diff_in_full(
    platform, tmp_path, capsys
):
    env = Env(tmp_path)
    env.configure()

    code, result = _run(env, _host(platform, env, base_merged=True), capsys)

    assert code == 0
    assert result["mode"] == "full"
    assert _stage(result, "delta")["reason"] == "BASE_MERGED"
    sent = "\n".join(prompts(env.stubs, "carrier"))
    assert _OWN_FILE in sent
    assert _BASE_FILE not in sent
    assert "Incremental review" not in sent


def test_a_fast_forward_without_a_base_merge_still_reviews_a_delta(platform, tmp_path, capsys):
    env = Env(tmp_path)
    env.configure()

    code, result = _run(env, _host(platform, env, base_merged=False), capsys)

    assert code == 0
    assert result["mode"] == "delta"
    assert _stage(result, "delta")["since_head"] == PRIOR_HEAD
    assert "Incremental review" in "\n".join(prompts(env.stubs, "carrier"))
