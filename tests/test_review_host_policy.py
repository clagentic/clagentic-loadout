"""`loadout-review run` and `post` read policy from the host API at the PR's base
revision when no --repo-path is given, as a reviewer run from a scratch
directory that is not a git repository does."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH, STAKES_PATH
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import Env

_DISPOSITION = """\
dispositions:
  - path: "a.py"
    rule_id: R1
    kind: false_positive
    rationale: "checked by hand"
    by: a-maintainer
    at: 2026-09-01
"""


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # Not a git repository, as a per-spawn reviewer directory is not.
    monkeypatch.chdir(scratch)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _document(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


def _run(env, capsys, **kwargs):
    _, payload = env.run(capsys=capsys, local_repo=False, **kwargs)
    return payload, _document(payload)


def test_a_run_from_a_directory_that_is_not_a_repository_reads_policy_from_the_host(env, capsys):
    env.configure(carrier_mode="facts_mild")

    payload, document = _run(env, capsys, base_files={"README.md": "base\n"})

    assert document["verdict_outcome"] == "CLEAN"
    assert document["verdict_reasons"] == []
    assert document["policy_base_sha"] == env.base_sha
    requests = [url for _, url in env.opener_state["requests"]]
    assert any(f"/git/trees/{env.base_sha}" in url for url in requests)


def test_post_succeeds_as_clean_without_a_checkout_when_all_findings_are_advisory(env, capsys):
    env.configure(carrier_mode="facts_mild")
    payload, _ = _run(env, capsys, base_files={"README.md": "base\n"})

    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "clean",
        capsys=capsys, local_repo=False, base_files={"README.md": "base\n"},
    )

    assert code == 0, posted
    assert posted["status"] == "clean"
    assert posted["verdict_outcome"] == "CLEAN"


def test_a_disposition_at_the_base_demotes_a_finding_without_a_local_checkout(env, capsys):
    env.configure(carrier_mode="facts_serious")
    files = {"README.md": "base\n", DISPOSITIONS_PATH: _DISPOSITION}

    payload, document = _run(env, capsys, base_files=files)

    (finding,) = document["findings"]
    assert finding["blocking"] is False
    assert finding["disposition"]["kind"] == "false_positive"
    assert document["verdict_outcome"] == "CLEAN"
    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "clean",
        capsys=capsys, local_repo=False, base_files=files,
    )
    assert code == 0, posted


def test_a_failing_contents_read_is_degraded_and_names_the_failure(env, capsys):
    env.configure(carrier_mode="facts_mild")
    files = {DISPOSITIONS_PATH: _DISPOSITION}

    payload, document = _run(env, capsys, base_files=files, contents_http=500)

    assert document["verdict_outcome"] == "DEGRADED"
    assert any(
        "dispositions ignored" in reason and "HTTP 500" in reason
        for reason in document["verdict_reasons"]
    )
    capsys.readouterr()
    code, _, err = env.invoke(
        "post", "--findings", payload["findings_file"], "--status", "clean",
        capsys=capsys, local_repo=False, base_files=files, contents_http=500,
    )
    assert code == review_cli.EXIT_FINDINGS_INVALID
    assert "DEGRADED" in err and "HTTP 500" in err
    assert env.opener_state["posted_body"] is None


def test_a_failing_tree_read_is_degraded_and_names_the_failure(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, document = _run(env, capsys, base_files={"README.md": "x"}, tree_http=502)

    assert document["verdict_outcome"] == "DEGRADED"
    assert any(
        "cannot read the file list" in reason and "HTTP 502" in reason
        for reason in document["verdict_reasons"]
    )


def test_a_truncated_tree_is_degraded_and_never_read_as_complete(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, document = _run(env, capsys, base_files={"README.md": "x"}, tree_truncated=True)

    assert document["verdict_outcome"] == "DEGRADED"
    assert any("truncated" in reason for reason in document["verdict_reasons"])


def test_a_malformed_policy_file_is_degraded_naming_the_cause(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, document = _run(env, capsys, base_files={STAKES_PATH: "profile: [unclosed\n"})

    assert document["verdict_outcome"] == "DEGRADED"
    assert any("not valid YAML" in reason for reason in document["verdict_reasons"])


def test_a_repository_with_no_policy_files_is_not_degraded_for_that_reason(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, document = _run(env, capsys, base_files={"src/a.py": "x = 1\n"})

    assert not any("policy" in reason for reason in document["verdict_reasons"])
    assert document["verdict_outcome"] == "CLEAN"


def test_an_explicit_repo_path_reads_the_local_repository_and_not_the_host(env, capsys):
    env.configure(carrier_mode="facts_mild")

    _, payload = env.run(capsys=capsys, local_repo=True)

    assert _document(payload)["verdict_outcome"] == "CLEAN"
    requests = [url for _, url in env.opener_state["requests"]]
    assert not any("/git/trees/" in url or "/contents/" in url for url in requests)


def test_a_context_with_no_policy_source_is_degraded_and_does_not_assume_the_current_directory():
    from clagentic_loadout.review.evaluation import EvaluationContext, evaluate_findings

    context = EvaluationContext(policy_base_sha="a" * 40, visibility="private")

    verdict = evaluate_findings([], "b" * 40, context)

    assert verdict.outcome == "DEGRADED"
    assert any("no policy source" in reason for reason in verdict.degraded)
