"""The churn breaker's units: the computation over rounds, the ledger read from
posted fences, the net delta, the brief text and the escalation_round setting."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.acquire.contract import RangeDiff
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.review import churn, churn_brief, churn_delta, churn_ledger
from clagentic_loadout.review.churn import Round
from clagentic_loadout.review.profile_config import (
    DEFAULT_ESCALATION_ROUND,
    ReviewProfileError,
    load_escalation_round,
    load_review_profile,
)
from tests._support.churn_fences import captured_comment, replace_fence, verdict_comment

HEADS = {name: name * 40 for name in "abcdef"}
R1 = ("R1", "a.py")
R2 = ("R2", "b.py")


def _rounds(*spec):
    """Rounds from ``(head letter, keys...)`` tuples; a bare letter is a clean round."""
    return [Round(HEADS[head], frozenset(keys)) for head, *keys in spec]


class TestAssess:
    @pytest.mark.parametrize(
        ("rounds", "escalation", "level", "listed"),
        [
            # Table rows: the rounds, the setting, the level, and (key, rounds) listed.
            ([("a", R1), ("b", R1)], 2, churn.LEVEL_BRIEF, [(R1, 2)]),
            ([("a", R1), ("b", R1), ("c", R1)], 2, churn.LEVEL_STOP, [(R1, 3)]),
            ([("a", R1), ("b", R1), ("c", R1), ("d", R1)], 2, churn.LEVEL_STOP, [(R1, 4)]),
            ([("a", R1)], 2, churn.LEVEL_NONE, []),
            ([("a", R1), ("b", R1)], 3, churn.LEVEL_NONE, [(R1, 2)]),
            ([("a", R1), ("b", R1), ("c", R1)], 3, churn.LEVEL_BRIEF, [(R1, 3)]),
            ([("a", R1), ("b", R1), ("c", R1), ("d", R1)], 3, churn.LEVEL_STOP, [(R1, 4)]),
            # Off: no level, no keys, whatever has churned.
            ([("a", R1), ("b", R1), ("c", R1), ("d", R1)], 0, churn.LEVEL_NONE, []),
            # A different key, or none, in between ends the chain.
            ([("a", R1), ("b", R2), ("c", R1)], 2, churn.LEVEL_NONE, []),
            ([("a", R1), ("b"), ("c", R1)], 2, churn.LEVEL_NONE, []),
            ([("a", R1), ("b", R1), ("c")], 2, churn.LEVEL_NONE, []),
            # Only a chain that ends in the current round counts, and only its own rounds.
            ([("a", R1), ("b", R1), ("c", R1, R2)], 2, churn.LEVEL_STOP, [(R1, 3)]),
            ([("a", R1, R2), ("b", R1, R2), ("c", R2)], 2, churn.LEVEL_STOP, [(R2, 3)]),
            # The highest-round key decides the level.
            ([("a", R1, R2), ("b", R1, R2), ("c", R1)], 3, churn.LEVEL_BRIEF, [(R1, 3)]),
            # Same rule in two files, or two rules in one file, are different keys.
            ([("a", R1), ("b", ("R1", "other.py"))], 2, churn.LEVEL_NONE, []),
            ([("a", R1), ("b", ("R9", "a.py"))], 2, churn.LEVEL_NONE, []),
            # N of 1 briefs on the first blocked round.
            ([("a", R1)], 1, churn.LEVEL_BRIEF, [(R1, 1)]),
            ([("a", R1), ("b", R1)], 1, churn.LEVEL_STOP, [(R1, 2)]),
        ],
    )
    def test_the_level_follows_the_highest_round_key(self, rounds, escalation, level, listed):
        got = churn.assess(_rounds(*[(r,) if isinstance(r, str) else r for r in rounds]), escalation)

        assert got.level == level
        assert [((k.rule_id, k.file), k.rounds) for k in got.keys] == listed

    def test_a_key_lists_the_heads_of_its_chain_oldest_first(self):
        got = churn.assess(_rounds(("a", R1), ("b", R2), ("c", R1), ("d", R1)), 2)

        [key] = got.keys
        assert key.heads == (HEADS["c"], HEADS["d"]) and key.rounds == 2

    def test_a_round_whose_fence_could_not_be_read_ends_the_chain(self):
        rounds = [Round(HEADS["a"], frozenset({R1})), Round(None), Round(HEADS["c"], frozenset({R1}))]

        assert churn.assess(rounds, 2).level == churn.LEVEL_NONE

    def test_no_rounds_is_no_churn(self):
        assert churn.assess([], 2).level == churn.LEVEL_NONE

    def test_with_current_replaces_a_prior_round_at_the_current_head(self):
        prior = _rounds(("a", R1), ("b", R1))
        current = Round(HEADS["b"], frozenset({R2}))

        assert churn.with_current(prior, current) == [prior[0], current]


def _ledger(comments, login, pr):
    return churn_ledger.rounds_from_comments(comments, login=login, pr_number=pr)


@pytest.fixture(params=[("github", "reviewer-one-app[bot]", 91), ("forgejo", "reviewer-one", 707)])
def shape(request):
    return request.param


def _blocking(shape, comment_id, head, *, login=None, blocks_on=(("R1", "a.py", 3),), **extra):
    platform, own_login, pr = shape
    return verdict_comment(
        platform, comment_id=comment_id, login=login or own_login, head=head, pr_number=pr,
        blocks_on=blocks_on, **extra,
    )


class TestLedger:
    def test_the_captured_blocking_comment_is_read_as_a_round_with_its_key(self, shape):
        platform, _, pr = shape
        captured = captured_comment(platform, "blocking")

        [only] = churn_ledger.rounds_from_comments(
            [captured], login=captured["user"]["login"], pr_number=pr
        )

        [(rule, file)] = only.keys
        assert rule.startswith("builder.path-choice") and "/" in file

    def test_rounds_are_the_roles_own_fences_in_post_order_one_per_head(self, shape):
        _, login, pr = shape
        comments = [
            _blocking(shape, 12, HEADS["b"]),
            _blocking(shape, 11, HEADS["a"]),
            _blocking(shape, 13, HEADS["c"], blocks_on=()),
        ]

        got = _ledger(comments, login, pr)

        assert [(r.head_sha, sorted(r.keys)) for r in got] == [
            (HEADS["a"], [R1]), (HEADS["b"], [R1]), (HEADS["c"], []),
        ]

    def test_a_fence_by_another_login_is_ignored(self, shape):
        _, login, pr = shape
        comments = [
            _blocking(shape, 11, HEADS["a"], login="somebody-else"),
            _blocking(shape, 12, HEADS["b"]),
        ]

        got = _ledger(comments, login, pr)

        assert [r.head_sha for r in got] == [HEADS["b"]]

    def test_several_fences_at_one_head_count_once_as_the_last_of_them(self, shape):
        _, login, pr = shape
        comments = [
            _blocking(shape, 11, HEADS["a"]),
            _blocking(shape, 12, HEADS["b"]),
            _blocking(shape, 13, HEADS["b"], blocks_on=()),
        ]

        got = _ledger(comments, login, pr)

        assert [(r.head_sha, sorted(r.keys)) for r in got] == [(HEADS["a"], [R1]), (HEADS["b"], [])]

    def test_a_fence_for_another_pull_request_is_not_a_round(self, shape):
        _, login, pr = shape
        comments = [
            _blocking(shape, 11, HEADS["a"]),
            replace_fence(_blocking(shape, 12, HEADS["b"]), pr_number=pr + 1),
        ]

        assert [r.head_sha for r in _ledger(comments, login, pr)] == [HEADS["a"]]

    def test_a_comment_without_a_fence_is_not_a_round(self, shape):
        _, login, pr = shape
        prose = {"id": 5, "user": {"login": login}, "body": "looks fine", "created_at": "x"}

        assert _ledger([prose], login, pr) == []

    @pytest.mark.parametrize(
        "damage",
        [
            {"findings_open": "not-a-list"},
            {"findings_open": [{"id": "f-unknown", "rule_id": "R1", "head": "x"}]},
            {"findings_open": ["not-an-object"]},
            {"findings_open": [{"id": "f-unknown", "rule_id": 7}]},
            {"evaluated_findings": None, "failure_sequences": None},
        ],
    )
    def test_blocking_keys_that_cannot_be_read_count_as_not_blocking(self, shape, damage):
        _, login, pr = shape
        comment = replace_fence(_blocking(shape, 11, HEADS["a"]), **damage)

        [only] = _ledger([comment], login, pr)

        assert only.head_sha == HEADS["a"] and only.keys == frozenset()

    @pytest.mark.parametrize(
        "body",
        ["```review-result\n{not json\n```\n", "```review-result\n[1]\n```\n"],
    )
    def test_a_fence_that_is_not_an_object_is_a_round_that_ends_the_chain(self, shape, body):
        _, login, pr = shape
        broken = {"id": 12, "user": {"login": login}, "body": body, "created_at": "x"}

        got = _ledger([_blocking(shape, 11, HEADS["a"]), broken], login, pr)

        assert got[0].keys == frozenset({R1}) and got[1] == Round(None)

    def test_a_comment_with_two_fences_is_a_round_that_ends_the_chain(self, shape):
        _, login, pr = shape
        one = _blocking(shape, 11, HEADS["a"])
        two = {**one, "id": 12, "body": one["body"] + one["body"]}

        got = _ledger([one, two], login, pr)

        assert got[1] == Round(None)

    def test_a_clean_fence_holds_nothing_open(self, shape):
        _, login, pr = shape

        [only] = _ledger([_blocking(shape, 11, HEADS["a"], blocks_on=())], login, pr)

        assert only == Round(HEADS["a"], frozenset())

    def test_a_blocking_fence_with_no_findings_open_holds_nothing_open(self, shape):
        _, login, pr = shape
        comment = replace_fence(_blocking(shape, 11, HEADS["a"]), findings_open=None)

        [only] = _ledger([comment], login, pr)

        assert only.keys == frozenset()

    def test_a_listing_the_host_cannot_serve_is_a_ledger_error(self):
        class Failing:
            def fetch_issue_comments(self, *, owner, repo, pr_number):
                raise AcquireFetchError("boom")

        with pytest.raises(churn_ledger.ChurnLedgerError, match="boom"):
            churn_ledger.read_rounds(Failing(), owner="o", repo="r", pr_number=1, login="x")
        with pytest.raises(churn_ledger.ChurnLedgerError, match="cannot list comments"):
            churn_ledger.read_rounds(object(), owner="o", repo="r", pr_number=1, login="x")


DIFF = """\
diff --git a/src/a.py b/src/a.py
index 1111111..2222222 100644
--- a/src/a.py
+++ b/src/a.py
@@ -1,3 +1,4 @@
 keep
-old one
-old two
+new one
+new two
+new three
diff --git a/docs/b.md b/docs/b.md
new file mode 100644
--- /dev/null
+++ b/docs/b.md
@@ -0,0 +1,2 @@
+--- looks like a file line but is added text
+two
"""


class _Backend:
    def __init__(self, span=None, error=None):
        self.span, self.error, self.calls = span, error, []

    def fetch_range_diff(self, *, owner, repo, base_sha, head_sha):
        self.calls.append((base_sha, head_sha))
        if self.error:
            raise self.error
        return self.span


class TestNetDelta:
    def test_counts_added_and_removed_lines_per_file_inside_hunks_only(self):
        files = churn_delta.count_changes(DIFF)

        assert [(f.path, f.added, f.removed) for f in files] == [
            ("docs/b.md", 2, 0), ("src/a.py", 3, 2),
        ]

    def test_a_fast_forward_is_read_between_the_two_heads(self):
        backend = _Backend(RangeDiff("a" * 40, "b" * 40, True, DIFF))

        delta, reason = churn_delta.read_net_delta(
            backend, owner="o", repo="r", from_head="a" * 40, to_head="b" * 40
        )

        assert reason is None and backend.calls == [("a" * 40, "b" * 40)]
        assert delta.to_dict()["files"][1] == {"path": "src/a.py", "added": 3, "removed": 2}

    @pytest.mark.parametrize(
        ("backend", "expected"),
        [
            (_Backend(RangeDiff("a", "b", False)), "not a fast-forward"),
            (_Backend(error=AcquireFetchError("HTTP 500")), "HTTP 500"),
            (object(), "cannot read a range diff"),
        ],
    )
    def test_a_delta_that_cannot_be_read_is_a_reason_not_an_error(self, backend, expected):
        delta, reason = churn_delta.read_net_delta(
            backend, owner="o", repo="r", from_head="a" * 40, to_head="b" * 40
        )

        assert delta is None and expected in reason


class TestBrief:
    KEY = churn.KeyChurn("R1", "a.py", 2, (HEADS["a"], HEADS["b"]))

    def test_the_brief_has_eight_steps_in_order(self):
        text = churn_brief.render(churn.LEVEL_BRIEF, self.KEY, None)

        assert len(churn_brief.STEPS) == 8
        numbered = [line for line in text.splitlines() if line[:2] in {f"{n}." for n in range(1, 9)}]
        assert [line[0] for line in numbered] == list("12345678")
        assert "STOP" not in text

    def test_step_one_quotes_the_spec_goal_and_scope(self):
        spec = churn_brief.SpecQuote("Ship the breaker.", ("config key",), ("merge gate",))

        text = churn_brief.render(churn.LEVEL_BRIEF, self.KEY, spec)

        assert "Goal: Ship the breaker." in text
        assert "In scope:\n   - config key" in text and "Out of scope:\n   - merge gate" in text

    def test_step_one_without_a_spec_says_so_and_points_at_the_task_description(self):
        text = churn_brief.render(churn.LEVEL_BRIEF, self.KEY, None)

        assert "No sealed spec was given" in text and "task description" in text

    def test_the_stop_level_leads_with_the_stop_line_and_the_three_options(self):
        text = churn_brief.render(churn.LEVEL_STOP, self.KEY, None)

        assert text.startswith("STOP.")
        for option in churn.STOP_OPTIONS:
            assert f"- {option}:" in text
        assert "1. Restate the brief" in text

    def test_there_is_no_brief_below_the_brief_level(self):
        with pytest.raises(ValueError):
            churn_brief.render(churn.LEVEL_NONE, self.KEY, None)


def _config(tmp_path, profile):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": {"reviewer": profile}}}), encoding="utf-8"
    )
    return tmp_path


class TestEscalationRoundSetting:
    def test_it_defaults_to_two_with_or_without_a_profile(self, tmp_path):
        assert load_escalation_round("reviewer", config_root=tmp_path) == DEFAULT_ESCALATION_ROUND == 2
        _config(tmp_path, {"carrier": ["c"]})
        assert load_escalation_round("reviewer", config_root=tmp_path) == 2

    @pytest.mark.parametrize("value", [0, 1, 2, 7, 10])
    def test_an_integer_from_zero_to_ten_is_accepted(self, tmp_path, value):
        _config(tmp_path, {"carrier": ["c"], "escalation_round": value})

        assert load_escalation_round("reviewer", config_root=tmp_path) == value
        assert load_review_profile("reviewer", config_root=tmp_path).escalation_round == value

    @pytest.mark.parametrize("value", [-1, 11, 2.0, 2.5, "2", True, None, [2]])
    def test_anything_else_is_an_error_naming_the_value(self, tmp_path, value):
        _config(tmp_path, {"carrier": ["c"], "escalation_round": value})

        for load in (load_escalation_round, load_review_profile):
            with pytest.raises(ReviewProfileError, match="escalation_round") as caught:
                load("reviewer", config_root=tmp_path)
            assert repr(value) in str(caught.value)

    def test_a_repo_level_value_is_ignored_with_a_warning(self, tmp_path, capsys):
        user = tmp_path / "user"
        user.mkdir()
        _config(user, {"carrier": ["c"], "escalation_round": 2})
        repo = tmp_path / "repo"
        (repo / ".clagentic" / "loadout").mkdir(parents=True)
        (repo / ".clagentic" / "loadout" / "config.yaml").write_text(
            yaml.safe_dump({"review": {"profiles": {"reviewer": {"escalation_round": 0}}}}),
            encoding="utf-8",
        )

        profile = load_review_profile("reviewer", config_root=user, repo_root=repo)

        assert profile.escalation_round == 2
        assert "escalation_round" in capsys.readouterr().err

    def test_a_repo_level_value_cannot_supply_a_missing_user_level_one(self, tmp_path):
        user = tmp_path / "user"
        user.mkdir()
        _config(user, {"carrier": ["c"]})
        repo = tmp_path / "repo"
        (repo / ".clagentic" / "loadout").mkdir(parents=True)
        (repo / ".clagentic" / "loadout" / "config.yaml").write_text(
            yaml.safe_dump({"review": {"profiles": {"reviewer": {"escalation_round": 0}}}}),
            encoding="utf-8",
        )

        assert load_review_profile("reviewer", config_root=user, repo_root=repo).escalation_round == 2
