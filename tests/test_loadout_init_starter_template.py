"""test_loadout_init_starter_template.py -- unit tests for
clagentic_loadout.loadout_init.starter_template (lr-c21507).

Covers:
  - starter_template_path resolves the packaged template file.
  - copy_starter_template lands a byte-identical copy at the repo's
    canonical .clagentic/loadout/config.yaml path, creating parent dirs.
  - copy_starter_template refuses to clobber an existing target without
    force=True, and does clobber with force=True.
  - target_config_path is always the CANONICAL new path, never redirected
    onto a legacy .loadout/config.yaml even when one already exists (unlike
    repo_config.resolve_repo_config_path's own read-side legacy fallback).
  - the packaged template itself: valid YAML, no deployment values (no real
    HOME=/git-host-base-url literal, no agent names), and every section it
    declares round-trips cleanly through the SAME section-owning loaders
    doctor.checks.check_repo_loadout_schema already validates against (so
    the template a repo copies is never itself invalid against its own
    schema).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from clagentic_loadout.loadout_init.starter_template import (
    StarterTemplateError,
    copy_starter_template,
    starter_template_path,
    target_config_path,
)
from clagentic_loadout.merge.gate_config import (
    load_authorized_roles,
    load_merge_requirements,
    load_required_reviewer_roles,
)
from clagentic_loadout.merge.post_merge_config import load_post_merge_steps
from clagentic_loadout.provisioning.roles import load_role_verbs
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH


class TestStarterTemplatePath:
    def test_resolves_the_packaged_file(self):
        path = starter_template_path()
        assert path.is_file()
        assert path.name == "starter_config.yaml"


class TestCopyStarterTemplate:
    def test_lands_at_the_canonical_new_path(self, tmp_path):
        result = copy_starter_template(tmp_path)

        assert result == tmp_path / DEFAULT_CONFIG_RELATIVE_PATH
        assert result.is_file()

    def test_copy_is_byte_identical_to_the_source(self, tmp_path):
        result = copy_starter_template(tmp_path)

        assert result.read_text(encoding="utf-8") == starter_template_path().read_text(
            encoding="utf-8"
        )

    def test_creates_parent_directories(self, tmp_path):
        assert not (tmp_path / ".clagentic").exists()

        copy_starter_template(tmp_path)

        assert (tmp_path / ".clagentic" / "loadout").is_dir()

    def test_refuses_to_overwrite_existing_target(self, tmp_path):
        target_dir = tmp_path / ".clagentic" / "loadout"
        target_dir.mkdir(parents=True)
        existing = target_dir / "config.yaml"
        existing.write_text("roles: {custom: [push]}\n", encoding="utf-8")

        with pytest.raises(StarterTemplateError) as exc_info:
            copy_starter_template(tmp_path)

        assert str(existing) in str(exc_info.value)
        assert existing.read_text(encoding="utf-8") == "roles: {custom: [push]}\n"

    def test_force_true_overwrites_existing_target(self, tmp_path):
        target_dir = tmp_path / ".clagentic" / "loadout"
        target_dir.mkdir(parents=True)
        existing = target_dir / "config.yaml"
        existing.write_text("roles: {custom: [push]}\n", encoding="utf-8")

        result = copy_starter_template(tmp_path, force=True)

        assert result.read_text(encoding="utf-8") == starter_template_path().read_text(
            encoding="utf-8"
        )

    def test_never_written_to_a_legacy_loadout_path_even_when_legacy_exists(self, tmp_path):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("wait: {}\n", encoding="utf-8")

        result = copy_starter_template(tmp_path)

        assert result == tmp_path / DEFAULT_CONFIG_RELATIVE_PATH
        # The legacy file is untouched -- /loadout-init never mutates it.
        assert (legacy_dir / "config.yaml").read_text(encoding="utf-8") == "wait: {}\n"


class TestTargetConfigPath:
    def test_is_repo_root_joined_with_the_default_relative_path(self, tmp_path):
        assert target_config_path(tmp_path) == tmp_path / DEFAULT_CONFIG_RELATIVE_PATH

    def test_honors_a_config_relative_path_override(self, tmp_path):
        result = target_config_path(tmp_path, config_relative_path="custom/config.yaml")
        assert result == tmp_path / "custom" / "config.yaml"


class TestPackagedTemplateContent:
    """The template itself must be clean (rule 1) and schema-valid against
    every real section-owning loader -- never a doctor-only fiction."""

    @pytest.fixture()
    def initialized_repo(self, tmp_path) -> Path:
        copy_starter_template(tmp_path)
        return tmp_path

    def test_is_valid_yaml_mapping(self):
        raw = yaml.safe_load(starter_template_path().read_text(encoding="utf-8"))
        assert isinstance(raw, dict)

    def test_carries_no_real_deployment_values(self):
        text = starter_template_path().read_text(encoding="utf-8")
        assert "HOME=/" not in text
        assert "forgejo.akuehner.com" not in text
        assert "http://" not in text and "https://" not in text

    def test_roles_section_loads_through_the_real_loader(self, initialized_repo):
        role_verbs = load_role_verbs(initialized_repo)
        assert "builder" in role_verbs
        assert "reviewer" in role_verbs
        assert "security" in role_verbs
        assert "merger" in role_verbs
        assert "lead" in role_verbs

    def test_merge_requirements_load_through_the_real_loader(self, initialized_repo):
        requirements = load_merge_requirements(initialized_repo)
        assert requirements["ci_pass"] is True
        assert requirements["tests_pass"] is True

    def test_required_reviewer_roles_load_through_the_real_loader(self, initialized_repo):
        assert load_required_reviewer_roles(initialized_repo) == ("reviewer", "security")

    def test_authorized_roles_load_through_the_real_loader(self, initialized_repo):
        assert load_authorized_roles(initialized_repo) == ("merger",)

    def test_no_post_merge_steps_declared_by_default(self, initialized_repo):
        # post_merge_steps is commented out in the template -- a fresh
        # /loadout-init has none until the guided elicitation adds real,
        # per-deployment values.
        assert load_post_merge_steps(initialized_repo) == []

    def test_template_gate_roles_are_all_satisfiable_by_its_own_roles_section(
        self, initialized_repo
    ):
        """lr-638945 root-cause regression: the shipped template must never
        again reproduce the live clagentic-github incident (comment #1) --
        every role named in required_reviewer_roles/authorized_roles must
        have a real verb set declared under this SAME template's roles:
        section. Locks the property doctor's own unsatisfiable-gate FAIL
        checks for, directly against the shipped template rather than a
        synthetic config, so a future edit that adds a gate role without
        adding its roles: entry (or vice versa) fails HERE first."""
        role_verbs = load_role_verbs(initialized_repo)
        required_reviewer_roles = load_required_reviewer_roles(initialized_repo)
        authorized_roles = load_authorized_roles(initialized_repo)
        for role in (*required_reviewer_roles, *authorized_roles):
            assert role in role_verbs, (
                f"template gate role {role!r} has no roles: entry -- this is "
                f"exactly the lr-638945 unsatisfiable-gate shape (comment #1)"
            )

    def test_template_declares_a_security_role_lr_638945(self, initialized_repo):
        """Root-cause regression (lr-638945 comment #1, ROOT CONTRIBUTOR):
        the starter template previously shipped roles: builder/reviewer/
        merger/lead with NO security role, while the /loadout-init skill's
        own step 2b guidance tells the operator to confirm reviewer AND
        security roles -- following the skill against the shipped template
        naturally produced an unsatisfiable gate (the live clagentic-github
        incident). This locks the fix: security must stay declared with a
        real, non-empty verb set (mirroring clagentic-console's reference
        shape, git-host-api + review-post) so a future edit cannot silently
        drop it back out."""
        role_verbs = load_role_verbs(initialized_repo)
        assert "security" in role_verbs
        assert len(role_verbs["security"]) > 0
        assert "git-host-api" in role_verbs["security"]
