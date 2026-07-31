"""test_transport_note_compose.py — tests for
clagentic_loadout.transport.note_compose (lr-10a996, BODY-TRANSPORT half).

Coverage:
  - build_caller_note_block: shape, empty/whitespace-only rejection.
  - build_composed_body: no tracking id -> prose unchanged; tracking id
    present -> tool-constructed fence appended, never caller-authored.
  - parse_caller_note_block: round-trips build_composed_body's output;
    malformed/missing-field/non-object payloads raise ValueError; no block
    present returns None.
  - The whole point of this module: the STRUCTURED INPUT (prose +
    caller_tracking_id) never needs a backtick anywhere -- only the
    tool-constructed OUTPUT contains the fence.
  - Regression (lr-a01aec): a caller_tracking_id containing literal
    backticks -- which git_host_api._SAFE_CALLER_TRACKING_ID_RE permits,
    since it forbids only whitespace/control characters -- can never forge
    or escape a markdown fence in the composed body, because that same
    regex forbids newlines and a fence delimiter only takes effect at the
    start of a line.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import note_compose
from clagentic_loadout.transport.git_host_api import _SAFE_CALLER_TRACKING_ID_RE


class TestBuildCallerNoteBlock:
    def test_builds_fenced_block(self):
        block = note_compose.build_caller_note_block("lr-10a996")
        assert "```loadout-note" in block
        assert '"caller_tracking_id": "lr-10a996"' in block
        assert block.endswith("```\n")

    def test_empty_string_rejected(self):
        with pytest.raises(ValueError):
            note_compose.build_caller_note_block("")

    def test_whitespace_only_rejected(self):
        with pytest.raises(ValueError):
            note_compose.build_caller_note_block("   ")


class TestBuildComposedBody:
    def test_no_tracking_id_returns_prose_unchanged(self):
        assert note_compose.build_composed_body("hello", caller_tracking_id=None) == "hello"

    def test_empty_tracking_id_returns_prose_unchanged(self):
        assert note_compose.build_composed_body("hello", caller_tracking_id="") == "hello"

    def test_whitespace_tracking_id_returns_prose_unchanged(self):
        assert note_compose.build_composed_body("hello", caller_tracking_id="   ") == "hello"

    def test_tracking_id_appends_tool_owned_fence(self):
        composed = note_compose.build_composed_body("Status update.", caller_tracking_id="lr-abc123")
        assert composed.startswith("Status update.\n")
        assert "```loadout-note" in composed
        assert '"caller_tracking_id": "lr-abc123"' in composed

    def test_caller_input_never_needs_a_backtick(self):
        # The whole point: prose + caller_tracking_id, as ordinary strings,
        # contain zero backticks -- the fence is added BY THE TOOL.
        prose = "Build green, all tests pass."
        tracking_id = "lr-10a996"
        assert "`" not in prose
        assert "`" not in tracking_id
        composed = note_compose.build_composed_body(prose, caller_tracking_id=tracking_id)
        assert "`" in composed  # only in the tool-appended fence

    def test_backtick_bearing_tracking_id_cannot_forge_or_escape_fence(self):
        """Regression test (lr-a01aec, BOBBIE non-blocking finding, PR #46
        comment 13130): git_host_api._SAFE_CALLER_TRACKING_ID_RE
        (^[\\x21-\\x7e]{1,128}$) permits backtick characters -- it only
        forbids whitespace/control characters (which includes newline,
        \\x0a, since that is outside the \\x21-\\x7e printable range). A
        caller_tracking_id containing literal backticks, INCLUDING a
        triple-backtick run, is therefore a value the CLI preflight check
        would accept and pass through to build_composed_body unchanged.

        The property this test locks: because the id can never carry a
        newline, it can never occupy the START of a line inside the
        composed body -- and a markdown fence delimiter only opens/closes
        when a line BEGINS with ``` -- so a caller can never use this field
        to forge a fence boundary of its own or prematurely close the
        tool-authored ```loadout-note``` fence. The id's backticks always
        land mid-line, inside the fenced block's JSON payload, inert.
        """
        # No newline (regex-illegal); a run of backticks well past the
        # triple-backtick fence-delimiter length, plus a fence-looking
        # language tag, to maximize the chance of a forged/escaped fence
        # if the invariant did NOT hold.
        tracking_id = "lr-9999`````evil-fence```loadout-note-forged"
        assert _SAFE_CALLER_TRACKING_ID_RE.match(tracking_id), (
            "fixture must be a value the CLI regex actually permits"
        )
        assert "\n" not in tracking_id

        composed = note_compose.build_composed_body(
            "Some prose with no backticks.", caller_tracking_id=tracking_id
        )

        lines = composed.splitlines()
        fence_lines = [line for line in lines if line.startswith("```")]
        # Exactly the tool-authored open + close of the single loadout-note
        # fence -- nothing the tracking id contributed opened or closed an
        # additional fence boundary.
        assert fence_lines == ["```loadout-note", "```"]

        # The tracking id's backticks are confined to a single line, deep
        # inside the fenced JSON payload -- never at the start of a line.
        payload_lines = [
            line for line in lines if "evil-fence" in line or "forged" in line
        ]
        assert len(payload_lines) == 1
        assert not payload_lines[0].startswith("```")

        # Round-trip: parse_caller_note_block recovers the tracking id
        # VERBATIM (backticks and all) as inert JSON string data -- it was
        # never interpreted as markdown structure.
        parsed = note_compose.parse_caller_note_block(composed)
        assert parsed == {"caller_tracking_id": tracking_id}


class TestParseCallerNoteBlock:
    def test_round_trips_composed_body(self):
        composed = note_compose.build_composed_body("prose here", caller_tracking_id="lr-42")
        parsed = note_compose.parse_caller_note_block(composed)
        assert parsed == {"caller_tracking_id": "lr-42"}

    def test_no_block_returns_none(self):
        assert note_compose.parse_caller_note_block("just prose, no fence") is None

    def test_malformed_json_raises_value_error(self):
        body = "prose\n```loadout-note\nnot json\n```\n"
        with pytest.raises(ValueError):
            note_compose.parse_caller_note_block(body)

    def test_non_object_json_raises_value_error(self):
        body = 'prose\n```loadout-note\n["a", "b"]\n```\n'
        with pytest.raises(ValueError):
            note_compose.parse_caller_note_block(body)

    def test_missing_required_field_raises_value_error(self):
        body = 'prose\n```loadout-note\n{}\n```\n'
        with pytest.raises(ValueError):
            note_compose.parse_caller_note_block(body)

    def test_extra_field_raises_value_error(self):
        body = (
            'prose\n```loadout-note\n'
            '{"caller_tracking_id": "lr-1", "extra": "nope"}\n```\n'
        )
        with pytest.raises(ValueError):
            note_compose.parse_caller_note_block(body)

    def test_last_block_wins_on_multiple_matches(self):
        first = note_compose.build_caller_note_block("lr-first")
        second = note_compose.build_caller_note_block("lr-second")
        body = f"prose\n{first}\n{second}"
        parsed = note_compose.parse_caller_note_block(body)
        assert parsed == {"caller_tracking_id": "lr-second"}

    def test_fence_language_tag_must_be_same_line_as_backticks(self):
        # Mirrors merge.verdict's own same-line-fence-tag requirement --
        # a tag on its own line is correctly treated as "no block found."
        body = 'prose\n```\nloadout-note\n{"caller_tracking_id": "lr-1"}\n```\n'
        assert note_compose.parse_caller_note_block(body) is None
