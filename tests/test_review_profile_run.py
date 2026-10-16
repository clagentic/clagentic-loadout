"""`loadout-review run` and `post` with the repo profile: the class rules become
findings with facts, a missing or invalid profile is a finding that cannot block,
and the deployment's own denial list comes from user-level configuration. Every
run is the real pipeline against a fake host and a real local repository."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review.evaluation import context_from_config
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.profile_settings import ProfileSettings
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import Env, write_stub
from tests._review_evaluate_support import commit_files

HOST = "host-one.example.test"
NAME = "agent-alpha"


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path, seed_profile=False)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _added(name: str, *lines: str) -> str:
    return "\n".join(
        [
            f"diff --git a/{name} b/{name}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{name}",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    ) + "\n"


def _configure(env: Env, *, deny=None, evaluator=None, carrier_mode="empty") -> None:
    """The user-level config: a stub carrier, and optionally the deployment's
    repo profile settings and evaluator threshold."""
    carrier = write_stub(env.stubs, "carrier", carrier_mode)
    review: dict = {"profiles": {"reviewer": {"carrier": carrier, "parallel": 1, "timeout_seconds": 10}}}
    if deny is not None:
        review["repo_profile"] = {"deny": deny}
    if evaluator is not None:
        review["evaluator"] = evaluator
    env.cfg.mkdir(parents=True, exist_ok=True)
    (env.cfg / "config.yaml").write_text(yaml.safe_dump({"review": review}), encoding="utf-8")


def _document(payload: dict) -> dict:
    return json.loads(Path(payload["findings_file"]).read_text(encoding="utf-8"))


def _rules(document: dict) -> list[tuple[str, str, int]]:
    return [(f["rule_id"], f["file"], f["line"]) for f in document["findings"]]


def test_a_repo_without_a_profile_gets_a_missing_profile_finding_that_does_not_block(env, capsys):
    _configure(env)

    code, payload = env.run(capsys=capsys)

    assert code == 0
    document = _document(payload)
    assert _rules(document) == [(pr.RULE_MISSING, PROFILE_PATH, 1)]
    (finding,) = document["findings"]
    assert (finding["blocking"], finding["computed_severity"]) == (False, "none")
    assert document["verdict_outcome"] == payload["verdict_outcome"] == "CLEAN"
    assert payload["blocking_count"] == 0


def test_the_missing_profile_finding_is_posted_and_the_posted_verdict_is_clean(env, capsys):
    _configure(env)
    _, payload = env.run(capsys=capsys)

    code, posted = env.post("--findings", payload["findings_file"], "--status", "clean", capsys=capsys)

    assert code == 0, posted
    assert posted["verdict_outcome"] == "CLEAN" and posted["status"] == "clean"
    body = env.opener_state["posted_body"]
    assert "Advisory findings (1):" in body and pr.RULE_MISSING in body
    fence = parse_verdict_block(body)
    assert fence["verdict_outcome"] == "CLEAN"
    assert "findings_open" not in fence


def test_an_invalid_profile_is_a_finding_naming_the_problem_and_does_not_block(env, capsys):
    _configure(env)
    base = commit_files(env.repo, {PROFILE_PATH: "class: not-a-class\n"})

    _, payload = env.run(capsys=capsys, base_sha=base)

    document = _document(payload)
    assert [r for r, _, _ in _rules(document)] == [pr.RULE_INVALID]
    assert "class must be one of" in document["findings"][0]["message"]
    assert document["verdict_outcome"] == "CLEAN"


def test_a_valid_profile_with_a_clean_diff_has_no_profile_finding(env, capsys):
    _configure(env)
    base = commit_files(env.repo, {PROFILE_PATH: "class: released-tool\n"})

    _, payload = env.run(capsys=capsys, base_sha=base)

    assert _document(payload)["findings"] == []
    assert payload["verdict_outcome"] == "CLEAN"


def test_a_profile_added_only_in_the_change_under_review_does_not_apply(env, capsys):
    _configure(env)
    commit_files(env.repo, {PROFILE_PATH: "class: released-tool\n"})

    _, payload = env.run(capsys=capsys)

    assert [r for r, _, _ in _rules(_document(payload))] == [pr.RULE_MISSING]


def test_a_hostname_the_deployment_denies_is_reported_when_a_released_tool_diff_adds_it(env, capsys):
    _configure(env, deny={"hosts": [HOST]})
    base = commit_files(env.repo, {PROFILE_PATH: "class: released-tool\n"})
    env.diff = _added("src/client.py", "import os", f"URL = 'https://{HOST}/api'")

    code, payload = env.run(capsys=capsys, base_sha=base)

    assert code == 0
    document = _document(payload)
    assert _rules(document) == [(pr.RULE_DENIED, "src/client.py", 2)]
    (finding,) = document["findings"]
    assert HOST in finding["message"]
    assert finding["facts_state"] == "complete"
    assert finding["fingerprint"].startswith("fp2:")
    # Held at low by its facts: advisory under the default threshold.
    assert (finding["computed_severity"], finding["blocking"]) == ("low", False)
    assert document["verdict_outcome"] == "CLEAN"


def test_an_agent_name_the_profile_itself_denies_is_reported(env, capsys):
    _configure(env)
    base = commit_files(
        env.repo, {PROFILE_PATH: f"class: released-tool\ndeny:\n  names: [{NAME}]\n"}
    )
    env.diff = _added("docs/notes.md", f"Written by {NAME}.")

    _, payload = env.run(capsys=capsys, base_sha=base)

    assert _rules(_document(payload)) == [(pr.RULE_DENIED, "docs/notes.md", 1)]


def test_the_same_diff_under_an_internal_profile_is_not_reported(env, capsys):
    _configure(env, deny={"hosts": [HOST]})
    base = commit_files(env.repo, {PROFILE_PATH: "class: internal\n"})
    env.diff = _added("src/client.py", f"URL = 'https://{HOST}/api'")

    _, payload = env.run(capsys=capsys, base_sha=base)

    assert _document(payload)["findings"] == []


def test_a_threshold_of_low_makes_a_rule_hit_block_and_the_post_records_it(env, capsys):
    _configure(env, deny={"hosts": [HOST]}, evaluator={"block_at": "low"})
    base = commit_files(env.repo, {PROFILE_PATH: "class: released-tool\n"})
    env.diff = _added("src/client.py", f"URL = 'https://{HOST}/api'")

    _, payload = env.run(capsys=capsys, base_sha=base)
    assert payload["verdict_outcome"] == "BLOCKED"
    code, posted = env.post(
        "--findings", payload["findings_file"], "--status", "blocking", capsys=capsys, base_sha=base
    )

    assert code == 0, posted
    assert parse_verdict_block(env.opener_state["posted_body"])["verdict_outcome"] == "BLOCKED"


def test_the_policy_is_read_through_the_host_when_no_local_repository_is_named(env, capsys):
    _configure(env, deny={"hosts": [HOST]})
    env.diff = _added("src/client.py", f"URL = 'https://{HOST}/api'")
    files = {"README.md": "base\n", PROFILE_PATH: "class: released-tool\n"}

    _, payload = env.run(capsys=capsys, local_repo=False, base_files=files)

    assert _rules(_document(payload)) == [(pr.RULE_DENIED, "src/client.py", 1)]


def test_an_unreadable_base_revision_is_degraded_and_states_no_profile_finding(env, capsys):
    _configure(env)

    _, payload = env.run(capsys=capsys, base_sha="e" * 40)

    document = _document(payload)
    assert document["findings"] == []
    assert document["verdict_outcome"] == "DEGRADED"


@pytest.mark.parametrize(
    "settings, key_path",
    [
        ({"deny": {"names": "not-a-list"}}, "review.repo_profile.deny.names"),
        ({"require": True}, "review.repo_profile.require"),
        ({"strict": True}, "review.repo_profile.strict"),
    ],
    ids=["deny", "removed-require", "unknown"],
)
def test_malformed_deployment_settings_stop_run_before_any_model_is_called(
    env, capsys, settings, key_path
):
    _configure(env)
    config = yaml.safe_load((env.cfg / "config.yaml").read_text(encoding="utf-8"))
    config["review"]["repo_profile"] = settings
    (env.cfg / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")

    code, _, err = env.invoke("run", capsys=capsys)

    assert code == review_cli.EXIT_PROFILE_INVALID
    assert key_path in err


def test_malformed_settings_cannot_reach_the_evaluator_setup_the_merge_gate_uses(env):
    _configure(env, deny={"names": "not-a-list"})

    context = context_from_config(env.cfg)

    assert context.profile_settings == ProfileSettings()


def test_a_resumed_run_states_the_same_profile_findings(env, tmp_path, capsys):
    _configure(env, deny={"hosts": [HOST]})
    base = commit_files(env.repo, {PROFILE_PATH: "class: released-tool\n"})
    env.diff = _added("src/client.py", f"URL = 'https://{HOST}/api'")

    _, first = env.run("--out", str(tmp_path / "one"), capsys=capsys, base_sha=base)
    _, second = env.run("--out", str(tmp_path / "one"), capsys=capsys, base_sha=base)

    assert _rules(_document(first)) == _rules(_document(second)) != []
