"""Merge-gate tests for findings state carried in the verdict fence, scanner
outcomes, the stale-verdict refusal wording, and the repo reviewer floor.

Every fixture runs through the real verb with an injected opener; nothing
touches a network or a real git tree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.attestation import build_attestation_body
from clagentic_loadout.merge.errors import VerdictStaleAfterCommentsError, VerdictStaleError
from clagentic_loadout.merge.repo_gate_runtime import load_repo_gate_at_base
from clagentic_loadout.merge.verdict import build_verdict_block, read_reviewer_verdict
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.transport import provider_config
from tests._gate_repo import git, init_gate_repo, write_deployment_config
from tests.test_merge_verb import (
    _AllowingAuthorityProvider,
    _RecordingTokenProvider,
    _base_args,
    _make_opener,
)

HEAD_A = "a" * 40
HEAD_B = "b" * 40
LOGIN = "reviewer-login"
NAME = "some-reviewer"


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _fence(status, head, state=None, *, name=NAME):
    return build_verdict_block(name, status, head, 1, findings_state=state)


def _comment(comment_id, body, *, login=LOGIN):
    return {"id": comment_id, "user": {"login": login}, "body": body}


def _merge(comments, *, head=HEAD_B, extra_args=None, repo_path=None, capsys=None):
    argv = _base_args(
        **{"--required-reviewer": f"{NAME}:{LOGIN}", **({"--repo-path": str(repo_path)} if repo_path else {})}
    )
    argv += extra_args or []
    code = verb.main(
        argv,
        token_provider=_RecordingTokenProvider(),
        authority_provider=_AllowingAuthorityProvider(),
        opener=_make_opener(pr_info=_pr_info(head, repo_path), comments=comments),
    )
    return code, (capsys.readouterr().err if capsys else "")


def _write_config(repo_path, merge_section):
    """Commit *merge_section* as the tracked gate at the base commit of a real
    repo at *repo_path*; the gitignored deployment file carries only
    machine-local keys."""
    return init_gate_repo(repo_path, tracked_gate=merge_section)


def _pr_info(head, repo_path=None):
    """PR payload whose base commit is *repo_path*'s main, when it is a repo."""
    info = {"head": {"sha": head}, "title": "feat: x"}
    if repo_path is not None and (Path(repo_path) / ".git").exists():
        info["base"] = {"ref": "main", "sha": git(Path(repo_path), "rev-parse", "main")}
    return info


F1_OPEN = {"findings_open": [{"id": "F1", "rule_id": "R1", "head": HEAD_A}]}
F1_CLEARED = {"cleared_claims": [{"id": "F1", "head": HEAD_B, "evidence": "guard added in b"}]}


class TestPriorFindingsMustBeResolved:
    def test_clean_at_a_later_head_without_clearing_is_refused_naming_the_finding(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("clean", HEAD_B)),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "F1" in err

    def test_clean_at_a_later_head_clearing_it_with_evidence_merges(self):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("clean", HEAD_B, F1_CLEARED)),
        ]
        code, _ = _merge(comments)
        assert code == verb.EXIT_OK

    def test_a_claim_naming_another_finding_does_not_clear_it(self, capsys):
        other = {"cleared_claims": [{"id": "F2", "head": HEAD_B, "evidence": "unrelated"}]}
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("clean", HEAD_B, other)),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "F1" in err

    def test_a_blocking_verdict_that_re_raises_it_still_refuses_as_blocking(self):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("blocking", HEAD_B, {"findings_open": [{"id": "F1", "rule_id": "R1", "head": HEAD_B}]})),
        ]
        code, _ = _merge(comments)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_history_with_no_findings_state_is_never_an_error(self):
        comments = [
            _comment(1, _fence("blocking", HEAD_A)),
            _comment(2, _fence("clean", HEAD_B)),
        ]
        code, _ = _merge(comments)
        assert code == verb.EXIT_OK

    def test_another_reviewers_open_finding_is_not_carried_over(self):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN, name="other-reviewer")),
            _comment(2, _fence("clean", HEAD_B)),
        ]
        code, _ = _merge(comments)
        assert code == verb.EXIT_OK


    def test_an_earlier_fence_with_a_malformed_head_is_unreadable_evidence_and_refuses(self, capsys):
        malformed = (
            '\n```review-result\n{"reviewer": "%s", "review_status": "blocking", "head_sha": "abc", '
            '"pr_number": 1, "fence_schema_version": 2, "findings_open": '
            '[{"id": "F1", "rule_id": "R1", "head": "abc"}]}\n```\n' % NAME
        )
        comments = [_comment(1, malformed), _comment(2, _fence("clean", HEAD_B, F1_CLEARED))]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "malformed head_sha" in err
        assert "#1" in err


class TestRequiredScanners:
    def _config(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": [], "required_scanners": {NAME: ["alpha"]}})
        return tmp_path

    @staticmethod
    def _scanners(status, reason=None):
        entry = {"scanner": "alpha", "status": status}
        if reason:
            entry["reason"] = reason
        return {"scanners_run": [entry]}

    def test_a_failed_required_scanner_refuses_naming_it(self, tmp_path, capsys):
        comments = [_comment(1, _fence("clean", HEAD_B, self._scanners("failed", "timed out")))]
        code, err = _merge(comments, repo_path=self._config(tmp_path), capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "'alpha'" in err

    def test_not_applicable_with_a_reason_proceeds(self, tmp_path):
        state = self._scanners("not_applicable", "no changed files in scope")
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=self._config(tmp_path))
        assert code == verb.EXIT_OK

    def test_a_clean_verdict_with_no_scanner_record_refuses_when_scanners_are_required(
        self, tmp_path, capsys
    ):
        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B))], repo_path=self._config(tmp_path), capsys=capsys
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "records no scanner outcomes" in err
        assert "'alpha'" in err
        assert "--ignore-repo-gate" in err

    def test_a_required_scanner_absent_from_a_non_empty_record_refuses_naming_it(
        self, tmp_path, capsys
    ):
        _write_config(
            tmp_path, {"required_reviewer_roles": [], "required_scanners": {NAME: ["alpha", "beta"]}}
        )
        state = self._scanners("ran")
        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B, state))], repo_path=tmp_path, capsys=capsys
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "'beta'" in err
        assert "'alpha'" not in err

    def test_every_required_scanner_recorded_beside_an_extra_one_proceeds(self, tmp_path):
        state = {
            "scanners_run": [
                {"scanner": "alpha", "status": "ran"},
                {"scanner": "extra", "status": "ran"},
            ]
        }
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=self._config(tmp_path))
        assert code == verb.EXIT_OK

    def test_a_clean_verdict_with_no_scanner_record_only_warns_when_none_are_required(
        self, tmp_path, capsys
    ):
        _write_config(tmp_path, {"required_reviewer_roles": []})
        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys
        )
        assert code == verb.EXIT_OK
        assert "records no scanner outcomes" in err

    def test_ignore_repo_gate_also_lifts_the_scanner_requirement(self, tmp_path, capsys):
        config = self._config(tmp_path)
        for state in (None, self._scanners("failed", "timed out")):
            code, err = _merge(
                [_comment(1, _fence("clean", HEAD_B, state))],
                repo_path=config,
                extra_args=["--ignore-repo-gate"],
                capsys=capsys,
            )
            assert code == verb.EXIT_OK
            assert "IGNORED via --ignore-repo-gate" in err

    def test_a_required_scanner_is_not_ignored_without_the_flag(self, tmp_path):
        state = self._scanners("failed", "timed out")
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=self._config(tmp_path))
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_padded_role_key_still_gates_the_role(self, tmp_path):
        _write_config(
            tmp_path, {"required_reviewer_roles": [], "required_scanners": {f"  {NAME} ": [" alpha "]}}
        )
        state = self._scanners("failed", "timed out")
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=tmp_path)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_role_keys_colliding_after_trimming_are_a_malformed_declaration(self, tmp_path, capsys):
        _write_config(
            tmp_path,
            {
                "required_reviewer_roles": [],
                "required_scanners": {NAME: ["alpha"], f" {NAME}": ["beta"]},
            },
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "more than once" in err
        assert "required_scanners NOT ENFORCED" in err

    def test_a_duplicate_scanner_entry_in_a_posted_fence_refuses(self, tmp_path):
        # Hand-built JSON: the emit side would refuse to construct this.
        fence = (
            '\n```review-result\n{"reviewer": "%s", "review_status": "clean", "head_sha": "%s", '
            '"pr_number": 1, "fence_schema_version": 2, "scanners_run": '
            '[{"scanner": "alpha", "status": "failed", "reason": "x"}, '
            '{"scanner": "alpha", "status": "ran"}]}\n```\n' % (NAME, HEAD_B)
        )
        code, _ = _merge([_comment(1, fence)], repo_path=self._config(tmp_path))
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_failed_scanner_is_ignored_when_no_scanner_is_required(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": []})
        state = self._scanners("failed", "timed out")
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=tmp_path)
        assert code == verb.EXIT_OK

    def test_a_malformed_scanner_declaration_warns_and_does_not_block(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": [], "required_scanners": ["alpha"]})
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "required_scanners NOT ENFORCED" in err


class TestScannerRolesMustBeReachable:
    def test_scanners_declared_for_a_role_nobody_requires_refuse_naming_it(self, tmp_path, capsys):
        _write_config(
            tmp_path, {"required_reviewer_roles": [], "required_scanners": {"orphan": ["alpha"]}}
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_USAGE
        assert "'orphan'" in err
        assert "--ignore-repo-gate" in err

    def test_ignore_repo_gate_lifts_the_unreachable_refusal(self, tmp_path):
        _write_config(
            tmp_path, {"required_reviewer_roles": [], "required_scanners": {"orphan": ["alpha"]}}
        )
        code, _ = _merge(
            [_comment(1, _fence("clean", HEAD_B))],
            repo_path=tmp_path,
            extra_args=["--ignore-repo-gate"],
        )
        assert code == verb.EXIT_OK

    def test_a_role_required_only_by_the_floor_is_reachable(self, tmp_path):
        _write_config(
            tmp_path,
            {"required_reviewer_roles": [NAME], "required_scanners": {NAME: ["alpha"]}},
        )
        state = {"scanners_run": [{"scanner": "alpha", "status": "ran"}]}
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B, state))], repo_path=tmp_path)
        assert code == verb.EXIT_OK

    def test_an_empty_scanner_list_for_an_unrequired_role_declares_nothing(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": [], "required_scanners": {"orphan": []}})
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path)
        assert code == verb.EXIT_OK


class TestSupersedesResolution:
    def test_a_supersedes_naming_an_earlier_verdict_comment_does_not_warn(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A)),
            _comment(2, _fence("clean", HEAD_B, {"supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes" not in err

    def test_a_supersedes_naming_no_earlier_verdict_comment_is_surfaced(self, capsys):
        comments = [_comment(2, _fence("clean", HEAD_B, {"supersedes": 99}))]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes comment #99" in err

    def test_a_supersedes_naming_another_reviewers_fence_is_surfaced(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, name="someone-else")),
            _comment(2, _fence("clean", HEAD_B, {"supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes comment #1" in err

    def test_a_supersedes_naming_a_malformed_fence_is_surfaced(self, capsys):
        broken = _fence("blocking", HEAD_A).replace("{", "{,", 1)
        comments = [
            _comment(1, broken),
            _comment(2, _fence("clean", HEAD_B, {"supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes comment #1" in err

    def test_supersession_still_resolves_no_finding(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("clean", HEAD_B, {"supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "F1" in err


class TestStaleVerdictWording:
    """A fenced verdict at a stale head never authorizes a merge; only the
    explanation differs."""

    def _read(self, comments, head=HEAD_B):
        return read_reviewer_verdict(
            [{**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"} for c in comments],
            LOGIN, head, 1, "o", "r", expected_reviewer_name=NAME,
        )

    def test_stale_fence_then_prose_only_comment_refuses_with_the_distinct_message(self, capsys):
        comments = [_comment(1, _fence("clean", HEAD_A)), _comment(2, "I looked again, fine.")]
        with pytest.raises(VerdictStaleAfterCommentsError, match="Commenting is not re-verdicting"):
            self._read(comments)
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "Commenting is not re-verdicting" in err

    def test_the_distinct_refusal_is_still_a_stale_refusal(self):
        assert issubclass(VerdictStaleAfterCommentsError, VerdictStaleError)

    def test_a_plain_stale_verdict_keeps_the_plain_message(self):
        with pytest.raises(VerdictStaleError) as caught:
            self._read([_comment(1, _fence("clean", HEAD_A))])
        assert not isinstance(caught.value, VerdictStaleAfterCommentsError)

    def test_stale_fence_then_fresh_fence_selects_the_fresh_one(self):
        comments = [_comment(1, _fence("clean", HEAD_A)), _comment(2, _fence("clean", HEAD_B))]
        assert self._read(comments).comment_id == 2
        code, _ = _merge(comments)
        assert code == verb.EXIT_OK

    def test_fresh_fence_listed_first_is_still_selected_over_an_older_stale_one(self):
        comments = [_comment(2, _fence("clean", HEAD_B)), _comment(1, _fence("clean", HEAD_A))]
        assert self._read(comments).comment_id == 2

    def test_a_newer_stale_fence_is_not_rescued_by_an_older_fresh_one(self):
        comments = [_comment(1, _fence("clean", HEAD_B)), _comment(2, _fence("clean", HEAD_A))]
        with pytest.raises(VerdictStaleError):
            self._read(comments)


class TestRepoReviewerFloor:
    def test_declared_role_is_required_without_any_flag(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": [NAME]})
        argv = _base_args(**{"--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info=_pr_info(HEAD_B, tmp_path), comments=[]),
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "required_reviewer_roles" in capsys.readouterr().err

    def test_declared_role_with_a_clean_verdict_merges(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": [NAME]})
        comments = [_comment(1, _fence("clean", HEAD_B), login=NAME)]
        code = verb.main(
            _base_args(**{"--repo-path": str(tmp_path)}),
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(pr_info=_pr_info(HEAD_B, tmp_path), comments=comments),
        )
        assert code == verb.EXIT_OK

    def test_floor_is_the_union_with_the_flags(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": ["second"]})
        comments = [_comment(1, _fence("clean", HEAD_B))]
        code, _ = _merge(comments, repo_path=tmp_path)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    @staticmethod
    def _only_ghost_is_unresolvable(monkeypatch):
        from clagentic_loadout.merge.reviewer_login import ReviewerLoginNotConfiguredError

        def resolve(name, platform):
            if name.startswith("ghost"):
                raise ReviewerLoginNotConfiguredError(
                    f"no GitHub App slug configured for reviewer {name!r}"
                )
            return name

        monkeypatch.setattr("clagentic_loadout.merge.repo_gate_runtime.resolve_reviewer_login", resolve)
        monkeypatch.setattr(verb, "resolve_reviewer_login", resolve)

    def test_an_unresolvable_role_is_dropped_with_a_warning_and_the_rest_still_merges(
        self, tmp_path, monkeypatch, capsys
    ):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(tmp_path, {"required_reviewer_roles": ["ghost"]})
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "'ghost'" in err and "forgejo" in err and "no GitHub App slug configured" in err
        assert "DROPPED" in err
        assert "NOT ENFORCED" not in err

    def test_a_failed_required_scanner_on_a_resolvable_role_still_refuses_beside_an_unresolvable_role(
        self, tmp_path, monkeypatch, capsys
    ):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(
            tmp_path,
            {
                "required_reviewer_roles": ["ghost"],
                "required_scanners": {NAME: ["alpha"], "ghost": ["beta"]},
            },
        )
        failed = {"scanners_run": [{"scanner": "alpha", "status": "failed", "reason": "x"}]}
        code, err = _merge([_comment(1, _fence("clean", HEAD_B, failed))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "alpha" in err
        assert "'ghost'" in err

    def test_an_unresolvable_roles_own_scanners_are_skipped_not_unreachable(
        self, tmp_path, monkeypatch, capsys
    ):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(
            tmp_path,
            {"required_reviewer_roles": ["ghost"], "required_scanners": {"ghost": ["beta"]}},
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "required_scanners entry is skipped" in err

    def test_a_resolvable_role_stays_enforced_beside_an_unresolvable_one(
        self, tmp_path, monkeypatch, capsys
    ):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(tmp_path, {"required_reviewer_roles": ["ghost", "never-posts"]})
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "'ghost'" in err

    def test_a_resolvable_declared_role_is_still_enforced(self, tmp_path, monkeypatch, capsys):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(tmp_path, {"required_reviewer_roles": ["never-posts"]})
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "NOT ENFORCED" not in err

    def test_a_declared_role_named_by_a_flag_keeps_the_flags_login(self, tmp_path, monkeypatch, capsys):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(tmp_path, {"required_reviewer_roles": ["ghost"]})
        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B))],
            repo_path=tmp_path,
            extra_args=["--required-reviewer", "ghost:ghost-login"],
            capsys=capsys,
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "NOT ENFORCED" not in err

    def test_an_unresolvable_explicit_flag_role_still_errors(self, tmp_path, monkeypatch, capsys):
        self._only_ghost_is_unresolvable(monkeypatch)
        _write_config(tmp_path, {"required_reviewer_roles": []})
        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B))],
            repo_path=tmp_path,
            extra_args=["--required-reviewer", "ghost2"],
            capsys=capsys,
        )
        assert code == verb.EXIT_USAGE
        assert "'ghost2'" in err

    @pytest.mark.parametrize(
        "section",
        [{"required_reviewer_roles": "reviewer"}, {"authorized_roles": ["merger"]}],
        ids=["not-a-list", "key-omitted"],
    )
    def test_an_unloadable_gate_config_falls_back_to_flags_only_and_merges(
        self, tmp_path, section, capsys
    ):
        _write_config(tmp_path, section)
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "required_reviewer_roles NOT ENFORCED" in err
        assert TRACKED_GATE_RELATIVE_PATH in err

    def test_a_malformed_reviewer_key_drops_the_whole_gate_even_beside_a_valid_scanners_key(
        self, tmp_path, capsys
    ):
        _write_config(
            tmp_path,
            {
                "required_reviewer_roles": "reviewer",
                "required_scanners": {NAME: ["alpha"]},
            },
        )
        failed = {"scanners_run": [{"scanner": "alpha", "status": "failed", "reason": "timed out"}]}
        code, err = _merge([_comment(1, _fence("clean", HEAD_B, failed))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "required_reviewer_roles NOT ENFORCED" in err
        assert "required_scanners NOT ENFORCED" in err

    def test_an_unsatisfiable_floor_refuses_unless_ignored_and_the_override_is_recorded(
        self, tmp_path, capsys
    ):
        _write_config(tmp_path, {"required_reviewer_roles": ["never-posts"]})
        refused, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert refused == verb.EXIT_GATE_RESULT_BLOCKED
        assert "--ignore-repo-gate" in err

        code, err = _merge(
            [_comment(1, _fence("clean", HEAD_B))],
            repo_path=tmp_path,
            extra_args=["--ignore-repo-gate"],
            capsys=capsys,
        )
        assert code == verb.EXIT_OK
        assert "IGNORED via --ignore-repo-gate" in err

    def test_both_gate_keys_are_read_from_one_snapshot(self, tmp_path, monkeypatch):
        repo = _write_config(tmp_path, {"required_reviewer_roles": [NAME], "required_scanners": {NAME: ["a"]}})
        real = yaml.safe_load
        gate_reads = []

        def counting(*args, **kwargs):
            if "required_reviewer_roles" in str(args[0]):
                gate_reads.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(yaml, "safe_load", counting)
        gate = load_repo_gate_at_base(tmp_path, base_sha=repo.base_sha, base_branch="main")
        assert len(gate_reads) == 1
        assert gate.warnings == ()
        assert gate.reviewer_roles == (NAME,)
        assert gate.scanners_for(NAME) == ("a",)

    def test_the_ignore_flag_help_names_exactly_the_gates_it_lifts(self, capsys):
        assert verb.main(["--help"]) == 0
        flat = " ".join(capsys.readouterr().out.split())
        assert "merge.required_reviewer_roles" in flat
        assert "merge.required_scanners" in flat
        assert "model attestation" in flat
        assert "single-fence" in flat

    def test_the_attestation_row_names_exactly_the_gates_it_lifts(self):
        body = build_attestation_body(
            gated_head_sha=HEAD_B,
            merged_sha=HEAD_A,
            required_reviewer_logins=[],
            ci_disposition="ok",
            repo_gate_ignored=True,
        )
        assert "merge.required_reviewer_roles and merge.required_scanners ignored" in body

    def test_the_override_appears_in_the_output_and_the_attestation(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": ["never-posts"]})
        posted: list[str] = []
        opener = _make_opener(
            pr_info=_pr_info(HEAD_B, tmp_path),
            comments=[_comment(1, _fence("clean", HEAD_B))],
        )

        def recording(req, timeout=15):
            if req.get_method() == "POST" and "/comments" in req.full_url:
                posted.append(json.loads(req.data.decode("utf-8"))["body"])
            return opener(req, timeout)

        code = verb.main(
            _base_args(**{"--required-reviewer": f"{NAME}:{LOGIN}", "--repo-path": str(tmp_path)})
            + ["--ignore-repo-gate"],
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=recording,
        )
        assert code == verb.EXIT_OK
        assert any("Repo gate" in body and "ignored" in body for body in posted)
        assert '"repo_gate_ignored": ["never-posts"]' in capsys.readouterr().out

    def test_no_repo_path_declares_nothing(self):
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B))])
        assert code == verb.EXIT_OK


class TestTheGateComesFromBaseNotTheWorkingTree:
    """The PR under review must not be able to relax its own gate."""

    def test_a_relaxed_working_tree_does_not_relax_a_strict_base(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": ["never-posts"]})
        # The working tree (as if holding the PR head) declares no requirement.
        (tmp_path / TRACKED_GATE_RELATIVE_PATH).write_text(
            yaml.safe_dump({"merge": {"required_reviewer_roles": []}}), encoding="utf-8"
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "never-posts" in err

    def test_a_corrupted_working_tree_does_not_force_the_fallback(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": ["never-posts"]})
        (tmp_path / TRACKED_GATE_RELATIVE_PATH).write_text("merge: [unclosed", encoding="utf-8")
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "NOT ENFORCED" not in err

    def test_a_strict_working_tree_is_not_enforced_when_base_declares_nothing(self, tmp_path, capsys):
        init_gate_repo(tmp_path, tracked_gate=None)
        gate_file = tmp_path / TRACKED_GATE_RELATIVE_PATH
        gate_file.write_text(yaml.safe_dump({"merge": {"required_reviewer_roles": ["never-posts"]}}), encoding="utf-8")
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "NOT ENFORCED" not in err

    def test_gate_keys_in_the_deployment_file_are_ignored_with_a_warning(self, tmp_path, capsys):
        init_gate_repo(tmp_path, tracked_gate=None)
        write_deployment_config(
            tmp_path, {"sync_tree_after_merge": False, "required_reviewer_roles": ["never-posts"]}
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "merge.required_reviewer_roles in" in err
        assert "IGNORED" in err
        assert TRACKED_GATE_RELATIVE_PATH in err

    def test_a_malformed_base_gate_falls_back_with_a_warning_even_if_the_working_tree_is_valid(
        self, tmp_path, capsys
    ):
        _write_config(tmp_path, {"required_reviewer_roles": "reviewer"})
        (tmp_path / TRACKED_GATE_RELATIVE_PATH).write_text(
            yaml.safe_dump({"merge": {"required_reviewer_roles": ["never-posts"]}}), encoding="utf-8"
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "required_reviewer_roles NOT ENFORCED" in err

    def test_the_introducing_pr_is_judged_by_a_base_with_no_gate(self, tmp_path, capsys):
        repo = init_gate_repo(
            tmp_path,
            tracked_gate=None,
            head_files={TRACKED_GATE_RELATIVE_PATH: yaml.safe_dump({"merge": {"required_reviewer_roles": ["never-posts"]}})},
        )
        code, err = _merge([_comment(1, _fence("clean", HEAD_B))], repo_path=tmp_path, capsys=capsys)
        assert repo.head_sha != repo.base_sha
        assert code == verb.EXIT_OK
        assert "NOT ENFORCED" not in err


def _raw_fence(**fields):
    """A fence assembled outside the tool, to model history the emit side would
    refuse to construct."""
    payload = {"reviewer": NAME, "review_status": "blocking", "head_sha": HEAD_A, "pr_number": 1}
    payload.update(fields)
    return "\n```review-result\n" + json.dumps(payload) + "\n```\n"


_F1_RAW_OPEN = [{"id": "F1", "rule_id": "R1", "head": HEAD_A}]
_DUP_SCANNERS = [
    {"scanner": "alpha", "status": "failed", "reason": "x"},
    {"scanner": "alpha", "status": "ran"},
]
_CLEAN_B = _fence("clean", HEAD_B)


def _classify(body, **overrides):
    from clagentic_loadout.merge.verdict import classify_earlier_fence

    return classify_earlier_fence(
        {"id": 7, "body": body}, **{"pr_number": 1, "reviewer_name": NAME, **overrides}
    )


class TestEarlierFenceClassification:
    """One row per cell of the earlier-fence disposition table. A row's body is
    the earlier comment (#1); the later clean verdict at HEAD_B is always
    well-formed and clears nothing."""

    IGNORED_ROWS = {
        "unparseable-stateless": _fence("blocking", HEAD_A).replace("{", "{,", 1),
        "json-not-an-object-stateless": "\n```review-result\n[1, 2]\n```\n",
        "other-reviewer-stateful": _fence("blocking", HEAD_A, F1_OPEN, name="other-reviewer"),
        "other-pr-stateful": build_verdict_block(NAME, "blocking", HEAD_A, 2, findings_state=F1_OPEN),
        "schema-error-stateless": _raw_fence(review_status="maybe"),
        "two-fences-stateless": _fence("blocking", HEAD_A) + _fence("blocking", HEAD_A),
        "malformed-head-sha-stateless": _raw_fence(head_sha="abc"),
    }
    REFUSED_ROWS = {
        "unparseable-stateful": _raw_fence().replace('"pr_number": 1}', '"findings_open": [}'),
        "json-not-an-object-stateful": '\n```review-result\n[{"findings_open": []}]\n```\n',
        "unparseable-version-2-stamp-only": _raw_fence().replace('"pr_number": 1}', '"fence_schema_version": 2,}'),
        "schema-error-explicit-null-state": _raw_fence(findings_open=None),
        "schema-error-version-2-without-state-keys": _raw_fence(
            fence_schema_version=2, review_status="maybe"
        ),
        "duplicate-scanners": _raw_fence(fence_schema_version=2, scanners_run=_DUP_SCANNERS),
        "two-fences-stateful": _fence("blocking", HEAD_A) + _fence("blocking", HEAD_A, F1_OPEN),
        "malformed-finding-head": _raw_fence(
            fence_schema_version=2, findings_open=[{"id": "F1", "rule_id": "R1", "head": "abc"}]
        ),
        "malformed-claim-head": _raw_fence(
            fence_schema_version=2, cleared_claims=[{"id": "F1", "head": "abc", "evidence": "e"}]
        ),
        "malformed-head-sha-stateful": _raw_fence(
            head_sha="abc", fence_schema_version=2, findings_open=_F1_RAW_OPEN
        ),
        "malformed-head-sha-version-2-stamp-only": _raw_fence(head_sha="abc", fence_schema_version=2),
    }

    def test_a_comment_without_a_fence_is_not_a_verdict(self):
        assert _classify("I looked again, fine.") is None
        assert _classify("") is None

    def test_a_well_formed_fence_is_valid_and_carries_its_head_and_state(self):
        fence = _classify(_fence("blocking", HEAD_A, F1_OPEN))
        assert (fence.kind, fence.comment_id, fence.head) == ("valid", 7, HEAD_A)
        assert [f["id"] for f in fence.state.findings_open] == ["F1"]

    @pytest.mark.parametrize("row", sorted(IGNORED_ROWS))
    def test_ignored_row_classifies_as_ignored_with_a_reason(self, row):
        fence = _classify(self.IGNORED_ROWS[row])
        assert (fence.kind, fence.comment_id) == ("ignored", 7)
        assert fence.reason

    @pytest.mark.parametrize("row", sorted(REFUSED_ROWS))
    def test_unreadable_stateful_row_classifies_as_unreadable_stateful(self, row):
        fence = _classify(self.REFUSED_ROWS[row])
        assert fence.kind == "unreadable_stateful"
        assert fence.reason

    @pytest.mark.parametrize("row", sorted(IGNORED_ROWS))
    def test_an_ignored_earlier_fence_never_blocks_a_later_clean_verdict(self, row, capsys):
        comments = [_comment(1, self.IGNORED_ROWS[row]), _comment(2, _CLEAN_B)]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "earlier comment #1" in err
        assert "is ignored" in err

    @pytest.mark.parametrize("row", sorted(REFUSED_ROWS))
    def test_an_unreadable_stateful_earlier_fence_refuses_naming_the_comment(self, row, capsys):
        comments = [_comment(1, self.REFUSED_ROWS[row]), _comment(2, _CLEAN_B)]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "#1" in err
        assert "may hold findings open" in err

    def test_every_unreadable_stateful_comment_is_named(self, capsys):
        comments = [
            _comment(1, self.REFUSED_ROWS["duplicate-scanners"]),
            _comment(2, self.REFUSED_ROWS["schema-error-explicit-null-state"]),
            _comment(3, _CLEAN_B),
        ]
        _, err = _merge(comments, capsys=capsys)
        assert "#1" in err and "#2" in err

    def test_stateless_fence_with_a_malformed_head_does_not_hide_an_earlier_open_finding(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, self.IGNORED_ROWS["malformed-head-sha-stateless"]),
            _comment(3, _CLEAN_B),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "F1" in err

    def test_the_ignored_fences_are_reported_on_the_verdict_with_their_reasons(self):
        verdict = read_reviewer_verdict(
            [
                {**c, "created_at": f"2026-01-01T00:00:{c['id']:02d}Z"}
                for c in [_comment(1, self.IGNORED_ROWS["unparseable-stateless"]), _comment(2, _CLEAN_B)]
            ],
            LOGIN, HEAD_B, 1, "o", "r", expected_reviewer_name=NAME,
        )
        assert [comment_id for comment_id, _ in verdict.ignored_earlier_fences] == [1]
        assert verdict.ignored_earlier_fences[0][1]

    def test_a_valid_fence_is_the_supersession_target_and_the_carry_forward_source(self, capsys):
        comments = [
            _comment(1, _fence("blocking", HEAD_A, F1_OPEN)),
            _comment(2, _fence("clean", HEAD_B, {**F1_CLEARED, "supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes" not in err

    def test_an_unreadable_fence_is_not_a_supersession_target(self, capsys):
        comments = [
            _comment(1, self.IGNORED_ROWS["malformed-head-sha-stateless"]),
            _comment(2, _fence("clean", HEAD_B, {"supersedes": 1})),
        ]
        code, err = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "supersedes comment #1" in err

    def test_the_newest_fence_stays_strict(self, capsys):
        comments = [_comment(1, _fence("blocking", HEAD_A)), _comment(2, self.IGNORED_ROWS["schema-error-stateless"])]
        code, _ = _merge(comments, capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED


class TestExplicitNullGateKeys:
    @pytest.mark.parametrize("key", ["required_reviewer_roles", "required_scanners"])
    def test_an_explicit_null_is_malformed_and_drops_the_whole_gate(self, tmp_path, capsys, key):
        section = {"required_reviewer_roles": [], "required_scanners": {NAME: ["alpha"]}}
        section[key] = None
        _write_config(tmp_path, section)
        code, err = _merge([_comment(1, _CLEAN_B)], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert f"merge.{key}" in err
        assert "required_reviewer_roles NOT ENFORCED" in err
        assert "required_scanners NOT ENFORCED" in err

    def test_an_absent_scanners_key_is_still_no_requirement(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": []})
        code, err = _merge([_comment(1, _CLEAN_B)], repo_path=tmp_path, capsys=capsys)
        assert code == verb.EXIT_OK
        assert "NOT ENFORCED" not in err
