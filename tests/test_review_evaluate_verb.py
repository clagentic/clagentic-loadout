"""`loadout-review evaluate` end to end against a real git repository."""

from __future__ import annotations

import json
import textwrap

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import evaluate_verb as verb
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH, STAKES_PATH

from _review_evaluate_support import (
    HEAD,
    OTHER_HEAD,
    commit_files,
    facts,
    finding,
    make_repo,
)

BY_DESIGN_ENTRY = textwrap.dedent(
    """\
    dispositions:
      - path: "src/**"
        rule_id: R1
        kind: by_design
        rationale: documented behaviour
        by: a-maintainer
        at: 2099-01-01
    """
)
INTERNAL_PROFILE = "profile: svc\nprofiles:\n  svc:\n    exposure: internal\n    data_class: internal\n    merge_control: controlled\n"


def write_findings(path, items, **top):
    path.write_text(json.dumps({"head_sha": HEAD, "findings": items, **top}), encoding="utf-8")
    return str(path)


def call(tmp_path, repo, findings_path, base, *extra, capsys, config_root=None):
    capsys.readouterr()
    code = review_cli.main(
        [
            "evaluate", "--findings", findings_path, "--base", base, "--head", HEAD,
            "--repo-path", str(repo), *extra,
        ],
        config_root=config_root if config_root is not None else tmp_path / "no-config",
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def parsed(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


def serious() -> dict:
    return facts(integrity="full", precondition="none")


def test_blocked_exit_code_json_and_rendering(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE, "src/a.py": "x = 1\n"})
    items = write_findings(tmp_path / "f.json", [finding(fact=serious())])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    assert code == verb.EXIT_BLOCKED == 40
    body = parsed(out)
    assert body["outcome"] == "BLOCKED"
    assert body["blocking"][0]["clear_with"].startswith("dispositions:")
    assert "To clear it" in body["rendering"]


def test_base_disposition_demotes_and_the_finding_is_still_printed(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo, {STAKES_PATH: INTERNAL_PROFILE, DISPOSITIONS_PATH: BY_DESIGN_ENTRY, "src/a.py": "x\n"}
    )
    items = write_findings(tmp_path / "f.json", [finding(fact=serious())])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    body = parsed(out)
    assert code == 0 and body["outcome"] == "CLEAN"
    advisory = body["advisory"][0]
    assert advisory["disposition"]["kind"] == "by_design"
    assert advisory["blocking"] is False
    assert advisory["file"] == "src/a.py"


def test_same_entry_present_only_in_the_head_does_not_apply(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE, "src/a.py": "x\n"})
    commit_files(repo, {DISPOSITIONS_PATH: BY_DESIGN_ENTRY})
    items = write_findings(tmp_path / "f.json", [finding(fact=serious())])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    assert code == verb.EXIT_BLOCKED
    assert parsed(out)["blocking"][0]["disposition"] is None


def test_floor_finding_on_a_tree_exposed_path_stays_blocking_under_by_design(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo,
        {
            STAKES_PATH: INTERNAL_PROFILE,
            DISPOSITIONS_PATH: BY_DESIGN_ENTRY,
            "src/edge/ingress.yaml": "kind: Ingress\n",
            "src/edge/a.py": "x\n",
        },
    )
    floor = facts(category="injection", integrity="partial", precondition="has_host_access")
    items = write_findings(tmp_path / "f.json", [finding(file="src/edge/a.py", fact=floor)])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    body = parsed(out)
    assert code == verb.EXIT_BLOCKED
    only = body["blocking"][0]
    assert only["stakes"]["exposure"] == "exposed"
    assert only["disposition"] is None
    assert "cannot clear a security-floor finding" in only["disposition_refused"]
    assert "security floor forbids clearing" in body["rendering"]


def test_profile_claiming_internal_is_treated_as_exposed_when_the_tree_says_exposed(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo, {STAKES_PATH: INTERNAL_PROFILE, "src/ingress.yaml": "k: v\n", "src/a.py": "x\n"}
    )
    items = write_findings(tmp_path / "f.json", [finding(fact=serious())])
    _, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    assert parsed(out)["blocking"][0]["stakes"]["exposure"] == "exposed"


def test_expected_head_mismatch_is_stale(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    items = write_findings(tmp_path / "f.json", [])
    code, out, _ = call(
        tmp_path, repo, items, base, "--expected-head", OTHER_HEAD, capsys=capsys
    )
    assert code == verb.EXIT_STALE == 42
    assert parsed(out)["outcome"] == "STALE"


def test_findings_recorded_for_another_head_than_the_current_one_are_stale(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"head_sha": OTHER_HEAD, "findings": []}), encoding="utf-8")
    code, out, _ = call(tmp_path, repo, str(path), base, capsys=capsys)
    assert code == verb.EXIT_STALE


def test_missing_facts_is_degraded_naming_the_finding(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    items = write_findings(tmp_path / "f.json", [finding(file="src/q.py", line=9, rule_id="RQ")])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    body = parsed(out)
    assert code == verb.EXIT_DEGRADED == 41
    assert body["blocking"][0]["severity"] == "critical"
    assert any("src/q.py:9:RQ" in reason for reason in body["degraded"])


def test_bare_array_uses_the_head_argument(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    path = tmp_path / "f.json"
    path.write_text(json.dumps([finding(fact=facts())]), encoding="utf-8")
    code, out, _ = call(tmp_path, repo, str(path), base, capsys=capsys)
    assert code == 0
    assert parsed(out)["head_sha"] == HEAD


def test_text_format_prints_only_the_rendering(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    items = write_findings(tmp_path / "f.json", [])
    code, out, _ = call(tmp_path, repo, items, base, "--format", "text", capsys=capsys)
    assert code == 0
    assert out.startswith("CLEAN")


def test_unreadable_base_is_degraded_not_a_crash(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a": "1\n"})
    items = write_findings(tmp_path / "f.json", [])
    code, out, _ = call(tmp_path, repo, items, "e" * 40, capsys=capsys)
    assert code == verb.EXIT_DEGRADED
    assert parsed(out)["degraded"]


@pytest.mark.parametrize("flag", ["--base", "--head", "--expected-head"])
def test_non_sha_arguments_are_a_usage_error_naming_the_value(tmp_path, capsys, flag):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"a": "1\n"})
    items = write_findings(tmp_path / "f.json", [])
    capsys.readouterr()
    values = {"--base": base, "--head": HEAD, flag: "main"}
    argv = ["evaluate", "--findings", items, "--repo-path", str(repo)]
    for name, value in values.items():
        argv += [name, value]
    code = review_cli.main(argv, config_root=tmp_path / "no-config")
    assert code == verb.EXIT_USAGE
    assert "'main'" in capsys.readouterr().err


def test_invalid_findings_file_exits_32_with_the_resolved_path(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"a": "1\n"})
    path = tmp_path / "f.json"
    path.write_text("{not json", encoding="utf-8")
    code, _, err = call(tmp_path, repo, str(path), base, capsys=capsys)
    assert code == verb.EXIT_FINDINGS_INVALID == 32
    assert str(path) in err


def test_finding_missing_a_required_key_exits_32(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"a": "1\n"})
    path = tmp_path / "f.json"
    path.write_text(json.dumps({"head_sha": HEAD, "findings": [{"file": "a"}]}), encoding="utf-8")
    code, _, err = call(tmp_path, repo, str(path), base, capsys=capsys)
    assert code == 32 and "rule_id" in err


def _config(tmp_path, text):
    root = tmp_path / "cfg"
    root.mkdir()
    (root / "config.yaml").write_text(text, encoding="utf-8")
    return root


def test_block_at_comes_from_user_config(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    medium = facts(integrity="full", precondition="can_merge")
    items = write_findings(tmp_path / "f.json", [finding(fact=medium)])
    code, _, _ = call(tmp_path, repo, items, base, capsys=capsys)
    assert code == 0
    cfg = _config(tmp_path, "review:\n  evaluator:\n    block_at: medium\n")
    code, _, _ = call(tmp_path, repo, items, base, capsys=capsys, config_root=cfg)
    assert code == verb.EXIT_BLOCKED


def test_invalid_block_at_config_exits_3_naming_the_value(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {"a": "1\n"})
    items = write_findings(tmp_path / "f.json", [])
    cfg = _config(tmp_path, "review:\n  evaluator:\n    block_at: severe\n")
    code, _, err = call(tmp_path, repo, items, base, capsys=capsys, config_root=cfg)
    assert code == verb.EXIT_EVALUATOR_CONFIG_INVALID == 3
    assert "'severe'" in err


def test_repo_level_config_cannot_set_the_threshold(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo,
        {
            STAKES_PATH: INTERNAL_PROFILE,
            ".clagentic/loadout/config.yaml": "review:\n  evaluator:\n    block_at: critical\n",
        },
    )
    items = write_findings(tmp_path / "f.json", [finding(fact=serious())])
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys)
    assert code == verb.EXIT_BLOCKED
    assert parsed(out)["block_at"] == "high"


def test_required_scanner_failure_in_the_findings_file_is_degraded(tmp_path, capsys):
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    cfg = _config(tmp_path, "review:\n  evaluator:\n    required_scanners: [secret-scan]\n")
    items = write_findings(
        tmp_path / "f.json",
        [],
        scanners_run=[{"scanner": "secret-scan", "status": "failed", "reason": "timeout"}],
    )
    code, out, _ = call(tmp_path, repo, items, base, capsys=capsys, config_root=cfg)
    assert code == verb.EXIT_DEGRADED
    assert "secret-scan" in parsed(out)["degraded"][0]


def test_evaluate_help_and_version_exit_ok(capsys):
    assert review_cli.main(["evaluate", "--help"]) == 0
    assert "--expected-head" in capsys.readouterr().out
    assert review_cli.main(["--version"]) == 0


def test_evaluate_makes_no_host_call_and_needs_no_identity(tmp_path, capsys):
    """No opener, token provider or identity provider is passed and none is needed."""
    repo = make_repo(tmp_path)
    base = commit_files(repo, {STAKES_PATH: INTERNAL_PROFILE})
    items = write_findings(tmp_path / "f.json", [])
    code = review_cli.main(
        ["evaluate", "--findings", items, "--base", base, "--head", HEAD, "--repo-path", str(repo)],
        config_root=tmp_path / "no-config",
        identity_provider=lambda: (_ for _ in ()).throw(AssertionError("identity resolved")),
    )
    assert code == 0
