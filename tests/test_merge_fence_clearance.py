"""A reviewer's evidence-bearing clearance of its own blocking fence at the same head.

Every gate case runs the real merge verb on both platforms against a real git
repository whose main tip is the PR base, with fences built by the product's own
builder; the captured cases replay fences read from real pull requests (names
anonymized). The policy that decides whether a clearance is enough for a finding
is read from the base revision's repo profile, so each case states it there."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.fence_clearance import (
    SOURCE_CLAIM,
    SOURCE_DROPPED,
    apply_clearances,
    clearances_in,
    open_findings,
)
from clagentic_loadout.merge.verdict import ReviewerVerdict, parse_verdict_block
from clagentic_loadout.merge.fence_state import state_from_fence
from clagentic_loadout.review.clearance_policy import ClearancePolicy
from clagentic_loadout.review.evaluate import OUTCOME_CLEAN, Verdict
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import PROFILE_PATH
from tests._gate_repo import commit_files
from tests._review_evaluate_support import facts
from tests._ruling_host import PLATFORMS
from tests._ruling_support import FP, HEAD, LOGIN, NAME, OTHER_FP, open_entries
from tests._ruling_support import evaluated_finding as _finding
from tests._ruling_support import fence as _fence
from tests._ruling_support import gate_repo, tip
from tests._support.merge_verb import AllowingAuthorityProvider, RecordingTokenProvider, base_args

PAYLOADS = Path(__file__).parent / "real_payloads" / "clearance_fences.json"

#: The default finding: location src/a.py:10, rule R1, category correctness.
FINDING = _finding(fingerprint=None)
FINDING_ID = finding_state_id(FINDING["file"], FINDING["line"], FINDING["rule_id"])
OTHER = _finding(fingerprint=None, line=20, message="another")
OTHER_ID = finding_state_id(OTHER["file"], OTHER["line"], OTHER["rule_id"])


def _profile(rule_ids=(), categories=()) -> str:
    return yaml.safe_dump(
        {
            "class": "internal",
            "review": {
                "clearance": {
                    "process_record": {"rule_ids": list(rule_ids), "categories": list(categories)}
                }
            },
        }
    )


def _repo(tmp_path, profile: str | None = None) -> Path:
    repo = gate_repo(tmp_path)
    if profile is not None:
        commit_files(repo, {PROFILE_PATH: profile}, "chore: add profile")
    return repo


def _blocked(*findings, opened=None, **more) -> str:
    findings = findings or (FINDING,)
    reasons = [f"{f['file']}:{f['line']}:{f['rule_id']} blocks (computed severity high)" for f in findings]
    return _fence("BLOCKED", findings=list(findings), reasons=reasons, opened=opened or open_entries(findings), **more)


def _claim(finding_id=FINDING_ID, evidence="the record is in the PR body", fingerprint=None, head=HEAD):
    claim = {"id": finding_id, "head": head, "evidence": evidence}
    if fingerprint is not None:
        claim["fingerprint"] = fingerprint
    return claim


def _clearing(*claims, **more) -> str:
    return _fence("CLEAN", cleared_claims=list(claims), **more)


def _comments(*bodies, logins=None):
    out = []
    for position, body in enumerate(bodies, start=1):
        login = (logins or {}).get(position, LOGIN)
        out.append(
            {"id": position, "user": {"login": login}, "body": body,
             "created_at": f"2026-01-01T00:00:{position:02d}Z"}
        )
    return out


def _gate(platform, comments, repo, capsys, *, name=NAME, login=LOGIN, head=HEAD, extra=()):
    info = {
        "head": {"sha": head}, "title": "feat: x",
        "base": {"ref": "main", "sha": tip(repo), "repo": {"private": True, "visibility": "private"}},
    }
    argv = base_args(
        **{"--platform": platform, "--required-reviewer": f"{name}:{login}", "--repo-path": str(repo)}
    )
    capsys.readouterr()
    code = verb.main(
        [*argv, *extra],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=PLATFORMS[platform](pr_info=info, comments=comments),
    )
    return code, capsys.readouterr().err


BLOCKED = verb.EXIT_GATE_RESULT_BLOCKED


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestProcessRecordRule:
    """The policy names the rule as process record: the reviewer's clearance is enough."""

    def test_a_clearance_claim_supersedes_the_blocking_fence(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(rule_ids=["R1"]))
        comments = _comments(_blocked(), _clearing(_claim()))
        code, err = _gate(platform, comments, repo, capsys)
        assert code == verb.EXIT_OK, err
        assert "superseded by the reviewer's own clearance" in err
        assert f"{FINDING_ID} cleared by comment #2 (cleared_claim): the record is in the PR body" in err

    def test_the_rule_is_matched_as_a_pattern(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(rule_ids=["R*"]))
        code, err = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == verb.EXIT_OK, err

    def test_the_old_blocking_fence_refuses_without_a_clearance(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(rule_ids=["R1"]))
        code, _ = _gate(platform, _comments(_blocked(), _fence("CLEAN")), repo, capsys)
        assert code == BLOCKED

    def test_a_dropped_candidate_at_the_same_location_and_rule_clears_it(
        self, platform, tmp_path, capsys
    ):
        repo = _repo(tmp_path, _profile(rule_ids=["R1"]))
        dropped = [{**{k: FINDING[k] for k in ("file", "line", "rule_id", "message")},
                    "reason": "the SOP record is in the PR body, read at the head"}]
        code, err = _gate(platform, _comments(_blocked(), _fence("CLEAN", dropped=dropped)), repo, capsys)
        assert code == verb.EXIT_OK, err
        assert "(dropped)" in err

    def test_a_clearance_that_names_the_blocks_fingerprint_clears_it(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(rule_ids=["R1"]))
        opened = [{**entry, "fingerprint": FP} for entry in open_entries([FINDING])]
        comments = _comments(_blocked(opened=opened), _clearing(_claim(fingerprint=FP)))
        code, err = _gate(platform, comments, repo, capsys)
        assert code == verb.EXIT_OK, err


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestSecurityContentStillNeedsADisposition:
    def test_a_rule_the_policy_does_not_name_refuses(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(rule_ids=["R9"]))
        code, err = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED
        assert "BLOCKED merge" in err
        assert "is not a process-record finding in the repo profile on the base branch" in err

    def test_no_profile_at_the_base_fails_closed(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path)
        code, _ = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_an_invalid_profile_fails_closed(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, "class: internal\nreview:\n  clearance: {process_record: {nope: 1}}\n")
        code, _ = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_a_policy_in_the_change_under_review_is_not_read(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path)
        # Present only on a branch that is not the base tip.
        commit_files(repo, {PROFILE_PATH: _profile(rule_ids=["R1"])}, "chore: not on main")
        from tests._gate_repo import git

        git(repo, "branch", "side")
        git(repo, "reset", "-q", "--hard", "HEAD~1")
        code, _ = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_a_security_category_is_not_cleared_by_a_category_policy_naming_another(
        self, platform, tmp_path, capsys
    ):
        injected = _finding(
            fact=facts(category="injection", confidentiality="full", integrity="full", availability="full"),
            fingerprint=None,
        )
        repo = _repo(tmp_path, _profile(categories=["correctness"]))
        comments = _comments(_blocked(injected), _clearing(_claim()))
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_a_finding_that_states_no_category_is_not_classified_by_category(
        self, platform, tmp_path, capsys
    ):
        bare = _finding(fact={"reachable": "yes"}, fingerprint=None)
        repo = _repo(tmp_path, _profile(categories=["correctness"]))
        code, _ = _gate(platform, _comments(_blocked(bare), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_a_category_the_policy_names_is_process_record(self, platform, tmp_path, capsys):
        repo = _repo(tmp_path, _profile(categories=["correctness"]))
        code, err = _gate(platform, _comments(_blocked(), _clearing(_claim())), repo, capsys)
        assert code == verb.EXIT_OK, err

    def test_a_standing_disposition_still_clears_a_security_finding_as_before(
        self, platform, tmp_path, capsys
    ):
        from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH

        repo = _repo(tmp_path, _profile(rule_ids=["R9"]))
        entry = {"path": "src/a.py", "rule_id": "R1", "kind": "false_positive",
                 "rationale": "ruled by hand", "by": "a-maintainer", "at": "2026-01-01",
                 "expires": "2999-01-01"}
        commit_files(repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": [entry]})}, "chore: d")
        code, err = _gate(platform, _comments(_blocked()), repo, capsys)
        assert code == verb.EXIT_OK, err


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestClearanceThatClearsNothing:
    @pytest.fixture
    def repo(self, tmp_path):
        return _repo(tmp_path, _profile(rule_ids=["R1"]))

    def test_another_reviewers_clearance(self, platform, repo, capsys):
        comments = _comments(_blocked(), _clearing(_claim()), logins={2: "someone-else"})
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_a_fence_of_another_reviewer_role_under_the_same_login(self, platform, repo, capsys):
        other = _clearing(_claim()).replace(f'"reviewer": "{NAME}"', '"reviewer": "other-role"')
        code, _ = _gate(platform, _comments(_blocked(), other), repo, capsys)
        assert code == BLOCKED

    def test_a_clearance_at_another_head(self, platform, repo, capsys):
        other_head = "c" * 40
        stale = _fence("CLEAN", head=other_head, cleared_claims=[_claim(head=other_head)])
        code, _ = _gate(platform, _comments(_blocked(), stale), repo, capsys)
        assert code == BLOCKED

    def test_a_clearance_without_evidence(self, platform, repo, capsys):
        blank = _clearing(_claim()).replace("the record is in the PR body", "   ")
        code, _ = _gate(platform, _comments(_blocked(), blank), repo, capsys)
        assert code == BLOCKED

    def test_a_dropped_candidate_without_a_reason(self, platform, repo, capsys):
        dropped = [{**{k: FINDING[k] for k in ("file", "line", "rule_id", "message")}, "reason": "fine"}]
        blank = _fence("CLEAN", dropped=dropped).replace('"reason": "fine"', '"reason": "  "')
        code, _ = _gate(platform, _comments(_blocked(), blank), repo, capsys)
        assert code == BLOCKED

    def test_a_dropped_candidate_at_another_location(self, platform, repo, capsys):
        dropped = [{"file": "src/a.py", "line": 11, "rule_id": "R1", "message": "m", "reason": "fine"}]
        code, _ = _gate(platform, _comments(_blocked(), _fence("CLEAN", dropped=dropped)), repo, capsys)
        assert code == BLOCKED

    def test_a_clearance_for_another_finding(self, platform, repo, capsys):
        code, _ = _gate(platform, _comments(_blocked(), _clearing(_claim(OTHER_ID))), repo, capsys)
        assert code == BLOCKED

    def test_a_clearance_posted_before_the_block(self, platform, repo, capsys):
        code, _ = _gate(platform, _comments(_clearing(_claim()), _blocked()), repo, capsys)
        assert code == BLOCKED

    def test_a_finding_the_later_fence_raises_again(self, platform, repo, capsys):
        comments = _comments(_blocked(), _blocked())
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_a_clearance_then_a_new_block_on_the_same_finding(self, platform, repo, capsys):
        comments = _comments(_blocked(), _clearing(_claim()), _blocked())
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_one_of_two_findings_cleared(self, platform, repo, capsys):
        comments = _comments(_blocked(FINDING, OTHER), _clearing(_claim()))
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_both_findings_cleared(self, platform, repo, capsys):
        comments = _comments(_blocked(FINDING, OTHER), _clearing(_claim(), _claim(OTHER_ID)))
        code, err = _gate(platform, comments, repo, capsys)
        assert code == verb.EXIT_OK, err

    def test_a_fingerprint_the_clearance_does_not_state(self, platform, repo, capsys):
        opened = [{**entry, "fingerprint": FP} for entry in open_entries([FINDING])]
        code, _ = _gate(platform, _comments(_blocked(opened=opened), _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_another_fingerprint(self, platform, repo, capsys):
        opened = [{**entry, "fingerprint": FP} for entry in open_entries([FINDING])]
        comments = _comments(_blocked(opened=opened), _clearing(_claim(fingerprint=OTHER_FP)))
        code, _ = _gate(platform, comments, repo, capsys)
        assert code == BLOCKED

    def test_a_fence_that_carries_no_findings_cannot_be_checked(self, platform, repo, capsys):
        unchecked = _fence("BLOCKED", findings=[FINDING], keep_findings=False)
        code, _ = _gate(platform, _comments(unchecked, _clearing(_claim())), repo, capsys)
        assert code == BLOCKED

    def test_a_newly_blocking_finding_the_clearance_does_not_cover(self, platform, repo, capsys):
        # The fence records one open finding, but its evaluated findings also hold
        # a second that blocks at the live base and is not listed as open.
        fence = _fence("BLOCKED", findings=[FINDING, OTHER], opened=open_entries([FINDING]))
        code, _ = _gate(platform, _comments(fence, _clearing(_claim())), repo, capsys)
        assert code == BLOCKED


def test_a_clearance_with_a_fingerprint_is_stamped_version_seven_and_without_keeps_its_version():
    assert parse_verdict_block(_clearing(_claim(fingerprint=FP)))["fence_schema_version"] == 7
    assert parse_verdict_block(_clearing(_claim()))["fence_schema_version"] == 3


def _verdict(body: str, comment_id: int, head: str = HEAD) -> ReviewerVerdict:
    data = parse_verdict_block(body)
    return ReviewerVerdict(
        reviewer=data["reviewer"], review_status=data["review_status"], head_sha=head,
        pr_number=data["pr_number"], comment_id=comment_id, comment_author_login=LOGIN,
        state=state_from_fence(data),
    )


def _stub_recompute(fence):
    return Verdict(outcome=OUTCOME_CLEAN, head_sha=HEAD, expected_head=HEAD, block_at="high", findings=())


class TestApplyClearances:
    policy = staticmethod(lambda: ClearancePolicy(rule_ids=("R1",)))

    def split(self, *bodies, policy=None):
        fences = [_verdict(body, position) for position, body in enumerate(bodies, start=1)]
        kept, superseded, _ = apply_clearances(
            list(reversed(fences)), policy=policy or self.policy, recompute=_stub_recompute
        )
        return [f.comment_id for f in kept], [s.verdict.comment_id for s in superseded]

    def test_the_newest_fence_is_never_superseded(self):
        assert self.split(_blocked()) == ([1], [])

    def test_a_cleared_block_leaves_only_the_clearing_fence(self):
        assert self.split(_blocked(), _clearing(_claim())) == ([2], [1])

    def test_the_policy_is_not_read_when_nothing_is_cleared(self):
        def refuse():
            raise AssertionError("policy read without a clearance")

        assert self.split(_blocked(), _fence("CLEAN"), policy=refuse) == ([2, 1], [])

    def test_an_unclassified_finding_is_security_content(self):
        assert self.split(_blocked(), _clearing(_claim()), policy=ClearancePolicy) == ([2, 1], [])

    def test_a_clean_fence_is_never_superseded(self):
        assert self.split(_fence("CLEAN"), _clearing(_claim())) == ([2, 1], [])

    def test_a_re_raise_between_two_clearances_is_answered_by_the_last(self):
        kept, superseded = self.split(_blocked(), _clearing(_claim()), _blocked(), _clearing(_claim()))
        assert (kept, superseded) == ([4, 2], [1, 3])

    def test_a_cleared_security_finding_is_reported_as_declined(self):
        fences = [_verdict(_blocked(), 1), _verdict(_clearing(_claim()), 2)]
        _, superseded, declined = apply_clearances(
            list(reversed(fences)), policy=ClearancePolicy, recompute=_stub_recompute
        )
        assert superseded == ()
        (line,) = declined
        assert "comment #1" in line and "needs a disposition on the base branch" in line

    def test_a_degraded_recomputation_leaves_the_fence_in_force(self):
        fences = [_verdict(_blocked(), 1), _verdict(_clearing(_claim()), 2)]
        degraded = Verdict(
            outcome=OUTCOME_CLEAN, head_sha=HEAD, expected_head=HEAD, block_at="high",
            findings=(), degraded=("the policy could not be read",),
        )
        kept, superseded, _ = apply_clearances(
            list(reversed(fences)), policy=self.policy, recompute=lambda fence: degraded
        )
        assert [f.comment_id for f in kept] == [2, 1] and superseded == ()

    def test_the_clearance_names_how_and_why(self):
        fences = [_verdict(_blocked(), 1), _verdict(_clearing(_claim()), 2)]
        _, superseded, _ = apply_clearances(
            list(reversed(fences)), policy=self.policy, recompute=_stub_recompute
        )
        (item,) = superseded
        assert item.describe_clearances() == [
            f"{FINDING_ID} cleared by comment #2 (cleared_claim): the record is in the PR body"
        ]

    def test_a_claim_for_another_head_is_not_a_clearance(self):
        data = parse_verdict_block(_clearing(_claim()))
        data["cleared_claims"][0]["head"] = "d" * 40
        fence = ReviewerVerdict(
            reviewer=NAME, review_status="clean", head_sha=HEAD, pr_number=1, comment_id=2,
            comment_author_login=LOGIN, state=state_from_fence(data),
        )
        assert clearances_in(fence) == ()

    def test_the_sources_are_recorded(self):
        dropped = [{"file": "src/a.py", "line": 10, "rule_id": "R1", "message": "m", "reason": "ok"}]
        kinds = {c.source for body in (_clearing(_claim()), _fence("CLEAN", dropped=dropped))
                 for c in clearances_in(_verdict(body, 1))}
        assert kinds == {SOURCE_CLAIM, SOURCE_DROPPED}


class TestOpenFindings:
    def test_a_finding_is_open_until_a_later_fence_clears_it(self):
        blocked, clearing = _verdict(_blocked(), 1), _verdict(_clearing(_claim()), 2)
        assert [e["id"] for _, e in open_findings([blocked])] == [FINDING_ID]
        assert open_findings([blocked, clearing]) == []

    def test_a_re_raised_finding_is_open_under_the_later_fence(self):
        first, second = _verdict(_blocked(), 1), _verdict(_blocked(), 2)
        ((fence, _),) = open_findings([first, second])
        assert fence.comment_id == 2


def _captured(name: str) -> list[dict]:
    return json.loads(PAYLOADS.read_text(encoding="utf-8"))[name]


POLICY_RULE = "*.path-choice.5"


@pytest.mark.parametrize("platform", sorted(PLATFORMS))
class TestCapturedFences:
    """Fences read from two real pull requests, one per host, anonymized."""

    def run(self, platform, tmp_path, capsys, comments, *, profile, head, name, login):
        repo = _repo(tmp_path, profile)
        return _gate(platform, comments, repo, capsys, name=name, login=login, head=head)

    def test_a_blocking_fence_then_a_clean_fence_that_dropped_the_reraised_candidate(
        self, platform, tmp_path, capsys
    ):
        comments = _captured("pr_a")
        head = "d3f108a65aa4e22b91a50bc2f1f7a868796f53e7"
        args = dict(head=head, name="reviewer-one", login="reviewer-one-app[bot]")
        refused, _ = self.run(platform, tmp_path / "a", capsys, comments, profile=None, **args)
        assert refused == BLOCKED
        code, err = self.run(
            platform, tmp_path / "b", capsys, comments,
            profile=_profile(rule_ids=[POLICY_RULE]), **args,
        )
        assert code == verb.EXIT_OK, err
        assert "(dropped)" in err

    def test_the_same_fences_still_refuse_for_a_security_content_policy(
        self, platform, tmp_path, capsys
    ):
        code, _ = self.run(
            platform, tmp_path, capsys, _captured("pr_a"),
            profile=_profile(rule_ids=["other.rule"]),
            head="d3f108a65aa4e22b91a50bc2f1f7a868796f53e7",
            name="reviewer-one", login="reviewer-one-app[bot]",
        )
        assert code == BLOCKED

    def test_clean_fences_that_raise_no_candidate_clear_nothing(self, platform, tmp_path, capsys):
        # A blocking fence followed by clean fences with neither a clearance nor a
        # dropped candidate: a fresh clearing review is needed, as before.
        code, err = self.run(
            platform, tmp_path, capsys, _captured("pr_b"),
            profile=_profile(rule_ids=[POLICY_RULE]),
            head="3970811154d69a333697d68b8d00e637411e2525",
            name="reviewer-one", login="reviewer-one",
        )
        assert code == BLOCKED
        assert "BLOCKED merge" in err
