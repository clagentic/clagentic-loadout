"""A policy date is the UTC calendar date of the moment it names, whatever offset it was written in."""

from __future__ import annotations

import datetime as dt

import pytest
import yaml

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import evaluate as evaluate_module
from clagentic_loadout.review import evaluation
from clagentic_loadout.review import profile_posture as pp
from clagentic_loadout.review import rulings
from clagentic_loadout.review.utc_date import parse_utc_date, today_utc

from _review_evaluate_support import HEAD, disposition, internal_stakes

PLUS_14 = dt.timezone(dt.timedelta(hours=14))
MINUS_12 = dt.timezone(dt.timedelta(hours=-12))

# (moment, UTC date). Each offset is exercised on both sides of midnight UTC.
MOMENTS = [
    (dt.datetime(2026, 10, 26, 9, 0, tzinfo=PLUS_14), dt.date(2026, 10, 25)),  # 19:00 UTC the day before
    (dt.datetime(2026, 10, 26, 15, 0, tzinfo=PLUS_14), dt.date(2026, 10, 26)),  # 01:00 UTC
    (dt.datetime(2026, 10, 26, 11, 0, tzinfo=MINUS_12), dt.date(2026, 10, 26)),  # 23:00 UTC
    (dt.datetime(2026, 10, 26, 13, 0, tzinfo=MINUS_12), dt.date(2026, 10, 27)),  # 01:00 UTC next day
]


def _doc(**fields):
    entry = {"path": "src/**", "rule_id": "R1", "kind": "by_design", "rationale": "why", "by": "someone"}
    entry.update(fields)
    return {"dispositions": [entry]}


@pytest.mark.parametrize(("moment", "expected"), MOMENTS)
class TestOffsetMoments:
    def test_the_datetime_object_resolves_to_its_utc_date(self, moment, expected):
        assert parse_utc_date(moment) == expected

    def test_the_iso_string_resolves_to_the_same_utc_date(self, moment, expected):
        assert parse_utc_date(moment.isoformat()) == expected

    def test_a_disposition_expiry_object_is_judged_on_the_utc_date(self, moment, expected):
        (entry,) = disp.parse_dispositions(_doc(at=dt.date(2026, 10, 1), expires=moment))
        assert entry.expires == expected
        assert entry.is_expired(expected) is False
        assert entry.is_expired(expected + dt.timedelta(days=1)) is True

    def test_a_disposition_expiry_string_is_judged_on_the_utc_date(self, moment, expected):
        (entry,) = disp.parse_dispositions(_doc(at="2026-10-01", expires=moment.isoformat()))
        assert entry.expires == expected
        assert entry.is_expired(expected) is False
        assert entry.is_expired(expected + dt.timedelta(days=1)) is True

    def test_a_yaml_timestamp_with_an_offset_is_read_as_the_same_moment(self, moment, expected):
        loaded = yaml.safe_load(f"expires: {moment.isoformat()}\n")
        (entry,) = disp.parse_dispositions(_doc(at=dt.date(2026, 10, 1), expires=loaded["expires"]))
        assert entry.expires == expected

    def test_a_ruling_date_goes_through_the_same_conversion(self, moment, expected):
        assert disp.date_field(moment, "rulings[1].expires") == expected
        ruling = rulings.build_ruling(
            repo="o/r", pr=1, fingerprint="fp2:x", rule_id="R1", path="a.py", kind="accepted_risk",
            rationale="why", by="someone", requested_by="caller", source_ref="ref",
            at=dt.date(2026, 10, 1), expires=moment.isoformat(),
        )
        assert ruling.expires == expected


class TestOtherForms:
    def test_a_naive_datetime_is_read_as_utc(self):
        assert parse_utc_date(dt.datetime(2026, 10, 26, 23, 59)) == dt.date(2026, 10, 26)

    def test_a_z_suffixed_string_is_utc(self):
        assert parse_utc_date("2026-10-26T23:30:00Z") == dt.date(2026, 10, 26)

    def test_a_bare_date_and_a_bare_date_string_are_unchanged(self):
        (entry,) = disp.parse_dispositions(_doc(at=dt.date(2026, 10, 1), expires="2026-12-01"))
        assert entry.at == dt.date(2026, 10, 1)
        assert entry.expires == dt.date(2026, 12, 1)

    @pytest.mark.parametrize("bad", ["soon", "2026-13-40", True, 20261026, 1.5, ""])
    def test_a_value_that_names_no_date_rejects_the_file_with_the_value(self, bad):
        with pytest.raises(disp.DispositionsFormatError) as raised:
            disp.parse_dispositions(_doc(at=dt.date(2026, 10, 1), expires=bad))
        assert "dispositions[1].expires" in str(raised.value)
        assert repr(bad) in str(raised.value)

    def test_a_ruling_with_an_unreadable_date_is_a_ruling_format_error(self):
        with pytest.raises(rulings.RulingFormatError):
            rulings.parse_rulings(
                {"rulings": [{
                    "repo": "o/r", "pr": 1, "fingerprint": "fp2:x", "rule_id": "R1", "path": "a.py",
                    "kind": "accepted_risk", "rationale": "why", "by": "someone",
                    "requested_by": "caller", "source_ref": "ref", "at": "garbage",
                }]}
            )

    def test_the_posture_reader_shares_the_helper(self):
        moment = dt.datetime(2026, 10, 26, 9, 0, tzinfo=PLUS_14)
        assert pp.parse_confirmed_at(moment) == parse_utc_date(moment) == dt.date(2026, 10, 25)
        assert pp.today_utc is today_utc

    def test_today_is_the_utc_date(self):
        assert today_utc(dt.datetime(2026, 10, 26, 9, 0, tzinfo=PLUS_14)) == dt.date(2026, 10, 25)


class TestClearingStanza:
    def test_it_writes_the_date_it_is_given(self):
        stanza = disp.clearing_stanza("a.py", "R1", "accepted_risk", dt.date(2026, 10, 25))
        assert "at: 2026-10-25" in stanza
        assert "expires: 2026-11-24" in stanza


class TestEvaluateDefaultDayIsUtc:
    # A disposition expiring 2026-10-25 holds through that UTC day. The clock is
    # an offset-aware moment, so reading its own local date would misjudge it.
    @pytest.mark.parametrize(
        ("now", "expired"),
        [
            (dt.datetime(2026, 10, 26, 1, 0, tzinfo=PLUS_14), False),  # 11:00 UTC on the 25th
            (dt.datetime(2026, 10, 26, 15, 0, tzinfo=PLUS_14), True),  # 01:00 UTC on the 26th
            (dt.datetime(2026, 10, 25, 11, 0, tzinfo=MINUS_12), False),  # 23:00 UTC on the 25th
            (dt.datetime(2026, 10, 25, 13, 0, tzinfo=MINUS_12), True),  # 01:00 UTC on the 26th
        ],
    )
    def test_expiry_is_judged_against_the_utc_date_when_no_day_is_given(self, monkeypatch, now, expired):
        monkeypatch.setattr(evaluate_module, "today_utc", lambda: today_utc(now))
        entry = disposition(expires=dt.date(2026, 10, 25))
        verdict = evaluate_module.evaluate([], internal_stakes(), [entry], HEAD, HEAD)
        assert bool(verdict.expired_dispositions) is expired


class TestDefaultTodayIsUtc:
    def test_the_evaluation_default_is_the_utc_date(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(evaluation, "today_utc", lambda: dt.date(2026, 10, 25))
        monkeypatch.setattr(evaluation, "evaluate", lambda *a, **kw: seen.update(kw) or "verdict")
        context = evaluation.EvaluationContext(visibility="private")
        assert evaluation.evaluate_findings([], "a" * 40, context) == "verdict"
        assert seen["today"] == dt.date(2026, 10, 25)

    def test_an_explicit_date_wins(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(evaluation, "evaluate", lambda *a, **kw: seen.update(kw) or "verdict")
        context = evaluation.EvaluationContext(visibility="private")
        evaluation.evaluate_findings([], "a" * 40, context, today=dt.date(2026, 1, 2))
        assert seen["today"] == dt.date(2026, 1, 2)
