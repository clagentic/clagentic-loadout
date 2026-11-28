"""`loadout-transcript decisions` through its real entry points, on synthetic transcripts
(invented session and agent ids, example tool inputs, temp dirs)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.transcript import verb
from clagentic_loadout.transcript.verb import EXIT_OK, EXIT_UNREADABLE_INPUT, main

SESSION = "sess-0001"
AGENT = "a1b2c3"


def _use(use_id: str, name: str, tool_input: dict, agent: str | None = None) -> str:
    rec = {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "id": use_id, "name": name, "input": tool_input}]},
    }
    if agent:
        rec["agentId"] = agent
    return json.dumps(rec)


def _result(
    use_id: str,
    source: str | None,
    reason: str | None = "other",
    decision: str = "accept",
    rule_source: str | None = None,
    agent: str | None = None,
) -> str:
    perm = {"decision": decision, "source": source, "reasonType": reason}
    if rule_source:
        perm["ruleSource"] = rule_source
    rec = {
        "type": "user",
        "timestamp": "2026-01-02T03:04:05Z",
        "message": {"content": [{"type": "tool_result", "tool_use_id": use_id, "content": "ok"}]},
        "permissionDecision": perm,
        "sandboxed": False,
    }
    if agent:
        rec["agentId"] = agent
    return json.dumps(rec)


def _write(path: Path, lines: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _lead(tmp_path: Path, lines: list[str]) -> Path:
    return _write(tmp_path / f"{SESSION}.jsonl", lines)


def _sub(tmp_path: Path, lines: list[str], agent: str = AGENT) -> Path:
    return _write(tmp_path / SESSION / "subagents" / f"agent-{agent}.jsonl", lines)


def _run(argv: list[str], capsys, entry=main) -> tuple[int, str, str]:
    rc = entry(argv)
    captured = capsys.readouterr()
    return rc, captured.out, captured.err


def _snapshot(root: Path) -> dict[str, bytes | None]:
    return {
        str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None)
        for p in sorted(root.rglob("*"))
    }


#: (decision, source, reasonType, ruleSource, expected class)
CLASS_ROWS = [
    ("accept", "user_permanent", "rule", "userSettings", "rule-or-config"),
    ("accept", "config", "rule", "flagSettings", "rule-or-config"),
    ("accept", "config", "subcommandResults", None, "rule-or-config"),
    ("reject", "user_reject", "other", None, "person-asked"),
    ("user_reject", "config", "other", None, "person-asked"),
    ("accept", "never_recorded", "other", None, "unclassified"),
    ("accept", "user_permanent", "other", None, "unclassified"),
    ("accept", None, None, None, "unclassified"),
]


@pytest.mark.parametrize("decision,source,reason,rule_source,expected", CLASS_ROWS)
def test_each_recorded_shape_lands_in_its_class(tmp_path, capsys, decision, source, reason, rule_source, expected):
    lead = _lead(
        tmp_path,
        [_use("t1", "Bash", {"command": "ls"}), _result("t1", source, reason, decision, rule_source)],
    )

    rc, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    data = json.loads(out)
    assert rc == EXIT_OK
    assert data["counts_by_class"][expected] == 1 and sum(data["counts_by_class"].values()) == 1
    listed = [d["class"] for d in data["listed_decisions"]]
    assert listed == ([] if expected == "rule-or-config" else [expected])


def test_person_asked_lead_decision_is_shown_in_full(tmp_path, capsys):
    tool_input = {"command": "ls -la", "description": "list " + "x" * 400}
    lead = _lead(
        tmp_path,
        [_use("t1", "Bash", tool_input), _result("t1", "user_reject", "other", "reject")],
    )

    rc, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    data = json.loads(out)
    assert rc == EXIT_OK
    assert data["listed_decisions"] == [
        {
            "class": "person-asked",
            "agent": "main",
            "tool": "Bash",
            "input": tool_input,
            "decision": "reject",
            "source": "user_reject",
            "reasonType": "other",
            "timestamp": "2026-01-02T03:04:05Z",
        }
    ]
    assert data["counts_by_combination"] == [
        {"class": "person-asked", "decision": "reject", "source": "user_reject",
         "reasonType": "other", "count": 1}
    ]


def test_ruleSource_is_shown_when_present(tmp_path, capsys):
    lead = _lead(
        tmp_path,
        [_use("t1", "Bash", {}), _result("t1", "weird", "other", "accept", "userSettings")],
    )

    _, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    (only,) = json.loads(out)["listed_decisions"]
    assert only["class"] == "unclassified" and only["ruleSource"] == "userSettings"


def test_subagent_decisions_are_counted_and_attributed_by_agent_id(tmp_path, capsys):
    lead = _lead(
        tmp_path,
        [_use("t1", "Read", {"file_path": "a.txt"}), _result("t1", "config", "subcommandResults")],
    )
    _sub(
        tmp_path,
        [
            _use("s1", "Write", {"file_path": "b.txt"}, AGENT),
            _result("s1", "user_reject", "other", "reject", agent=AGENT),
            _use("s2", "Read", {"file_path": "c.txt"}, AGENT),
            _result("s2", "config", "rule", agent=AGENT),
        ],
    )

    rc, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    data = json.loads(out)
    assert rc == EXIT_OK and data["subagent_transcripts"] == 1 and data["decisions"] == 3
    assert data["counts_by_class"] == {"person-asked": 1, "unclassified": 0, "rule-or-config": 2}
    (only,) = data["listed_decisions"]
    assert (only["agent"], only["tool"], only["input"]) == (AGENT, "Write", {"file_path": "b.txt"})


def test_all_rule_or_config_prints_zero_and_exits_ok(tmp_path, capsys):
    lead = _lead(tmp_path, [_use("t1", "Read", {}), _result("t1", "config", "rule")])

    rc, out, _ = _run(["decisions", str(lead)], capsys)

    assert rc == EXIT_OK
    assert "person-asked: 0" in out and "unclassified: 0" in out and "rule-or-config: 1" in out
    assert "accept/config/rule: 1 (rule-or-config)" in out


@pytest.mark.parametrize("entry", [main, umbrella_main], ids=["verb", "umbrella"])
def test_unreadable_lead_names_the_path_and_prints_no_count(tmp_path, capsys, entry):
    missing = tmp_path / "absent.jsonl"
    argv = ["decisions", str(missing)]

    rc, out, err = _run(argv if entry is main else ["transcript", *argv], capsys, entry)

    assert rc == EXIT_UNREADABLE_INPUT == 118
    assert out == "" and str(missing) in err


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files whatever their mode")
def test_unreadable_subagent_transcript_is_named_and_no_count_printed(tmp_path, capsys):
    lead = _lead(tmp_path, [_use("t1", "Read", {}), _result("t1", "config", "rule")])
    sub = _sub(tmp_path, [_use("s1", "Read", {}, AGENT)])
    sub.chmod(0)
    try:
        rc, out, err = _run(["decisions", str(lead)], capsys)
    finally:
        sub.chmod(stat.S_IRUSR | stat.S_IWUSR)

    assert rc == EXIT_UNREADABLE_INPUT and out == "" and str(sub) in err


def test_malformed_lines_are_skipped_and_counted(tmp_path, capsys):
    lead = _lead(
        tmp_path, ["not json", "[1, 2]", _use("t1", "Read", {}), _result("t1", "config", "rule")]
    )

    rc, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    data = json.loads(out)
    assert rc == EXIT_OK and data["skipped_lines"] == 2 and data["decisions"] == 1


def test_a_decision_without_its_tool_use_is_still_counted(tmp_path, capsys):
    lead = _lead(tmp_path, [_result("orphan", "user_reject", "other", "reject")])

    rc, out, _ = _run(["decisions", str(lead), "--json"], capsys)

    (only,) = json.loads(out)["listed_decisions"]
    assert rc == EXIT_OK and only["tool"] is None and only["input"] is None


def test_running_it_changes_nothing(tmp_path, capsys):
    lead = _lead(tmp_path, [_use("t1", "Bash", {"command": "ls"}), _result("t1", "user_reject")])
    _sub(tmp_path, [_use("s1", "Read", {}, AGENT), _result("s1", "config", "rule", agent=AGENT)])
    before = _snapshot(tmp_path)

    rc, _, _ = _run(["decisions", str(lead)], capsys)

    assert rc == EXIT_OK and _snapshot(tmp_path) == before


def test_counts_never_change_the_exit_code(tmp_path, capsys):
    lines = []
    for n in range(5):
        lines += [_use(f"t{n}", "Bash", {"command": f"echo {n}"}), _result(f"t{n}", "user_reject")]
        lines += [_use(f"u{n}", "Bash", {}), _result(f"u{n}", "odd")]
    lead = _lead(tmp_path, lines)

    rc, out, _ = _run(["decisions", str(lead)], capsys)

    assert rc == EXIT_OK and "person-asked: 5" in out and "unclassified: 5" in out


def test_usage_error_exits_one(capsys):
    assert main(["decisions"]) == verb.EXIT_USAGE
    capsys.readouterr()
