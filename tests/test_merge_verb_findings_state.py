"""Merge-gate tests for findings state carried in the verdict fence, scanner
outcomes, the stale-verdict refusal wording, and the repo reviewer floor.

Every fixture runs through the real verb with an injected opener; nothing
touches a network or a real git tree."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.attestation import build_attestation_body
from clagentic_loadout.merge.errors import VerdictStaleAfterCommentsError, VerdictStaleError
from clagentic_loadout.merge.repo_gate_runtime import load_repo_gate
from clagentic_loadout.merge.verdict import build_verdict_block, read_reviewer_verdict
from clagentic_loadout.transport import provider_config
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
        opener=_make_opener(
            pr_info={"head": {"sha": head}, "title": "feat: x"}, comments=comments
        ),
    )
    return code, (capsys.readouterr().err if capsys else "")


def _write_config(repo_path, merge_section):
    config_dir = repo_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"merge": {"sync_tree_after_merge": False, **merge_section}}),
        encoding="utf-8",
    )


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
            opener=_make_opener(pr_info={"head": {"sha": HEAD_B}, "title": "feat: x"}, comments=[]),
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
            opener=_make_opener(pr_info={"head": {"sha": HEAD_B}, "title": "feat: x"}, comments=comments),
        )
        assert code == verb.EXIT_OK

    def test_floor_is_the_union_with_the_flags(self, tmp_path):
        _write_config(tmp_path, {"required_reviewer_roles": ["second"]})
        comments = [_comment(1, _fence("clean", HEAD_B))]
        code, _ = _merge(comments, repo_path=tmp_path)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

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
        assert str(tmp_path) in err

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
        _write_config(tmp_path, {"required_reviewer_roles": [NAME], "required_scanners": {NAME: ["a"]}})
        real = yaml.safe_load
        reads = []

        def counting(*args, **kwargs):
            reads.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(yaml, "safe_load", counting)
        gate = load_repo_gate(tmp_path)
        assert len(reads) == 1
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
            pr_info={"head": {"sha": HEAD_B}, "title": "feat: x"},
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

    def test_a_role_with_no_resolvable_login_refuses_and_names_the_override(self, tmp_path, capsys):
        _write_config(tmp_path, {"required_reviewer_roles": ["security"]})
        argv = _base_args(**{"--platform": "github", "--repo-path": str(tmp_path)})
        code = verb.main(
            argv,
            token_provider=_RecordingTokenProvider(),
            authority_provider=_AllowingAuthorityProvider(),
            opener=_make_opener(),
        )
        assert code == verb.EXIT_USAGE
        assert "--ignore-repo-gate" in capsys.readouterr().err

    def test_no_repo_path_declares_nothing(self):
        code, _ = _merge([_comment(1, _fence("clean", HEAD_B))])
        assert code == verb.EXIT_OK
