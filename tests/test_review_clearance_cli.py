"""`loadout-review open` and `loadout-review post --clear`: a reviewer clears its own
earlier finding at the same head, on both platforms, against a pull request whose
earlier round is a verdict comment built from one captured from a real host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge.fence_clearance import apply_clearances
from clagentic_loadout.merge.fence_state import state_from_fence
from clagentic_loadout.merge.verdict import ReviewerVerdict, parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.clearance_policy import ClearancePolicy
from clagentic_loadout.review.evaluate import OUTCOME_CLEAN, Verdict
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.transport import github_app_config, provider_config
from tests._review_cli_support import Env, identity_provider, make_diff
from tests._review_evaluate_support import facts
from tests._support.churn_fences import replace_fence, verdict_comment
from tests._support.review_host import ReviewHost

BOT_SLUG = "reviewer-app"
KEY = ("R1", "a.py", 3)
OPEN_ID = finding_state_id("a.py", 3, "R1")
FP = "fp2:0123456789abcdef"
REASON = "the compliance record is in the PR body, read at this head"
SERIOUS = facts(confidentiality="full", integrity="full", availability="full")


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path

    def seed(self, *, login=None, comment_id=100, head=None, **changes) -> dict:
        comment = verdict_comment(
            self.host.platform, comment_id=comment_id, login=login or self.host.login,
            head=head or self.host.head_sha, pr_number=self.host.number, blocks_on=(KEY,),
        )
        if changes:
            comment = replace_fence(comment, **changes)
        self.host.seeded.append(comment)
        return comment

    def review(self, command, *extra, capsys):
        argv = [
            command, "--caller", "reviewer", "--repo", "some-owner/some-repo",
            *self.host.platform_args(), "--repo-path", str(self.env.repo), *extra,
        ]
        capsys.readouterr()
        code = review_cli.main(
            argv,
            token_provider=self.env.token_provider,
            opener=self.host.opener,
            identity_provider=identity_provider("reviewer"),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def post(self, findings, *extra, capsys):
        document = {
            "owner": "some-owner", "repo": "some-repo", "pr_number": self.host.number,
            "head_sha": self.host.head_sha, "findings": findings,
        }
        path = self.root / "findings.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return self.review(
            "post", "--findings", str(path), "--base-sha", self.env.base_sha, *extra, capsys=capsys
        )

    def posted_fence(self) -> dict:
        return parse_verdict_block(self.host.last_body)


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path)
    host = ReviewHost(
        request.param, diff=make_diff({"a.py": 4}), base_sha=env.base_sha, files=["a.py"],
    )
    monkeypatch.setenv("STUB_DIR", str(env.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    if request.param == "github":
        # A GitHub reviewer's login is derived from its app slug.
        (tmp_path / "user-config").mkdir(exist_ok=True)
        (tmp_path / "user-config" / "config.yaml").write_text(
            yaml.safe_dump({"github_app": {"slugs": {"reviewer": BOT_SLUG}}}), encoding="utf-8"
        )
        host.login = f"{BOT_SLUG}[bot]"
    return World(env, host, tmp_path)


class TestOpen:
    def test_it_lists_the_roles_open_finding_with_the_id_to_clear_it_by(self, world, capsys):
        world.seed()

        code, result, _ = world.review("open", capsys=capsys)

        assert code == 0 and result["result"] == "open_findings"
        assert result["head_sha"] == world.host.head_sha
        (item,) = result["open"]
        assert item["id"] == OPEN_ID
        assert (item["rule_id"], item["file"], item["line"]) == ("R1", "a.py", 3)
        assert item["comment_id"] == 100 and item["message"]

    def test_a_finding_the_role_already_cleared_is_not_listed(self, world, capsys):
        world.seed()
        cleared = world.seed(comment_id=101)
        world.host.seeded[-1] = replace_fence(
            cleared, findings_open=None, review_status="clean",
            cleared_claims=[{"id": OPEN_ID, "head": world.host.head_sha, "evidence": REASON}],
        )
        _, result, _ = world.review("open", capsys=capsys)
        assert result["open"] == []

    @pytest.mark.parametrize(
        "variant",
        ["another_login", "another_head"],
    )
    def test_findings_of_another_reviewer_or_another_head_are_not_listed(
        self, world, capsys, variant
    ):
        if variant == "another_login":
            world.seed(login="someone-else")
        else:
            world.seed(head="c" * 40)
        _, result, _ = world.review("open", capsys=capsys)
        assert result["open"] == []


class TestClear:
    def test_it_records_the_clearance_in_the_fence(self, world, capsys):
        world.seed()

        code, result, _ = world.post([], "--status", "clean", "--clear", OPEN_ID, REASON, capsys=capsys)

        assert code == 0 and result["result"] == "posted" and result["status"] == "clean"
        fence = world.posted_fence()
        assert fence["cleared_claims"] == [
            {"id": OPEN_ID, "head": world.host.head_sha, "evidence": REASON}
        ]
        assert fence["fence_schema_version"] == 3
        assert "findings_open" not in fence

    def test_the_claim_carries_the_open_findings_fingerprint_and_the_fence_is_version_seven(
        self, world, capsys
    ):
        world.seed(
            findings_open=[
                {"id": OPEN_ID, "rule_id": "R1", "head": world.host.head_sha, "fingerprint": FP}
            ],
            fence_schema_version=6,
        )

        code, _, _ = world.post([], "--status", "clean", "--clear", OPEN_ID, REASON, capsys=capsys)

        assert code == 0
        fence = world.posted_fence()
        assert fence["cleared_claims"][0]["fingerprint"] == FP
        assert fence["fence_schema_version"] == 7

    def test_what_it_posts_supersedes_the_earlier_block_at_the_gate(self, world, capsys):
        seeded = world.seed()
        world.post([], "--status", "clean", "--clear", OPEN_ID, REASON, capsys=capsys)

        def verdict(body, comment_id):
            data = parse_verdict_block(body)
            return ReviewerVerdict(
                reviewer=data["reviewer"], review_status=data["review_status"],
                head_sha=data["head_sha"], pr_number=data["pr_number"], comment_id=comment_id,
                comment_author_login="x", state=state_from_fence(data),
            )

        fences = [verdict(world.host.last_body, 2), verdict(seeded["body"], 1)]
        clean = Verdict(
            outcome=OUTCOME_CLEAN, head_sha="", expected_head="", block_at="high", findings=()
        )
        kept, superseded, _ = apply_clearances(
            fences, policy=lambda: ClearancePolicy(rule_ids=("R1",)), recompute=lambda f: clean
        )
        assert [f.comment_id for f in kept] == [2]
        assert [s.verdict.comment_id for s in superseded] == [1]

    @pytest.mark.parametrize(
        "clear",
        [("f-0000000000000000", REASON), (OPEN_ID, "   ")],
        ids=["unknown-id", "blank-reason"],
    )
    def test_a_bad_request_is_refused_and_nothing_is_posted(self, world, capsys, clear):
        world.seed()
        code, _, err = world.post([], "--status", "clean", "--clear", *clear, capsys=capsys)
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert world.host.posted == []
        assert "--clear" in err

    def test_another_reviewers_finding_cannot_be_cleared(self, world, capsys):
        world.seed(login="someone-else")
        code, _, err = world.post([], "--status", "clean", "--clear", OPEN_ID, REASON, capsys=capsys)
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "holds no such finding open" in err and world.host.posted == []

    def test_a_finding_at_another_head_cannot_be_cleared(self, world, capsys):
        world.seed(head="c" * 40)
        code, _, _ = world.post([], "--status", "clean", "--clear", OPEN_ID, REASON, capsys=capsys)
        assert code == review_cli.EXIT_FINDINGS_INVALID and world.host.posted == []

    def test_a_finding_this_review_still_holds_open_cannot_be_cleared_by_it(self, world, capsys):
        world.seed()
        finding = {
            "file": "a.py", "line": 3, "rule_id": "R1", "message": "R1 is wrong", "facts": SERIOUS,
        }
        code, _, err = world.post([finding], "--clear", OPEN_ID, REASON, capsys=capsys)
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "still holds that finding open" in err and world.host.posted == []

    def test_naming_one_finding_twice_is_refused(self, world, capsys):
        world.seed()
        code, _, _ = world.post(
            [], "--status", "clean", "--clear", OPEN_ID, REASON, "--clear", OPEN_ID, REASON,
            capsys=capsys,
        )
        assert code == review_cli.EXIT_FINDINGS_INVALID and world.host.posted == []

    def test_a_post_without_clear_carries_no_clearance(self, world, capsys):
        world.seed()
        world.post([], "--status", "clean", capsys=capsys)
        assert "cleared_claims" not in world.posted_fence()
