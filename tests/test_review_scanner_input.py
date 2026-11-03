"""review.scanner_input and review.scanner_findings, without a host."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from clagentic_loadout.review import scanner_input as si
from clagentic_loadout.review.changed_lines import DiffUnreadableError
from clagentic_loadout.review.dropped_overlap import candidate_id
from clagentic_loadout.review.scanner_findings import ingest, merge_into
from tests._review_cli_support import make_diff

HEAD = "b" * 40
SCHEMA = (
    Path(si.__file__).resolve().parent.parent / "schemas" / "scanner-input.schema.json"
)


def _doc(**scanner):
    return {"head_sha": HEAD, "scanners": [{"scanner": "s1", "status": "ran", **scanner}]}


def _parse(document):
    return si.parse_scanner_input(document, reviewed_head=HEAD)


def test_the_schema_accepts_what_the_loader_accepts():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = _doc(hits=[{"file": "a.py", "line": 2, "rule_id": "r", "message": "m"}])

    jsonschema.validate(document, schema)
    assert _parse(document).hits[0].scanner == "s1"


def test_a_kind_field_is_refused_by_the_loader_and_the_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = _doc(kind="other")

    with pytest.raises(si.ScannerInputError, match="unknown field"):
        _parse(document)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


@pytest.mark.parametrize(
    "document",
    [
        {"head_sha": HEAD, "scanners": [{"scanner": "s1", "status": "weird"}]},
        {"head_sha": "short", "scanners": []},
        {"head_sha": HEAD, "scanners": [], "range": "x"},
    ],
)
def test_the_schema_and_the_loader_refuse_the_same_documents(document):
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)
    with pytest.raises(si.ScannerInputError):
        _parse(document)


def test_a_stamp_for_another_head_names_both():
    with pytest.raises(si.ScannerInputError) as exc:
        si.parse_scanner_input(_doc(), reviewed_head="c" * 40)

    assert HEAD in str(exc.value) and "c" * 40 in str(exc.value)


def test_a_leading_dot_slash_is_dropped_and_parent_segments_are_refused():
    assert _parse(_doc(hits=[{"file": "./a.py", "line": 1, "rule_id": "r", "message": "m"}])).hits[0].file == "a.py"
    with pytest.raises(si.ScannerInputError):
        _parse(_doc(hits=[{"file": "../a.py", "line": 1, "rule_id": "r", "message": "m"}]))


def test_a_message_is_one_line_without_fence_syntax():
    hit = _parse(_doc(hits=[{"file": "a.py", "line": 1, "rule_id": "r", "message": "a\n```b```\n~~~c"}])).hits[0]

    assert "\n" not in hit.message and "`" not in hit.message and "~~~" not in hit.message


def test_an_unreadable_file_is_refused(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{", encoding="utf-8")
    with pytest.raises(si.ScannerInputError):
        si.load_scanner_input(path, reviewed_head=HEAD)


def test_ingestion_reads_the_diff_only_when_there_is_a_hit():
    assert ingest(_parse(_doc()), "not a diff", [], {}).findings == ()
    hit = {"file": "a.py", "line": 1, "rule_id": "r", "message": "m"}
    with pytest.raises(DiffUnreadableError):
        ingest(_parse(_doc(hits=[hit])), "not a diff", [], {})


def test_replacement_counts_a_location_once_and_keeps_other_rules():
    hit = {"file": "a.py", "line": 2, "rule_id": "r", "message": "m"}
    ingestion = ingest(_parse(_doc(hits=[hit, hit])), make_diff({"a.py": 4}), [], {})
    same = {"file": "a.py", "line": 2, "rule_id": "s1.r", "message": "mild"}
    other = {"file": "a.py", "line": 2, "rule_id": "other", "message": "mild"}

    merged = merge_into([same, other], ingest(_parse(_doc(hits=[hit, hit])), make_diff({"a.py": 4}), [same, other], {}))

    assert len(ingestion.findings) == 2
    assert [candidate_id(f) for f in merged] == ["a.py:2:other", "a.py:2:s1.r"]
    assert merged[1]["message"] == "m"
