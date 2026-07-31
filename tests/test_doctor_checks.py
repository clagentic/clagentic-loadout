"""test_doctor_checks.py — coverage for clagentic_loadout.doctor.checks
(lr-e625).

Conformance (repo CLAUDE.md rule 6a): every test in this module runs with a
synthetic/invented config root and role taxonomy, no lore/LORE_* import or
dependency anywhere, and no network dependency -- `check_credentials`'s
probe execution is injected via `probe_runner` rather than exec'd for real.
"""

from __future__ import annotations

import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.doctor.checks import (
    PROBE_CALLER,
    check_attestation_source_configured,
    check_builder_identity_config,
    check_credentials,
    check_github_app_slugs_coverage,
    check_repo_loadout_schema,
)
from clagentic_loadout.transport.attestation import (
    ATTESTATION_CONFIG_SECTION,
    ATTESTED_IDENTITY_ENV_VAR,
    ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR,
)
from clagentic_loadout.push.identity_config import CONFIG_SECTION_BUILDER_IDENTITY
from clagentic_loadout.review.login_config import (
    CONFIG_KEY_REVIEWER_LOGINS,
    CONFIG_SECTION_REVIEW,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.github_app_config import (
    CONFIG_KEY_CALLERS,
    CONFIG_KEY_SLUG,
    CONFIG_KEY_SLUGS,
    CONFIG_SECTION_GITHUB_APP,
    USER_CONFIG_FILENAME as GITHUB_APP_CONFIG_FILENAME,
)
from clagentic_loadout.transport.provider_config import (
    CONFIG_KEY_COMMAND_FORGEJO,
    CONFIG_KEY_COMMAND_GITHUB,
    CONFIG_KEY_PROVIDER_FORGEJO,
    CONFIG_KEY_PROVIDER_GITHUB,
    CONFIG_SECTION_CREDENTIALS,
    USER_CONFIG_FILENAME as PROVIDER_CONFIG_FILENAME,
)

from tests._import_guard import ForbiddenImportFoundError, assert_module_never_imports

_PY = sys.executable


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    """Same host-state-leak backstop test_transport_provider_config.py's own
    autouse fixture applies (lr-a7c2): a doctor check that omits an explicit
    config_root falls through to DEFAULT_USER_CONFIG_ROOT, the REAL
    ~/.config/clagentic/loadout/ directory -- pin every default to an
    isolated per-test directory so a live deployment config on this host can
    never leak into a test's expected result."""
    isolated_root = tmp_path / "isolated-user-config-root"
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", isolated_root)


def _write_provider_config(config_root, mapping: dict) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / PROVIDER_CONFIG_FILENAME).write_text(yaml.safe_dump(mapping))


def _write_github_app_config(config_root, mapping: dict) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / GITHUB_APP_CONFIG_FILENAME).write_text(yaml.safe_dump(mapping))


def _write_loadout_config(repo_root, yaml_text: str) -> None:
    loadout_dir = repo_root / ".clagentic" / "loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def _write_legacy_loadout_config(repo_root, yaml_text: str) -> None:
    loadout_dir = repo_root / ".loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


class TestCheckCredentials:
    def test_default_static_provider_is_ok_with_no_probe(self, tmp_path):
        results = check_credentials(config_root=tmp_path, env={})
        assert len(results) == 2
        for result in results:
            assert result.ok is True
            assert result.resolved["provider_kind"] == "static"

    def test_command_helper_not_found_on_path(self, tmp_path):
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: "definitely-not-a-real-command-lr-e625",
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is False
        assert forgejo_result.resolved["failure_mode"] == "not-found"

    def test_command_helper_not_executable(self, tmp_path):
        helper = tmp_path / "not-executable-helper.sh"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o644)  # no execute bit
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_GITHUB: "command",
                    CONFIG_KEY_COMMAND_GITHUB: str(helper),
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        github_result = next(r for r in results if r.resolved["platform"] == PLATFORM_GITHUB)
        assert github_result.ok is False
        assert github_result.resolved["failure_mode"] == "not-executable"

    def test_command_helper_world_writable(self, tmp_path):
        helper = tmp_path / "world-writable-helper.sh"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o777)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: str(helper),
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is False
        assert forgejo_result.resolved["failure_mode"] == "world-writable"

    def test_command_helper_probe_downstream_refusal_is_ok(self, tmp_path):
        """A correctly configured helper legitimately refuses an
        unrecognized probe caller -- that is the EXPECTED shape, reported
        ok=True with failure_mode='downstream-refusal', never treated as the
        helper being broken (missing mapping vs downstream, per task item 1)."""
        helper = tmp_path / "refusing-helper.sh"
        helper.write_text("#!/bin/sh\necho 'no such caller' >&2\nexit 1\n")
        helper.chmod(0o755)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: str(helper),
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is True
        assert forgejo_result.resolved["failure_mode"] == "downstream-refusal"
        assert forgejo_result.resolved["probe_exit_code"] == 1

    def test_command_helper_probe_never_receives_a_real_role(self, tmp_path):
        """The probe argv's final element is the fixed PROBE_CALLER sentinel
        -- never a real role/caller name from any deployment's own
        taxonomy."""
        helper = tmp_path / "recording-helper.sh"
        helper.write_text("#!/bin/sh\necho \"$1\"\nexit 1\n")
        helper.chmod(0o755)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_GITHUB: "command",
                    CONFIG_KEY_COMMAND_GITHUB: str(helper),
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        github_result = next(r for r in results if r.resolved["platform"] == PLATFORM_GITHUB)
        assert github_result.resolved["configured_command"] == str(helper)
        # The probe ran with PROBE_CALLER, never a real role -- confirmed via
        # the injected probe_runner test below (this test only proves the
        # constant itself is not a plausible real role name).
        assert PROBE_CALLER not in ("builder", "reviewer", "merger", "lead")

    def test_probe_runner_is_invoked_with_probe_caller_appended(self, tmp_path):
        helper = tmp_path / "helper"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o755)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: str(helper),
                }
            },
        )
        captured_argv = []

        def fake_probe_runner(argv):
            captured_argv.append(argv)
            return subprocess.CompletedProcess(argv, returncode=0, stdout=b"fake-token\n", stderr=b"")

        results = check_credentials(config_root=tmp_path, env={}, probe_runner=fake_probe_runner)
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is True
        assert forgejo_result.resolved["failure_mode"] == "ok"
        assert captured_argv == [[str(helper), PROBE_CALLER]]

    def test_probe_timeout_classified_as_probe_timeout(self, tmp_path):
        helper = tmp_path / "helper"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o755)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_GITHUB: "command",
                    CONFIG_KEY_COMMAND_GITHUB: str(helper),
                }
            },
        )

        def timing_out_probe_runner(argv):
            raise subprocess.TimeoutExpired(cmd=argv, timeout=1.0)

        results = check_credentials(config_root=tmp_path, env={}, probe_runner=timing_out_probe_runner)
        github_result = next(r for r in results if r.resolved["platform"] == PLATFORM_GITHUB)
        assert github_result.ok is False
        assert github_result.resolved["failure_mode"] == "probe-timeout"

    def test_invalid_provider_kind_reported_not_raised(self, tmp_path):
        _write_provider_config(
            tmp_path,
            {CONFIG_SECTION_CREDENTIALS: {CONFIG_KEY_PROVIDER_FORGEJO: "not-a-real-kind"}},
        )
        results = check_credentials(config_root=tmp_path, env={})
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is False
        assert "invalid provider config" in forgejo_result.summary

    def test_quoted_command_probed_via_shlex_split_parity(self, tmp_path):
        """lr-b2d1c3 (BOBBIE 13332): a quoted/space-bearing token_command
        must be probed via shlex.split identically to how
        transport.provider_config.resolve_platform_provider splits it for
        the real mint -- a bare whitespace .split() would previously mangle
        an argument containing spaces (e.g. a quoted path)."""
        helper_dir = tmp_path / "helper dir with spaces"
        helper_dir.mkdir()
        helper = helper_dir / "helper.sh"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o755)
        quoted_command = f'"{helper}" --flag "an argument"'
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: quoted_command,
                }
            },
        )
        captured_argv = []

        def fake_probe_runner(argv):
            captured_argv.append(argv)
            return subprocess.CompletedProcess(argv, returncode=0, stdout=b"fake-token\n", stderr=b"")

        results = check_credentials(config_root=tmp_path, env={}, probe_runner=fake_probe_runner)
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is True
        assert forgejo_result.resolved["failure_mode"] == "ok"
        # shlex.split preserves "an argument" as ONE token and resolves the
        # quoted path correctly -- the SAME split provider_config.
        # resolve_platform_provider uses to build the real minting argv.
        assert captured_argv == [[str(helper), "--flag", "an argument", PROBE_CALLER]]

    def test_quoted_command_path_classification_matches_shlex_split(self, tmp_path):
        """The executable-half classification (_classify_command_path, via
        check_credentials) must resolve argv[0] with the SAME shlex.split
        as the probe itself -- a quoted path with spaces must not be
        misclassified as not-found due to whitespace-only splitting."""
        helper_dir = tmp_path / "helper dir with spaces"
        helper_dir.mkdir()
        helper = helper_dir / "helper.sh"
        helper.write_text("#!/bin/sh\necho token\n")
        helper.chmod(0o755)
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_GITHUB: "command",
                    CONFIG_KEY_COMMAND_GITHUB: f'"{helper}"',
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        github_result = next(r for r in results if r.resolved["platform"] == PLATFORM_GITHUB)
        assert github_result.ok is True
        assert github_result.resolved["failure_mode"] == "ok"
        assert github_result.resolved["resolved_path"] == str(helper)

    def test_unbalanced_quote_command_classified_not_found(self, tmp_path):
        """A malformed/unbalanced-quote command string degrades to
        not-found (the same shape the real minting path would also fail to
        parse) rather than raising out of the health check."""
        _write_provider_config(
            tmp_path,
            {
                CONFIG_SECTION_CREDENTIALS: {
                    CONFIG_KEY_PROVIDER_FORGEJO: "command",
                    CONFIG_KEY_COMMAND_FORGEJO: 'unterminated "quote',
                }
            },
        )
        results = check_credentials(config_root=tmp_path, env={})
        forgejo_result = next(r for r in results if r.resolved["platform"] == PLATFORM_FORGEJO)
        assert forgejo_result.ok is False
        assert forgejo_result.resolved["failure_mode"] == "not-found"


class TestCheckGithubAppSlugsCoverage:
    def test_no_slugs_map_configured_is_a_no_op_pass(self, tmp_path):
        result = check_github_app_slugs_coverage(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["slugs_configured"] is False

    def test_global_slug_only_no_slugs_map_is_a_no_op_pass(self, tmp_path):
        _write_github_app_config(
            tmp_path, {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUG: "single-global-app"}}
        )
        result = check_github_app_slugs_coverage(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["slugs_configured"] is False

    def test_full_coverage_passes(self, tmp_path):
        _write_github_app_config(
            tmp_path,
            {
                CONFIG_SECTION_GITHUB_APP: {
                    CONFIG_KEY_SLUGS: {"builder": "app-builder", "reviewer": "app-reviewer"}
                }
            },
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(
            repo_root,
            "roles:\n  builder:\n    - push\n  reviewer:\n    - git-host-api\n",
        )
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["missing_slugs"] == []
        assert result.resolved["orphaned_slugs"] == []

    def test_missing_slug_is_the_lr_e41f_gap(self, tmp_path):
        """The exact lr-e41f incident shape: a declared caller with no
        matching github_app.slugs entry."""
        _write_github_app_config(
            tmp_path,
            {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUGS: {"builder": "app-builder"}}},
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(
            repo_root,
            "roles:\n  builder:\n    - push\n  reviewer:\n    - git-host-api\n",
        )
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is False
        assert result.resolved["missing_slugs"] == ["reviewer"]
        # lr-3160c0: the summary text is user-facing (doctor's stdout
        # report) and must never carry an internal task id -- asserting the
        # id's absence, not its presence, keeps this test aligned with the
        # anonymization guard's own AST check on this same string.
        assert "lr-e41f" not in result.summary
        assert "reviewer" in result.summary

    def test_orphaned_slug_reported_but_not_a_failure(self, tmp_path):
        _write_github_app_config(
            tmp_path,
            {
                CONFIG_SECTION_GITHUB_APP: {
                    CONFIG_KEY_SLUGS: {"builder": "app-builder", "zorbnaut": "app-zorbnaut"}
                }
            },
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(repo_root, "roles:\n  builder:\n    - push\n")
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["orphaned_slugs"] == ["zorbnaut"]

    def test_synthetic_role_taxonomy_no_builtin_names(self, tmp_path):
        """Conformance (rule 6a): entirely invented role names, no
        dependency on the seed builder/reviewer/merger/lead taxonomy."""
        _write_github_app_config(
            tmp_path,
            {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUGS: {"zorbnaut": "app-zorbnaut"}}},
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(repo_root, "roles:\n  zorbnaut:\n    - git-host-api\n")
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True

    def test_agent_name_keyed_deployment_uses_declared_caller_registry(self, tmp_path):
        """lr-46a83a: the exact live-deployment shape. A deployment whose
        token helper (and therefore github_app.slugs) is keyed by AGENT NAME
        (amos/peaches/bobbie/naomi) rather than provisioning.roles' bare
        ROLE-name taxonomy (builder/reviewer/security/merger) declares that
        key-space via github_app.callers -- the check must validate slugs
        coverage against the DECLARED registry, not silently compare against
        the (here, unrelated) role taxonomy and false-positive FAIL on a
        correctly configured deployment."""
        _write_github_app_config(
            tmp_path,
            {
                CONFIG_SECTION_GITHUB_APP: {
                    CONFIG_KEY_CALLERS: ["amos", "peaches", "bobbie", "naomi"],
                    CONFIG_KEY_SLUGS: {
                        "amos": "clagentic-builder",
                        "peaches": "clagentic-reviewer",
                        "bobbie": "clagentic-security",
                        "naomi": "clagentic-merger",
                    },
                }
            },
        )
        # The role taxonomy is a COMPLETELY DIFFERENT key-space here (bare
        # role names) -- if the check fell back to it despite callers being
        # declared, every one of these roles would be "missing" and every
        # slugs entry would be "orphaned". Declaring github_app.callers must
        # prevent that false positive.
        repo_root = tmp_path / "repo"
        _write_loadout_config(
            repo_root,
            "roles:\n  builder:\n    - push\n  reviewer:\n    - git-host-api\n"
            "  security:\n    - git-host-api\n  merger:\n    - merge\n",
        )
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["missing_slugs"] == []
        assert result.resolved["orphaned_slugs"] == []
        assert result.resolved["caller_source"] == "github_app.callers"

    def test_agent_name_keyed_deployment_missing_slug_still_flagged(self, tmp_path):
        """The lr-46a83a holden-reader-slug shape: a declared caller
        (holden, mapped reader in the token helper) with no matching
        github_app.slugs entry is a REAL gap and must still FAIL -- the
        caller-registry fix must not silence real missing-slug findings,
        only stop comparing against the wrong key-space."""
        _write_github_app_config(
            tmp_path,
            {
                CONFIG_SECTION_GITHUB_APP: {
                    CONFIG_KEY_CALLERS: ["amos", "peaches", "bobbie", "naomi", "holden"],
                    CONFIG_KEY_SLUGS: {
                        "amos": "clagentic-builder",
                        "peaches": "clagentic-reviewer",
                        "bobbie": "clagentic-security",
                        "naomi": "clagentic-merger",
                    },
                }
            },
        )
        result = check_github_app_slugs_coverage(config_root=tmp_path)
        assert result.ok is False
        assert result.resolved["missing_slugs"] == ["holden"]
        assert result.resolved["caller_source"] == "github_app.callers"
        # lr-3160c0: summary text is user-facing and must never carry an
        # internal task id -- see the sibling assertion above for why this
        # checks absence rather than presence.
        assert "lr-e41f" not in result.summary
        assert "holden" in result.summary

    def test_no_callers_declared_falls_back_to_role_taxonomy(self, tmp_path):
        """No github_app.callers declared at all -- byte-identical to
        pre-lr-46a83a behavior: falls back to provisioning.roles as the
        reference default."""
        _write_github_app_config(
            tmp_path,
            {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_SLUGS: {"builder": "app-builder"}}},
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(repo_root, "roles:\n  builder:\n    - push\n")
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["caller_source"] == "provisioning.roles (reference default)"

    def test_malformed_callers_entry_falls_back_to_role_taxonomy(self, tmp_path):
        """A malformed github_app.callers value (non-string entry) is
        treated as not-configured -- degrade-to-default, mirroring every
        other read in this module, never a partial/best-effort caller
        list."""
        _write_github_app_config(
            tmp_path,
            {
                CONFIG_SECTION_GITHUB_APP: {
                    CONFIG_KEY_CALLERS: ["amos", 42],
                    CONFIG_KEY_SLUGS: {"builder": "app-builder"},
                }
            },
        )
        repo_root = tmp_path / "repo"
        _write_loadout_config(repo_root, "roles:\n  builder:\n    - push\n")
        result = check_github_app_slugs_coverage(repo_root=repo_root, config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["caller_source"] == "provisioning.roles (reference default)"


class TestCheckRepoLoadoutSchema:
    def test_missing_config_file_is_ok(self, tmp_path):
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["exists"] is False

    def test_valid_config_passes(self, tmp_path):
        _write_loadout_config(
            tmp_path,
            "roles:\n  builder:\n    - push\nwait:\n  scoped_test_patterns:\n    - '^go test'\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["errors"] == []

    def test_malformed_roles_section_fails(self, tmp_path):
        _write_loadout_config(tmp_path, "roles:\n  builder: not-a-list\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("roles:" in err for err in result.resolved["errors"])

    def test_malformed_wait_pattern_fails(self, tmp_path):
        _write_loadout_config(tmp_path, "wait:\n  scoped_test_patterns:\n    - '['\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("wait:" in err for err in result.resolved["errors"])

    def test_malformed_post_merge_steps_fails(self, tmp_path):
        _write_loadout_config(tmp_path, "merge:\n  post_merge_steps:\n    - {}\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("merge:" in err for err in result.resolved["errors"])

    def test_repo_local_credentials_section_flagged(self, tmp_path):
        _write_loadout_config(
            tmp_path, "credentials:\n  token_provider_forgejo: command\n"
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("credentials" in err for err in result.resolved["errors"])

    def test_unknown_section_reported_but_not_a_failure(self, tmp_path):
        _write_loadout_config(tmp_path, "some_future_verb:\n  key: value\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["unknown_sections"] == ["some_future_verb"]

    def test_unreadable_yaml_reported_not_raised(self, tmp_path):
        _write_loadout_config(tmp_path, "not: valid: yaml: [")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert "could not be read as YAML" in result.summary

    def test_legacy_dir_with_no_config_is_warn_not_failure(self, tmp_path):
        """lr-446c35: a bare legacy .loadout/ marker dir with no config.yaml
        inside it (e.g. only used as release.detector's ownership marker) is
        a migration-incomplete WARN, never a schema failure."""
        (tmp_path / ".loadout").mkdir()
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["legacy_dir_present"] is True
        assert "WARN" in result.summary
        assert ".loadout" in result.summary

    def test_no_legacy_dir_no_warn(self, tmp_path):
        result = check_repo_loadout_schema(tmp_path)
        assert result.resolved["legacy_dir_present"] is False
        assert "WARN" not in result.summary

    def test_legacy_config_yaml_is_read_and_flagged_as_warn(self, tmp_path):
        """lr-446c35: a repo whose ONLY config.yaml is at the legacy path is
        still validated (through the same fallback every section-owning
        loader uses) -- ok=True on a valid config, but flagged as using the
        legacy path in both the summary and resolved values."""
        _write_legacy_loadout_config(
            tmp_path, "roles:\n  builder:\n    - push\n"
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["using_legacy_path"] is True
        assert result.resolved["legacy_dir_present"] is True
        assert "WARN" in result.summary
        assert ".loadout/config.yaml" in result.summary

    def test_new_path_wins_and_no_warn_when_both_present(self, tmp_path):
        _write_legacy_loadout_config(tmp_path, "roles:\n  legacy-role:\n    - push\n")
        _write_loadout_config(tmp_path, "roles:\n  builder:\n    - push\n")

        result = check_repo_loadout_schema(tmp_path)

        assert result.ok is True
        assert result.resolved["using_legacy_path"] is False
        assert "WARN" not in result.summary

    def test_valid_pre_checks_and_gate_declaration_config_passes(self, tmp_path):
        """lr-0a03c3: merge.pre_checks / merge_requirements /
        required_reviewer_roles / authorized_roles all live in the SAME
        merge: section post_merge_steps already occupies -- a repo declaring
        all of them together must validate cleanly through the same
        loaders the real consumers use."""
        _write_loadout_config(
            tmp_path,
            "merge:\n"
            "  pre_checks:\n"
            "    - cmd: make lint\n"
            "  post_merge_steps:\n"
            "    - cmd: scripts/install.sh\n"
            "  merge_requirements:\n"
            "    tests_pass: true\n"
            "    ci_pass: true\n"
            "    max_changed_files: 30\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["errors"] == []

    def test_malformed_pre_checks_fails(self, tmp_path):
        _write_loadout_config(tmp_path, "merge:\n  pre_checks:\n    - {}\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("merge.pre_checks:" in err for err in result.resolved["errors"])

    def test_malformed_merge_requirements_fails(self, tmp_path):
        _write_loadout_config(
            tmp_path, "merge:\n  merge_requirements:\n    tests_pass: not-a-bool\n"
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("merge (gate declaration):" in err for err in result.resolved["errors"])

    def test_malformed_required_reviewer_roles_fails(self, tmp_path):
        _write_loadout_config(
            tmp_path, "merge:\n  required_reviewer_roles: not-a-list\n"
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("merge (gate declaration):" in err for err in result.resolved["errors"])

    def test_malformed_authorized_roles_fails(self, tmp_path):
        _write_loadout_config(tmp_path, "merge:\n  authorized_roles:\n    - ''\n")
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any("merge (gate declaration):" in err for err in result.resolved["errors"])

    def test_merge_section_present_omitting_required_reviewer_roles_fails(self, tmp_path):
        """lr-638945: a merge: section that has already opted into repo-tier
        gate config (post_merge_steps declared here) but omits
        required_reviewer_roles entirely is a schema FAIL -- an ambiguous
        gate reads as protection but isn't one."""
        _write_loadout_config(
            tmp_path, "merge:\n  post_merge_steps:\n    - cmd: scripts/install.sh\n"
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert any(
            "required_reviewer_roles" in err and "merge (gate declaration):" in err
            for err in result.resolved["errors"]
        )

    def test_explicit_empty_required_reviewer_roles_passes(self, tmp_path):
        """The explicit-[] opt-out is a valid, deliberate declaration --
        schema passes cleanly."""
        _write_loadout_config(
            tmp_path,
            "merge:\n"
            "  post_merge_steps:\n"
            "    - cmd: scripts/install.sh\n"
            "  required_reviewer_roles: []\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["errors"] == []

    def test_unsatisfiable_gate_role_fails_when_roles_section_exists(self, tmp_path):
        """lr-638945 comment #1: the LIVE clagentic-github incident, verbatim
        shape. roles: is DECLARED (builder/reviewer only, no security) and
        merge.required_reviewer_roles names 'security' -- security has no
        verb set and can never emit a verdict. The config AS WRITTEN can
        never satisfy its own gate, so this is a hard FAIL, not a WARN."""
        _write_loadout_config(
            tmp_path,
            "roles:\n"
            "  builder:\n"
            "    - push\n"
            "  reviewer:\n"
            "    - git-host-api\n"
            "  merger:\n"
            "    - merge\n"
            "  lead:\n"
            "    - git-host-api\n"
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "    - security\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert result.resolved["unsatisfiable_gate_roles"] == ["security"]
        assert result.resolved["unknown_gate_roles"] == []
        assert any(
            "security" in err and "unsatisfiable" in err
            for err in result.resolved["errors"]
        )
        # The error must be self-remediating: name the file, the offending
        # role, why it's unsatisfiable, and the concrete fix options.
        assert any(str(tmp_path) in err for err in result.resolved["errors"])
        assert any("roles" in err and "verb set" in err for err in result.resolved["errors"])

    def test_unsatisfiable_gate_role_in_authorized_roles_also_fails(self, tmp_path):
        _write_loadout_config(
            tmp_path,
            "roles:\n"
            "  builder:\n"
            "    - push\n"
            "merge:\n"
            "  required_reviewer_roles: []\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        assert result.resolved["unsatisfiable_gate_roles"] == ["merger"]
        assert any("merger" in err for err in result.resolved["errors"])

    def test_gate_roles_matching_declared_roles_passes_cleanly(self, tmp_path):
        _write_loadout_config(
            tmp_path,
            "roles:\n"
            "  reviewer:\n"
            "    - git-host-api\n"
            "  merger:\n"
            "    - merge\n"
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["unknown_gate_roles"] == []
        assert result.resolved["unsatisfiable_gate_roles"] == []

    def test_gate_role_outside_default_role_verbs_warns_when_roles_absent(self, tmp_path):
        """No roles: section declared at all -- the cross-check falls back
        to DEFAULT_ROLE_VERBS, a REFERENCE default, not this repo's own
        declaration. A gate role outside that reference set is not provably
        unsatisfiable (the deployment may resolve roles elsewhere), so this
        stays a WARN (ok remains True) rather than escalating to FAIL."""
        _write_loadout_config(
            tmp_path,
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "    - security\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["unknown_gate_roles"] == ["security"]
        assert result.resolved["unsatisfiable_gate_roles"] == []
        assert "WARN" in result.summary
        assert "security" in result.summary

    def test_gate_roles_fall_back_to_default_role_verbs_when_roles_absent(self, tmp_path):
        """No roles: section declared -- the cross-check falls back to
        DEFAULT_ROLE_VERBS (builder/reviewer/merger/lead), mirroring
        load_role_verbs' own no-repo-config default."""
        _write_loadout_config(
            tmp_path,
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "  authorized_roles:\n"
            "    - merger\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is True
        assert result.resolved["unknown_gate_roles"] == []
        assert result.resolved["unsatisfiable_gate_roles"] == []


class TestCheckBuilderIdentityConfig:
    def test_no_config_at_all_is_ok_no_op(self, tmp_path):
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["builder_identity_configured"] is False
        assert result.resolved["reviewer_logins_configured"] is False

    def test_well_formed_builder_identity_is_ok(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_BUILDER_IDENTITY: {
                        "name": "clagentic-builder[bot]",
                        "email": "bot@example.com",
                    }
                }
            )
        )
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["builder_identity_configured"] is True

    def test_malformed_builder_identity_fails(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({CONFIG_SECTION_BUILDER_IDENTITY: {"name": "bot"}})
        )
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is False
        assert any("builder_identity:" in err for err in result.resolved["errors"])

    def test_well_formed_reviewer_logins_is_ok(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {CONFIG_SECTION_REVIEW: {CONFIG_KEY_REVIEWER_LOGINS: {"reviewer": "rev-bot"}}}
            )
        )
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["reviewer_logins_configured"] is True
        assert result.resolved["reviewer_logins_roles"] == ["reviewer"]

    def test_malformed_reviewer_logins_fails(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({CONFIG_SECTION_REVIEW: {CONFIG_KEY_REVIEWER_LOGINS: ["rev-bot"]}})
        )
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is False
        assert any(CONFIG_KEY_REVIEWER_LOGINS in err for err in result.resolved["errors"])

    def test_both_sections_configured_together(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_BUILDER_IDENTITY: {"name": "bot", "email": "bot@example.com"},
                    CONFIG_SECTION_REVIEW: {CONFIG_KEY_REVIEWER_LOGINS: {"security": "sec-bot"}},
                }
            )
        )
        result = check_builder_identity_config(config_root=tmp_path)
        assert result.ok is True
        assert result.resolved["builder_identity_configured"] is True
        assert result.resolved["reviewer_logins_configured"] is True

    def test_no_repo_local_tier(self):
        import inspect

        sig = inspect.signature(check_builder_identity_config)
        assert "repo_root" not in sig.parameters


class TestCheckAttestationSourceConfigured:
    """Coverage for check_attestation_source_configured (lr-8e1593,
    revives the one legitimate hardening from lr-424519): WARN when
    github_app.callers is declared but no attestation source at all is
    configured."""

    def test_no_callers_declared_is_ok_noop(self, tmp_path):
        result = check_attestation_source_configured(config_root=tmp_path, env={})
        assert result.ok is True
        assert result.resolved["callers_declared"] is False

    def test_callers_declared_no_attestation_source_warns(self, tmp_path):
        _write_github_app_config(tmp_path, {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["builder"]}})
        result = check_attestation_source_configured(config_root=tmp_path, env={})
        # WARN, not a hard failure -- ok stays True, but the summary/resolved
        # surface the gap.
        assert result.ok is True
        assert "WARN" in result.summary
        assert result.resolved["callers_declared"] is True
        assert result.resolved["configured_env_source"] is False
        assert result.resolved["sidecar_single_path_source"] is False
        assert result.resolved["sidecar_adapter_list_source"] is False

    def test_callers_declared_with_configured_env_var_passes_clean(self, tmp_path):
        _write_github_app_config(tmp_path, {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["builder"]}})
        env = {ATTESTED_IDENTITY_ENV_VAR: "SOME_ENV_VAR"}
        result = check_attestation_source_configured(config_root=tmp_path, env=env)
        assert result.ok is True
        assert "WARN" not in result.summary
        assert result.resolved["configured_env_source"] is True

    def test_callers_declared_with_sidecar_single_path_env_passes_clean(self, tmp_path):
        _write_github_app_config(tmp_path, {CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["builder"]}})
        env = {ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR: "/some/path"}
        result = check_attestation_source_configured(config_root=tmp_path, env=env)
        assert result.ok is True
        assert "WARN" not in result.summary
        assert result.resolved["sidecar_single_path_source"] is True

    def test_callers_declared_with_sidecars_adapter_list_passes_clean(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / GITHUB_APP_CONFIG_FILENAME).write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["builder"]},
                    ATTESTATION_CONFIG_SECTION: {
                        "sidecars": [
                            {"dir": "/tmp", "file_prefix": "spawn-", "session_id_env": "SPAWN_ID"}
                        ]
                    },
                }
            )
        )
        result = check_attestation_source_configured(config_root=tmp_path, env={})
        assert result.ok is True
        assert "WARN" not in result.summary
        assert result.resolved["sidecar_adapter_list_source"] is True

    def test_callers_declared_with_empty_sidecars_list_still_warns(self, tmp_path):
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / GITHUB_APP_CONFIG_FILENAME).write_text(
            yaml.safe_dump(
                {
                    CONFIG_SECTION_GITHUB_APP: {CONFIG_KEY_CALLERS: ["builder"]},
                    ATTESTATION_CONFIG_SECTION: {"sidecars": []},
                }
            )
        )
        result = check_attestation_source_configured(config_root=tmp_path, env={})
        assert result.ok is True
        assert "WARN" in result.summary


class TestUnsatisfiableGateIsDiagnosticOnlyNotABootstrapTrap:
    """lr-638945, operator bootstrap-safety directive: an unsatisfiable-gate
    (or ambiguous-gate) config must produce a loadout-doctor DIAGNOSTIC only
    -- it must never become a reason loadout-push/loadout-merge themselves
    refuse to run, because that would block the exact operation (push a
    corrected config, land it) needed to fix the config doctor is
    complaining about. This is the same bricked-repo shape a credential-
    guard/repo-name refusal produced elsewhere before that gate was
    corrected to admit it; this suite locks the equivalent property here so
    a future refactor wiring this validation into a write/merge path cannot
    silently recreate it."""

    def test_unsatisfiable_gate_is_diagnostic_only_not_a_merge_blocker(self, tmp_path):
        """The exact live-incident config shape (comment #1: security
        required but not declared in roles:) is a hard doctor FAIL ..."""
        _write_loadout_config(
            tmp_path,
            "roles:\n"
            "  builder:\n"
            "    - push\n"
            "  reviewer:\n"
            "    - git-host-api\n"
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - reviewer\n"
            "    - security\n",
        )
        doctor_result = check_repo_loadout_schema(tmp_path)
        assert doctor_result.ok is False

        # ... but the SAME underlying loaders that produced that FAIL must
        # not raise when called the way a write/merge-path caller would --
        # today, no such caller exists (merge.verb/push.verb do not import
        # merge.gate_config at all, confirmed by
        # test_merge_verb_and_push_verb_never_import_gate_config below), so
        # this asserts the diagnostic-only property directly at the loader
        # level: load_required_reviewer_roles/load_authorized_roles/
        # load_role_verbs (the exact three loaders check_repo_loadout_schema
        # composes) must each still return a normal value for this same
        # config, not raise -- the FAIL is check_repo_loadout_schema's own
        # cross-check finding, layered ON TOP of loaders that keep working.
        from clagentic_loadout.merge.gate_config import (
            load_authorized_roles,
            load_required_reviewer_roles,
        )
        from clagentic_loadout.provisioning.roles import load_role_verbs

        assert load_required_reviewer_roles(tmp_path) == ("reviewer", "security")
        assert load_authorized_roles(tmp_path) == ()
        assert "security" not in load_role_verbs(tmp_path)

    def test_merge_verb_and_push_verb_never_import_gate_config(self):
        """Static guarantee: merge.gate_config's required_reviewer_roles/
        authorized_roles loaders (and therefore any error they raise) are
        never reachable from merge.verb or push.verb's module -- the two
        modules that actually push/merge. A future change that imports
        merge.gate_config into either module is exactly the kind of change
        this test exists to force a deliberate look at (see gate_config.py's
        own "BLAST RADIUS" docstring section).

        lr-3f1851: this is an AST-level source check
        (tests._import_guard.assert_module_never_imports), not a
        `vars(module)` symbol-table inspection -- the prior version only
        caught `from ... import gate_config`-shaped imports (which bind a
        name in the checked module's namespace); it would NOT have caught a
        qualified-submodule import (`import clagentic_loadout.merge.
        gate_config`, referenced later via the dotted attribute chain),
        which binds only the top-level `clagentic_loadout` package name and
        leaves `vars(module)` with nothing gate_config-shaped to find. See
        test_qualified_submodule_import_shape_is_still_caught below for the
        regression this closes, and tests/_import_guard.py's own module
        docstring for the full rationale."""
        import clagentic_loadout.merge.verb as merge_verb_module
        import clagentic_loadout.push.verb as push_verb_module

        for module in (merge_verb_module, push_verb_module):
            assert_module_never_imports(module, "clagentic_loadout.merge.gate_config")

    def test_qualified_submodule_import_shape_is_still_caught(self, tmp_path):
        """The exact gap lr-3f1851 closes: a synthetic module using the
        qualified-submodule import shape (`import a.b.c`, referenced via the
        dotted attribute chain) binds only the top-level package name in its
        own namespace -- a `vars(module)` inspection has nothing
        gate_config-shaped to find here, but the AST-level check must still
        catch it, since the import statement itself is present regardless of
        what name ends up bound at runtime."""
        synthetic_module_path = tmp_path / "synthetic_qualified_importer.py"
        synthetic_module_path.write_text(
            "import clagentic_loadout.merge.gate_config\n"
            "\n"
            "def call_it(repo_root):\n"
            "    return clagentic_loadout.merge.gate_config."
            "load_required_reviewer_roles(repo_root)\n"
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "synthetic_qualified_importer", synthetic_module_path
        )
        synthetic_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(synthetic_module)

        # The exact symbol-table gap: no gate_config-shaped name is bound.
        assert "gate_config" not in vars(synthetic_module)
        assert "load_required_reviewer_roles" not in vars(synthetic_module)

        with pytest.raises(ForbiddenImportFoundError, match="BOOTSTRAP SAFETY"):
            assert_module_never_imports(
                synthetic_module, "clagentic_loadout.merge.gate_config"
            )

    def test_module_with_no_forbidden_import_passes_cleanly(self, tmp_path):
        """A module that genuinely does not import the forbidden module
        (even one that merely NAMES it in a comment/docstring -- the false-
        positive a naive substring/grep scan would produce, since
        merge/verb.py legitimately names 'gate_config' in prose today) must
        pass the AST-level check cleanly."""
        synthetic_module_path = tmp_path / "synthetic_clean_module.py"
        synthetic_module_path.write_text(
            "# this module talks about gate_config in a comment only\n"
            '"""gate_config is mentioned here too, in a docstring."""\n'
            "import os\n"
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "synthetic_clean_module", synthetic_module_path
        )
        synthetic_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(synthetic_module)

        assert_module_never_imports(
            synthetic_module, "clagentic_loadout.merge.gate_config"
        )

    def test_from_import_shape_is_caught(self, tmp_path):
        """`from clagentic_loadout.merge import gate_config` -- the shape
        the ORIGINAL vars(module) check already caught -- must still be
        caught by the AST-level replacement (no regression on the shapes
        that already worked)."""
        synthetic_module_path = tmp_path / "synthetic_from_importer.py"
        synthetic_module_path.write_text(
            "from clagentic_loadout.merge import gate_config\n"
        )
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "synthetic_from_importer", synthetic_module_path
        )
        synthetic_module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(synthetic_module)

        with pytest.raises(ForbiddenImportFoundError):
            assert_module_never_imports(
                synthetic_module, "clagentic_loadout.merge.gate_config"
            )

    def test_required_reviewer_roles_not_declared_error_has_self_remediating_message(
        self, tmp_path
    ):
        """The error itself, not just the doctor summary wrapping it, must
        name the config path and both remediation options -- an operator
        reading only the raised exception (e.g. from a raw script, not
        loadout-doctor's own formatting) must be able to fix it without
        cross-referencing docs."""
        from clagentic_loadout.merge.gate_config import (
            RequiredReviewerRolesNotDeclaredError,
            load_required_reviewer_roles,
        )

        _write_loadout_config(tmp_path, "merge:\n  authorized_roles:\n    - merger\n")
        with pytest.raises(RequiredReviewerRolesNotDeclaredError) as exc_info:
            load_required_reviewer_roles(tmp_path)
        message = str(exc_info.value)
        assert str(tmp_path) in message
        assert "required_reviewer_roles: []" in message
        assert "Declare the" in message

    def test_unsatisfiable_gate_doctor_error_names_path_role_and_remediation(self, tmp_path):
        """The doctor-level FAIL message for an unsatisfiable gate role must
        be self-remediating: name the config path, the offending role, WHY
        it's unsatisfiable, and the three concrete fixes."""
        _write_loadout_config(
            tmp_path,
            "roles:\n"
            "  builder:\n"
            "    - push\n"
            "merge:\n"
            "  required_reviewer_roles:\n"
            "    - security\n",
        )
        result = check_repo_loadout_schema(tmp_path)
        assert result.ok is False
        (error_message,) = [
            err for err in result.resolved["errors"] if "security" in err
        ]
        assert str(tmp_path) in error_message
        assert "verb set" in error_message
        assert "'roles'" in error_message
        assert "(1) add" in error_message
        assert "(2) remove" in error_message
        assert "(3)" in error_message and "[]" in error_message
