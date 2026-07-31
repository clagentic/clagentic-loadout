"""test_merge_attestation.py — unit coverage for
clagentic_loadout.merge.attestation.build_attestation_body (lr-20e866).

Pure-function coverage only: no I/O, no platform selection, no fail-open
handling -- those live in merge.verb and are covered in
test_merge_verb_attestation.py instead. This file asserts the attestation
body's CONTENT contract: tool identity + version, gated/merged SHAs,
required-reviewer logins, CI disposition, and the lore-free conformance
CLAUDE.md rule 6a requires (no lore vocabulary, no crew vocabulary, no
LORE_* references anywhere in the rendered body).

TABLE RENDERING (lr-0b77dd): the body renders its fields as a markdown
field/value table, not a bullet list -- see TestFieldValueTableRendering
below for the structural assertions; every other class in this file asserts
CONTENT (which labels/values appear), unaffected by the rendering format
since a table row and a bullet line both carry the same label/value text.
"""

from __future__ import annotations

from clagentic_loadout.merge.attestation import ATTESTATION_HEADER, build_attestation_body

_FULL_SHA = "a" * 40
_OTHER_FULL_SHA = "b" * 40


class TestAttestationHeaderAndVersion:
    def test_header_names_the_tool(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="9.9.9",
        )
        assert ATTESTATION_HEADER in body
        assert "clagentic-loadout" in body

    def test_pinned_version_is_used_verbatim(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="combined_state='success' (1 status(es), 0 run(s))",
            version="1.2.3",
        )
        assert "v1.2.3" in body

    def test_omitted_version_falls_back_to_get_version(self):
        from clagentic_loadout._version import get_version

        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
        )
        assert f"v{get_version()}" in body


class TestShaFields:
    def test_gated_and_merged_sha_both_present(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_OTHER_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert _FULL_SHA in body
        assert _OTHER_FULL_SHA in body
        assert "Gated HEAD SHA" in body
        assert "Merged SHA" in body

    def test_same_gated_and_merged_sha_both_still_rendered(self):
        # Today loadout-merge's merge_pr backends do not return a distinct
        # post-merge commit SHA, so callers pass the same value for both --
        # the two fields are kept SEPARATE in the signature (not collapsed),
        # this test only proves both labels render even when the values are
        # identical.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert body.count(_FULL_SHA) == 2


class TestRequiredReviewerLogins:
    def test_logins_rendered_when_present(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["peaches", "bobbie"],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "peaches" in body
        assert "bobbie" in body
        assert "Reviews" in body

    def test_empty_reviewer_list_omits_reviews_line(self):
        # lr-b6da32: an empty reviewer list is no longer rendered with a
        # "(none required)" placeholder -- that framing misread as
        # "unreviewed" even on merges that did carry a clean review. The
        # line is omitted entirely, matching every other optional field.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=[],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "Reviews" not in body
        assert "(none required)" not in body

    def test_default_reviewer_list_is_empty(self):
        # required_reviewer_logins is optional -- omitting it entirely must
        # be byte-identical to passing an empty list.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "Reviews" not in body
        assert "(none required)" not in body


class TestCiDispositionPassthrough:
    def test_ci_disposition_rendered_verbatim(self):
        disposition = "combined_state='success' (3 status(es), 2 run(s))"
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition=disposition,
            version="1.0.0",
        )
        assert disposition in body


class TestWorkItemLines:
    """lr-eb22f3: task_id and issue_number each render independently and
    are OMITTED (not placeholdered) when absent -- same omit-cleanly
    treatment the reviewer-logins field now also uses (lr-b6da32)."""

    def test_task_id_rendered_when_present(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            task_id="lr-eb22f3",
            version="1.0.0",
        )
        assert "lr-eb22f3" in body
        assert "task_id" in body

    def test_task_id_omitted_cleanly_when_absent(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "task_id" not in body
        assert "(not recorded)" not in body

    def test_issue_number_rendered_when_present(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            issue_number=42,
            version="1.0.0",
        )
        assert "#42" in body
        assert "Issue" in body

    def test_issue_number_omitted_cleanly_when_absent(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "Issue" not in body
        assert "(not recorded)" not in body

    def test_both_rendered_together(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            task_id="lr-eb22f3",
            issue_number=42,
            version="1.0.0",
        )
        assert "lr-eb22f3" in body
        assert "#42" in body

    def test_issue_number_zero_is_not_treated_as_absent(self):
        # issue_number=0 is not a realistic git-host issue number, but the
        # implementation must key off `is not None`, never truthiness, so a
        # boundary value is never silently swallowed.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            issue_number=0,
            version="1.0.0",
        )
        assert "#0" in body


class TestFieldValueTableRendering:
    """lr-0b77dd: the body renders as a markdown `| Field | Value |` table
    (restoring the retired reference gate-note's presentation), not a
    bullet list -- and carries no 'Authorize rationale' row (named
    trade-off: no lore-free, caller-identity-free source for it exists in
    merge.verb today; see this module's docstring for the full rationale)."""

    def test_body_renders_a_markdown_table_not_a_bullet_list(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["some-reviewer"],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            task_id="lr-0b77dd",
            issue_number=42,
            version="1.0.0",
        )
        assert "| Field | Value |" in body
        assert "| --- | --- |" in body
        assert "| Gated HEAD SHA | `" in body
        assert "| Merged SHA | `" in body
        assert "| Reviews | some-reviewer |" in body
        assert "| CI status | no-runner-by-design (0 commit-status entries at HEAD) |" in body
        assert "| task_id | lr-0b77dd |" in body
        assert "| Issue | #42 |" in body
        for line in body.splitlines():
            assert not line.startswith("- "), f"found a bullet-list line: {line!r}"

    def test_omitted_fields_are_missing_rows_not_placeholdered_values(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "| Reviews |" not in body
        assert "| task_id |" not in body
        assert "| Issue |" not in body

    def test_no_authorize_rationale_row(self):
        # Named trade-off (lr-0b77dd): sourcing a rationale line would
        # require either a new pre_checks_summary-shaped parameter
        # (reintroducing a seam merge.verb deliberately stripped) or
        # restating fields already rendered above -- neither is a genuine
        # rationale, so the row is omitted entirely rather than faked.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["some-reviewer"],
            ci_disposition="combined_state='success' (1 status(es), 0 run(s))",
            task_id="lr-0b77dd",
            issue_number=42,
            version="1.0.0",
        )
        lowered = body.lower()
        assert "authorize rationale" not in lowered
        assert "rationale" not in lowered


class TestTableCellEscaping:
    """Pre-merge security-audit finding (lr-0b77dd): the table format
    sharpens an unescaped `|` or newline in an interpolated value from
    cosmetic (harmless on the old bullet form) to STRUCTURAL row/cell
    injection. required_reviewer_logins and task_id are merger-role-trusted
    CLI input today (not a live exploit), but the escaping is a two-line
    hardening fix folded into this same rewrite rather than deferred."""

    def test_pipe_in_reviewer_login_does_not_break_out_of_its_cell(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["evil|Injected|Cell"],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "| Reviews | evil\\|Injected\\|Cell |" in body
        # exactly one row for Reviews -- an unescaped '|' would have split
        # this into extra columns / rows instead of one intact cell.
        reviews_lines = [line for line in body.splitlines() if line.startswith("| Reviews")]
        assert len(reviews_lines) == 1

    def test_newline_in_reviewer_login_collapses_to_a_single_row(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["multi\nline\r\nlogin"],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            version="1.0.0",
        )
        assert "| Reviews | multi line login |" in body
        reviews_lines = [line for line in body.splitlines() if "Reviews" in line]
        assert len(reviews_lines) == 1

    def test_pipe_and_newline_in_task_id_render_as_a_single_intact_row(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            task_id="lr-0b77dd|evil\nrow",
            version="1.0.0",
        )
        assert "| task_id | lr-0b77dd\\|evil row |" in body
        task_id_lines = [line for line in body.splitlines() if "task_id" in line]
        assert len(task_id_lines) == 1

    def test_table_structure_survives_hostile_values_in_every_untrusted_field(self):
        # Every row present, every row a single well-formed '| Field |
        # Value |' line -- proves the escaping keeps the WHOLE table intact,
        # not just the one field under test above.
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["a|b", "c\nd"],
            ci_disposition="no-runner-by-design (0 commit-status entries at HEAD)",
            task_id="x|y\nz",
            issue_number=1,
            version="1.0.0",
        )
        table_lines = [line for line in body.splitlines() if line.startswith("|")]
        # header + separator + Gated HEAD SHA + Merged SHA + Reviews +
        # CI status + task_id + Issue == 8 rows, no more, no fewer.
        assert len(table_lines) == 8
        for line in table_lines:
            assert line.startswith("| ") and line.endswith(" |")


class TestLoreFreeConformance:
    """CLAUDE.md rule 6a: zero lore references, zero LORE_* env, zero crew
    vocabulary anywhere in the rendered attestation body -- this is PURE
    git-host/product data, distinct from the reference gate-note's task-signal
    half (never ported here, see merge.verb's module docstring point 5)."""

    _FORBIDDEN_SUBSTRINGS = ("lore", "LORE_", "crew", "Archivist", "Sentinel")

    def test_rendered_body_carries_no_lore_or_crew_vocabulary(self):
        body = build_attestation_body(
            gated_head_sha=_FULL_SHA,
            merged_sha=_FULL_SHA,
            required_reviewer_logins=["peaches", "bobbie"],
            ci_disposition="combined_state='success' (1 status(es), 0 run(s))",
            version="1.0.0",
        )
        lowered = body.lower()
        for forbidden in self._FORBIDDEN_SUBSTRINGS:
            assert forbidden.lower() not in lowered, f"found forbidden token {forbidden!r} in body"
