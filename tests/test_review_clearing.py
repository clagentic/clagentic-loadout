"""review.clearing: the decision of which earlier open findings are cleared."""

from __future__ import annotations

from clagentic_loadout.review.clearing import PriorOpen, derive_clearing
from clagentic_loadout.review.evaluation import finding_state_id

HEAD = "c" * 40
PRIOR_HEAD = "b" * 40


def _prior(line, rule="R1"):
    return PriorOpen(
        id=finding_state_id("a.py", line, rule), rule_id=rule, head=PRIOR_HEAD,
        file="a.py", line=line, message="m",
    )


def _drop(line, reason="by design", rule="R1"):
    return {"file": "a.py", "line": line, "rule_id": rule, "message": "m", "reason": reason}


def _derive(prior, drops, map_line):
    return derive_clearing(
        prior, head_sha=HEAD, raised_ids=set(), resolved_entries=[], caller_rulings=[],
        demoted={}, drops=drops, map_line=map_line,
    )


def test_a_drop_whose_line_moved_maps_through_the_prior_head():
    result = _derive([_prior(3)], [_drop(5)], lambda head, file, line: line + 2)

    assert [c["id"] for c in result.claims] == [_prior(3).id]


def test_a_drop_that_two_priors_map_to_clears_neither():
    priors = [_prior(3), _prior(4)]

    result = _derive(priors, [_drop(5)], lambda head, file, line: 5)

    assert result.claims == ()
    assert [p.id for p in result.uncleared] == [p.id for p in priors]


def test_two_drops_for_one_prior_are_ambiguous():
    result = _derive([_prior(3)], [_drop(3), _drop(3, "again")], lambda *a: None)

    assert result.claims == ()
    assert "more than one dropped candidate" in result.notes[0]


def test_a_finding_the_verdict_raises_again_is_never_cleared():
    prior = _prior(3)

    result = derive_clearing(
        [prior], head_sha=HEAD, raised_ids={prior.id}, resolved_entries=[], caller_rulings=[],
        demoted={prior.id: "demoted"}, drops=[_drop(3)], map_line=lambda *a: None,
    )

    assert result.claims == () and result.uncleared == ()
