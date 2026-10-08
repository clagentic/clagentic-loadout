"""A repo-declared reviewer role resolves to the login that posts it through one
path: the role itself first (exactly as `--required-reviewer` resolves a name),
then, only when that fails, the deployment's optional `github_app.role_callers`
mapping. A deployment with no mapping behaves as it did before the mapping
existed, on both platforms.

Except for `TestMergeEnforcesAMappedRole`, which stubs the merge-side resolver
to isolate the enforcement step, every GitHub-side read goes through a real
user-level config file under a per-test directory, never a patched resolver,
so those tests prove the config -> login path and not a stand-in for it."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clagentic_loadout.doctor.checks import check_repo_loadout_schema
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.repo_gate_runtime import RepoGate, with_resolvable_reviewer_roles
from clagentic_loadout.merge.reviewer_login import (
    SOURCE_BARE_NAME,
    SOURCE_ROLE_CALLERS,
    SOURCE_SLUGS,
    ReviewerLoginNotConfiguredError,
    resolve_declared_role,
    resolve_reviewer_login,
)
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.transport import github_app_config
from tests._gate_repo import git, init_gate_repo
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HEAD = "b" * 40
GITHUB_REMOTE = "https://github.com/some-owner/some-repo.git"


@pytest.fixture
def user_config(tmp_path, monkeypatch):
    """Write the user-level `github_app:` section the resolvers read."""
    root = tmp_path / "user-config"

    def write(*, slugs=None, role_callers=None, slug=None, callers=None):
        section = {
            key: value
            for key, value in (
                ("slugs", slugs),
                ("role_callers", role_callers),
                ("slug", slug),
                ("callers", callers),
            )
            if value is not None
        }
        root.mkdir(parents=True, exist_ok=True)
        (root / "config.yaml").write_text(yaml.safe_dump({"github_app": section}), encoding="utf-8")
        monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", root)

    return write


class TestResolveDeclaredRole:
    def test_a_role_with_its_own_slug_resolves_unchanged(self, user_config):
        user_config(slugs={"reviewer": "app-r"}, role_callers={"reviewer": "other"})
        resolution = resolve_declared_role("reviewer", PLATFORM_GITHUB)
        assert (resolution.requirement, resolution.login, resolution.source) == (
            "reviewer",
            "app-r[bot]",
            SOURCE_SLUGS,
        )

    def test_a_mapped_role_resolves_to_the_callers_login_and_name(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        resolution = resolve_declared_role("reviewer", PLATFORM_GITHUB)
        assert resolution.role == "reviewer"
        assert resolution.requirement == "peaches"
        assert resolution.login == "app-p[bot]"
        assert resolution.source == SOURCE_ROLE_CALLERS

    def test_an_unmapped_role_is_unresolved_and_names_the_key_that_would_resolve_it(self, user_config):
        user_config(slugs={"peaches": "app-p"})
        with pytest.raises(ReviewerLoginNotConfiguredError, match=r"github_app\.role_callers\.reviewer"):
            resolve_declared_role("reviewer", PLATFORM_GITHUB)

    def test_a_mapping_to_a_caller_without_a_slug_names_the_missing_slug(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "ghost"})
        with pytest.raises(ReviewerLoginNotConfiguredError, match=r"github_app\.slugs\.ghost"):
            resolve_declared_role("reviewer", PLATFORM_GITHUB)

    def test_a_malformed_mapping_is_no_mapping(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers=["reviewer"])
        with pytest.raises(ReviewerLoginNotConfiguredError):
            resolve_declared_role("reviewer", PLATFORM_GITHUB)

    def test_the_flag_path_never_consults_the_mapping(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        with pytest.raises(ReviewerLoginNotConfiguredError):
            resolve_reviewer_login("reviewer", PLATFORM_GITHUB)

    def test_forgejo_resolves_a_mapped_role_to_the_callers_account(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        resolution = resolve_declared_role("reviewer", PLATFORM_FORGEJO)
        assert (resolution.requirement, resolution.login, resolution.source) == (
            "peaches",
            "peaches",
            SOURCE_ROLE_CALLERS,
        )

    def test_forgejo_mapping_wins_over_the_role_being_a_listed_caller(self, user_config):
        user_config(role_callers={"reviewer": "peaches"}, callers=["reviewer", "peaches"])
        assert resolve_declared_role("reviewer", PLATFORM_FORGEJO).login == "peaches"

    def test_forgejo_a_listed_caller_resolves_as_the_bare_role(self, user_config):
        user_config(callers=["reviewer"])
        resolution = resolve_declared_role("reviewer", PLATFORM_FORGEJO)
        assert (resolution.requirement, resolution.login, resolution.source) == (
            "reviewer",
            "reviewer",
            SOURCE_BARE_NAME,
        )

    def test_forgejo_with_no_callers_list_keeps_the_bare_role(self, user_config):
        user_config(slugs={"peaches": "app-p"})
        resolution = resolve_declared_role("reviewer", PLATFORM_FORGEJO)
        assert (resolution.login, resolution.source) == ("reviewer", SOURCE_BARE_NAME)

    def test_forgejo_an_unmapped_role_outside_the_callers_list_is_unresolved(self, user_config):
        user_config(callers=["peaches", "bobbie"])
        with pytest.raises(ReviewerLoginNotConfiguredError, match=r"github_app\.role_callers\.reviewer"):
            resolve_declared_role("reviewer", PLATFORM_FORGEJO)


class TestGateRolesFollowTheMapping:
    def _gate(self, roles, scanners=None):
        return RepoGate(reviewer_roles=tuple(roles), required_scanners=scanners)

    def test_a_mapped_role_is_required_under_the_callers_name_with_a_notice(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        gate = with_resolvable_reviewer_roles(self._gate(["reviewer"]), PLATFORM_GITHUB)
        assert gate.reviewer_roles == ("peaches",)
        assert gate.warnings == ()
        assert any("'reviewer'" in n and "'peaches'" in n and "app-p[bot]" in n for n in gate.notices)

    def test_an_unmapped_role_degrades_with_the_warning_as_before(self, user_config):
        user_config(slugs={"peaches": "app-p"})
        gate = with_resolvable_reviewer_roles(self._gate(["reviewer", "peaches"]), PLATFORM_GITHUB)
        assert gate.reviewer_roles == ("peaches",)
        assert gate.notices == ()
        assert len(gate.warnings) == 1
        assert "'reviewer'" in gate.warnings[0] and "DROPPED" in gate.warnings[0]

    def test_a_role_and_the_caller_it_maps_to_are_one_requirement(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        gate = with_resolvable_reviewer_roles(self._gate(["reviewer", "peaches"]), PLATFORM_GITHUB)
        assert gate.reviewer_roles == ("peaches",)

    def test_a_mapped_roles_scanners_move_to_the_callers_requirement(self, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        gate = with_resolvable_reviewer_roles(
            self._gate(["reviewer", "peaches"], {"reviewer": ("a", "b"), "peaches": ("b", "c")}),
            PLATFORM_GITHUB,
        )
        assert gate.scanners_for("peaches") == ("a", "b", "c")
        assert gate.scanners_for("reviewer") == ()

    def test_a_gate_with_nothing_to_map_or_drop_is_returned_unchanged(self, user_config):
        user_config(slugs={"reviewer": "app-r"}, role_callers={"other": "reviewer"})
        gate = self._gate(["reviewer"])
        assert with_resolvable_reviewer_roles(gate, PLATFORM_GITHUB) is gate

    def test_forgejo_a_mapped_role_is_required_under_the_callers_account(self, user_config):
        user_config(role_callers={"reviewer": "peaches"})
        gate = with_resolvable_reviewer_roles(
            self._gate(["reviewer"], {"reviewer": ("a",)}), PLATFORM_FORGEJO
        )
        assert gate.reviewer_roles == ("peaches",)
        assert gate.warnings == ()
        assert gate.scanners_for("peaches") == ("a",)

    def test_forgejo_an_unmapped_role_outside_the_callers_list_degrades_with_the_warning(self, user_config):
        user_config(callers=["peaches"])
        gate = with_resolvable_reviewer_roles(self._gate(["reviewer", "peaches"]), PLATFORM_FORGEJO)
        assert gate.reviewer_roles == ("peaches",)
        assert len(gate.warnings) == 1
        assert "'reviewer'" in gate.warnings[0] and "DROPPED" in gate.warnings[0]

    def test_forgejo_a_listed_caller_role_is_returned_unchanged(self, user_config):
        user_config(callers=["reviewer"])
        gate = self._gate(["reviewer"])
        assert with_resolvable_reviewer_roles(gate, PLATFORM_FORGEJO) is gate

    def test_forgejo_with_no_callers_list_is_returned_unchanged(self, user_config):
        user_config()
        gate = self._gate(["reviewer"])
        assert with_resolvable_reviewer_roles(gate, PLATFORM_FORGEJO) is gate


def _pr_info(repo_path: Path) -> dict:
    return {
        "head": {"sha": HEAD},
        "title": "feat: x",
        "base": {"ref": "main", "sha": git(repo_path, "rev-parse", "main")},
    }


class TestMergeEnforcesAMappedRole:
    """The verb runs on a platform whose bare role always resolves, so the
    declared role `role-reviewer` is made unresolvable the way a GitHub
    deployment without a slug for it is: its direct resolution fails. The
    mapping then comes from a real config file."""

    @pytest.fixture(autouse=True)
    def _role_names_are_not_callers(self, monkeypatch):
        def resolve(name, platform):
            if name.startswith("role-"):
                raise ReviewerLoginNotConfiguredError(f"no GitHub App slug configured for reviewer {name!r}")
            return name

        monkeypatch.setattr("clagentic_loadout.merge.repo_gate_runtime.resolve_reviewer_login", resolve)

    def _merge(self, tmp_path, gate, comments, *, flags=(), capsys):
        init_gate_repo(tmp_path, tracked_gate=gate)
        argv = base_args(**{"--repo-path": str(tmp_path)})
        for flag in flags:
            argv += ["--required-reviewer", flag]
        code = verb.main(
            argv,
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=make_opener(pr_info=_pr_info(tmp_path), comments=comments),
        )
        return code, capsys.readouterr().err

    @staticmethod
    def _verdict(name="peaches", status="clean", state=None):
        return {
            "id": 1,
            "user": {"login": name},
            "body": build_verdict_block(name, status, HEAD, 1, findings_state=state),
        }

    def test_a_missing_verdict_from_the_mapped_caller_refuses(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"}, role_callers={"role-reviewer": "peaches"})
        code, err = self._merge(tmp_path, {"required_reviewer_roles": ["role-reviewer"]}, [], capsys=capsys)
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "peaches" in err
        assert "DROPPED" not in err

    def test_a_clean_verdict_from_the_mapped_caller_merges(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"}, role_callers={"role-reviewer": "peaches"})
        code, err = self._merge(
            tmp_path, {"required_reviewer_roles": ["role-reviewer"]}, [self._verdict()], capsys=capsys
        )
        assert code == verb.EXIT_OK
        assert "'peaches' verdict PASSED" in err

    def test_a_blocking_verdict_from_the_mapped_caller_refuses(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"}, role_callers={"role-reviewer": "peaches"})
        code, _ = self._merge(
            tmp_path,
            {"required_reviewer_roles": ["role-reviewer"]},
            [self._verdict(status="blocking")],
            capsys=capsys,
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_role_and_an_equivalent_flag_are_one_requirement(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"}, role_callers={"role-reviewer": "peaches"})
        code, err = self._merge(
            tmp_path,
            {"required_reviewer_roles": ["role-reviewer"]},
            [self._verdict()],
            flags=["peaches"],
            capsys=capsys,
        )
        assert code == verb.EXIT_OK
        assert err.count("'peaches' verdict PASSED") == 1

    def test_an_unmapped_role_degrades_exactly_as_before(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"})
        code, err = self._merge(tmp_path, {"required_reviewer_roles": ["role-reviewer"]}, [], capsys=capsys)
        assert code == verb.EXIT_OK
        assert "'role-reviewer'" in err and "DROPPED" in err

    def test_the_mapped_roles_required_scanners_are_enforced(self, tmp_path, user_config, capsys):
        user_config(slugs={"peaches": "app-p"}, role_callers={"role-reviewer": "peaches"})
        failed = {"scanners_run": [{"scanner": "alpha", "status": "failed", "reason": "x"}]}
        code, err = self._merge(
            tmp_path,
            {
                "required_reviewer_roles": ["role-reviewer"],
                "required_scanners": {"role-reviewer": ["alpha"]},
            },
            [self._verdict(state=failed)],
            capsys=capsys,
        )
        assert code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "alpha" in err


class TestDoctorReportsResolutionPerRole:
    def _repo(self, tmp_path, roles, remote=GITHUB_REMOTE):
        repo = tmp_path / "repo"
        repo.mkdir()
        git(repo, "init", "-q", "-b", "main")
        git(repo, "remote", "add", "origin", remote)
        gate = repo / TRACKED_GATE_RELATIVE_PATH
        gate.parent.mkdir(parents=True, exist_ok=True)
        gate.write_text(yaml.safe_dump({"merge": {"required_reviewer_roles": roles}}), encoding="utf-8")
        git(repo, "add", "--", TRACKED_GATE_RELATIVE_PATH)
        git(repo, "commit", "-q", "-m", "base")
        return repo

    def test_a_mapped_role_is_reported_resolved_with_its_source(self, tmp_path, user_config):
        user_config(slugs={"peaches": "app-p"}, role_callers={"reviewer": "peaches"})
        result = check_repo_loadout_schema(self._repo(tmp_path, ["reviewer"]))
        assert result.ok is True
        (entry,) = result.resolved["reviewer_role_resolution"]
        assert entry == {
            "role": "reviewer",
            "platform": PLATFORM_GITHUB,
            "resolved": True,
            "requirement": "peaches",
            "login": "app-p[bot]",
            "source": SOURCE_ROLE_CALLERS,
        }
        assert "reviewer -> app-p[bot]" in result.summary
        assert "WARN" not in result.summary

    def test_an_unresolved_role_names_the_mapping_key_that_would_resolve_it(self, tmp_path, user_config):
        user_config(slugs={"peaches": "app-p"})
        result = check_repo_loadout_schema(self._repo(tmp_path, ["reviewer", "peaches"]))
        assert result.ok is False
        by_role = {e["role"]: e for e in result.resolved["reviewer_role_resolution"]}
        assert by_role["peaches"]["resolved"] is True
        assert by_role["reviewer"]["resolved"] is False
        assert by_role["reviewer"]["mapping_key"] == "github_app.role_callers.reviewer"
        assert "set github_app.role_callers.reviewer: <caller>" in result.summary

    def test_a_forgejo_remote_reports_the_bare_role(self, tmp_path, user_config):
        user_config()
        repo = self._repo(tmp_path, ["reviewer"], remote="http://git-host.example.com:3000/o/r.git")
        result = check_repo_loadout_schema(repo)
        assert result.ok is True
        (entry,) = result.resolved["reviewer_role_resolution"]
        assert entry["login"] == "reviewer" and entry["source"] == SOURCE_BARE_NAME
