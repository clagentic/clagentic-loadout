"""A required role's verdict at a head is the union of every fence the role
posted at that head: all of them must pass, in whatever order they were posted.

Every verb-level case runs the real merge verb with an injected host opener.
Fences are built with the product's own fence builder, the one the post verbs
use."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.errors import (
    VerdictMalformedError,
    VerdictMissingError,
    VerdictStaleError,
)
from clagentic_loadout.merge.fence_union import collect_head_fences
from clagentic_loadout.merge.verdict import (
    build_verdict_block,
    fenced_comment_ids_newest_first,
    read_reviewer_verdict,
)
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.transport import provider_config
from tests._gate_repo import commit_files, git, init_gate_repo
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


def _github_opener(comments):
    """Answers only GitHub-shaped URLs, with no CI evidence at the head."""
    stamped = [{**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments]

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


class TestEveryFenceAtTheHeadMustPass:
    def test_blocked_then_clean_refuses(self, capsys):
        code, err, _, _ = _run([_comment(1, "blocking"), _comment(2, "clean")], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #1" in err

    def test_clean_then_blocked_refuses(self, capsys):
        code, err, _, _ = _run([_comment(1, "clean"), _comment(2, "blocking")], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #2" in err

    def test_two_clean_fences_pass_and_each_is_named(self, capsys):
        code, err, _, _ = _run([_comment(1, "clean"), _comment(2, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert "posted 2 fences" in err and "#2, #1" in err
        assert "comment_id=1" in err and "comment_id=2" in err

    def test_one_blocking_among_several_clean_refuses(self, capsys):
        comments = [
            _comment(1, "clean"),
            _comment(2, "blocking"),
            _comment(3, "clean"),
            _comment(4, "clean"),
        ]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #2" in err

    def test_timestamps_not_list_order_decide_which_fence_is_newer(self, capsys):
        later_clean = {**_comment(7, "clean"), "created_at": "2026-01-02T00:00:00Z"}
        earlier_blocking = {**_comment(9, "blocking"), "created_at": "2026-01-01T00:00:00Z"}

        code, err, _, _ = _run([later_clean, earlier_blocking], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #9" in err

    def test_a_later_prose_comment_does_not_hide_a_blocked_fence(self, capsys):
        prose = {"id": 3, "user": {"login": LOGIN}, "body": "looks fine to me"}

        code, err, _, _ = _run([_comment(1, "blocking"), _comment(2, "clean"), prose], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED


class TestFencesForAnotherHead:
    def test_an_old_head_fence_between_head_fences_is_ignored(self, capsys):
        comments = [
            _comment(1, "clean"),
            _comment(2, "blocking", head=OLD_HEAD),
            _comment(3, "clean"),
        ]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_OK, err
        assert "comment_id=3" in err and "comment_id=1" in err
        assert "comment_id=2" not in err

    def test_an_old_head_fence_does_not_end_the_evaluation(self, capsys):
        comments = [
            _comment(1, "blocking"),
            _comment(2, "clean", head=OLD_HEAD),
            _comment(3, "clean"),
        ]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #1" in err

    def test_a_clean_old_head_fence_never_rescues_a_blocking_one(self, capsys):
        comments = [_comment(1, "clean", head=OLD_HEAD), _comment(2, "blocking")]

        code, err, _, _ = _run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #2" in err

    def test_a_newest_fence_for_an_old_head_is_a_stale_verdict_as_before(self, capsys):
        code, err, _, _ = _run([_comment(1, "clean"), _comment(2, "clean", head=OLD_HEAD)], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "STALE GATE DATA" in err


class TestOneFencePerRoleIsAsBefore:
    def test_a_passing_fence_prints_the_single_fence_lines_only(self, capsys):
        code, err, _, _ = _run([_comment(1, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert (
            f"merge: {NAME!r} verdict PASSED -- review_status='clean', "
            f"head_sha={HEAD!r}, comment_id=1"
        ) in err.splitlines()
        assert "fences at head" not in err
        assert not [
            line
            for line in err.splitlines()
            if line.startswith(("merge: NOTICE -- comment #", "merge: WARNING -- comment #"))
        ]
        assert "scanner outcomes" not in err
        assert "judgment alone" not in err

    def test_report_only_output_is_the_single_fence_output(self, capsys):
        code, err, out, requests = _run([_comment(1, "clean")], capsys, extra=["--report-only"])

        assert code == verb.EXIT_OK, err
        assert out.splitlines()[:1] == [
            f"merge: report-only -- verdict {NAME}: not evaluated (status asserted by the reviewer)"
        ]
        assert [r for r in requests if r[0] != "GET"] == []

    def test_a_lone_blocking_fence_refuses_naming_itself_only(self, capsys):
        code, err, _, _ = _run([_comment(1, "blocking")], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #1" in err
        assert "fences at head" not in err

    def test_a_single_fence_refused_by_a_later_check_is_evaluated_once_and_unlabelled(
        self, capsys, monkeypatch
    ):
        computed = []
        real = verb.evaluate_at_gate

        def evaluate(candidate, *args, **kwargs):
            result = real(candidate, *args, **kwargs)
            object.__setattr__(result, "notices", ("recomputed at the live base",))
            computed.append(candidate.comment_id)
            return result

        monkeypatch.setattr(verb, "evaluate_at_gate", evaluate)

        # The evaluation is computed and its notices printed first; the
        # recorded status refuses the fence in the check that follows.
        code, err, _, _ = _run([_comment(1, "blocking")], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert computed == [1]
        assert "merge: NOTICE -- recomputed at the live base" in err.splitlines()

    def test_a_malformed_older_fence_with_no_state_is_ignored_as_before(self, capsys):
        broken = {"id": 1, "user": {"login": LOGIN}, "body": "```review-result\n{not json\n```"}

        code, err, _, _ = _run([broken, _comment(2, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert "earlier comment #1" in err and "is ignored" in err
        assert "fences at head" not in err

    def test_a_newer_unreadable_post_still_refuses(self, capsys):
        broken = {"id": 2, "user": {"login": LOGIN}, "body": "```review-result\n{not json\n```"}

        code, err, _, _ = _run([_comment(1, "clean"), broken], capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "invalid JSON" in err


class TestNoticesNameTheirComment:
    def test_each_fences_evaluation_notice_names_its_comment(self, capsys, monkeypatch):
        real = verb.evaluate_at_gate

        def noisy(candidate, *args, **kwargs):
            result = real(candidate, *args, **kwargs)
            object.__setattr__(result, "notices", ("recomputed at the live base",))
            return result

        monkeypatch.setattr(verb, "evaluate_at_gate", noisy)

        code, err, _, _ = _run([_comment(1, "clean"), _comment(2, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert "NOTICE -- comment #2: recomputed at the live base" in err
        assert "NOTICE -- comment #1: recomputed at the live base" in err

    def test_an_ignored_earlier_comment_is_warned_about_once(self, capsys):
        broken = {"id": 1, "user": {"login": LOGIN}, "body": "```review-result\n{not json\n```"}

        code, err, _, _ = _run([broken, _comment(2, "clean"), _comment(3, "clean")], capsys)

        assert code == verb.EXIT_OK, err
        assert err.count("earlier comment #1") == 1


class TestHostsAndModes:
    @pytest.mark.parametrize("report_only", [False, True])
    def test_a_blocked_and_a_clean_fence_refuse_on_forgejo(self, capsys, report_only):
        comments = [_comment(1, "clean"), _comment(2, "blocking")]

        code, err, _, requests = _run(
            comments, capsys, extra=["--report-only"] if report_only else []
        )

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert [r for r in requests if r[0] != "GET"] == []

    @pytest.mark.parametrize("report_only", [False, True])
    def test_a_blocked_and_a_clean_fence_refuse_on_github(self, capsys, report_only):
        comments = [_comment(1, "blocking"), _comment(2, "clean")]

        code, err, _, requests = _run(
            comments,
            capsys,
            platform="github",
            opener=_github_opener(comments),
            extra=["--report-only"] if report_only else [],
        )

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #1" in err
        assert [r for r in requests if r[0] != "GET"] == []

    def test_two_clean_fences_pass_in_report_only_on_forgejo(self, capsys):
        code, err, out, requests = _run(
            [_comment(1, "clean"), _comment(2, "clean")], capsys, extra=["--report-only"]
        )

        assert code == verb.EXIT_OK, err
        assert "would be merged" in out
        assert out.count("report-only -- verdict") == 2
        assert [r for r in requests if r[0] != "GET"] == []

    def test_two_clean_fences_pass_in_report_only_on_github(self, capsys):
        comments = [_comment(1, "clean"), _comment(2, "clean")]

        code, err, out, _ = _run(
            comments,
            capsys,
            platform="github",
            opener=_github_opener(comments),
            extra=["--report-only"],
        )

        assert code == verb.EXIT_OK, err
        assert "would be merged" in out


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

    def test_one_fence_per_role_passes(self, capsys):
        comments = [
            _comment(1, "clean"),
            _comment(2, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
        ]

        code, err = self._argv_run(comments, capsys)

        assert code == verb.EXIT_OK, err

    def test_another_roles_clean_fence_does_not_cover_a_blocked_fence(self, capsys):
        comments = [
            _comment(1, "blocking"),
            _comment(2, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
        ]

        code, err = self._argv_run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_second_fence_of_one_role_is_judged_for_that_role_only(self, capsys):
        comments = [
            _comment(1, "clean"),
            _comment(2, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
            _comment(3, "blocking", login=OTHER_LOGIN, name=OTHER_NAME),
        ]

        code, err = self._argv_run(comments, capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #3" in err


class TestEvaluatedFences:
    """Version 3 fences, whose outcome the gate recomputes at the live base."""

    _FINDING = {
        "file": "src/a.py", "line": 10, "rule_id": "R1", "message": "wrong",
        "facts": facts(confidentiality="full", integrity="full", availability="full"),
    }

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

    @staticmethod
    def _repo(tmp_path, *, rule=None):
        repo = init_gate_repo(tmp_path / "repo", tracked_gate={"required_reviewer_roles": []})
        if rule is not None:
            entry = {
                "path": "src/**", "rule_id": rule, "kind": "false_positive",
                "rationale": "ruled by hand", "by": "a-maintainer", "at": "2026-01-01",
                "expires": "2999-01-01",
            }
            commit_files(
                repo.path, {DISPOSITIONS_PATH: yaml.safe_dump({"dispositions": [entry]})}, "rule"
            )
        return repo

    def _merge(self, repo, comments, capsys, extra=()):
        info = repo.pr_info()
        info["base"] = {"ref": "main", "sha": git(repo.path, "rev-parse", "main"),
                        "repo": {"private": True}}
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

    def _comments(self, repo, first, second):
        return [
            {"id": 1, "user": {"login": LOGIN}, "body": first(repo.head_sha)},
            {"id": 2, "user": {"login": LOGIN}, "body": second(repo.head_sha)},
        ]

    def _clean(self):
        return lambda head: self._fence("CLEAN", head)

    def _blocked(self):
        return lambda head: self._fence("BLOCKED", head, findings=[self._FINDING])

    @pytest.mark.parametrize("blocked_first", [True, False])
    def test_a_kept_finding_blocks_whichever_fence_posted_first(
        self, tmp_path, capsys, blocked_first
    ):
        repo = self._repo(tmp_path)
        pair = (self._blocked(), self._clean()) if blocked_first else (self._clean(), self._blocked())

        code, err = self._merge(repo, self._comments(repo, *pair), capsys)

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in err

    @pytest.mark.parametrize("blocked_first", [True, False])
    def test_a_base_branch_disposition_covering_the_finding_lets_it_pass(
        self, tmp_path, capsys, blocked_first
    ):
        repo = self._repo(tmp_path, rule="R1")
        pair = (self._blocked(), self._clean()) if blocked_first else (self._clean(), self._blocked())

        code, err = self._merge(repo, self._comments(repo, *pair), capsys)

        assert code == verb.EXIT_OK, err
        assert "false_positive" in err
        assert "comment_id=1" in err and "comment_id=2" in err

    def test_a_disposition_for_another_rule_does_not_cover_the_finding(self, tmp_path, capsys):
        repo = self._repo(tmp_path, rule="R2")

        code, err = self._merge(
            repo, self._comments(repo, self._clean(), self._blocked()), capsys
        )

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in err

    def test_report_only_refuses_the_same_way(self, tmp_path, capsys):
        repo = self._repo(tmp_path)

        code, err = self._merge(
            repo, self._comments(repo, self._clean(), self._blocked()), capsys,
            extra=["--report-only"],
        )

        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in err


class TestReaderExclusion:
    def _read(self, comments, **kwargs):
        stamped = [{**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments]
        return read_reviewer_verdict(
            stamped, LOGIN, HEAD, 1, OWNER, REPO, expected_reviewer_name=NAME, **kwargs
        )

    def test_default_selects_the_newest_fence(self):
        assert self._read([_comment(1, "clean"), _comment(2, "blocking")]).comment_id == 2

    def test_excluded_comments_are_passed_over(self):
        verdict = self._read(
            [_comment(1, "clean"), _comment(2, "blocking")], exclude_comment_ids=(2,)
        )

        assert verdict.comment_id == 1
        assert verdict.review_status == "clean"

    def test_excluding_every_fence_reads_as_no_verdict(self):
        with pytest.raises(VerdictMissingError):
            self._read([_comment(1, "clean")], exclude_comment_ids=(1,))

    def test_fenced_ids_are_newest_first_and_skip_prose(self):
        comments = [
            _comment(1, "clean"),
            {"id": 2, "user": {"login": LOGIN}, "body": "prose only"},
            _comment(3, "blocking"),
            _comment(4, "clean", login=OTHER_LOGIN, name=OTHER_NAME),
        ]
        stamped = [{**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments]

        assert fenced_comment_ids_newest_first(stamped, LOGIN, 1, OWNER, REPO) == [3, 1]


class TestCollectHeadFences:
    class _Verdict:
        def __init__(self, comment_id):
            self.comment_id = comment_id

    def _reader(self, ids, unreadable=()):
        def read(excluded):
            remaining = [i for i in ids if i not in excluded]
            if not remaining:
                raise VerdictMissingError("none left")
            if remaining[0] in unreadable:
                raise unreadable[remaining[0]]
            return self._Verdict(remaining[0])

        return read

    def test_every_readable_fence_is_returned_newest_first(self):
        fences = collect_head_fences(self._reader([4, 3, 2, 1]), lambda: [4, 3, 2, 1])

        assert [f.comment_id for f in fences] == [4, 3, 2, 1]

    def test_a_fence_that_is_not_for_the_head_is_skipped_without_ending_the_walk(self):
        reader = self._reader([4, 3, 2, 1], unreadable={3: VerdictStaleError("other head")})

        fences = collect_head_fences(reader, lambda: [4, 3, 2, 1])

        assert [f.comment_id for f in fences] == [4, 2, 1]

    def test_an_unreadable_older_fence_is_skipped(self):
        reader = self._reader([3, 2, 1], unreadable={2: VerdictMalformedError("bad")})

        assert [f.comment_id for f in collect_head_fences(reader, lambda: [3, 2, 1])] == [3, 1]

    def test_a_failure_reading_the_newest_fence_is_raised_as_before(self):
        reader = self._reader([2, 1], unreadable={2: VerdictStaleError("other head")})

        with pytest.raises(VerdictStaleError):
            collect_head_fences(reader, lambda: [2, 1])

    def test_no_fence_at_all_is_raised_as_before(self):
        with pytest.raises(VerdictMissingError):
            collect_head_fences(self._reader([]), lambda: [])

    def test_a_reader_that_ignores_the_exclusion_cannot_duplicate_or_loop(self):
        fences = collect_head_fences(lambda excluded: self._Verdict(2), lambda: [2, 1])

        assert [f.comment_id for f in fences] == [2]
