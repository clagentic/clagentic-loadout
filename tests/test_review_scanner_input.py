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


def test_a_scanner_name_with_a_dot_is_refused_by_the_loader_and_the_schema():
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    document = {"head_sha": HEAD, "scanners": [{"scanner": "a.b", "status": "ran"}]}

    with pytest.raises(si.ScannerInputError, match="scanner"):
        _parse(document)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, schema)


def test_scanner_and_rule_pairs_that_would_share_a_dotted_id_stay_distinct():
    hit = {"file": "a.py", "line": 2, "rule_id": "b.c", "message": "m"}
    other = {"file": "a.py", "line": 2, "rule_id": "c", "message": "m"}
    document = {
        "head_sha": HEAD,
        "scanners": [
            {"scanner": "a", "status": "ran", "hits": [hit]},
            {"scanner": "ab", "status": "ran", "hits": [other]},
        ],
    }

    ingestion = ingest(_parse(document), make_diff({"a.py": 4}), [], {})

    assert sorted(f["rule_id"] for f in ingestion.findings) == ["a.b.c", "ab.c"]


def _hit_file(value):
    return _parse(_doc(hits=[{"file": value, "line": 1, "rule_id": "r", "message": "m"}])).hits[0].file


@pytest.mark.parametrize("value", ["src/./x.py", "src//x.py", "./src/x.py", "././src/x.py"])
def test_a_hit_path_is_put_in_the_form_the_diff_uses(value):
    path = _hit_file(value)
    assert path == "src/x.py"
    assert ingest(_parse(_doc(hits=[{"file": value, "line": 1, "rule_id": "r", "message": "m"}])),
                  make_diff({"src/x.py": 2}), [], {}).findings != ()


@pytest.mark.parametrize("value", ["/abs", "../x", "a/../b", "src/", "./", "a\\b"])
def test_a_hit_path_that_does_not_normalize_cleanly_is_refused(value):
    with pytest.raises(si.ScannerInputError):
        _hit_file(value)


def test_a_hit_in_a_file_cut_off_by_a_diff_that_ends_after_the_first_file_is_in_range():
    diff = make_diff({"a.py": 2})
    hit = {"file": "b.py", "line": 1, "rule_id": "r", "message": "m"}

    ingestion = ingest(_parse(_doc(hits=[hit])), diff, [], {})

    assert len(ingestion.findings) == 1 and ingestion.outside_range == ()


def test_a_context_line_of_a_listed_file_is_outside_range():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n ctx\n+new\n ctx2\n"
    )
    hit = {"file": "a.py", "line": 1, "rule_id": "r", "message": "m"}

    ingestion = ingest(_parse(_doc(hits=[hit])), diff, [], {})

    assert ingestion.findings == () and len(ingestion.outside_range) == 1


def _evidence(diff, file, line):
    hit = {"file": file, "line": line, "rule_id": "r", "message": "m"}
    ingestion = ingest(_parse(_doc(hits=[hit])), diff, [], {})
    return ingestion.findings[0]["facts"]["evidence"]


def test_the_evidence_of_a_hit_on_an_added_line_says_the_change_adds_it():
    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n"
        "@@ -1,2 +1,3 @@\n ctx\n+new\n ctx2\n"
    )

    assert _evidence(diff, "a.py", 2).endswith("the change adds this line")


def test_the_evidence_of_a_hit_not_shown_as_context_does_not_claim_the_line_is_added():
    diff = make_diff({"a.py": 2})
    unlisted = _evidence(diff, "b.py", 1)

    assert "adds" not in unlisted
    assert unlisted.endswith("so it is counted in range")
