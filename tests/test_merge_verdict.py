"""test_merge_verdict.py — tests for clagentic_loadout.merge.verdict
(lr-885f, Wave B slice 4).

Coverage:
  - build_verdict_block: valid statuses round-trip through parse_verdict_block;
    invalid review_status rejected before ever constructing a block.
  - parse_verdict_block: no-fence -> None; same-line-tag requirement (a tag
    on its own line is NOT a match); malformed JSON / non-object / missing
    field / invalid enum all raise VerdictMalformedError; only the LAST
    fenced block in a body wins (retry semantics).
  - read_reviewer_verdict (the merge-gate assertion entry point):
      * authorship verified by user.login, never comment body claims
      * missing comment from expected login -> VerdictMissingError
      * comment present but no fenced block -> VerdictMissingError
      * malformed head_sha stamp -> VerdictMalformedError (distinct from a
        stale-gate condition)
      * head_sha mismatch (stale) -> VerdictStaleError
      * blocking review_status parses successfully (assert_clean_verdict is
        the separate enforcement step) and IS refused by assert_clean_verdict
      * clean, current-SHA, correct-author verdict -> success, fields correct
      * role/content consistency (lr-23fe19, defense-in-depth ON TOP OF the
        user.login binding): expected_reviewer_name given + block's own
        'reviewer' field disagrees -> VerdictRoleMismatchError (replays the
        console PR #332 shape: a security-audit body's 'reviewer' field
        posted under the code-reviewer App's correct login); a genuine,
        matching verdict still passes; the parameter is opt-in (omitting it
        is a no-op, unaffected existing callers/tests keep passing)
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.errors import (
    VerdictBlockingError,
    VerdictMalformedError,
    VerdictMissingError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)
from clagentic_loadout.merge.verdict import (
    ReviewerVerdict,
    assert_clean_verdict,
    assert_single_own_verdict_block,
    assert_verdict_block_count_at_most_one,
    build_findings_verdict_body,
    build_verdict_block,
    find_all_verdict_blocks,
    parse_verdict_block,
    read_reviewer_verdict,
)

_FULL_SHA = "a" * 40
_OTHER_FULL_SHA = "b" * 40


def _comment(comment_id: int, login: str, body: str, created_at: str | None = None) -> dict:
    # created_at defaults to a value derived from comment_id so existing
    # call sites that don't care about ordering still get a valid,
    # monotonic-with-id timestamp (id and created_at agreeing is the common
    # case; tests that need id/created_at to DISAGREE, e.g. an out-of-order
    # API response, pass created_at explicitly).
    if created_at is None:
        created_at = f"2026-01-01T00:00:{comment_id:02d}Z"
    return {
        "id": comment_id,
        "user": {"login": login},
        "body": body,
        "created_at": created_at,
    }


class TestBuildVerdictBlock:
    def test_round_trips_through_parse(self):
        block = build_verdict_block("some-reviewer", "clean", _FULL_SHA, 42)
        parsed = parse_verdict_block(f"some prose\n{block}\nmore prose")
        assert parsed == {
            "reviewer": "some-reviewer",
            "review_status": "clean",
            "head_sha": _FULL_SHA,
            "pr_number": 42,
        }

    def test_invalid_status_rejected_before_construction(self):
        with pytest.raises(ValueError):
            build_verdict_block("some-reviewer", "not-a-status", _FULL_SHA, 42)


class TestParseVerdictBlock:
    def test_no_fence_returns_none(self):
        assert parse_verdict_block("just prose, no fence at all") is None

    def test_fence_tag_on_own_line_is_not_a_match(self):
        # The fence language tag MUST be on the SAME LINE as the opening
        # triple-backticks. A tag on its own separate line is a different
        # (non-conformant) shape and must be treated as "no block found."
        body = '```\nreview-result\n{"reviewer":"x","review_status":"clean","head_sha":"' + _FULL_SHA + '","pr_number":1}\n```'
        assert parse_verdict_block(body) is None

    def test_malformed_json_raises(self):
        body = "```review-result\n{not valid json\n```"
        with pytest.raises(VerdictMalformedError):
            parse_verdict_block(body)

    def test_non_object_json_raises(self):
        body = "```review-result\n[1, 2, 3]\n```"
        with pytest.raises(VerdictMalformedError):
            parse_verdict_block(body)

    def test_missing_required_field_raises(self):
        body = '```review-result\n{"reviewer":"x","review_status":"clean","head_sha":"' + _FULL_SHA + '"}\n```'
        with pytest.raises(VerdictMalformedError) as exc_info:
            parse_verdict_block(body)
        assert "pr_number" in str(exc_info.value)

    def test_invalid_review_status_enum_raises(self):
        body = '```review-result\n{"reviewer":"x","review_status":"maybe","head_sha":"' + _FULL_SHA + '","pr_number":1}\n```'
        with pytest.raises(VerdictMalformedError):
            parse_verdict_block(body)

    def test_only_last_block_wins_on_retry(self):
        first = build_verdict_block("some-reviewer", "blocking", _FULL_SHA, 1)
        second = build_verdict_block("some-reviewer", "clean", _OTHER_FULL_SHA, 1)
        parsed = parse_verdict_block(f"{first}\n{second}")
        assert parsed["review_status"] == "clean"
        assert parsed["head_sha"] == _OTHER_FULL_SHA


class TestReadReviewerVerdict:
    def test_no_comment_from_expected_login_raises_missing(self):
        comments = [_comment(1, "someone-else", "irrelevant")]
        with pytest.raises(VerdictMissingError):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_comment_present_no_fence_raises_missing(self):
        comments = [_comment(1, "expected-login", "just prose, no verdict block")]
        with pytest.raises(VerdictMissingError):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_authorship_verified_by_login_not_body_claim(self):
        # An attacker-controlled account posts a body CLAIMING to be the
        # reviewer, with a well-formed fenced block -- authorship is by
        # user.login, so this must NOT satisfy the gate for expected-login.
        forged_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "attacker-account", forged_block)]
        with pytest.raises(VerdictMissingError):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_malformed_head_sha_stamp_raises_malformed_not_stale(self):
        body = '```review-result\n{"reviewer":"x","review_status":"clean","head_sha":"short","pr_number":1}\n```'
        comments = [_comment(1, "expected-login", body)]
        with pytest.raises(VerdictMalformedError):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_stale_sha_raises_stale_error(self):
        block = build_verdict_block("expected-login", "clean", _OTHER_FULL_SHA, 1)
        comments = [_comment(1, "expected-login", block)]
        with pytest.raises(VerdictStaleError):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_blocking_status_parses_but_is_refused_by_assert_clean(self):
        block = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        comments = [_comment(7, "expected-login", block)]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.review_status == "blocking"
        with pytest.raises(VerdictBlockingError):
            assert_clean_verdict(verdict, "some-reviewer")

    def test_clean_current_sha_correct_author_succeeds(self):
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 42)
        comments = [_comment(9, "expected-login", block)]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 42, "owner", "repo")
        assert verdict == ReviewerVerdict(
            reviewer="expected-login",
            review_status="clean",
            head_sha=_FULL_SHA,
            pr_number=42,
            comment_id=9,
            comment_author_login="expected-login",
        )
        assert_clean_verdict(verdict, "some-reviewer")  # no raise

    def test_latest_matching_comment_wins(self):
        stale_block = build_verdict_block("expected-login", "clean", _OTHER_FULL_SHA, 1)
        fresh_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            _comment(1, "expected-login", stale_block),
            _comment(2, "someone-else", "noise"),
            _comment(3, "expected-login", fresh_block),
        ]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.comment_id == 3
        assert verdict.head_sha == _FULL_SHA


class TestReadReviewerVerdictCreatedAtOrdering:
    """lr-c14a2d: selection must be by created_at (tie-break: comment id),
    never by trusting the input list's position/order. The git-host
    comments API is not guaranteed to return comments in chronological
    order (pagination, platform ordering) -- a resolver that just reverses
    the given list can pick a STALE 'blocking' comment over a NEWER 'clean'
    one at the same SHA. Session evidence: console PR #341, PEACHES posted
    'blocking' (comment 4980366091) on a false claim, then a corrected
    'clean' (4980399228) at the same SHA -- the merge stayed blocked
    because the resolver did not deterministically prefer the newer clean."""

    def test_out_of_order_list_older_blocking_after_newer_clean_resolves_clean(self):
        # Simulates an out-of-order API response: the OLDER 'blocking'
        # comment appears LATER in the list than the NEWER 'clean' one.
        # created_at is the sole source of truth for "latest" -- list
        # position must not matter.
        blocking_block = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        clean_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            _comment(2, "expected-login", clean_block, created_at="2026-01-01T00:00:10Z"),
            # Older created_at than comment 2, but LATER in list order --
            # and a HIGHER comment id, so a naive id- or position-based
            # tie-break would also get this wrong.
            _comment(3, "expected-login", blocking_block, created_at="2026-01-01T00:00:05Z"),
        ]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.review_status == "clean"
        assert verdict.comment_id == 2

    def test_out_of_order_list_github_payload_shape(self):
        # GitHub issue-comments payload shape: 'Z'-suffixed UTC ISO 8601
        # created_at, integer id, nested user.login.
        blocking_block = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        clean_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            {
                "id": 4980399228,
                "user": {"login": "expected-login"},
                "body": clean_block,
                "created_at": "2026-07-15T14:32:10Z",
                "html_url": "https://github.com/owner/repo/pull/1#issuecomment-4980399228",
            },
            {
                "id": 4980366091,
                "user": {"login": "expected-login"},
                "body": blocking_block,
                "created_at": "2026-07-15T13:05:00Z",
                "html_url": "https://github.com/owner/repo/pull/1#issuecomment-4980366091",
            },
        ]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.review_status == "clean"
        assert verdict.comment_id == 4980399228

    def test_out_of_order_list_forgejo_payload_shape(self):
        # Forgejo issue-comments payload shape: same field names/format as
        # GitHub (created_at ISO 8601 'Z'-suffixed, nested user.login) --
        # both platforms share this shape (merge.github_backend /
        # merge.forgejo_backend docstrings), but this is asserted here
        # explicitly per lr-c14a2d's requirement to verify both platforms.
        blocking_block = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        clean_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            {
                "id": 205,
                "user": {"login": "expected-login"},
                "body": clean_block,
                "created_at": "2026-07-15T14:32:10Z",
                "html_url": "https://forgejo.example/owner/repo/issues/1#issuecomment-205",
            },
            {
                "id": 198,
                "user": {"login": "expected-login"},
                "body": blocking_block,
                "created_at": "2026-07-15T13:05:00Z",
                "html_url": "https://forgejo.example/owner/repo/issues/1#issuecomment-198",
            },
        ]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.review_status == "clean"
        assert verdict.comment_id == 205

    def test_tie_break_on_comment_id_when_created_at_equal(self):
        stale_block = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        fresh_block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        same_ts = "2026-01-01T00:00:00Z"
        comments = [
            _comment(10, "expected-login", stale_block, created_at=same_ts),
            _comment(11, "expected-login", fresh_block, created_at=same_ts),
        ]
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.comment_id == 11
        assert verdict.review_status == "clean"

    def test_missing_created_at_on_matching_comment_fails_closed(self):
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comment = {"id": 1, "user": {"login": "expected-login"}, "body": block}
        with pytest.raises(VerdictMalformedError, match="created_at"):
            read_reviewer_verdict([comment], "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_unparseable_created_at_on_matching_comment_fails_closed(self):
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comment = {
            "id": 1,
            "user": {"login": "expected-login"},
            "body": block,
            "created_at": "not-a-timestamp",
        }
        with pytest.raises(VerdictMalformedError, match="created_at"):
            read_reviewer_verdict([comment], "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_multiple_same_reviewer_comments_is_not_treated_as_an_error(self):
        # Contrast with assert_single_own_verdict_block (a DIFFERENT
        # concern: multiple verdict BLOCKS inside one comment BODY). Here,
        # multiple separate COMMENTS from the same reviewer login is NORMAL
        # supersede behavior -- a reviewer re-posting a corrected verdict --
        # and must resolve cleanly to the newest, not raise.
        older = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        newer = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            _comment(1, "expected-login", older, created_at="2026-01-01T00:00:01Z"),
            _comment(2, "expected-login", newer, created_at="2026-01-01T00:00:02Z"),
        ]
        # No raise -- multiple same-reviewer comments resolve deterministically.
        verdict = read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")
        assert verdict.review_status == "clean"
        assert verdict.comment_id == 2

    def test_assert_single_own_verdict_block_unaffected_by_multiple_comments(self):
        # assert_single_own_verdict_block operates on ONE comment body, not
        # the comment LIST -- confirms the two concerns don't interact: a
        # single (already-selected) comment's body with exactly one block
        # still passes even though the PR overall has multiple same-reviewer
        # comments (tested above via read_reviewer_verdict).
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        assert_single_own_verdict_block(block, "expected-login")  # no raise


class TestReadReviewerVerdictRoleConsistency:
    """Defense-in-depth role/content consistency (lr-23fe19), layered ON TOP
    OF the user.login authorship binding tested above -- never a
    replacement for it. CONCRETE EVIDENCE this guards against: console PR
    #332 (lr-f00c6f Fault 3, re-scoped by lr-23fe19 comment #1) -- a
    security-audit verdict body ('reviewer': 'bobbie') was posted under the
    code-reviewer App's own (correct) login after a shared body-staging path
    was clobbered. The user.login check alone passes in that shape (the App
    identity IS correct); only a check of the fence's own self-declared
    'reviewer' field against the required-reviewer name catches it."""

    def test_security_audit_body_under_reviewer_login_raises_role_mismatch(self):
        # THE PR #332 SHAPE: bobbie's (security-audit) verdict content,
        # posted under peaches's (code-review) App login. The login is
        # exactly right for the 'peaches' required-reviewer slot -- only the
        # fence's own 'reviewer' field ('bobbie') betrays the mismatch.
        clobbered_block = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "peaches-login", clobbered_block)]
        with pytest.raises(VerdictRoleMismatchError, match="ROLE/CONTENT MISMATCH"):
            read_reviewer_verdict(
                comments,
                "peaches-login",
                _FULL_SHA,
                1,
                "owner",
                "repo",
                expected_reviewer_name="peaches",
            )

    def test_genuine_matching_verdict_passes(self):
        # The non-adversarial case: peaches's own verdict, posted under
        # peaches's own login, self-declaring 'peaches' -- all three agree.
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "peaches-login", block)]
        verdict = read_reviewer_verdict(
            comments,
            "peaches-login",
            _FULL_SHA,
            1,
            "owner",
            "repo",
            expected_reviewer_name="peaches",
        )
        assert verdict.reviewer == "peaches"
        assert verdict.review_status == "clean"

    def test_case_insensitive_match_passes(self):
        # A fence's 'reviewer' field carrying a different case than the
        # tool-authoritative reviewer name is still a genuine match, not a
        # mismatch -- this check compares identity, not a casing convention.
        block = build_verdict_block("PEACHES", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "peaches-login", block)]
        verdict = read_reviewer_verdict(
            comments,
            "peaches-login",
            _FULL_SHA,
            1,
            "owner",
            "repo",
            expected_reviewer_name="peaches",
        )
        assert verdict.review_status == "clean"

    def test_omitting_expected_reviewer_name_skips_the_check(self):
        # Back-compat / opt-in: a caller that does not pass
        # expected_reviewer_name is unaffected even when the fence's
        # 'reviewer' field disagrees with the login-matched identity.
        mismatched_block = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "peaches-login", mismatched_block)]
        verdict = read_reviewer_verdict(
            comments, "peaches-login", _FULL_SHA, 1, "owner", "repo"
        )
        assert verdict.reviewer == "bobbie"


class TestReadReviewerVerdictEnforceSingleFence:
    """lr-5260f9: multi-fence refusal (enforce_single_fence), the merge-gate
    CONSUMER-side half of the fix, ENFORCED BY DEFAULT (reviewer override
    of the original opt-in shape, per BOBBIE/PEACHES's blocking finding on
    PR #142: a body with a 'blocking' fence followed by a 'clean' fence
    resolving to 'clean' under last-fence-wins is a gate-bypass primitive,
    not a benign ambiguity a caller should have to opt out of). THE SHAPE
    this replays: a reviewer-verdict comment body carrying TWO fenced
    ```review-result``` blocks (observed against a Forgejo deployment,
    reproduced via a controlled comparison in this task's own comment
    thread) -- refused by default; enforce_single_fence=False (the
    documented per-repo opt-out for legacy multi-fence comments) restores
    the permissive last-fence-wins parse."""

    def test_default_is_on_blocking_then_clean_multi_fence_is_refused(self):
        # THE EVIDENCE FOR THE DEFAULT (BOBBIE's finding): a blocking fence
        # followed by a clean fence must NOT silently resolve to clean when
        # the caller omits enforce_single_fence entirely -- this is the
        # gate-bypass shape, and the safe default catches it without the
        # caller having to know to opt in.
        blocking = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        clean = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "expected-login", f"{blocking}\n{clean}")]
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_default_is_on_pr_485_shape_two_identical_fences_refused(self):
        # THE PR #485 SHAPE: two IDENTICAL fences back to back (the actual
        # producer-side bug this task's other half closes) -- refused by
        # default, with no enforce_single_fence argument passed at all.
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "expected-login", f"{block}\n{block}")]
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            read_reviewer_verdict(comments, "expected-login", _FULL_SHA, 1, "owner", "repo")

    def test_default_is_on_single_fence_body_still_passes(self):
        # Non-adversarial, ordinary case: the default never refuses a
        # genuinely single-fence comment -- every existing well-formed
        # caller is unaffected by the flip.
        block = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "expected-login", block)]
        verdict = read_reviewer_verdict(
            comments, "expected-login", _FULL_SHA, 1, "owner", "repo"
        )
        assert verdict.review_status == "clean"

    def test_explicit_opt_out_multi_fence_still_last_fence_wins(self):
        # THE OPT-OUT PATH (merge: enforce_single_verdict_fence: false at
        # the config layer; enforce_single_fence=False here): a repo with
        # legacy multi-fence comments it cannot immediately clean up can
        # still fall back to the pre-lr-5260f9 unconditional last-fence-wins
        # parse, explicitly.
        stale = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        fresh = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [_comment(1, "expected-login", f"{stale}\n{fresh}")]
        verdict = read_reviewer_verdict(
            comments,
            "expected-login",
            _FULL_SHA,
            1,
            "owner",
            "repo",
            enforce_single_fence=False,
        )
        assert verdict.review_status == "clean"

    def test_retry_shape_two_different_comments_unaffected_by_default(self):
        # The count check is PER-COMMENT-BODY, not across the reviewer's
        # comment history -- a normal retry (two SEPARATE comments, each
        # with exactly one fence) is untouched by the default-on
        # enforcement, matching TestReadReviewerVerdictCreatedAtOrdering's
        # existing "not an error" contract for multiple same-reviewer
        # comments.
        older = build_verdict_block("expected-login", "blocking", _FULL_SHA, 1)
        newer = build_verdict_block("expected-login", "clean", _FULL_SHA, 1)
        comments = [
            _comment(1, "expected-login", older, created_at="2026-01-01T00:00:01Z"),
            _comment(2, "expected-login", newer, created_at="2026-01-01T00:00:02Z"),
        ]
        verdict = read_reviewer_verdict(
            comments, "expected-login", _FULL_SHA, 1, "owner", "repo"
        )
        assert verdict.review_status == "clean"
        assert verdict.comment_id == 2


class TestAssertVerdictBlockCountAtMostOne:
    """assert_verdict_block_count_at_most_one (lr-5260f9): the COUNT-ONLY
    primitive assert_single_own_verdict_block now delegates to, and
    read_reviewer_verdict's enforce_single_fence (default True) calls
    directly (no reviewer-identity assertion, since read_reviewer_verdict
    already has its own, more precise role-mismatch check downstream)."""

    def test_zero_blocks_does_not_raise(self):
        # Unlike assert_single_own_verdict_block, an ABSENT block is not
        # this function's concern -- read_reviewer_verdict's own
        # VerdictMissingError already covers "no fence at all" separately.
        assert_verdict_block_count_at_most_one("just prose, no fence")  # no raise

    def test_one_block_does_not_raise(self):
        block = build_verdict_block("reviewer", "clean", _FULL_SHA, 1)
        assert_verdict_block_count_at_most_one(block)  # no raise

    def test_two_identical_blocks_raises(self):
        # THE PR #485 SHAPE exactly: two byte-identical fences.
        block = build_verdict_block("reviewer", "clean", _FULL_SHA, 1)
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            assert_verdict_block_count_at_most_one(f"{block}\n{block}")

    def test_two_different_blocks_raises(self):
        first = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        second = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            assert_verdict_block_count_at_most_one(f"{first}\n{second}")

    def test_error_names_the_opt_out_remedy_inline(self):
        # An operator hitting this cold, at a blocked merge gate, must be
        # able to act from stderr alone -- the message must name the exact
        # config key, its value, and where it lives, without requiring a
        # trip to docs/.
        block = build_verdict_block("reviewer", "clean", _FULL_SHA, 1)
        with pytest.raises(VerdictMalformedError) as exc_info:
            assert_verdict_block_count_at_most_one(f"{block}\n{block}")
        message = str(exc_info.value)
        assert "enforce_single_verdict_fence: false" in message
        assert ".clagentic/loadout/config.yaml" in message
        assert "legacy" in message.lower()
        # Bypass-mechanics clause (bobbie.sast.error-message-remedy): the
        # message must also state the CONSEQUENCE of opting out, not just
        # the mechanism -- last-fence-wins resolving a blocking+clean body
        # to clean is the concrete gate-bypass shape the reader must
        # understand they are re-enabling. Asserting on the stable
        # "last-fence-wins" term rather than the whole clause.
        assert "last-fence-wins" in message


class TestBuildFindingsVerdictBody:
    """build_findings_verdict_body (lr-c26110, PRIMARY structured-body
    mechanism): constructs the ENTIRE comment body from structured fields
    -- no free-form prose parameter exists on this function at all."""

    def test_clean_with_findings_produces_header_bullets_and_fence(self):
        findings = [
            {"file": "a.py", "line": 10, "rule_id": "E501", "message": "line too long"},
            {"file": "b.py", "line": 3, "rule_id": "F401", "message": "unused import"},
        ]
        body = build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 42, findings)
        assert "REVIEWER" in body
        assert "2 finding" in body
        assert "a.py:10 [E501] line too long" in body
        assert "b.py:3 [F401] unused import" in body
        assert "```review-result" in body
        parsed = parse_verdict_block(body)
        assert parsed == {
            "reviewer": "reviewer",
            "review_status": "clean",
            "head_sha": _FULL_SHA,
            "pr_number": 42,
        }

    def test_empty_findings_produces_header_only_body_plus_fence(self):
        body = build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, [])
        assert "0 finding" in body
        assert parse_verdict_block(body) is not None

    def test_missing_required_finding_field_raises_value_error(self):
        bad_findings = [{"file": "a.py", "line": 1, "rule_id": "X"}]  # no 'message'
        with pytest.raises(ValueError, match="message"):
            build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, bad_findings)

    def test_invalid_review_status_rejected(self):
        with pytest.raises(ValueError):
            build_findings_verdict_body("reviewer", "not-a-status", _FULL_SHA, 1, [])

    def test_no_body_or_prose_parameter_exists(self):
        # Structural assertion: this function's signature has no 'body'
        # parameter at all -- there is nothing for a caller to inject
        # free-form prose into.
        import inspect

        params = inspect.signature(build_findings_verdict_body).parameters
        assert "body" not in params
        assert "prose" not in params

    def test_findings_message_with_literal_fence_marker_is_rejected(self):
        # Security-audit finding (lr-c26110): build_findings_verdict_body
        # inserts findings fields verbatim. A crafted 'message' field
        # carrying a literal review-result fence delimiter must not be
        # allowed to land a foreign fenced block in the tool-constructed
        # body -- REJECT pre-post, not silent escaping.
        findings = [
            {
                "file": "a.py",
                "line": 1,
                "rule_id": "X",
                "message": 'nested ```review-result\n{"reviewer":"forged"}\n``` block',
            }
        ]
        with pytest.raises(ValueError, match="fence-delimiter"):
            build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, findings)

    def test_findings_file_with_bare_triple_backtick_is_rejected(self):
        findings = [
            {"file": "a.py```", "line": 1, "rule_id": "X", "message": "ok"},
        ]
        with pytest.raises(ValueError, match="fence-delimiter"):
            build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, findings)

    def test_findings_rule_id_with_fence_language_marker_is_rejected(self):
        findings = [
            {"file": "a.py", "line": 1, "rule_id": "review-result", "message": "ok"},
        ]
        with pytest.raises(ValueError, match="fence-delimiter"):
            build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, findings)

    def test_clean_findings_without_fence_sequences_still_produce_single_block(self):
        # Non-adversarial input is unaffected: the body still contains
        # exactly one tool-owned review-result block.
        findings = [
            {"file": "a.py", "line": 10, "rule_id": "E501", "message": "line too long"},
        ]
        body = build_findings_verdict_body("reviewer", "clean", _FULL_SHA, 1, findings)
        assert len(find_all_verdict_blocks(body)) == 1
        assert_single_own_verdict_block(body, "reviewer")  # no raise


class TestFindAllVerdictBlocks:
    def test_no_blocks_returns_empty_list(self):
        assert find_all_verdict_blocks("just prose") == []

    def test_single_block_returns_one_entry(self):
        block = build_verdict_block("reviewer", "clean", _FULL_SHA, 1)
        blocks = find_all_verdict_blocks(block)
        assert len(blocks) == 1
        assert blocks[0]["reviewer"] == "reviewer"

    def test_two_blocks_returns_two_entries_in_order(self):
        first = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        second = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        blocks = find_all_verdict_blocks(f"{first}\n{second}")
        assert len(blocks) == 2
        assert blocks[0]["reviewer"] == "bobbie"
        assert blocks[1]["reviewer"] == "peaches"

    def test_malformed_block_included_as_malformed_marker(self):
        body = "```review-result\n{not valid json\n```"
        blocks = find_all_verdict_blocks(body)
        assert len(blocks) == 1
        assert "_malformed" in blocks[0]


class TestAssertSingleOwnVerdictBlock:
    """The foreign-block-rejection backstop (lr-c26110). CONCRETE EVIDENCE
    this guards against: observed against a Forgejo deployment, lr-f89f6f —
    a comment body carrying a correctly-tagged 'own' block AND a foreign
    reviewer's block riding along, which a last-match-only re-parse would
    not catch."""

    def test_single_own_block_passes(self):
        block = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        assert_single_own_verdict_block(block, "peaches")  # no raise

    def test_no_block_raises(self):
        with pytest.raises(VerdictMalformedError, match="no fenced"):
            assert_single_own_verdict_block("just prose, no fence", "peaches")

    def test_two_blocks_same_reviewer_still_raises(self):
        # Even if BOTH blocks happen to be tagged with the caller's own
        # reviewer id, more than one block is itself the refused shape --
        # a genuine single-post invocation never needs two.
        first = build_verdict_block("peaches", "blocking", _FULL_SHA, 1)
        second = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            assert_single_own_verdict_block(f"{first}\n{second}", "peaches")

    def test_foreign_reviewer_block_alongside_own_block_raises(self):
        # THE PR #380 SHAPE: a body carrying peaches's own block plus
        # bobbie's foreign block riding along.
        foreign = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        own = build_verdict_block("peaches", "clean", _FULL_SHA, 1)
        body = f"bobbie's narrative here\n{foreign}\n{own}"
        with pytest.raises(VerdictMalformedError, match="2 fenced"):
            assert_single_own_verdict_block(body, "peaches")

    def test_single_block_wrong_reviewer_raises(self):
        foreign = build_verdict_block("bobbie", "clean", _FULL_SHA, 1)
        with pytest.raises(VerdictMalformedError, match="FOREIGN"):
            assert_single_own_verdict_block(foreign, "peaches")

    def test_single_malformed_block_raises(self):
        body = "```review-result\n{not valid json\n```"
        with pytest.raises(VerdictMalformedError, match="could not be parsed"):
            assert_single_own_verdict_block(body, "peaches")
