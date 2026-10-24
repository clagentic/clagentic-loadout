"""The lighter docs-only review, end to end through `loadout-review run` and `post`,
on both platforms, against pull requests built from payloads captured from real
hosts.

The path is opt-in and errs toward more review: it needs a docs-only
classification at the base, a sealed spec whose docs-only justification is
non-empty, and a reviewer who does not reject it. A rejection anywhere on the pull
request means a full review from then on."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import fence_state
from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record.store import records_at_head, records_root
from tests._review_cli_support import Env, identity_provider, make_diff, prompts
from tests._review_evaluate_support import commit_files, facts
from tests._support.review_host import PRIOR_HEAD, ReviewHost

DOCS_PROFILE = """\
class: internal
review:
  docs_only:
    paths: ["docs/**/*.md", "README.md"]
    depended_on: ["docs/README.md"]
"""

JUSTIFICATION = "Only wording in the guide changed; no behaviour is touched."
SPEC_HEAD = "c" * 40
CRITERIA = ("ac-1", "ac-2")

#: Blocks on the worst-case stakes an unstated profile resolves to, and is not a
#: security-floor category, so a full review blocks on it and the lighter one
#: does not.
BLOCKING = facts(confidentiality="full", integrity="full", availability="full")
SECRET = facts(
    category="secrets_exposure", confidentiality="full", integrity="full", availability="full"
)


def _finding(line=3, fact=BLOCKING, **extra):
    item = {
        "file": "docs/guide.md", "line": line, "rule_id": "R1", "message": "a claim is wrong",
        "spec_tag": "in-spec", **extra,
    }
    if fact is not None:
        item["facts"] = fact
    return item


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path

    def review(
        self, command: str, *extra: str, capsys, local_repo: bool = True, caller: str = "reviewer"
    ):
        argv = [
            command, "--caller", caller, "--repo", "some-owner/some-repo",
            *self.host.platform_args(),
            *(["--repo-path", str(self.env.repo)] if local_repo else []),
            *extra,
        ]
        capsys.readouterr()
        code = review_cli.main(
            argv,
            token_provider=self.env.token_provider,
            opener=self.host.opener,
            identity_provider=identity_provider(caller),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def write(self, name: str, document) -> str:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def seal(self, justification: str | None = JUSTIFICATION) -> model.SealedSpec:
        draft = model.new_draft("planner-role")
        draft["goal"] = "Fix the guide."
        draft["acceptance_criteria"] = [
            {"id": cid, "given": "g", "when": "w", "then": "t"} for cid in CRITERIA
        ]
        draft["docs_only_justification"] = justification
        sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
        spec_store.write_sealed(spec_store.specs_root(), sealed)
        return sealed

    def post(self, findings, *extra: str, capsys, spec: model.SealedSpec | None = None, **kwargs):
        args = [
            "--findings", self.write("findings.json", self._document(findings)),
            "--head-sha", self.host.head_sha, "--base-sha", self.env.base_sha, *extra,
        ]
        if spec is not None:
            args += [
                "--spec-hash", spec.spec_hash,
                "--spec-coverage",
                self.write(
                    "coverage.json", [{"criterion": c, "status": "met"} for c in CRITERIA]
                ),
            ]
        return self.review("post", *args, capsys=capsys, **kwargs)

    def _document(self, findings) -> dict:
        return {
            "owner": "some-owner", "repo": "some-repo", "pr_number": self.host.number,
            "head_sha": self.host.head_sha, "findings": findings,
        }

    def fence(self) -> dict:
        return parse_verdict_block(self.host.last_body)

    def records(self):
        return records_at_head(records_root(), self.host.head_sha).records


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path, seed_profile=False)
    env.base_sha = commit_files(
        env.repo,
        {PROFILE_PATH: DOCS_PROFILE, "docs/guide.md": "guide\n", "docs/README.md": "index\n",
         "src/app.py": "x = 1\n"},
    )
    host = ReviewHost(
        request.param,
        diff=make_diff({"docs/guide.md": 3}),
        base_sha=env.base_sha,
        files=["docs/guide.md"],
    )
    monkeypatch.setenv("STUB_DIR", str(env.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return World(env, host, tmp_path)


class TestTheLighterPath:
    def test_a_docs_only_change_with_a_justification_gets_one_nonblocking_pass(
        self, world, capsys
    ):
        spec = world.seal()

        code, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert code == 0
        assert result["status"] == "clean" and result["blocking_count"] == 0
        assert result["finding_count"] == 1
        assert result["docs_only"]["lighter_review"] is True
        body = world.host.last_body
        assert "Advisory findings (1):" in body and "Blocking findings" not in body
        assert "docs-only pass: advisory" in body
        assert "docs-only: lighter review, justification accepted" in body
        fence = world.fence()
        assert fence["review_status"] == "clean" and fence["verdict_outcome"] == "CLEAN"
        # Nothing is held open, and nothing advisory is handed to the gate to judge.
        assert "findings_open" not in fence
        assert fence["evaluated_findings"] == []

    def test_the_waiver_object_names_the_allowlist_source_and_the_justification_verbatim(
        self, world, capsys
    ):
        spec = world.seal()

        world.post([_finding()], spec=spec, capsys=capsys)

        fence = world.fence()
        assert fence["fence_schema_version"] == fence_state.DOCS_ONLY_FENCE_SCHEMA_VERSION == 5
        assert fence["docs_only"] == {
            "classification": "docs-only",
            "allowlist_source": {"base_sha": world.env.base_sha, "path": PROFILE_PATH},
            "justification": JUSTIFICATION,
            "spec_hash": spec.spec_hash,
            "accepted": True,
            "rejection_reason": None,
        }
        # The spec fields the review already carried are still there.
        assert fence["spec_hash"] == spec.spec_hash

    def test_the_verdict_record_carries_the_same_object_at_schema_version_two(
        self, world, capsys
    ):
        spec = world.seal()

        world.post([_finding()], spec=spec, capsys=capsys)

        [reviewer] = [r for r in world.records() if r.gate == "reviewer:reviewer"]
        assert reviewer.docs_only == world.fence()["docs_only"]
        assert reviewer.as_dict()["schema_version"] == 2
        assert reviewer.passed and reviewer.spec_hash == spec.spec_hash
        # The coverage pass is recorded as it was, at version one.
        [coverage] = [r for r in world.records() if r.gate == "spec-coverage"]
        assert coverage.as_dict()["schema_version"] == 1 and coverage.docs_only is None

    def test_a_secret_finding_still_blocks_and_is_held_open(self, world, capsys):
        spec = world.seal()
        findings = [_finding(3, SECRET, rule_id="SECRET"), _finding(4)]

        code, result, _ = world.post(findings, spec=spec, capsys=capsys)

        assert code == 0
        assert result["status"] == "blocking" and result["blocking_count"] == 1
        fence = world.fence()
        assert [e["rule_id"] for e in fence["findings_open"]] == ["SECRET"]
        assert [e["rule_id"] for e in fence["evaluated_findings"]] == ["SECRET"]
        assert fence["verdict_outcome"] == "BLOCKED"
        body = world.host.last_body
        assert "Blocking findings (1):" in body and "Advisory findings (1):" in body
        # The path was still the lighter one: the waiver is recorded.
        assert fence["docs_only"]["accepted"] is True

    @pytest.mark.parametrize("fact", [None, {"category": "not-a-category"}, {}])
    def test_a_finding_that_states_no_usable_category_may_be_a_secret_so_it_blocks(
        self, world, capsys, fact
    ):
        spec = world.seal()

        _, result, _ = world.post([_finding(fact=fact)], spec=spec, capsys=capsys)

        assert result["status"] == "blocking" and result["blocking_count"] == 1
        assert world.fence()["findings_open"]

    def test_a_clean_review_with_no_findings_is_still_a_real_verdict_with_the_waiver(
        self, world, capsys
    ):
        spec = world.seal()

        code, result, _ = world.post([], spec=spec, capsys=capsys)

        assert code == 0 and result["status"] == "clean"
        assert world.fence()["docs_only"]["accepted"] is True

    def test_the_scanner_state_the_deployment_requires_is_still_required(
        self, world, capsys
    ):
        world.env.cfg.mkdir(parents=True, exist_ok=True)
        (world.env.cfg / "config.yaml").write_text(
            yaml.safe_dump({"review": {"evaluator": {"required_scanners": ["secret-scan"]}}}),
            encoding="utf-8",
        )
        spec = world.seal()

        _, missing, _ = world.post([], spec=spec, capsys=capsys)

        assert missing["status"] == "blocking"
        assert world.fence()["verdict_outcome"] == "DEGRADED"
        state = world.write(
            "state.json", {"scanners_run": [{"scanner": "secret-scan", "status": "ran"}]}
        )
        _, ran, _ = world.post([], "--state-file", state, spec=spec, capsys=capsys)
        assert ran["status"] == "clean"
        assert world.fence()["verdict_outcome"] == "CLEAN"

    def test_the_allowlist_is_read_from_the_host_when_no_local_repository_is_named(
        self, world, capsys
    ):
        world.host.base_files = {
            PROFILE_PATH: DOCS_PROFILE, "docs/guide.md": "guide\n", "docs/README.md": "index\n"
        }
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys, local_repo=False)

        assert result["status"] == "clean"
        assert world.fence()["docs_only"]["accepted"] is True

    def test_a_security_reviewer_role_takes_the_same_path_under_its_own_gate(
        self, world, capsys
    ):
        world.env.cfg.mkdir(parents=True, exist_ok=True)
        (world.env.cfg / "config.yaml").write_text(
            yaml.safe_dump({"review": {"evaluator": {"required_scanners": ["secret-scan"]}}}),
            encoding="utf-8",
        )
        spec = world.seal()
        state = world.write(
            "state.json", {"scanners_run": [{"scanner": "secret-scan", "status": "ran"}]}
        )

        code, result, _ = world.post(
            [_finding(3, SECRET, rule_id="SECRET"), _finding(4)],
            "--state-file", state, spec=spec, capsys=capsys, caller="security-reviewer",
        )

        assert code == 0 and result["blocking_count"] == 1
        fence = world.fence()
        assert fence["reviewer"] == "security-reviewer"
        assert fence["docs_only"]["accepted"] is True
        assert [e["rule_id"] for e in fence["findings_open"]] == ["SECRET"]
        assert [r.gate for r in world.records() if r.docs_only is not None] == [
            "reviewer:security-reviewer"
        ]


class TestFullReview:
    """Every way the lighter path does not apply gives the review it always gave."""

    def test_no_spec_means_a_full_review_even_for_pure_documentation(self, world, capsys):
        _, result, _ = world.post([_finding()], "--status", "blocking", capsys=capsys)

        assert result["status"] == "blocking" and result["blocking_count"] == 1
        assert "docs_only" not in world.fence()
        assert "docs_only" not in result
        [reviewer] = [r for r in world.records() if r.gate == "reviewer:reviewer"]
        assert reviewer.docs_only is None and reviewer.as_dict()["schema_version"] == 1

    def test_a_spec_with_no_justification_means_a_full_review(self, world, capsys):
        spec = world.seal(justification=None)

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1
        assert "docs_only" not in world.fence() and "docs_only" not in result

    def test_a_code_change_with_a_justification_means_a_full_review(self, world, capsys):
        world.host.diff = make_diff({"docs/guide.md": 3, "src/app.py": 2})
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1
        assert "docs_only" not in world.fence()
        assert result["docs_only"]["lighter_review"] is False
        assert "classified as code" in result["docs_only"]["note"]

    def test_a_listed_instruction_file_makes_the_change_code(self, world, capsys):
        world.host.diff = make_diff({"docs/README.md": 3})
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1 and "docs_only" not in world.fence()

    def test_a_change_to_the_allowlist_is_judged_by_the_base_and_is_code(self, world, capsys):
        # The change widens the list to everything; the base's list decides.
        world.host.diff = make_diff({PROFILE_PATH: 4, "docs/guide.md": 3})
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1 and "docs_only" not in world.fence()

    def test_a_base_with_no_allowlist_means_a_full_review(self, world, capsys):
        world.env.base_sha = commit_files(world.env.repo, {PROFILE_PATH: "class: internal\n"})
        world.host.pull["base"]["sha"] = world.env.base_sha
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1 and "docs_only" not in world.fence()

    def test_unreadable_earlier_verdicts_mean_a_full_review(self, world, capsys):
        world.host.comment_read_failures = 1
        spec = world.seal()

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1
        assert "could not be read" in result["docs_only"]["note"]
        assert "docs_only" not in world.fence()

    def test_a_justification_that_cannot_be_recorded_in_the_fence_means_a_full_review(
        self, world, capsys
    ):
        spec = world.seal(justification="Wording only. ```review-result is just text here.")

        _, result, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert result["blocking_count"] == 1
        assert "cannot be recorded in the fence" in result["docs_only"]["note"]
        assert "docs_only" not in world.fence()


class TestRejection:
    REASON = "The guide change alters documented behaviour, so it needs tests."

    def test_rejecting_the_justification_posts_a_full_review_and_records_the_rejection(
        self, world, capsys
    ):
        spec = world.seal()

        code, result, _ = world.post(
            [_finding()], "--reject-justification", self.REASON, spec=spec, capsys=capsys
        )

        assert code == 0
        assert result["status"] == "blocking" and result["blocking_count"] == 1
        assert result["docs_only"]["lighter_review"] is False
        fence = world.fence()
        assert fence["docs_only"]["accepted"] is False
        assert fence["docs_only"]["rejection_reason"] == self.REASON
        assert fence["docs_only"]["justification"] == JUSTIFICATION
        assert fence["findings_open"] and len(fence["evaluated_findings"]) == 1
        assert f"docs-only: justification rejected, full review: {self.REASON}" in (
            world.host.last_body
        )
        [reviewer] = [r for r in world.records() if r.gate == "reviewer:reviewer"]
        assert reviewer.docs_only["accepted"] is False and not reviewer.passed

    def test_a_rejection_makes_every_later_round_a_full_review_whatever_the_spec_says(
        self, world, capsys
    ):
        spec = world.seal()
        world.post([_finding()], "--reject-justification", self.REASON, spec=spec, capsys=capsys)

        _, later, _ = world.post([_finding()], spec=spec, capsys=capsys)

        assert later["status"] == "blocking" and later["blocking_count"] == 1
        assert "rejected the justification" in later["docs_only"]["note"]
        assert "docs_only" not in world.fence()

    def test_the_next_run_classifies_the_pull_request_as_code(self, world, capsys):
        world.env.configure()
        _, before, _ = world.review("run", capsys=capsys)
        assert before["changeset"]["classification"] == "docs-only"
        assert before["changeset"]["lighter_review_available"] is True
        spec = world.seal()
        world.post([_finding()], "--reject-justification", self.REASON, spec=spec, capsys=capsys)

        code, after, _ = world.review("run", capsys=capsys)

        assert code == 0
        assert after["changeset"]["classification"] == "code"
        assert after["changeset"]["lighter_review_available"] is False
        assert after["changeset"]["path_classification"] == "docs-only"
        assert "rejected the justification" in after["changeset"]["note"]

    def test_a_delta_run_after_a_rejection_stays_full(self, world, capsys):
        world.env.configure()
        world.host.range_diff = make_diff({"docs/guide.md": 1})
        _, first, _ = world.review("run", "--prior-head-sha", PRIOR_HEAD, capsys=capsys)
        assert first["mode"] == "delta"
        assert first["changeset"]["lighter_review_available"] is True
        spec = world.seal()
        world.post([_finding()], "--reject-justification", self.REASON, spec=spec, capsys=capsys)

        code, result, _ = world.review("run", "--prior-head-sha", PRIOR_HEAD, capsys=capsys)

        assert code == 0 and result["mode"] == "delta"
        assert result["changeset"]["classification"] == "code"
        assert result["changeset"]["lighter_review_available"] is False

    def test_a_rejection_needs_a_justification_to_reject(self, world, capsys):
        no_justification = world.seal(justification=None)

        code_a, _, err_a = world.post(
            [_finding()], "--reject-justification", self.REASON, spec=no_justification,
            capsys=capsys,
        )
        code_b, _, err_b = world.post(
            [_finding()], "--reject-justification", self.REASON, capsys=capsys
        )

        assert code_a == code_b == review_cli.EXIT_USAGE
        assert "justification" in err_a and "justification" in err_b
        assert world.host.posted == []

    @pytest.mark.parametrize("reason", ["", "   ", "two\nlines", "x" * 400, "has ``` fence"])
    def test_a_reason_that_is_not_one_clean_line_is_a_usage_error(self, world, capsys, reason):
        spec = world.seal()

        code, _, err = world.post(
            [_finding()], "--reject-justification", reason, spec=spec, capsys=capsys
        )

        assert code == review_cli.EXIT_USAGE and "--reject-justification" in err
        assert world.host.posted == []


class TestRun:
    def test_the_run_result_records_the_classification_and_its_allowlist_source(
        self, world, capsys
    ):
        world.env.configure()

        code, result, _ = world.review("run", capsys=capsys)

        assert code == 0
        block = result["changeset"]
        assert block["classification"] == "docs-only"
        assert block["allowlist_source"] == {
            "base_sha": world.env.base_sha, "path": PROFILE_PATH, "state": "ok",
        }
        assert block["code_paths"] == [] and block["path_count"] == 1
        assert block["lighter_review_available"] is True
        # The findings a post reads do not depend on the classification.
        document = json.loads(Path(result["findings_file"]).read_text(encoding="utf-8"))
        assert "changeset" not in document

    def test_a_delta_run_classifies_the_whole_pull_request_not_the_slice_it_reviews(
        self, world, capsys
    ):
        world.host.diff = make_diff({"docs/guide.md": 3, "src/app.py": 2})
        world.host.range_diff = make_diff({"docs/guide.md": 1})
        world.env.configure()

        code, result, _ = world.review("run", "--prior-head-sha", PRIOR_HEAD, capsys=capsys)

        assert code == 0 and result["mode"] == "delta"
        assert result["changeset"]["classification"] == "code"
        assert [p["path"] for p in result["changeset"]["code_paths"]] == ["src/app.py"]

    def test_a_change_with_code_in_it_is_reported_with_the_paths_that_made_it_code(
        self, world, capsys
    ):
        world.host.diff = make_diff({"docs/guide.md": 3, "src/app.py": 2})
        world.env.configure()

        _, result, _ = world.review("run", capsys=capsys)

        block = result["changeset"]
        assert block["classification"] == "code" and block["lighter_review_available"] is False
        assert block["code_paths"] == [{"path": "src/app.py", "reason": "a script or source file"}]

    def test_the_classification_does_not_depend_on_whether_the_changed_file_list_is_served(
        self, world, capsys
    ):
        world.host.files = []
        world.env.configure()

        _, result, _ = world.review("run", capsys=capsys)

        assert result["changeset"]["classification"] == "docs-only"

    def test_a_file_the_list_names_but_the_diff_does_not_still_counts(self, world, capsys):
        world.host.files = ["docs/guide.md", "src/hidden.py"]
        world.env.configure()

        _, result, _ = world.review("run", capsys=capsys)

        assert result["changeset"]["classification"] == "code"

    def test_unreadable_earlier_verdicts_close_the_lighter_path_on_a_run(self, world, capsys):
        world.host.comment_read_failures = 1
        world.env.configure()

        _, result, _ = world.review("run", capsys=capsys)

        assert result["changeset"]["lighter_review_available"] is False
        assert "could not be read" in result["changeset"]["note"]

    def test_the_allowlist_is_read_from_the_host_when_no_local_repository_is_named(
        self, world, capsys
    ):
        world.host.base_files = {PROFILE_PATH: DOCS_PROFILE, "docs/guide.md": "guide\n"}
        world.env.configure()

        _, result, _ = world.review("run", capsys=capsys, local_repo=False)

        assert result["changeset"]["classification"] == "docs-only"
        assert result["changeset"]["allowlist_source"]["base_sha"] == world.env.base_sha

    def test_comments_are_read_only_when_the_lighter_path_is_open(self, world, capsys):
        world.host.diff = make_diff({"docs/guide.md": 3, "src/app.py": 2})
        world.env.configure()

        world.review("run", capsys=capsys)

        assert not [r for r in world.host.requests if "/comments" in r[1]]


class TestJustificationInThePrompt:
    """The justification reaches every chunk prompt of a run the lighter path is open
    to, and no other run's prompt."""

    HEADING = "## Docs-only justification"

    def _run(self, world, capsys, spec, *extra):
        before = len(prompts(world.env.stubs, "carrier"))
        code, result, _ = world.review("run", "--spec-hash", spec.spec_hash, *extra, capsys=capsys)
        assert code == 0
        return result, prompts(world.env.stubs, "carrier")[before:]

    def test_an_eligible_run_quotes_the_justification_verbatim_and_asks_to_judge_it(
        self, world, capsys
    ):
        world.env.configure()
        spec = world.seal()

        result, sent = self._run(world, capsys, spec)

        assert result["changeset"]["lighter_review_available"] is True
        assert sent
        for prompt in sent:
            assert self.HEADING in prompt
            assert f"> {JUSTIFICATION}" in prompt
            assert "classified as docs-only" in prompt
            assert "Judge whether this justification holds" in prompt
            # The output contract stays last.
            assert prompt.rfind(self.HEADING) < prompt.rfind("## Diff chunk")

    def test_every_chunk_of_a_multi_chunk_run_carries_it(self, world, capsys):
        world.host.diff = make_diff({"docs/guide.md": 30})
        world.env.configure(chunk_lines=10)
        spec = world.seal()

        result, sent = self._run(world, capsys, spec)

        assert result["chunk_count"] > 1 and len(sent) >= result["chunk_count"]
        assert all(f"> {JUSTIFICATION}" in prompt for prompt in sent)

    def test_a_multi_line_justification_is_quoted_line_by_line(self, world, capsys):
        world.env.configure()
        spec = world.seal(justification="First line.\nSecond line.")

        _, sent = self._run(world, capsys, spec)

        assert all("> First line.\n> Second line." in prompt for prompt in sent)

    def test_a_code_classified_run_carries_none(self, world, capsys):
        world.host.diff = make_diff({"docs/guide.md": 3, "src/app.py": 2})
        world.env.configure()
        spec = world.seal()

        result, sent = self._run(world, capsys, spec)

        assert result["changeset"]["classification"] == "code"
        assert sent and all(self.HEADING not in p and JUSTIFICATION not in p for p in sent)

    def test_a_run_after_a_rejection_carries_none(self, world, capsys):
        world.env.configure()
        spec = world.seal()
        world.post(
            [_finding()], "--reject-justification", TestRejection.REASON, spec=spec, capsys=capsys
        )

        result, sent = self._run(world, capsys, spec)

        assert result["changeset"]["classification"] == "code"
        assert sent and all(self.HEADING not in p and JUSTIFICATION not in p for p in sent)

    def test_a_run_whose_earlier_verdicts_cannot_be_read_carries_none(self, world, capsys):
        world.host.comment_read_failures = 1
        world.env.configure()
        spec = world.seal()

        result, sent = self._run(world, capsys, spec)

        assert result["changeset"]["lighter_review_available"] is False
        assert sent and all(self.HEADING not in p for p in sent)

    def test_a_spec_without_a_justification_carries_none(self, world, capsys):
        world.env.configure()
        spec = world.seal(justification=None)

        _, sent = self._run(world, capsys, spec)

        assert sent and all(self.HEADING not in p for p in sent)

    def test_a_run_given_no_spec_carries_none(self, world, capsys):
        world.env.configure()
        world.seal()

        code, _, _ = world.review("run", capsys=capsys)

        sent = prompts(world.env.stubs, "carrier")
        assert code == 0 and sent and all(self.HEADING not in p for p in sent)

    def test_the_justification_is_part_of_what_a_resume_binds(self, world, capsys):
        world.env.configure()
        first = world.seal()
        second = world.seal(justification="A different claim about the same change.")

        _, sent_first = self._run(world, capsys, first)
        _, sent_second = self._run(world, capsys, second)

        assert sent_first and sent_second
        assert all("A different claim" in p and JUSTIFICATION not in p for p in sent_second)

    def test_a_spec_that_is_not_in_the_store_is_refused_before_any_host_call(
        self, world, capsys
    ):
        world.env.configure()

        code, _, err = world.review("run", "--spec-hash", "a" * 64, capsys=capsys)

        assert code == review_cli.EXIT_SPEC_UNAVAILABLE
        assert world.host.requests == [] and err

    def test_a_malformed_spec_hash_is_a_usage_error(self, world, capsys):
        world.env.configure()

        code, _, err = world.review("run", "--spec-hash", "nothex", capsys=capsys)

        assert code == review_cli.EXIT_USAGE and "--spec-hash" in err
