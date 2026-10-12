"""The facts vocabulary, its published schema, and the optional contract field."""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from clagentic_loadout.review import facts as fx
from clagentic_loadout.review.findings_contract import validate_finding

from _review_evaluate_support import facts, finding

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent
    / "src" / "clagentic_loadout" / "schemas" / "review-facts.json"
)


@pytest.fixture(scope="module")
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_schema_vocabularies_equal_the_code_vocabularies(schema):
    props = schema["properties"]
    assert tuple(props["category"]["enum"]) == fx.CATEGORIES
    assert tuple(props["reachable"]["enum"]) == fx.REACHABLE_VALUES
    assert tuple(props["attacker_precondition"]["enum"]) == fx.PRECONDITIONS
    assert tuple(props["durability"]["enum"]) == fx.DURABILITY_VALUES
    assert props["cwe"]["pattern"] == fx.CWE_PATTERN
    assert props["evidence"]["maxLength"] == fx.MAX_EVIDENCE_CHARS
    impact = props["impact"]["properties"]
    assert tuple(impact) == fx.IMPACT_DIMENSIONS
    assert all(tuple(v["enum"]) == fx.IMPACT_LEVELS for v in impact.values())


def test_a_valid_facts_object_satisfies_the_schema_and_parses_complete(schema):
    sample = facts(category="ssrf", cwe="CWE-918", evidence="redirect follows internal hosts")
    jsonschema.validate(sample, schema)
    parsed = fx.parse_facts(sample)
    assert parsed.state == fx.STATE_COMPLETE
    assert parsed.facts.category == "ssrf"
    assert parsed.facts.impact == ("none", "none", "none")


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(category="made_up"),
        lambda d: d.update(reachable="maybe"),
        lambda d: d.update(attacker_precondition="root"),
        lambda d: d.update(durability="forever"),
        lambda d: d["impact"].update(integrity="total"),
        lambda d: d.update(cwe="79"),
        lambda d: d.update(evidence="x" * 301),
        lambda d: d.update(extra_key=1),
        lambda d: d.pop("impact"),
        lambda d: d["impact"].pop("availability"),
    ],
)
def test_what_the_schema_rejects_the_parser_reports_as_invalid(schema, mutate):
    sample = facts()
    mutate(sample)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(sample, schema)
    assert fx.parse_facts(sample).state == fx.STATE_INVALID


def test_absent_and_non_object_facts_resolve_to_the_worst_case():
    assert fx.parse_facts(None).state == fx.STATE_ABSENT
    assert fx.parse_facts(None).facts == fx.WORST_CASE_FACTS
    invalid = fx.parse_facts("high")
    assert invalid.state == fx.STATE_INVALID
    assert invalid.facts == fx.WORST_CASE_FACTS


def test_unknown_is_a_complete_statement_that_resolves_to_the_worst_case():
    parsed = fx.parse_facts(facts(reachable="unknown", durability="unknown"))
    assert parsed.state == fx.STATE_COMPLETE
    assert parsed.facts.reachable == "unknown"


def test_floor_categories_are_all_real_categories():
    assert fx.FLOOR_CATEGORIES <= set(fx.CATEGORIES)


def test_contract_carries_facts_only_when_asked_and_stays_valid_without():
    item = finding(fact=facts())
    item["severity"] = "blocking"
    plain = validate_finding(item, 1)
    assert "facts" not in plain
    carried = validate_finding(item, 1, carry_facts=True)
    assert carried["facts"] == item["facts"]
    no_facts = dict(item)
    del no_facts["facts"]
    assert "facts" not in validate_finding(no_facts, 1, carry_facts=True)


def test_contract_keeps_a_malformed_facts_value_for_the_evaluator_to_judge():
    item = finding()
    item["severity"] = "nit"
    item["facts"] = "very bad"
    assert validate_finding(item, 1, carry_facts=True)["facts"] == "very bad"
