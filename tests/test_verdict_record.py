"""The verdict record: its schema, the store it lives in, the one reader that
consumers read it through, the fence adapter, and the `loadout-record` verb."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record import verb as record_verb
from clagentic_loadout.verdict_record.emit import emit_record, try_emit_record
from clagentic_loadout.verdict_record.reader import (
    STATUS_FAILED,
    STATUS_MEASURED,
    STATUS_NOT_MEASURED,
    RequiredGate,
    read_gates,
    record_from_fence,
)
from clagentic_loadout.verdict_record.record import (
    RecordFormatError,
    build_record,
    gate_name,
    new_record_id,
    validate_record_dict,
)
from clagentic_loadout.verdict_record.store import (
    RECORDS_DIR_ENV_VAR,
    RecordNotFoundError,
    StoreError,
    load_record,
    newest_record_for_gate,
    records_at_head,
    records_root,
    write_record,
)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:00:05Z"
T2 = "2026-01-01T00:00:09Z"


def _record(gate="reviewer:agent-alpha", head=HEAD_A, exit=0, started=T0, finished=T1, **kw):
    return build_record(
        gate=gate, head_sha=head, tool="test-tool", exit=exit, started=started,
        finished=finished, **kw,
    )


class TestSchema:
    def test_a_record_carries_every_field_the_contract_names(self):
        data = _record(repo="some-owner/some-repo", tree_sha="c" * 40, evidence_ref="comment:7").as_dict()
        assert set(data) == {
            "schema_version", "id", "gate", "head_sha", "tree_sha", "repo", "spec_hash",
            "tool", "exit", "started", "finished", "evidence_ref", "waiver",
        }
        assert data["schema_version"] == 1
        assert data["spec_hash"] is None and data["waiver"] is None

    def test_round_trips_through_the_validator(self):
        original = _record(repo="some-owner/some-repo")
        assert validate_record_dict(original.as_dict()) == original

    @pytest.mark.parametrize(
        "overrides",
        [
            {"head_sha": "abc123"},
            {"head_sha": "A" * 40},
            {"gate": ""},
            {"gate": "two\nlines"},
            {"tool": ""},
            {"started": "yesterday"},
            {"started": T1, "finished": T0},
            {"repo": "no-slash"},
            {"tree_sha": "short"},
        ],
    )
    def test_a_value_the_schema_refuses_is_refused(self, overrides):
        fields = {"gate": "ci", "head_sha": HEAD_A, "tool": "t", "exit": 0, "started": T0, "finished": T1}
        fields.update(overrides)
        with pytest.raises(RecordFormatError):
            build_record(**fields)

    def test_an_exit_must_be_an_integer(self):
        data = _record().as_dict()
        for bad in ("0", 0.5, True, None):
            with pytest.raises(RecordFormatError):
                validate_record_dict({**data, "exit": bad})

    def test_an_unknown_field_is_refused(self):
        with pytest.raises(RecordFormatError):
            validate_record_dict({**_record().as_dict(), "extra": 1})

    def test_a_sha256_object_name_is_a_valid_head(self):
        assert _record(head="d" * 64).head_sha == "d" * 64


class TestStore:
    def test_write_then_read_by_id(self, tmp_path):
        root = tmp_path / "records"
        record = _record()
        path = write_record(root, record)
        assert path == root / HEAD_A / f"{record.id}.json"
        assert load_record(root, record.id) == record

    def test_record_is_readable_by_every_user_and_dir_is_traversable(self, tmp_path):
        root = tmp_path / "records"
        record = _record()
        path = write_record(root, record)
        assert (path.stat().st_mode & 0o777) == 0o644
        assert ((root / HEAD_A).stat().st_mode & 0o555) == 0o555

    def test_records_at_head_lists_only_that_head_oldest_first(self, tmp_path):
        root = tmp_path / "records"
        first = _record(record_id="20260101T000000Z-00000001")
        second = _record(gate="ci", record_id="20260101T000001Z-00000002")
        other = _record(head=HEAD_B)
        for record in (second, first, other):
            write_record(root, record)
        found = records_at_head(root, HEAD_A)
        assert [r.id for r in found.records] == [first.id, second.id]
        assert found.skipped == ()

    def test_a_missing_root_holds_no_records(self, tmp_path):
        assert records_at_head(tmp_path / "absent", HEAD_A).records == ()

    def test_an_unknown_id_is_not_found(self, tmp_path):
        with pytest.raises(RecordNotFoundError):
            load_record(tmp_path / "records", new_record_id())

    @pytest.mark.parametrize("bad", ["../etc/passwd", "x", "20260101T000000Z-zzzzzzzz", ""])
    def test_an_id_is_validated_before_it_touches_a_path(self, tmp_path, bad):
        with pytest.raises(StoreError, match="not a verdict record id"):
            load_record(tmp_path / "records", bad)

    def test_a_head_is_validated_before_it_touches_a_path(self, tmp_path):
        with pytest.raises(StoreError, match="not a full commit sha"):
            records_at_head(tmp_path / "records", "../../x")

    def test_a_file_that_is_not_a_valid_record_is_skipped_and_named(self, tmp_path):
        root = tmp_path / "records"
        good = _record()
        write_record(root, good)
        bad_id = "20260101T000000Z-badbadff"
        (root / HEAD_A / f"{bad_id}.json").write_text("{not json", encoding="utf-8")
        found = records_at_head(root, HEAD_A)
        assert [r.id for r in found.records] == [good.id]
        assert len(found.skipped) == 1 and bad_id in found.skipped[0]

    def test_a_record_filed_under_another_head_is_not_counted(self, tmp_path):
        root = tmp_path / "records"
        record = _record(head=HEAD_B)
        (root / HEAD_A).mkdir(parents=True)
        (root / HEAD_A / f"{record.id}.json").write_text(
            json.dumps(record.as_dict()), encoding="utf-8"
        )
        found = records_at_head(root, HEAD_A)
        assert found.records == ()
        assert "filed under" in found.skipped[0]

    def test_a_symlinked_head_directory_is_not_written_through(self, tmp_path):
        root = tmp_path / "records"
        root.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        os.symlink(elsewhere, root / HEAD_A)
        with pytest.raises(StoreError, match="symlink"):
            write_record(root, _record())
        assert list(elsewhere.iterdir()) == []

    def test_a_root_inside_the_repository_under_review_is_refused(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        with pytest.raises(StoreError, match="the pull request under review can write"):
            write_record(repo / "records", _record(), untrusted_roots=(repo,))
        with pytest.raises(StoreError):
            records_at_head(repo / "records", HEAD_A, untrusted_roots=(repo,))

    def test_newest_record_of_a_gate_across_heads_skips_other_repos(self, tmp_path):
        root = tmp_path / "records"
        write_record(root, _record(head=HEAD_A, repo="some-owner/other", record_id="20260101T000009Z-00000009"))
        wanted = _record(head=HEAD_B, repo="some-owner/some-repo", record_id="20260101T000001Z-00000001")
        write_record(root, wanted)
        found = newest_record_for_gate(root, "reviewer:agent-alpha", repo="some-owner/some-repo")
        assert found == wanted
        assert newest_record_for_gate(root, "ci", repo="some-owner/some-repo") is None


class TestRecordsRoot:
    def test_environment_variable_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(RECORDS_DIR_ENV_VAR, str(tmp_path / "from-env"))
        assert records_root() == tmp_path / "from-env"

    def test_user_config_setting_is_next(self, monkeypatch, tmp_path):
        monkeypatch.delenv(RECORDS_DIR_ENV_VAR, raising=False)
        (tmp_path / "config.yaml").write_text(
            f"verdict_records:\n  dir: {tmp_path / 'from-config'}\n", encoding="utf-8"
        )
        assert records_root(config_root=tmp_path) == tmp_path / "from-config"

    def test_an_unconfigured_deployment_still_has_a_working_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv(RECORDS_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "cfg")
        root = records_root()
        assert root == tmp_path / "cfg" / "state" / "verdict-records"
        record = emit_record(
            gate="ci", head_sha=HEAD_A, tool="t", exit=0, started=T0, finished=T1
        )
        assert load_record(root, record.id) == record


class TestReader:
    def _read(self, tmp_path, gates, head=HEAD_A, **kw):
        readings, _skipped = read_gates(
            tmp_path / "records", [RequiredGate(g) for g in gates], head, **kw
        )
        return {r.gate: r for r in readings}

    def test_a_gate_with_no_record_is_not_measured(self, tmp_path):
        reading = self._read(tmp_path, ["ci"])["ci"]
        assert reading.status == STATUS_NOT_MEASURED and reading.other_head is None
        assert not reading.ok

    def test_a_record_at_the_head_measures_the_gate(self, tmp_path):
        write_record(tmp_path / "records", _record(gate="ci"))
        reading = self._read(tmp_path, ["ci"])["ci"]
        assert reading.status == STATUS_MEASURED and reading.ok

    def test_a_record_at_another_head_is_not_measured_and_names_that_head(self, tmp_path):
        write_record(tmp_path / "records", _record(gate="ci", head=HEAD_B))
        reading = self._read(tmp_path, ["ci"])["ci"]
        assert reading.status == STATUS_NOT_MEASURED
        assert reading.other_head == HEAD_B
        assert reading.expected_head == HEAD_A

    def test_a_record_that_did_not_pass_is_a_failed_gate(self, tmp_path):
        write_record(tmp_path / "records", _record(gate="ci", exit=3))
        reading = self._read(tmp_path, ["ci"])["ci"]
        assert reading.status == STATUS_FAILED and reading.record.exit == 3

    def test_the_newest_record_at_the_head_decides(self, tmp_path):
        root = tmp_path / "records"
        write_record(root, _record(gate="ci", exit=1, started=T0, finished=T1))
        write_record(root, _record(gate="ci", exit=0, started=T1, finished=T2))
        assert self._read(tmp_path, ["ci"])["ci"].status == STATUS_MEASURED
        write_record(root, _record(gate="ci", exit=1, started=T2, finished="2026-01-01T00:01:00Z"))
        assert self._read(tmp_path, ["ci"])["ci"].status == STATUS_FAILED

    def test_a_record_naming_another_repository_does_not_count(self, tmp_path):
        write_record(tmp_path / "records", _record(gate="ci", repo="some-owner/other"))
        readings = self._read(tmp_path, ["ci"], repo="some-owner/some-repo")
        assert readings["ci"].status == STATUS_NOT_MEASURED

    def test_every_required_gate_is_read_in_the_order_asked(self, tmp_path):
        write_record(tmp_path / "records", _record(gate="b"))
        readings, _ = read_gates(
            tmp_path / "records", [RequiredGate("a"), RequiredGate("b")], HEAD_A
        )
        assert [(r.gate, r.status) for r in readings] == [
            ("a", STATUS_NOT_MEASURED), ("b", STATUS_MEASURED),
        ]


class TestFenceAdapter:
    def _fence(self, head=HEAD_A, exit=0):
        return record_from_fence(
            gate="reviewer:agent-alpha", head_sha=head, exit=exit,
            comment_id=77, created_at="2026-01-01T02:00:00+02:00", repo="some-owner/some-repo",
        )

    def test_a_fence_becomes_a_record_naming_its_comment(self):
        record = self._fence()
        assert record.evidence_ref == "comment:77"
        assert record.tool == "verdict-fence"
        assert record.exit == 0
        assert record.started == record.finished == "2026-01-01T00:00:00Z"

    def test_a_fence_the_gate_did_not_pass_does_not_pass(self, tmp_path):
        readings, _ = read_gates(
            tmp_path / "records", [RequiredGate("reviewer:agent-alpha")], HEAD_A,
            adapted={"reviewer:agent-alpha": [self._fence(exit=1)]},
        )
        assert readings[0].status == STATUS_FAILED

    def test_the_reader_takes_an_adapted_record_at_the_exact_head(self, tmp_path):
        readings, _ = read_gates(
            tmp_path / "records", [RequiredGate("reviewer:agent-alpha")], HEAD_A,
            adapted={"reviewer:agent-alpha": [self._fence()]},
        )
        assert readings[0].status == STATUS_MEASURED
        assert readings[0].record.evidence_ref == "comment:77"

    def test_an_adapted_record_at_another_head_is_not_measured(self, tmp_path):
        readings, _ = read_gates(
            tmp_path / "records", [RequiredGate("reviewer:agent-alpha")], HEAD_A,
            adapted={"reviewer:agent-alpha": [self._fence(head=HEAD_B)]},
        )
        assert readings[0].status == STATUS_NOT_MEASURED


class TestTryEmit:
    def test_a_refused_write_is_reported_and_returns_none(self, tmp_path, capsys):
        result = try_emit_record(
            "test-prog", gate="ci", head_sha="short", tool="t", exit=0, started=T0, finished=T1,
            root=tmp_path / "records",
        )
        assert result is None
        err = capsys.readouterr().err
        assert "test-prog" in err and "not measured" in err


class TestVerb:
    def test_show_prints_the_record_by_id(self, tmp_path, capsys):
        record = emit_record(
            gate="ci", head_sha=HEAD_A, tool="t", exit=0, started=T0, finished=T1,
            evidence_ref="check:x",
        )
        assert record_verb.main(["show", record.id]) == record_verb.EXIT_OK
        assert json.loads(capsys.readouterr().out) == record.as_dict()

    def test_show_of_an_unknown_id_is_not_found(self, capsys):
        assert record_verb.main(["show", new_record_id()]) == record_verb.EXIT_NOT_FOUND
        assert "no verdict record" in capsys.readouterr().err

    def test_show_of_a_malformed_id_is_a_usage_error(self, capsys):
        assert record_verb.main(["show", "../x"]) == record_verb.EXIT_USAGE

    def test_show_of_an_invalid_record_reports_the_file(self, capsys):
        record = emit_record(gate="ci", head_sha=HEAD_A, tool="t", exit=0, started=T0, finished=T1)
        path = records_root() / HEAD_A / f"{record.id}.json"
        path.write_text("[]", encoding="utf-8")
        assert record_verb.main(["show", record.id]) == record_verb.EXIT_RECORD_INVALID

    def test_list_filters_by_head_and_gate(self, capsys):
        emit_record(gate="ci", head_sha=HEAD_A, tool="t", exit=0, started=T0, finished=T1)
        emit_record(gate="other", head_sha=HEAD_A, tool="t", exit=0, started=T0, finished=T1)
        emit_record(gate="ci", head_sha=HEAD_B, tool="t", exit=0, started=T0, finished=T1)
        assert record_verb.main(["list", "--head", HEAD_A, "--gate", "ci"]) == record_verb.EXIT_OK
        listed = json.loads(capsys.readouterr().out)
        assert [r["gate"] for r in listed["records"]] == ["ci"]
        assert listed["records"][0]["head_sha"] == HEAD_A

    def test_list_needs_a_full_sha(self, capsys):
        assert record_verb.main(["list", "--head", "abc"]) == record_verb.EXIT_USAGE

    def test_the_verb_has_no_write_subcommand(self, capsys):
        assert record_verb.main(["write", "--gate", "ci"]) == record_verb.EXIT_USAGE


@pytest.mark.parametrize(
    "module",
    [
        "clagentic_loadout.verdict_record.store",
        "clagentic_loadout.verdict_record.emit",
        "clagentic_loadout.verdict_record.verb",
        "clagentic_loadout.push.verb",
        "clagentic_loadout.review.verb",
        "clagentic_loadout.merge.verb",
        "clagentic_loadout.merge.record_gate",
    ],
)
def test_each_entry_module_imports_first_in_a_fresh_interpreter(module):
    """The review package imports the review verb, which writes records, so an
    import order that differs from the test suite's (the push verb or the store
    first) must not trip a circular import. Each console script starts that way."""
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src_dir},
    )
    assert result.returncode == 0, result.stderr


def test_gate_names_are_kind_colon_name():
    assert gate_name("reviewer", "agent-alpha") == "reviewer:agent-alpha"
