"""loadout-merge and loadout-ruling resolve the required reviewer roles the same way.

A role the merge enforces is a role whose fences the ruling verb can attribute
and rule on, in a repository with a tracked gate file and in one without. Every
case runs the real verbs against fake hosts for both platforms; names are
synthetic."""

from __future__ import annotations

import json

import pytest
import yaml

from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.repo_gate_runtime import RepoGate
from clagentic_loadout.merge.required_reviewers import (
    parse_required_reviewers,
    resolve_required_reviewers,
)
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review import ruling_verb as ruling
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.attestation import SOURCE_CONFIGURED, Identity
from tests._gate_repo import init_gate_repo
from tests._ruling_host import FENCE_COMMENT, host_opener
from tests._ruling_support import FP, REPO, TODAY, registry_dir
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    RefusingTokenProvider,
    base_args,
    make_opener,
)
from tests.test_merge_computed_verdict import HEAD, NAME, _fence, _tip
from tests.test_merge_gate_rulings import _PLATFORMS, _finding

OTHER_ROLE = "another-reviewer"
EARLIER_HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")
    monkeypatch.setattr(
        "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
        lambda **kwargs: f"{kwargs['caller']}-app",
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = registry_dir(tmp_path) / "rulings.yaml"
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))
    return path


def _login(platform):
    return NAME if platform == "forgejo" else f"{NAME}-app[bot]"


def _blocked_comment(platform, *, login=None):
    return {
        "id": FENCE_COMMENT,
        "user": {"login": login or _login(platform)},
        "body": _fence("BLOCKED", findings=[_finding()]),
        "created_at": "2026-01-01T00:00:07Z",
    }


def _identity(subject):
    identity = Identity(subject, SOURCE_CONFIGURED)
    return lambda: identity


def _record(platform, comments, *extra, role="merger", token_provider=None, **host):
    argv = [
        "--role", role, "record",
        "--platform", platform, "--repo", REPO, "--pr", "1",
        "--fence-comment", str(FENCE_COMMENT), "--fingerprint", FP,
        "--kind", "false_positive", "--rationale", "not a defect",
        "--requested-by", "some-caller", "--source-ref", "design note 4",
        *extra,
    ]
    return ruling.main(
        argv,
        token_provider=token_provider or RecordingTokenProvider(),
        opener=host_opener(comments, **host),
        identity_provider=_identity(role),
        today=TODAY,
    )


def _merge(platform, comments, repo, capsys, *extra):
    info = {
        "head": {"sha": HEAD},
        "title": "feat: x",
        "base": {"ref": "main", "sha": _tip(repo), "repo": {"private": True, "visibility": "private"}},
    }
    argv = base_args(**{"--platform": platform, "--repo-path": str(repo)})
    capsys.readouterr()
    code = merge_verb.main(
        [*argv, *extra],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=_PLATFORMS[platform](pr_info=info, comments=comments),
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def ungated_repo(tmp_path):
    """A repository whose base commit has no tracked gate file."""
    init_gate_repo(tmp_path / "repo", tracked_gate=None)
    return tmp_path / "repo"


@pytest.mark.parametrize("platform", sorted(_PLATFORMS))
class TestRepositoryWithoutAGateFile:
    def test_a_role_the_merge_enforces_through_the_flag_is_one_the_ruling_can_attribute(
        self, platform, registry
    ):
        comments = [_blocked_comment(platform)]

        assert _record(platform, comments, "--required-reviewer", NAME, with_gate=False) == ruling.EXIT_OK
        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert (stored["fingerprint"], stored["rule_id"], stored["by"]) == (FP, "R1", "merger")

    def test_without_the_flag_the_fence_still_cannot_be_attributed(self, platform, registry, capsys):
        code = _record(platform, [_blocked_comment(platform)], with_gate=False)

        assert code == ruling.EXIT_FENCE_INVALID
        assert "declares none and no --required-reviewer was given" in capsys.readouterr().err
        assert not registry.exists()

    def test_a_ruling_for_a_role_the_merge_would_not_require_is_refused(
        self, platform, registry, capsys
    ):
        code = _record(
            platform, [_blocked_comment(platform)], "--required-reviewer", OTHER_ROLE, with_gate=False
        )

        assert code == ruling.EXIT_FENCE_INVALID
        assert "not the login of a required reviewer role" in capsys.readouterr().err
        assert not registry.exists()

    def test_a_comment_from_an_account_that_is_no_required_role_is_refused(self, platform, registry):
        comments = [_blocked_comment(platform, login="someone-else")]

        code = _record(platform, comments, "--required-reviewer", NAME, with_gate=False)

        assert code == ruling.EXIT_FENCE_INVALID
        assert not registry.exists()

    @pytest.mark.parametrize("mode", [[], ["--report-only"]], ids=["merge", "report-only"])
    def test_a_ruling_recorded_through_the_flag_is_honoured_by_the_merge(
        self, platform, mode, registry, ungated_repo, capsys
    ):
        comments = [_blocked_comment(platform)]
        flag = ["--required-reviewer", NAME]

        refused, _, err = _merge(platform, comments, ungated_repo, capsys, *flag, *mode)
        assert refused == merge_verb.EXIT_GATE_RESULT_BLOCKED
        assert "src/a.py:10:R1" in err

        assert _record(platform, comments, *flag, with_gate=False) == ruling.EXIT_OK

        code, out, err = _merge(platform, comments, ungated_repo, capsys, *flag, *mode)
        assert code == merge_verb.EXIT_OK, err
        assert "ruling(s) recorded for this PR" in err
        if not mode:
            evaluation = json.loads(out.strip().splitlines()[-1])["verdict_evaluations"][0]
            assert evaluation["gate_outcome"] == "CLEAN" and evaluation["fence_outcome"] == "BLOCKED"
            assert "src/a.py:10:R1" in evaluation["applied_rulings"][0]


@pytest.mark.parametrize("platform", sorted(_PLATFORMS))
class TestRepositoryWithAGateFile:
    def test_a_gate_declared_role_is_attributed_as_before(self, platform, registry):
        comments = [_blocked_comment(platform)]

        assert _record(platform, comments, gate_roles=(NAME,)) == ruling.EXIT_OK

    def test_the_flag_adds_to_the_gate_and_never_removes_from_it(self, platform, registry):
        """The gate requires the reviewer; a flag naming another role leaves that
        reviewer's fence attributable and adds the other role."""
        comments = [_blocked_comment(platform)]

        code = _record(platform, comments, "--required-reviewer", OTHER_ROLE, gate_roles=(NAME,))

        assert code == ruling.EXIT_OK

    def test_a_role_in_neither_the_gate_nor_the_flags_is_refused(self, platform, registry, capsys):
        code = _record(
            platform,
            [_blocked_comment(platform)],
            "--required-reviewer", OTHER_ROLE,
            gate_roles=("third-reviewer",),
        )

        assert code == ruling.EXIT_FENCE_INVALID
        assert "not the login of a required reviewer role" in capsys.readouterr().err

    def test_a_role_only_the_flag_names_is_attributable_beside_a_gate_role(self, platform, registry):
        comments = [_blocked_comment(platform)]

        code = _record(platform, comments, "--required-reviewer", NAME, gate_roles=("third-reviewer",))

        assert code == ruling.EXIT_OK


class TestFlagInput:
    def test_a_name_and_login_pair_is_refused_naming_the_value(self, registry, capsys):
        code = _record(
            "forgejo",
            [_blocked_comment("forgejo", login="pinned-login")],
            "--required-reviewer", f"{NAME}:pinned-login",
            token_provider=RefusingTokenProvider(),
            with_gate=False,
        )

        assert code == ruling.EXIT_USAGE
        err = capsys.readouterr().err
        assert f"{NAME}:pinned-login" in err and "bare role name" in err
        assert not registry.exists()

    def test_an_empty_role_name_is_refused(self, registry):
        assert _record("forgejo", [_blocked_comment("forgejo")], "--required-reviewer", " ") == (
            ruling.EXIT_USAGE
        )

    def test_the_ignore_repo_gate_flag_does_not_exist(self, registry, capsys):
        code = _record("forgejo", [_blocked_comment("forgejo")], "--ignore-repo-gate")

        assert code == ruling.EXIT_USAGE
        assert "--ignore-repo-gate" in capsys.readouterr().err

    def test_a_role_with_no_login_on_the_platform_is_a_usage_error(self, registry, monkeypatch, capsys):
        from clagentic_loadout.merge.reviewer_login import ReviewerLoginNotConfiguredError

        def no_slug(**kwargs):
            raise ReviewerLoginNotConfiguredError("no app slug configured for the role")

        monkeypatch.setattr("clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", no_slug)

        code = _record("github", [_blocked_comment("github")], "--required-reviewer", NAME)

        assert code == ruling.EXIT_USAGE
        assert "no app slug configured" in capsys.readouterr().err

    def test_the_flag_does_not_decide_who_may_record(self, registry):
        code = _record(
            "forgejo",
            [_blocked_comment("forgejo")],
            "--required-reviewer", NAME,
            role="helper",
            with_gate=False,
        )

        assert code == ruling.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_the_help_names_the_flag_without_a_work_item_reference(self, capsys):
        with pytest.raises(SystemExit):
            ruling._build_arg_parser().parse_args(["record", "--help"])

        text = capsys.readouterr().out
        assert "--required-reviewer" in text and "--ignore-repo-gate" not in text
        assert "lr-" not in text


class TestSharedResolution:
    def test_the_floor_and_the_flags_are_united(self):
        gate = RepoGate(reviewer_roles=("floor-role",))

        resolved = resolve_required_reviewers(gate, "forgejo", {"flag-role": "flag-login"})

        assert resolved.reviewers == {"flag-role": "flag-login", "floor-role": "floor-role"}
        assert resolved.floor_only == {"floor-role": "floor-role"}

    def test_a_flag_keeps_its_login_for_a_role_the_floor_also_declares(self):
        gate = RepoGate(reviewer_roles=("shared-role",))

        resolved = resolve_required_reviewers(gate, "forgejo", {"shared-role": "pinned-login"})

        assert resolved.reviewers == {"shared-role": "pinned-login"}
        assert resolved.floor_only == {}

    def test_no_gate_and_no_flags_require_nothing(self):
        assert resolve_required_reviewers(RepoGate(), "forgejo", {}).reviewers == {}

    def test_ignoring_the_gate_leaves_the_flags_alone(self):
        gate = RepoGate(reviewer_roles=("floor-role",))

        resolved = resolve_required_reviewers(
            gate, "forgejo", {"flag-role": "flag-login"}, ignore_repo_gate=True
        )

        assert resolved.reviewers == {"flag-role": "flag-login"}

    @pytest.mark.parametrize(
        ("overridable", "mentions_override"), [(True, True), (False, False)]
    )
    def test_scanners_for_a_role_that_is_not_required_are_refused(self, overridable, mentions_override):
        gate = RepoGate(required_scanners={"orphan-role": ("scanner",)})

        resolved = resolve_required_reviewers(gate, "forgejo", {})

        assert resolved.unreachable_scanner_roles == ("orphan-role",)
        with pytest.raises(MergeUsageError) as caught:
            resolved.require_reachable_scanners(overridable=overridable)

        assert "'orphan-role'" in str(caught.value)
        assert ("--ignore-repo-gate" in str(caught.value)) is mentions_override

    def test_resolving_reports_unreachable_scanners_without_refusing(self):
        gate = RepoGate(required_scanners={"orphan-role": ("scanner",)})

        resolved = resolve_required_reviewers(gate, "forgejo", {"flag-role": "flag-login"})

        assert resolved.unreachable_scanner_roles == ("orphan-role",)

    def test_ignoring_the_gate_leaves_nothing_to_refuse(self):
        gate = RepoGate(required_scanners={"orphan-role": ("scanner",)})

        resolved = resolve_required_reviewers(gate, "forgejo", {}, ignore_repo_gate=True)

        resolved.require_reachable_scanners()

    def test_bare_only_refuses_a_pair_and_names_it(self):
        with pytest.raises(MergeUsageError, match="some-role:some-login"):
            parse_required_reviewers(["some-role:some-login"], "forgejo", bare_only=True)

    def test_a_pair_is_accepted_by_default(self):
        assert parse_required_reviewers(["some-role:some-login"], "forgejo") == {
            "some-role": "some-login"
        }


class TestEarlierHeadFencesStayANotice:
    """An earlier fence's finding that the current verdict neither clears nor
    re-raises is a notice, not a refusal. The ruling path leaves it that way: the
    fence union and this rule are untouched, and a same-head blocked fence clears
    only through a recorded ruling."""

    def _merge(self, comments, capsys):
        argv = base_args(**{"--required-reviewer": NAME})
        capsys.readouterr()
        code = merge_verb.main(
            argv,
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=make_opener(pr_info={"head": {"sha": HEAD}, "title": "feat: x"}, comments=comments),
        )
        return code, capsys.readouterr().err

    @staticmethod
    def _fence_at(status, head, state=None):
        return {"user": {"login": NAME}, "body": build_verdict_block(NAME, status, head, 1, findings_state=state)}

    def test_an_earlier_head_blocked_fence_is_a_notice_not_a_refusal(self, capsys):
        opened = {"findings_open": [{"id": "F1", "rule_id": "R1", "head": EARLIER_HEAD}]}
        comments = [
            {"id": 1, **self._fence_at("blocking", EARLIER_HEAD, opened)},
            {"id": 2, **self._fence_at("clean", HEAD)},
        ]

        code, err = self._merge(comments, capsys)

        assert code == merge_verb.EXIT_OK, err
        assert "NOTICE" in err and "F1" in err

    def test_a_same_head_blocked_fence_still_refuses_a_later_clean_one(self, capsys):
        comments = [
            {"id": 1, **self._fence_at("blocking", HEAD)},
            {"id": 2, **self._fence_at("clean", HEAD)},
        ]

        code, err = self._merge(comments, capsys)

        assert code == merge_verb.EXIT_GATE_RESULT_BLOCKED
        assert "in verdict comment #1" in err
