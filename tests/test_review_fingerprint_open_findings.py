"""The post path states a blocking finding's fingerprint in findings_open and in
the suggested disposition entry, and only when it is certain."""

from __future__ import annotations

import datetime as dt

from clagentic_loadout.review.dispositions import clearing_stanza
from clagentic_loadout.review.evaluate import evaluate
from clagentic_loadout.review.evaluation import findings_open_from_verdict
from tests._review_evaluate_support import exposed_stakes, facts, finding

FP = "fp2:0123456789abcdef"
HEAD = "b" * 40
BLOCKING = facts(confidentiality="full", integrity="full", availability="full")


def _verdict(*findings):
    return evaluate(list(findings), exposed_stakes(), [], HEAD, HEAD, today=dt.date(2026, 10, 1))


def _open(*findings):
    return findings_open_from_verdict(_verdict(*findings), HEAD, findings)


def test_a_blocking_finding_with_a_fingerprint_is_held_open_with_it():
    assert _open(finding(fact=BLOCKING, fingerprint=FP))[0]["fingerprint"] == FP


def test_a_finding_without_one_is_held_open_without_it():
    assert "fingerprint" not in _open(finding(fact=BLOCKING))[0]


def test_two_findings_sharing_an_id_with_different_fingerprints_state_none():
    first = finding(fact=BLOCKING, fingerprint=FP)
    second = finding(fact=BLOCKING, fingerprint="fp2:fedcba9876543210", message="other")
    assert "fingerprint" not in _open(first, second)[0]


def test_the_suggested_entry_names_the_fingerprint_when_one_is_known():
    day = dt.date(2026, 1, 1)
    assert f'    fingerprint: "{FP}"' in clearing_stanza("a.py", "R1", "false_positive", day, FP)
    assert "fingerprint" not in clearing_stanza("a.py", "R1", "false_positive", day)
