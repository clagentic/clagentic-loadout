"""Tests for review.delta: the decision between a delta and a full review, the
prompt framing, and the carry-over of findings the delta cannot have resolved."""

from __future__ import annotations

from clagentic_loadout.acquire.contract import AcquiredPr, RangeDiff
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review.delta import (
    MAX_LISTED_FINDINGS,
    MODE_DELTA,
    MODE_FULL,
    REASON_DELTA_EMPTY,
    REASON_NON_FAST_FORWARD,
    REASON_RANGE_UNAVAILABLE,
    REASON_RANGE_UNSUPPORTED,
    REASON_SAME_HEAD,
    DeltaContext,
    carried_findings,
    render_delta_note,
    resolve_delta,
)

_SINCE = "1" * 40
_HEAD = "2" * 40
_BASE = "3" * 40


def _finding(file="a.py", line=3, rule="R1", severity="blocking", message="bad"):
    return {"file": file, "line": line, "rule_id": rule, "severity": severity, "message": message}


def _acquired(head=_HEAD) -> AcquiredPr:
    return AcquiredPr(
        owner="some-owner", repo="some-repo", pr_number=7, base_sha=_BASE, head_sha=head,
        diff_text="full diff text",
    )


class _Backend:
    def __init__(self, *, fast_forward=True, diff="delta diff text", error=None):
        self.fast_forward = fast_forward
        self.diff = diff
        self.error = error
        self.requested: list[tuple[str, str]] = []

    def fetch_range_diff(self, *, owner, repo, base_sha, head_sha):
        self.requested.append((base_sha, head_sha))
        if self.error is not None:
            raise self.error
        return RangeDiff(
            base_sha=base_sha, head_sha=head_sha, fast_forward=self.fast_forward,
            diff_text=self.diff if self.fast_forward else "",
        )


def test_a_fast_forward_is_reviewed_as_a_delta_against_the_last_reviewed_head():
    backend = _Backend()

    resolution = resolve_delta(backend, _acquired(), _SINCE, [_finding()])

    assert resolution.mode == MODE_DELTA
    assert backend.requested == [(_SINCE, _HEAD)]
    assert resolution.acquired.diff_text == "delta diff text"
    assert resolution.acquired.base_sha == _SINCE
    assert resolution.acquired.head_sha == _HEAD
    assert resolution.stage_fields() == {"mode": "delta", "since_head": _SINCE}


def test_praise_is_not_an_open_finding():
    findings = [_finding(), _finding(rule="R2", severity="praise"), _finding(rule="R3", severity="nit")]

    resolution = resolve_delta(_Backend(), _acquired(), _SINCE, findings)

    assert [f["rule_id"] for f in resolution.context.open_findings] == ["R1", "R3"]


def test_the_same_head_falls_back_to_the_full_diff_without_reading_a_range():
    backend = _Backend()

    resolution = resolve_delta(backend, _acquired(head=_SINCE), _SINCE, [])

    assert resolution.mode == MODE_FULL
    assert resolution.reason == REASON_SAME_HEAD
    assert resolution.acquired.diff_text == "full diff text"
    assert backend.requested == []
    assert resolution.stage_fields() == {"mode": "full", "reason": REASON_SAME_HEAD}


def test_a_head_that_is_not_a_fast_forward_falls_back_to_the_full_diff():
    resolution = resolve_delta(_Backend(fast_forward=False), _acquired(), _SINCE, [_finding()])

    assert resolution.context is None
    assert resolution.reason == REASON_NON_FAST_FORWARD
    assert resolution.acquired.diff_text == "full diff text"


def test_an_unreadable_range_falls_back_to_the_full_diff_and_keeps_the_cause():
    backend = _Backend(error=AcquireFetchError("cannot compare: HTTP 404"))

    resolution = resolve_delta(backend, _acquired(), _SINCE, [])

    assert resolution.context is None
    assert resolution.reason == REASON_RANGE_UNAVAILABLE
    assert "HTTP 404" in resolution.detail


def test_a_range_with_no_reviewable_change_falls_back_to_the_full_diff():
    resolution = resolve_delta(_Backend(diff="  \n"), _acquired(), _SINCE, [])

    assert resolution.context is None
    assert resolution.reason == REASON_DELTA_EMPTY


def test_a_transport_that_cannot_read_a_range_falls_back_to_the_full_diff():
    resolution = resolve_delta(object(), _acquired(), _SINCE, [])

    assert resolution.context is None
    assert resolution.reason == REASON_RANGE_UNSUPPORTED


def test_the_note_names_the_last_head_and_lists_every_open_finding():
    context = DeltaContext(_SINCE, (_finding(), _finding(file="b.py", line=9, rule="R2", severity="nit", message="meh")))

    note = render_delta_note(context)

    assert _SINCE[:12] in note
    assert "ONLY the changes made since" in note
    assert "- a.py:3 [R1] (blocking) bad" in note
    assert "- b.py:9 [R2] (nit) meh" in note


def test_the_note_says_so_when_nothing_is_open():
    assert "- none" in render_delta_note(DeltaContext(_SINCE, ()))


def test_the_listing_is_bounded_and_says_how_many_were_left_out():
    findings = tuple(_finding(line=n + 1, rule=f"R{n}") for n in range(MAX_LISTED_FINDINGS + 3))

    note = render_delta_note(DeltaContext(_SINCE, findings))

    assert note.count("\n- ") == MAX_LISTED_FINDINGS + 1
    assert "and 3 more not listed" in note


def test_no_open_finding_is_dropped_past_the_listing_cap_even_on_a_touched_file():
    total = MAX_LISTED_FINDINGS + 25
    findings = tuple(_finding(file="a.py", line=n + 1, rule=f"R{n}") for n in range(total))
    context = DeltaContext(_SINCE, findings)

    carried = carried_findings(context, {"a.py"})

    # The first MAX_LISTED_FINDINGS are shown to the reviewer to re-judge; every
    # one beyond the cap is carried forward, so nothing is silently lost.
    assert [f["rule_id"] for f in carried] == [f"R{n}" for n in range(MAX_LISTED_FINDINGS, total)]
    listed_rules = {f"[R{n}]" for n in range(MAX_LISTED_FINDINGS)}
    note = render_delta_note(context)
    assert all(rule in note for rule in listed_rules)
    assert "and 25 more not listed; they are carried forward" in note


def test_only_findings_on_files_the_delta_did_not_touch_are_carried():
    context = DeltaContext(_SINCE, (_finding(file="a.py"), _finding(file="b.py", rule="R2")))

    carried = carried_findings(context, {"a.py"})

    assert [f["file"] for f in carried] == ["b.py"]
    assert carried[0] is not context.open_findings[1]
    assert carried_findings(context, {"a.py", "b.py"}) == []
