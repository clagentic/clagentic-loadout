"""A passed gate fence at a head is not superseded by a later post by the same
role at that head.

Every verb-level case runs the real merge verb with an injected host opener.
Fences are built with the product's own fence builder, the one the post verbs
use. A control for each shape proves the refusal is still reached when no fence
at the head passes."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.errors import VerdictBlockingError, VerdictMissingError
from clagentic_loadout.merge.fence_precedence import select_passed_fence
from clagentic_loadout.merge.verdict import build_verdict_block, read_reviewer_verdict
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.transport import provider_config
from tests._gate_repo import init_gate_repo
from tests._review_evaluate_support import facts
from tests._support.fakes import json_resp, paged
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HEAD = "a" * 40
OLD_HEAD = "b" * 40
LOGIN = "reviewer-login"
NAME = "some-reviewer"
OTHER_LOGIN = "security-login"
OTHER_NAME = "some-security"
OWNER, REPO = "some-owner", "some-repo"

_REVIEW_ARG = {"--required-reviewer": f"{NAME}:{LOGIN}"}


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _comment(comment_id, status, *, head=HEAD, login=LOGIN, name=NAME):
    return {
        "id": comment_id,
        "user": {"login": login},
        "body": build_verdict_block(name, status, head, 1),
    }


def _run(comments, capsys, *, extra=(), argv_overrides=None, opener=None, platform="forgejo"):
    requests: list[tuple[str, str]] = []
    inner = opener or make_opener(
        pr_info={"head": {"sha": HEAD}, "title": "feat: x"}, comments=comments
    )

    def recording(req, timeout=15):
        requests.append((req.get_method(), req.full_url))
        return inner(req, timeout)

    argv = base_args(**{"--platform": platform, **(argv_overrides or _REVIEW_ARG)}) + list(extra)
    capsys.readouterr()
    code = verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=recording,
    )
    captured = capsys.readouterr()
    return code, captured.err, captured.out, requests


class TestPassedFenceIsNotSuperseded:
    def test_a_blocking_post_after_a_clean_fence_at_the_same_head_still_merges(self, capsys):
        comments = [_comment(1, "clean"), _comment(2, "blocking")]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_OK, err
        assert "comment_id=1" in err
        assert "comment #2" in err and "does not supersede" in err

    def test_report_only_reaches_the_same_result_and_writes_nothing(self, capsys):
        comments = [_comment(1, "clean"), _comment(2, "blocking")]

        code, err, out, requests = _run(comments, capsys, extra=["--report-only"])

        assert code == verb.EXIT_OK, err
        assert "would be merged" in out
        assert [r for r in requests if r[0] != "GET"] == []

    def test_several_same_role_fences_settle_on_the_newest_one_that_passes(self, capsys):
        comments = [
            _comment(1, "blocking"),
            _comment(2, "clean"),
            _comment(3, "blocking"),
            _comment(4, "blocking"),
        ]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_OK, err
        assert "comment_id=2" in err
        assert "comment #4" in err and "comment #3" in err

    def test_timestamps_not_list_order_decide_which_fence_is_newer(self, capsys):
        later_blocking = {**_comment(7, "blocking"), "created_at": "2026-01-02T00:00:00Z"}
        earlier_clean = {**_comment(9, "clean"), "created_at": "2026-01-01T00:00:00Z"}

        code, err, _, _ = _run([later_blocking, earlier_clean], capsys)

        assert code == verb.EXIT_OK, err
        assert "comment_id=9" in err

    def test_the_same_result_on_github(self, capsys):
        comments = [_comment(1, "clean"), _comment(2, "blocking")]

        code, err, _, _ = _run(
            comments, capsys, platform="github", opener=_github_opener(comments),
            extra=["--report-only"],
        )

        assert code == verb.EXIT_OK, err
        assert "comment_id=1" in err and "does not supersede" in err


class TestBehaviourWithoutAPassedFenceIsUnchanged:
    def test_a_lone_blocking_fence_refuses(self, capsys):
        code, err, _, _ = _run([_comment(1, "blocking")], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "comment #1" in err

    def test_several_blocking_fences_refuse_naming_the_newest(self, capsys):
        code, err, _, _ = _run(
            [_comment(1, "blocking"), _comment(2, "blocking"), _comment(3, "blocking")], capsys
        )

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #3" in err
        assert "in verdict comment #2" not in err
        assert "in verdict comment #1" not in err

    def test_a_clean_fence_after_a_blocking_one_merges_without_a_notice(self, capsys):
        code, err, _, _ = _run([_comment(1, "blocking"), _comment(2, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert "does not supersede" not in err

    def test_a_clean_fence_for_an_older_head_never_rescues_a_blocking_fence_at_this_head(
        self, capsys
    ):
        comments = [_comment(1, "clean", head=OLD_HEAD), _comment(2, "blocking")]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "comment #2" in err

    def test_a_newer_unreadable_post_still_refuses(self, capsys):
        broken = {
            "id": 2,
            "user": {"login": LOGIN},
            "body": "```review-result\n{not json\n```",
        }

        code, err, _, _ = _run([_comment(1, "clean"), broken], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "invalid JSON" in err


class TestRolesAreIndependent:
    def _argv_run(self, comments, capsys):
        inner = make_opener(pr_info={"head": {"sha": HEAD}, "title": "feat: x"}, comments=comments)
        argv = base_args(**_REVIEW_ARG) + ["--required-reviewer", f"{OTHER_NAME}:{OTHER_LOGIN}"]
        capsys.readouterr()
        code = verb.main(
            argv,
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=inner,
        )
        return code, capsys.readouterr().err

    def test_each_role_settles_on_its_own_fences(self, capsys):
        comments = [
            _comment(1, "clean"),
            _comment(2, "blocking"),
            _comment(3, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
        ]

        code, err = self._argv_run(comments, capsys)

        assert code == verb.EXIT_OK, err

    def test_another_roles_clean_fence_does_not_rescue_a_role_with_none(self, capsys):
        comments = [
            _comment(1, "blocking"),
            _comment(2, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
        ]

        code, err = self._argv_run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "BLOCKED merge" in err


class TestEvaluatedFences:
    """Version 3 fences, whose outcome the gate recomputes at the live base."""

    @staticmethod
    def _fence(outcome, head, *, findings=()):
        state = {
            "verdict_outcome": outcome,
            "verdict_reasons": [] if outcome == "CLEAN" else ["recorded"],
            "evaluator_version": EVALUATOR_VERSION,
            "evaluated_findings": list(findings),
        }
        if outcome == "BLOCKED":
            state["findings_open"] = [
                {
                    "id": finding_state_id(f["file"], f["line"], f["rule_id"]),
                    "rule_id": f["rule_id"],
                    "head": head,
                }
                for f in findings
            ]
        status = "clean" if outcome == "CLEAN" else "blocking"
        return build_verdict_block(NAME, status, head, 1, findings_state=state)

    def _merge(self, repo, comments, capsys, extra=()):
        info = repo.pr_info()
        info["base"]["repo"] = {"private": True}
        inner = make_opener(pr_info=info, comments=comments)
        argv = base_args(**{**_REVIEW_ARG, "--repo-path": str(repo.path)}) + list(extra)
        capsys.readouterr()
        code = verb.main(
            argv,
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=inner,
        )
        return code, capsys.readouterr().err

    def test_a_blocked_real_run_after_a_clean_gate_fence_keeps_the_gate_result(
        self, tmp_path, capsys
    ):
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"required_reviewer_roles": []})
        finding = {
            "file": "src/a.py", "line": 10, "rule_id": "R1", "message": "wrong",
            "facts": facts(confidentiality="full", integrity="full", availability="full"),
        }
        comments = [
            {"id": 1, "user": {"login": LOGIN}, "body": self._fence("CLEAN", repo.head_sha)},
            {
                "id": 2,
                "user": {"login": LOGIN},
                "body": self._fence("BLOCKED", repo.head_sha, findings=[finding]),
            },
        ]

        code, err = self._merge(repo, comments, capsys)

        assert code == verb.EXIT_OK, err
        assert "comment_id=1" in err

    def test_control_the_blocked_fence_alone_refuses(self, tmp_path, capsys):
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"required_reviewer_roles": []})
        finding = {
            "file": "src/a.py", "line": 10, "rule_id": "R1", "message": "wrong",
            "facts": facts(confidentiality="full", integrity="full", availability="full"),
        }
        comments = [
            {
                "id": 2,
                "user": {"login": LOGIN},
                "body": self._fence("BLOCKED", repo.head_sha, findings=[finding]),
            }
        ]

        code, err = self._merge(repo, comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in err


class TestReaderExclusion:
    def _read(self, comments, **kwargs):
        stamped = [
            {**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments
        ]
        return read_reviewer_verdict(
            stamped, LOGIN, HEAD, 1, OWNER, REPO, expected_reviewer_name=NAME, **kwargs
        )

    def test_default_selects_the_newest_fence(self):
        verdict = self._read([_comment(1, "clean"), _comment(2, "blocking")])

        assert verdict.comment_id == 2

    def test_excluded_comments_are_passed_over(self):
        verdict = self._read(
            [_comment(1, "clean"), _comment(2, "blocking")], exclude_comment_ids=(2,)
        )

        assert verdict.comment_id == 1
        assert verdict.review_status == "clean"

    def test_excluding_every_fence_reads_as_no_verdict(self):
        with pytest.raises(VerdictMissingError):
            self._read([_comment(1, "clean")], exclude_comment_ids=(1,))


class TestSelectPassedFence:
    class _Verdict:
        def __init__(self, comment_id):
            self.comment_id = comment_id

    def _reader(self, ids):
        def read(excluded):
            remaining = [i for i in ids if i not in excluded]
            if not remaining:
                raise VerdictMissingError("none left")
            return self._Verdict(remaining[0])

        return read

    def test_the_first_pass_is_returned_with_what_was_passed_over(self):
        def check(verdict):
            if verdict.comment_id != 2:
                raise VerdictBlockingError(f"refused {verdict.comment_id}")
            return "passed"

        chosen = select_passed_fence(self._reader([4, 3, 2, 1]), check, (VerdictBlockingError,))

        assert chosen.verdict.comment_id == 2
        assert chosen.result == "passed"
        assert [cid for cid, _ in chosen.passed_over] == [4, 3]

    def test_when_none_passes_the_newest_refusal_is_raised(self):
        def check(verdict):
            raise VerdictBlockingError(f"refused {verdict.comment_id}")

        with pytest.raises(VerdictBlockingError, match="refused 3"):
            select_passed_fence(self._reader([3, 2, 1]), check, (VerdictBlockingError,))

    def test_an_error_outside_the_refusals_propagates_at_once(self):
        def check(verdict):
            raise RuntimeError("not a refusal")

        with pytest.raises(RuntimeError):
            select_passed_fence(self._reader([2, 1]), check, (VerdictBlockingError,))

    def test_a_first_read_failure_is_raised_as_it_always_was(self):
        with pytest.raises(VerdictMissingError):
            select_passed_fence(self._reader([]), lambda v: "x", (VerdictBlockingError,))

    def test_a_reader_that_ignores_the_exclusion_cannot_loop(self):
        def stubborn(excluded):
            return self._Verdict(1)

        def check(verdict):
            raise VerdictBlockingError("refused")

        with pytest.raises(VerdictBlockingError, match="refused"):
            select_passed_fence(stubborn, check, (VerdictBlockingError,))


def _github_opener(comments):
    """Answers only GitHub-shaped URLs, with no CI evidence at the head."""
    stamped = [
        {**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments
    ]

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
        assert "api.github.com" in url, f"expected GitHub-shaped URL, got: {url}"
        if method == "GET" and url.endswith("/files"):
            return json_resp(200, [{"filename": "a.py"}])
        if method == "GET" and url.split("?")[0].endswith("/comments"):
            return json_resp(200, paged(url, stamped))
        if method == "GET" and url.endswith("/status"):
            return json_resp(200, {"state": "", "statuses": []})
        if method == "GET" and url.endswith("/check-runs"):
            return json_resp(200, {"total_count": 0, "check_runs": []})
        if method == "GET" and "/compare/" in url:
            return json_resp(200, {"commits": [], "ahead_by": 0})
        if method == "GET" and "/pulls/" in url:
            return json_resp(200, {"head": {"sha": HEAD}, "title": "feat: x"})
        raise AssertionError(f"unexpected call: {method} {url}")

    return opener
