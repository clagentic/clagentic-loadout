"""The merge gate's own computation of a reviewer's verdict.

Every case runs the real verb with an injected host opener and a real git
repository, because the gate reads policy (dispositions) from the PR's live base
commit. Fences are built with the product's own fence builder."""

from __future__ import annotations

import base64
import json
import urllib.parse
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.gate_evaluation import unmatched_recorded_blocks
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._gate_repo import commit_files, git, init_gate_repo
from tests._review_evaluate_support import facts
from tests._support.fakes import FakeResponse
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
LOGIN = "reviewer-login"
NAME = "some-reviewer"
PATH = "src/a.py"

#: Critical on the worst-case stakes every unstated path resolves to.
BLOCKING_FACTS = facts(confidentiality="full", integrity="full", availability="full")
#: A security-floor category: by_design can never clear it.
FLOOR_FACTS = facts(category="injection", confidentiality="full", integrity="full", availability="full")
#: Scores none: advisory, never blocks.
HARMLESS_FACTS = facts()


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _finding(fact=BLOCKING_FACTS, *, line=10, rule="R1", **extra):
    entry = {"file": PATH, "line": line, "rule_id": rule, "message": "something is wrong", **extra}
    if fact is not None:
        entry["facts"] = fact
    return entry


def _disposition_file(*, kind="false_positive", expires="2999-01-01", rule="R1", path="src/**"):
    entry = {
        "path": path, "rule_id": rule, "kind": kind, "rationale": "ruled by hand",
        "by": "a-maintainer", "at": "2026-01-01", "expires": expires,
    }
    return yaml.safe_dump({"dispositions": [entry]})


def _repo(tmp_path: Path, *, dispositions: str | None = None, **merge_gate) -> Path:
    """A repo whose main tip carries the tracked gate (and dispositions)."""
    path = tmp_path / "repo"
    init_gate_repo(path, tracked_gate={"required_reviewer_roles": [], **merge_gate})
    if dispositions is not None:
        commit_files(path, {DISPOSITIONS_PATH: dispositions}, "chore: add dispositions")
    return path


def _tip(repo: Path) -> str:
    return git(repo, "rev-parse", "main")


_DERIVE = object()


def _open_entries(findings):
    """findings_open as the post path writes it: one entry per distinct id."""
    entries = {}
    for item in findings:
        state_id = finding_state_id(item["file"], item["line"], item["rule_id"])
        entries.setdefault(state_id, {"id": state_id, "rule_id": item["rule_id"], "head": HEAD})
    return list(entries.values())


def _fence(
    outcome, *, findings=(), status=None, head=HEAD, reasons=None, base=None, keep_findings=True,
    opened=_DERIVE, **more,
):
    """A fence of *outcome*. A BLOCKED fence records every finding as blocking
    unless *opened* names the entries (None leaves findings_open out)."""
    state = {
        "verdict_outcome": outcome,
        "verdict_reasons": reasons if reasons is not None else ([] if outcome == "CLEAN" else ["recorded"]),
        "evaluator_version": EVALUATOR_VERSION,
        **more,
    }
    if opened is _DERIVE:
        opened = _open_entries(findings) if outcome == "BLOCKED" else None
    if opened is not None:
        state["findings_open"] = opened
    if keep_findings:
        state["evaluated_findings"] = list(findings)
    if base:
        state["policy_base_sha"] = base
    status = status or ("clean" if outcome == "CLEAN" else "blocking")
    return build_verdict_block(NAME, status, head, 1, findings_state=state)


def _comment(comment_id, body):
    return {"id": comment_id, "user": {"login": LOGIN}, "body": body}


class _Run:
    def __init__(self, code, err, out, posted, requests):
        self.code, self.err, self.out, self.posted, self.requests = code, err, out, posted, requests


def _base_files(repo: Path) -> dict[str, str]:
    """The file tree at the repository's main tip, as the host would serve it."""
    names = git(repo, "ls-tree", "-r", "--name-only", _tip(repo)).splitlines()
    return {name: git(repo, "show", f"{_tip(repo)}:{name}") for name in names}


def _host_files_opener(inner, files: dict[str, str], requests: list[str], contents_http: int):
    """Serve the git-trees and contents endpoints from *files*, defer the rest."""

    def opener(req, timeout=15):
        url = req.full_url
        if req.get_method() == "GET" and "/git/trees/" in url:
            requests.append(url)
            entries = [{"path": name, "type": "blob"} for name in files]
            return FakeResponse(200, json.dumps({"tree": entries, "truncated": False}).encode())
        if req.get_method() == "GET" and "/contents/" in url:
            requests.append(url)
            if contents_http != 200:
                return FakeResponse(contents_http, b"{}")
            name = urllib.parse.unquote(url.split("/contents/", 1)[1].split("?")[0])
            if name not in files:
                return FakeResponse(404, b"{}")
            encoded = base64.b64encode(files[name].encode("utf-8")).decode("ascii")
            body = {"type": "file", "encoding": "base64", "content": encoded}
            return FakeResponse(200, json.dumps(body).encode())
        return inner(req, timeout)

    return opener


def _merge(
    comments, repo: Path | None, capsys, *, head=HEAD, extra=None, pr_extra=None,
    local=True, contents_http=200,
):
    """Run the merge verb against *repo*'s main tip as the PR base. *local*
    False passes no --repo-path (the repository is then reachable only through
    the host's tree and contents endpoints)."""
    posted: list[str] = []
    policy_requests: list[str] = []
    info = {"head": {"sha": head}, "title": "feat: x"}
    if repo is not None:
        info["base"] = {"ref": "main", "sha": _tip(repo), "repo": {"private": True}}
    info.update(pr_extra or {})
    inner = make_opener(pr_info=info, comments=comments)
    served = _host_files_opener(
        inner, _base_files(repo) if repo is not None else {}, policy_requests, contents_http
    )

    def opener(req, timeout=15):
        if req.get_method() == "POST" and "/comments" in req.full_url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return served(req, timeout)

    argv = base_args(
        **{"--required-reviewer": f"{NAME}:{LOGIN}", **({"--repo-path": str(repo)} if repo and local else {})}
    )
    capsys.readouterr()
    code = verb.main(
        argv + (extra or []),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return _Run(code, captured.err, captured.out, posted, policy_requests)


class TestBlocked:
    def test_a_blocked_fence_refuses_naming_each_finding_and_its_clearing_stanza(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        findings = [_finding(), _finding(line=20, rule="R2")]
        run = _merge([_comment(1, _fence("BLOCKED", findings=findings, base=_tip(repo)))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in run.err and "src/a.py:20:R2" in run.err
        assert run.err.count("dispositions:") == 2
        assert '- path: "src/a.py"' in run.err or 'path: "src/a.py"' in run.err
        assert 'rule_id: "R2"' in run.err
        assert DISPOSITIONS_PATH in run.err

    def test_a_security_floor_finding_says_by_design_cannot_clear_it(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding(FLOOR_FACTS)]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "security floor forbids" in run.err

    def test_a_blocked_fence_then_a_clean_fence_at_the_same_head_merges(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        blocked = [_finding()]
        # The later fence clears what the earlier one held open, as the
        # prior-findings gate requires of any later clean verdict.
        cleared = [
            {"id": entry["id"], "head": HEAD, "evidence": "fixed in the follow-up commit"}
            for entry in _open_entries(blocked)
        ]
        comments = [
            _comment(1, _fence("BLOCKED", findings=blocked)),
            _comment(2, _fence("CLEAN", findings=[], cleared_claims=cleared)),
        ]
        run = _merge(comments, repo, capsys)

        assert run.code == verb.EXIT_OK, run.err

    def test_a_clean_fence_whose_poster_asked_for_blocking_still_refuses(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", status="blocking", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "BLOCKED merge" in run.err


class TestClean:
    def test_clean_at_the_merge_head_that_the_gate_reproduces_proceeds(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        findings = [_finding(HARMLESS_FACTS)]
        run = _merge([_comment(1, _fence("CLEAN", findings=findings))], repo, capsys)

        assert run.code == verb.EXIT_OK
        result = json.loads(run.out.strip().splitlines()[-1])
        assert result["verdict_evaluations"] == [
            {
                "reviewer": NAME, "evaluated": True, "fence_outcome": "CLEAN",
                "gate_outcome": "CLEAN", "applied_dispositions": [], "degraded_allowed": False,
                "degraded_reasons": [],
            }
        ]

    def test_the_recorded_policy_base_is_never_read(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        elsewhere = "d" * 40
        run = _merge([_comment(1, _fence("CLEAN", findings=[], base=elsewhere))], repo, capsys)

        assert run.code == verb.EXIT_OK

    def test_a_clean_fence_with_no_base_to_read_policy_at_refuses(self, tmp_path, capsys):
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], None, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "records CLEAN" in run.err
        assert "policy base revision is not known" in run.err

    def test_a_fence_that_does_not_carry_its_findings_cannot_be_checked(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", keep_findings=False))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "evaluated_findings" in run.err


class TestStale:
    def test_an_outcome_of_stale_refuses_naming_both_heads(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("STALE", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "STALE" in run.err and HEAD in run.err

    def test_a_fence_for_another_head_refuses_naming_the_heads(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", findings=[], head=OTHER_HEAD))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert OTHER_HEAD in run.err and HEAD in run.err


class TestDegraded:
    def _degraded(self):
        # No facts on a blocking finding: the gate computes DEGRADED as well.
        return _fence("DEGRADED", findings=[_finding(None)], reasons=["facts absent on src/a.py:10:R1"])

    def test_degraded_refuses_by_default_naming_the_reasons(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "DEGRADED" in run.err
        assert "facts absent" in run.err
        assert "allow_degraded" in run.err

    def test_degraded_for_a_role_that_is_not_allowed_refuses_when_another_is(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {"another-role": True, NAME: False}})
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_degraded_proceeds_when_allowed_for_the_role_and_the_attestation_records_it(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: True}})
        run = _merge([_comment(1, self._degraded())], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "allow_degraded" in run.err
        assert run.posted, "the merge-completion attestation was not posted"
        assert "DEGRADED allowed by merge.evaluator.allow_degraded" in run.posted[-1]
        result = json.loads(run.out.strip().splitlines()[-1])
        assert result["verdict_evaluations"][0]["degraded_allowed"] is True

    def test_allow_degraded_never_lets_a_blocked_fence_through(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: True}})
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_malformed_evaluator_declaration_is_strict(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"allow_degraded": {NAME: "yes"}})
        degraded = _merge([_comment(1, self._degraded())], repo, capsys)
        legacy = _merge([_comment(1, build_verdict_block(NAME, "clean", HEAD, 1))], repo, capsys)

        assert degraded.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "merge.evaluator is malformed" in degraded.err
        assert legacy.code == verb.EXIT_GATE_RESULT_BLOCKED


class TestLiveBasePolicy:
    def test_a_floor_finding_whose_disposition_expired_before_merge_refuses_naming_it(
        self, tmp_path, capsys
    ):
        expired = _disposition_file(kind="accepted_risk", expires="2026-01-02")
        repo = _repo(tmp_path, dispositions=expired)
        run = _merge(
            [_comment(1, _fence("CLEAN", findings=[_finding(FLOOR_FACTS)]))], repo, capsys
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "expired" in run.err
        assert "src/** R1" in run.err
        assert "records CLEAN" in run.err

    def test_a_disposition_merged_after_the_review_demotes_the_block_and_is_named(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        reviewed_at = _tip(repo)
        commit_files(repo, {DISPOSITIONS_PATH: _disposition_file()}, "chore: rule on it")
        fence = _fence("BLOCKED", findings=[_finding()], base=reviewed_at)
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "false_positive" in run.err
        assert "src/** R1" in run.err
        assert _tip(repo) in run.err
        assert "false_positive" in run.posted[-1]

    def test_a_disposition_that_narrows_by_fingerprint_never_demotes_a_recorded_block(
        self, tmp_path, capsys
    ):
        entry = yaml.safe_load(_disposition_file())
        entry["dispositions"][0]["fingerprint"] = "fp2:" + "a" * 16
        repo = _repo(tmp_path, dispositions=yaml.safe_dump(entry))
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "fingerprint" in run.err

    def test_a_stricter_policy_merged_after_the_review_refuses_naming_the_policy_change(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        reviewed_at = _tip(repo)
        # The review was posted when the base ruled the finding a false positive;
        # the ruling has since been removed from the base.
        commit_files(repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": []})}, "chore: revoke")
        fence = _fence("CLEAN", findings=[_finding()], base=reviewed_at)
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "policy base moved" in run.err
        assert reviewed_at in run.err and _tip(repo) in run.err
        assert "src/a.py:10:R1" in run.err

    def test_a_recorded_block_without_a_disposition_to_explain_it_is_not_cleared(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        # The fence says BLOCKED but its finding is harmless: the gate's own
        # computation is CLEAN with nothing applied, and a block stands.
        fence = _fence("BLOCKED", findings=[_finding(HARMLESS_FACTS)])
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert finding_state_id(PATH, 10, "R1") in run.err
        assert "no disposition at the live base matches it" in run.err

    def test_a_disposition_on_another_finding_does_not_clear_a_recorded_block(
        self, tmp_path, capsys
    ):
        # A is harmless at the gate (not blocking for a reason that is not a
        # disposition); B is blocking and a live-base disposition matches only B.
        repo = _repo(tmp_path, dispositions=_disposition_file(rule="R2"))
        finding_a = _finding(HARMLESS_FACTS, line=10, rule="R1")
        finding_b = _finding(BLOCKING_FACTS, line=20, rule="R2")
        fence = _fence(
            "BLOCKED", findings=[finding_a, finding_b], opened=_open_entries([finding_a])
        )
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert finding_state_id(PATH, 10, "R1") in run.err
        assert finding_state_id(PATH, 20, "R2") not in run.err

    def test_a_blocked_fence_with_an_empty_findings_open_refuses(self, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        fence = _fence("BLOCKED", findings=[_finding()], opened=[])
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "findings_open is empty or absent" in run.err

    def test_a_blocked_fence_with_no_findings_open_refuses(self, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        fence = _fence("BLOCKED", findings=[_finding()], opened=None)
        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "findings_open is empty or absent" in run.err

    def test_two_recorded_findings_sharing_an_id_pass_when_a_disposition_covers_both(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        first = _finding(message="first problem")
        second = _finding(message="second problem")
        assert finding_state_id(first["file"], first["line"], first["rule_id"]) == finding_state_id(
            second["file"], second["line"], second["rule_id"]
        )
        run = _merge([_comment(1, _fence("BLOCKED", findings=[first, second]))], repo, capsys)

        assert run.code == verb.EXIT_OK, run.err
        assert "false_positive" in run.err

    def test_two_recorded_findings_sharing_an_id_refuse_when_only_one_is_covered(
        self, tmp_path, capsys
    ):
        # by_design clears the ordinary finding and can never clear the floor one.
        repo = _repo(tmp_path, dispositions=_disposition_file(kind="by_design"))
        ordinary = _finding(message="ordinary problem")
        floor = _finding(FLOOR_FACTS, message="floor problem")
        run = _merge([_comment(1, _fence("BLOCKED", findings=[ordinary, floor]))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "security floor forbids" in run.err


class TestNotEvaluated:
    def _legacy(self):
        return build_verdict_block(NAME, "clean", HEAD, 1)

    def test_a_pre_v3_fence_applies_its_asserted_status_with_a_notice(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, self._legacy())], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "was not evaluated" in run.err
        assert "asserted by its poster" in run.err or "status its poster asserted" in run.err

    def test_a_pre_v3_blocking_fence_still_refuses_by_its_status(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, build_verdict_block(NAME, "blocking", HEAD, 1))], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_require_evaluated_refuses_a_pre_v3_fence_naming_it(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"require_evaluated": True})
        run = _merge([_comment(7, self._legacy())], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "comment #7" in run.err
        assert "require_evaluated" in run.err

    def test_require_evaluated_does_not_disturb_an_evaluated_fence(self, tmp_path, capsys):
        repo = _repo(tmp_path, evaluator={"require_evaluated": True})
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], repo, capsys)

        assert run.code == verb.EXIT_OK


class TestReportOnly:
    def test_prints_the_computed_verdict_and_merges_nothing(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge([_comment(1, _fence("CLEAN", findings=[]))], repo, capsys, extra=["--report-only"])

        assert run.code == verb.EXIT_OK
        assert f"verdict {NAME}: fence CLEAN, gate CLEAN" in run.out
        assert "nothing was merged" in run.out
        assert not run.posted

    def test_a_blocked_fence_is_refused_the_same_way(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        run = _merge(
            [_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys, extra=["--report-only"]
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in run.err
        assert not run.posted


class TestPolicySource:
    """Policy is read at the live base: through the host with no --repo-path, from
    the local repository only when one is named."""

    def test_with_no_local_checkout_policy_is_read_through_the_host_at_the_live_base(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)
        reviewed_at = _tip(repo)
        commit_files(repo, {DISPOSITIONS_PATH: _disposition_file()}, "chore: rule on it")
        run = _merge(
            [_comment(1, _fence("BLOCKED", findings=[_finding()], base=reviewed_at))],
            repo, capsys, local=False,
        )

        assert run.code == verb.EXIT_OK, run.err
        assert "false_positive" in run.err
        assert any(_tip(repo) in url for url in run.requests)
        assert any(DISPOSITIONS_PATH in url for url in run.requests)
        assert not any(reviewed_at in url for url in run.requests)

    def test_with_a_repo_path_the_host_is_not_asked_for_policy(self, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        run = _merge([_comment(1, _fence("BLOCKED", findings=[_finding()]))], repo, capsys)

        assert run.code == verb.EXIT_OK, run.err
        assert run.requests == []

    def test_a_stricter_policy_on_the_base_refuses_with_no_local_checkout(self, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        reviewed_at = _tip(repo)
        commit_files(repo, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": []})}, "chore: revoke")
        fence = _fence("CLEAN", findings=[_finding()], base=reviewed_at)
        run = _merge([_comment(1, fence)], repo, capsys, local=False)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "policy base moved" in run.err
        assert "src/a.py:10:R1" in run.err

    def test_the_recorded_policy_base_is_not_read_through_the_host(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        permissive = "d" * 40
        run = _merge(
            [_comment(1, _fence("CLEAN", findings=[], base=permissive))], repo, capsys, local=False
        )

        assert run.code == verb.EXIT_OK, run.err
        assert run.requests and not any(permissive in url for url in run.requests)

    def test_a_host_policy_read_failure_is_degraded_and_refuses(self, tmp_path, capsys):
        repo = _repo(tmp_path, dispositions=_disposition_file())
        run = _merge(
            [_comment(1, _fence("CLEAN", findings=[_finding(HARMLESS_FACTS)]))],
            repo, capsys, local=False, contents_http=500,
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "DEGRADED" in run.err
        assert "HTTP 500" in run.err


class TestRecordedBlocksMatchedIndividually:
    """merge.gate_evaluation._unmatched_recorded_blocks on stand-in findings: the
    cases the evaluator cannot produce through the verb, because a disposition is
    keyed on path and rule alone."""

    @staticmethod
    def _computed(*findings):
        return SimpleNamespace(findings=findings)

    @staticmethod
    def _found(*, disposition, message="m", praise=False):
        return SimpleNamespace(
            file=PATH, line=10, rule_id="R1", message=message, disposition=disposition, praise=praise
        )

    @staticmethod
    def _entry():
        return {"id": finding_state_id(PATH, 10, "R1"), "rule_id": "R1", "head": HEAD}

    _APPLIED = {"path": "src/**", "rule_id": "R1", "kind": "false_positive"}

    def test_a_shared_id_is_matched_only_when_every_finding_with_it_is_covered(self):
        covered = self._found(disposition=self._APPLIED, message="one")
        uncovered = self._found(disposition=None, message="two")

        assert unmatched_recorded_blocks([self._entry()], self._computed(covered, covered)) == []
        left = unmatched_recorded_blocks([self._entry()], self._computed(covered, uncovered))
        assert [entry["id"] for entry, _ in left] == [self._entry()["id"]]

    def test_each_of_two_entries_with_one_id_is_judged(self):
        uncovered = self._found(disposition=None)

        left = unmatched_recorded_blocks(
            [self._entry(), self._entry()], self._computed(uncovered)
        )
        assert len(left) == 2

    def test_a_fingerprint_narrowed_disposition_never_matches(self):
        narrowed = self._found(disposition={**self._APPLIED, "fingerprint": "fp2:" + "a" * 16})

        left = unmatched_recorded_blocks([self._entry()], self._computed(narrowed))
        assert "fingerprint" in left[0][1]

    def test_a_recorded_id_the_gate_does_not_hold_is_unmatched(self):
        left = unmatched_recorded_blocks([self._entry()], self._computed())
        assert len(left) == 1

    def test_praise_sharing_an_id_does_not_count_as_the_recorded_finding(self):
        covered = self._found(disposition=self._APPLIED)
        praise = self._found(disposition=None, praise=True)

        assert unmatched_recorded_blocks([self._entry()], self._computed(covered, praise)) == []
        assert len(unmatched_recorded_blocks([self._entry()], self._computed(praise))) == 1
