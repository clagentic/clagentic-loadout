"""The churn breaker through `loadout-review post` and `loadout-review churn`, on both
platforms, against pull requests whose earlier rounds are verdict comments built from
the ones captured from real hosts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import churn
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.transport import github_app_config, provider_config
from tests._review_cli_support import Env, identity_provider, make_diff
from tests._review_evaluate_support import facts
from tests._support.churn_fences import verdict_comment
from tests._support.fakes import FakeResponse, json_resp
from tests._support.review_host import PRIOR_HEAD, ReviewHost

SERIOUS = facts(confidentiality="full", integrity="full", availability="full")
KEY = ("R1", "a.py", 3)
MIDDLE_HEAD = "e" * 40
SPEC_HEAD = "c" * 40
CRITERIA = ("ac-1", "ac-2")
BOT_SLUG = "reviewer-app"


def _finding(rule="R1", line=3, **extra):
    return {
        "file": "a.py", "line": line, "rule_id": rule, "message": f"{rule} is wrong",
        "facts": SERIOUS, **extra,
    }


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path
    tmp_path: Path
    opener: object = None

    def __post_init__(self):
        self.opener = self.host.opener

    def configure(self, escalation_round) -> None:
        self.env.cfg.mkdir(parents=True, exist_ok=True)
        (self.env.cfg / "config.yaml").write_text(
            yaml.safe_dump(
                {"review": {"profiles": {"reviewer": {"carrier": ["c"], "escalation_round": escalation_round}}}}
            ),
            encoding="utf-8",
        )

    def seed(self, *heads, login=None, blocks_on=(KEY,), reviewer="reviewer") -> None:
        """Earlier rounds by *login* (default: the host's account), one per head."""
        base = 100 + len(self.host.seeded)
        for offset, head in enumerate(heads):
            self.host.seeded.append(
                verdict_comment(
                    self.host.platform, comment_id=base + offset, login=login or self.host.login,
                    head=head, pr_number=self.host.number, blocks_on=blocks_on, reviewer=reviewer,
                )
            )

    def review(self, command, *extra, capsys, caller="reviewer"):
        argv = [
            command, "--caller", caller, "--repo", "some-owner/some-repo",
            *self.host.platform_args(), "--repo-path", str(self.env.repo), *extra,
        ]
        capsys.readouterr()
        code = review_cli.main(
            argv,
            token_provider=self.env.token_provider,
            opener=self.opener,
            identity_provider=identity_provider(caller),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def write(self, name, document) -> str:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def post(self, findings, *extra, capsys, **kwargs):
        document = {
            "owner": "some-owner", "repo": "some-repo", "pr_number": self.host.number,
            "head_sha": self.host.head_sha, "findings": findings,
        }
        return self.review(
            "post", "--findings", self.write("findings.json", document),
            "--base-sha", self.env.base_sha, *extra, capsys=capsys, **kwargs,
        )

    def seal(self) -> model.SealedSpec:
        draft = model.new_draft("planner-role")
        draft["goal"] = "Break the review churn."
        draft["scope"] = {"in": ["the churn output"], "out": ["the merge gate"]}
        draft["acceptance_criteria"] = [
            {"id": cid, "given": "g", "when": "w", "then": "t"} for cid in CRITERIA
        ]
        sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
        spec_store.write_sealed(spec_store.specs_root(), sealed)
        return sealed

    def post_with_spec(self, findings, spec, *, capsys):
        coverage = self.write(
            "coverage.json", [{"criterion": c, "status": "met"} for c in CRITERIA]
        )
        return self.post(
            findings, "--spec-hash", spec.spec_hash, "--spec-coverage", coverage, capsys=capsys
        )


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path)
    host = ReviewHost(
        request.param, diff=make_diff({"a.py": 4}), base_sha=env.base_sha, files=["a.py"],
        range_diff=make_diff({"a.py": 2}),
    )
    monkeypatch.setenv("STUB_DIR", str(env.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return World(env, host, tmp_path, tmp_path)


def _churn(result):
    return result["churn"]


class TestSecondBlockedRound:
    def test_a_second_blocked_round_on_the_same_key_gets_the_net_delta_and_the_brief(
        self, world, capsys
    ):
        world.seed(PRIOR_HEAD)

        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0 and result["result"] == "posted"
        report = _churn(result)
        assert report["level"] == churn.LEVEL_BRIEF and report["escalation_round"] == 2
        assert report["keys"] == [
            {"rule_id": "R1", "file": "a.py", "rounds": 2, "heads": [PRIOR_HEAD, world.host.head_sha]}
        ]
        assert report["net_delta"] == {
            "from_head": PRIOR_HEAD,
            "to_head": world.host.head_sha,
            "files": [{"path": "a.py", "added": 2, "removed": 0}],
        }
        steps = [line for line in report["brief"].splitlines() if line[:2] in {f"{n}." for n in "12345678"}]
        assert [line[0] for line in steps] == list("12345678")
        assert report["options"] == [] and "STOP" not in report["brief"]

    def test_a_first_blocked_round_has_nothing_to_report(self, world, capsys):
        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0
        assert _churn(result) == {
            "escalation_round": 2, "keys": [], "level": "none",
            "net_delta": None, "brief": None, "options": [],
        }

    def _comment_reads(self, world) -> int:
        return len([r for r in world.host.requests if "/comments" in r[1] and r[0] == "GET"])

    def test_the_ledger_is_read_only_when_something_blocks_and_the_breaker_is_on(
        self, world, capsys
    ):
        world.seed(PRIOR_HEAD)
        start = self._comment_reads(world)
        world.post([], "--status", "clean", capsys=capsys)
        clean_reads = self._comment_reads(world) - start
        world.host.posted.clear()
        start = self._comment_reads(world)
        world.configure(0)
        world.post([_finding()], capsys=capsys)
        off_reads = self._comment_reads(world) - start
        world.host.posted.clear()
        start = self._comment_reads(world)
        world.configure(2)

        world.post([_finding()], capsys=capsys)

        assert clean_reads == off_reads
        assert self._comment_reads(world) - start > off_reads


class TestThirdBlockedRound:
    def test_the_caller_is_told_to_stop_and_offered_three_options_and_nothing_is_refused(
        self, world, capsys
    ):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)

        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0 and result["result"] == "posted" and result["status"] == "blocking"
        report = _churn(result)
        assert report["level"] == churn.LEVEL_STOP
        assert report["keys"][0]["rounds"] == 3
        assert report["options"] == ["simplify", "ship", "continue"]
        assert report["brief"].startswith("STOP.")
        for option in report["options"]:
            assert f"- {option}:" in report["brief"]
        assert report["net_delta"]["from_head"] == PRIOR_HEAD
        assert len(world.host.posted) == 1

    def test_a_later_round_stays_at_stop(self, world, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD, "f" * 40)

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["level"] == "stop" and _churn(result)["keys"][0]["rounds"] == 4


class TestBreakerOff:
    def test_an_escalation_round_of_zero_emits_no_brief_and_no_stop_line(self, world, capsys):
        world.configure(0)
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)

        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0
        assert _churn(result) == {
            "escalation_round": 0, "keys": [], "level": "none",
            "net_delta": None, "brief": None, "options": [],
        }

    def test_a_repo_level_value_cannot_switch_it_off(self, world, capsys):
        config = world.env.repo / ".clagentic" / "loadout"
        config.mkdir(parents=True, exist_ok=True)
        (config / "config.yaml").write_text(
            yaml.safe_dump({"review": {"profiles": {"reviewer": {"escalation_round": 0}}}}),
            encoding="utf-8",
        )
        world.seed(PRIOR_HEAD)

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["level"] == "brief"

    @pytest.mark.parametrize("value", [11, -1, "2", 1.5])
    def test_an_invalid_setting_is_refused_before_anything_is_posted(self, world, capsys, value):
        world.configure(value)

        code, _, err = world.post([_finding()], capsys=capsys)

        assert code == review_cli.EXIT_PROFILE_INVALID and "escalation_round" in err
        assert world.host.posted == []

    def test_a_raised_setting_delays_the_brief(self, world, capsys):
        world.configure(3)
        world.seed(PRIOR_HEAD)

        _, second, _ = world.post([_finding()], capsys=capsys)

        assert _churn(second)["level"] == "none" and _churn(second)["keys"][0]["rounds"] == 2


class TestSealedSpec:
    def test_the_restate_step_quotes_the_spec_goal_and_scope(self, world, capsys):
        world.seed(PRIOR_HEAD)
        spec = world.seal()

        code, result, _ = world.post_with_spec([_finding(spec_tag="in-spec")], spec, capsys=capsys)

        assert code == 0
        brief = _churn(result)["brief"]
        assert "Goal: Break the review churn." in brief
        assert "In scope:\n   - the churn output" in brief
        assert "Out of scope:\n   - the merge gate" in brief

    def test_without_a_spec_step_one_says_none_was_given(self, world, capsys):
        world.seed(PRIOR_HEAD)

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert "No sealed spec was given" in _churn(result)["brief"]


class TestLedgerRules:
    def test_fences_by_another_login_are_not_counted(self, world, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD, login="somebody-else")

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["level"] == "none" and _churn(result)["keys"] == []

    def test_several_fences_at_one_head_count_once(self, world, capsys):
        world.seed(PRIOR_HEAD, PRIOR_HEAD)

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["level"] == "brief" and _churn(result)["keys"][0]["rounds"] == 2

    def test_a_round_at_the_current_head_is_replaced_by_the_one_being_posted(self, world, capsys):
        world.seed(PRIOR_HEAD, world.host.head_sha)

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["keys"][0]["rounds"] == 2

    def test_a_clean_round_in_between_breaks_the_chain(self, world, capsys):
        world.seed(PRIOR_HEAD)
        world.seed(MIDDLE_HEAD, blocks_on=())

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["level"] == "none" and _churn(result)["keys"] == []

    def test_a_round_blocking_on_other_keys_breaks_the_chain(self, world, capsys):
        world.seed(PRIOR_HEAD)
        world.seed(MIDDLE_HEAD, blocks_on=(("R2", "a.py", 9),))

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["keys"] == []

    def test_a_fence_whose_blocking_keys_cannot_be_read_breaks_the_chain(self, world, capsys):
        world.seed(PRIOR_HEAD)
        world.seed(MIDDLE_HEAD)
        broken = world.host.seeded[-1]
        body = broken["body"].replace('"findings_open": [{"id": "f-', '"findings_open": [{"id": "x-')
        assert body != broken["body"]
        broken["body"] = body

        _, result, _ = world.post([_finding()], capsys=capsys)

        assert _churn(result)["keys"] == []

    def test_a_post_for_a_different_key_does_not_continue_the_chain(self, world, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)

        _, result, _ = world.post([_finding(rule="R2")], capsys=capsys)

        assert _churn(result)["keys"] == []

    def test_an_unreadable_comment_listing_is_reported_and_the_post_still_succeeds(
        self, world, capsys, monkeypatch
    ):
        world.seed(PRIOR_HEAD)

        def unreadable(self, *, owner, repo, pr_number):
            raise AcquireFetchError("the listing is gone")

        # Only the ledger lists comments through the acquire backends; the post's
        # own read-back of the landed comment uses the review-post path.
        for backend in (GithubAcquireBackend, ForgejoAcquireBackend):
            monkeypatch.setattr(backend, "fetch_issue_comments", unreadable)

        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0 and result["result"] == "posted"
        assert _churn(result)["level"] == "none"
        assert "the listing is gone" in _churn(result)["ledger_unavailable"]

    def test_the_security_reviewer_role_has_its_own_ledger(self, world, capsys):
        world.host.login = "security-reviewer"
        world.seed(PRIOR_HEAD, reviewer="security-reviewer")
        # A reviewer-role round at the same key is not this role's round.
        world.seed(MIDDLE_HEAD, login="reviewer")

        code, result, _ = world.post([_finding()], capsys=capsys, caller="security-reviewer")

        assert code == 0
        assert parse_verdict_block(world.host.last_body)["reviewer"] == "security-reviewer"
        assert _churn(result)["level"] == "brief"
        assert _churn(result)["keys"][0]["heads"] == [PRIOR_HEAD, world.host.head_sha]


class TestNetDeltaUnavailable:
    def test_a_compare_that_fails_gives_a_null_delta_with_a_reason_and_still_a_brief(
        self, world, capsys
    ):
        world.seed(PRIOR_HEAD)
        inner = world.host.opener

        def no_compare(req, timeout=30):
            if "/compare/" in req.full_url:
                return FakeResponse(500, b"{}")
            return inner(req, timeout)

        world.opener = no_compare

        code, result, _ = world.post([_finding()], capsys=capsys)

        assert code == 0 and result["result"] == "posted"
        report = _churn(result)
        assert report["level"] == "brief" and report["brief"]
        assert report["net_delta"] is None
        assert "range diff could not be read" in report["net_delta_unavailable"]

    def test_a_rewritten_history_gives_a_null_delta_with_a_reason(self, world, capsys):
        world.seed(MIDDLE_HEAD)
        inner = world.host.opener

        def diverged(req, timeout=30):
            # GitHub names the relation in the compare body; Forgejo's fake already
            # reports no commits forward from a head it does not know.
            if "/compare/" in req.full_url and req.get_header("Accept", "") != (
                "application/vnd.github.v3.diff"
            ):
                return json_resp(200, {"status": "diverged", "total_commits": 0})
            return inner(req, timeout)

        world.opener = diverged

        _, result, _ = world.post([_finding()], capsys=capsys)

        report = _churn(result)
        assert report["level"] == "brief" and report["net_delta"] is None
        assert report["net_delta_unavailable"]


class TestThePostedCommentIsUntouched:
    def test_the_comment_body_and_fence_are_the_same_with_and_without_churn(self, world, capsys):
        world.configure(0)
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)
        _, off, _ = world.post([_finding()], capsys=capsys)
        without = world.host.last_body
        world.host.posted.clear()
        world.configure(2)

        _, on, _ = world.post([_finding()], capsys=capsys)

        assert _churn(off)["level"] == "none" and _churn(on)["level"] == "stop"
        assert world.host.last_body == without
        fence = parse_verdict_block(world.host.last_body)
        assert "churn" not in fence and fence["fence_schema_version"] == 3

    def test_no_posted_text_mentions_the_brief(self, world, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)

        world.post([_finding()], capsys=capsys)

        assert "Restate the brief" not in world.host.last_body
        assert "STOP." not in world.host.last_body


class TestTheChurnVerb:
    @pytest.fixture
    def login(self, world, tmp_path):
        """The login the verb resolves for the reviewer role, set on the host."""
        if world.host.platform == "github":
            root = tmp_path / "user-config"
            root.mkdir(exist_ok=True)
            with (root / "config.yaml").open("a", encoding="utf-8") as handle:
                handle.write(yaml.safe_dump({"github_app": {"slugs": {"reviewer": BOT_SLUG}}}))
            world.host.login = f"{BOT_SLUG}[bot]"
        return world.host.login

    def test_it_reports_the_same_object_a_post_would_without_posting(self, world, login, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD, world.host.head_sha)

        code, result, _ = world.review("churn", capsys=capsys)

        assert code == 0 and result["result"] == "churn"
        assert result["reviewer_login"] == login and result["head_sha"] == world.host.head_sha
        report = result["churn"]
        assert report["level"] == "stop" and report["keys"][0]["rounds"] == 3
        assert report["options"] == ["simplify", "ship", "continue"]
        assert report["net_delta"]["files"] == [{"path": "a.py", "added": 2, "removed": 0}]
        assert world.host.posted == []

    def test_the_verb_and_the_post_agree_on_the_same_history(self, world, login, capsys):
        world.seed(PRIOR_HEAD, MIDDLE_HEAD)
        _, posted, _ = world.post([_finding()], capsys=capsys)

        _, read, _ = world.review("churn", capsys=capsys)

        assert read["churn"] == posted["churn"]

    def test_it_quotes_a_named_sealed_spec(self, world, login, capsys):
        world.seed(PRIOR_HEAD, world.host.head_sha)
        spec = world.seal()

        _, result, _ = world.review("churn", "--spec-hash", spec.spec_hash, capsys=capsys)

        assert "Goal: Break the review churn." in result["churn"]["brief"]

    def test_a_pull_request_with_no_rounds_has_no_churn(self, world, login, capsys):
        code, result, _ = world.review("churn", capsys=capsys)

        assert code == 0 and result["head_sha"] is None
        assert result["churn"]["level"] == "none" and result["churn"]["keys"] == []

    def test_another_logins_rounds_are_not_counted(self, world, login, capsys):
        world.seed(PRIOR_HEAD, world.host.head_sha, login="somebody-else")

        _, result, _ = world.review("churn", capsys=capsys)

        assert result["churn"]["level"] == "none"

    def test_the_breaker_switched_off_reads_no_comments(self, world, login, capsys):
        world.configure(0)
        world.seed(PRIOR_HEAD, world.host.head_sha)

        _, result, _ = world.review("churn", capsys=capsys)

        assert result["churn"]["level"] == "none" and result["churn"]["escalation_round"] == 0
        assert not [r for r in world.host.requests if "/comments" in r[1]]

    def test_an_unreadable_comment_listing_is_an_error_of_its_own(self, world, login, capsys):
        world.host.comment_read_failures = 1

        code, _, err = world.review("churn", capsys=capsys)

        assert code == review_cli.EXIT_ACQUIRE_FAILED and "cannot read comments" in err

    def test_an_unresolvable_login_is_a_configuration_error(self, world, capsys):
        if world.host.platform != "github":
            pytest.skip("a Forgejo role is its own login")

        code, _, err = world.review("churn", capsys=capsys)

        assert code == review_cli.EXIT_PROFILE_INVALID and "slug" in err

    def test_an_invalid_setting_is_a_configuration_error(self, world, login, capsys):
        world.configure(99)

        code, _, err = world.review("churn", capsys=capsys)

        assert code == review_cli.EXIT_PROFILE_INVALID and "99" in err
