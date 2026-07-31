"""test_guard_infra_ops.py — INFRA (host-operator) Bash-command allow-checker
(lr-6f61aa, sub-epic lr-19ae42 sub-slice SE4, epic lr-5a8d Wave C — THIS
SLICE COMPLETES lr-19ae42).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, hosts, and
values only -- no real agent names, no LORE present, no real machine
identifiers.

Tests move with their subjects (CLAUDE.md rule 6) -- this file covers
`guard.infra_ops` directly (`InfraOpWrapper`, `InfraOpsConfig`,
`check_infra_op_wrapper`, `is_infra_params_file_path_contained`).
`role_allowlist.py`'s thin `BashRole.INFRA`/`check_infra_command`/
`check_bash_call` wiring is covered in test_guard_role_allowlist_se4.py.
"""

from __future__ import annotations

import os

import pytest

from clagentic_loadout.guard.infra_ops import (
    InfraOpsConfig,
    InfraOpWrapper,
    check_infra_op_wrapper,
    is_infra_params_file_path_contained,
)


def _install_binary_wrapper() -> InfraOpWrapper:
    return InfraOpWrapper(
        verb_name="infra-install-binary",
        required_flags=("--host", "--package", "--version"),
    )


def _rotate_token_wrapper() -> InfraOpWrapper:
    return InfraOpWrapper(
        verb_name="infra-rotate-token",
        required_flags=("--service", "--token-name"),
    )


def _run_scoped_command_json_wrapper() -> InfraOpWrapper:
    return InfraOpWrapper(
        verb_name="infra-run-scoped-command",
        required_flags=("--host", "--template-id", "--template-params-json"),
        template_params_json_flag="--template-params-json",
        admits_ack_prefix=True,
    )


def _run_scoped_command_file_wrapper() -> InfraOpWrapper:
    return InfraOpWrapper(
        verb_name="infra-run-scoped-command",
        required_flags=("--host", "--template-id", "--template-params-file"),
        template_params_file_flag="--template-params-file",
        admits_ack_prefix=True,
    )


# ---------------------------------------------------------------------------
# InfraOpWrapper construction guards.
# ---------------------------------------------------------------------------


class TestInfraOpWrapperConstruction:
    def test_json_and_file_flags_mutually_exclusive(self):
        with pytest.raises(ValueError):
            InfraOpWrapper(
                verb_name="infra-run-scoped-command",
                required_flags=("--host", "--template-params-json"),
                template_params_json_flag="--template-params-json",
                template_params_file_flag="--template-params-file",
            )


# ---------------------------------------------------------------------------
# Op-wrapper admission — exact flag-shape grants.
# ---------------------------------------------------------------------------


class TestCheckInfraOpWrapperBasicGrants:
    def _config(self) -> InfraOpsConfig:
        return InfraOpsConfig(op_wrappers=(_install_binary_wrapper(), _rotate_token_wrapper()))

    def test_install_binary_admitted(self):
        ok, reason = check_infra_op_wrapper(
            "infra-install-binary --host host.example.com --package widget --version 1.2.3",
            config=self._config(),
        )
        assert ok is True, reason

    def test_rotate_token_admitted(self):
        ok, reason = check_infra_op_wrapper(
            "infra-rotate-token --service auth --token-name deploy-key",
            config=self._config(),
        )
        assert ok is True, reason

    def test_wrong_verb_denied(self):
        ok, reason = check_infra_op_wrapper(
            "infra-restart-service --host h --service-unit u",
            config=self._config(),
        )
        assert ok is False

    def test_missing_required_flag_denied(self):
        ok, reason = check_infra_op_wrapper(
            "infra-install-binary --host host.example.com --package widget",
            config=self._config(),
        )
        assert ok is False

    def test_extra_trailing_content_denied_whole_string_anchor(self):
        # The reference's own hard constraint: no extra flag/positional arg
        # can be appended past the last required value (whole-string $
        # anchor) -- this is the structural property that makes the
        # SE1/SE2 bare-verb ANSI-C hard-deny gate inapplicable here (see
        # check_infra_op_wrapper's own "MANDATORY ANSI-C ANALYSIS").
        ok, reason = check_infra_op_wrapper(
            "infra-install-binary --host h --package p --version 1 --extra-flag x",
            config=self._config(),
        )
        assert ok is False

    def test_extra_leading_content_denied_whole_string_anchor(self):
        ok, reason = check_infra_op_wrapper(
            "echo pwned; infra-install-binary --host h --package p --version 1",
            config=self._config(),
        )
        assert ok is False

    def test_value_with_whitespace_denied(self):
        # _INFRA_VALUE excludes whitespace -- a caller cannot smuggle a
        # multi-word value into a single flag slot.
        ok, reason = check_infra_op_wrapper(
            'infra-install-binary --host "h x" --package p --version 1',
            config=self._config(),
        )
        assert ok is False

    def test_bare_basename_only_when_no_install_path_prefix_configured(self):
        wrapper = InfraOpWrapper(
            verb_name="infra-install-binary",
            required_flags=("--host", "--package", "--version"),
            install_path_prefix="/opt/infra/bin",
        )
        cfg = InfraOpsConfig(op_wrappers=(wrapper,))
        ok, reason = check_infra_op_wrapper(
            "/opt/infra/bin/infra-install-binary --host h --package p --version 1",
            config=cfg,
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# compound_check runs first (reference: _compound_check before the
# op-wrapper pattern list).
# ---------------------------------------------------------------------------


class TestCheckInfraOpWrapperCompoundDenied:
    def test_compound_expression_denied(self):
        cfg = InfraOpsConfig(op_wrappers=(_install_binary_wrapper(),))
        ok, reason = check_infra_op_wrapper(
            "infra-install-binary --host h --package p --version 1 && rm -rf /",
            config=cfg,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# run_scoped_command --template-params-json shape.
# ---------------------------------------------------------------------------


class TestCheckInfraOpWrapperTemplateParamsJson:
    def _config(self) -> InfraOpsConfig:
        return InfraOpsConfig(op_wrappers=(_run_scoped_command_json_wrapper(),))

    def test_compact_json_admitted(self):
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a":"b"}',
            config=self._config(),
        )
        assert ok is True, reason

    def test_json_with_whitespace_denied(self):
        # template_params must be whitespace-free (caller minifies it) --
        # see _INFRA_TEMPLATE_PARAMS_JSON_VALUE's own rationale.
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a": "b"}',
            config=self._config(),
        )
        assert ok is False

    def test_json_with_command_substitution_denied(self):
        # BOBBIE PR #119 comment 15898, rule bobbie.sast.2: a JSON params
        # value carrying a $(...) command substitution must NOT satisfy
        # _INFRA_TEMPLATE_PARAMS_JSON_VALUE -- the grammar must exclude `$`
        # (and backtick) exactly as its sibling _INFRA_VALUE already does,
        # so this host-mutation op wrapper can never admit a live command
        # substitution inside an admitted value.
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a":"$(id)"}',
            config=self._config(),
        )
        assert ok is False

    def test_json_with_backtick_command_substitution_denied(self):
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a":"`id`"}',
            config=self._config(),
        )
        assert ok is False

    def test_json_with_dollar_var_expansion_denied(self):
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a":"$HOME"}',
            config=self._config(),
        )
        assert ok is False

    def test_json_metacharacter_free_value_still_admitted(self):
        # Legitimate, metacharacter-free JSON params values remain admitted
        # -- the fix must not over-tighten past the reference posture.
        ok, reason = check_infra_op_wrapper(
            'infra-run-scoped-command --host h --template-id t1 '
            '--template-params-json {"a":"b","c":"d","n":1}',
            config=self._config(),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# run_scoped_command --template-params-file shape + path containment.
# ---------------------------------------------------------------------------


class TestCheckInfraOpWrapperTemplateParamsFile:
    def _config(self) -> InfraOpsConfig:
        return InfraOpsConfig(op_wrappers=(_run_scoped_command_file_wrapper(),))

    def test_contained_path_admitted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_infra_op_wrapper(
            f"infra-run-scoped-command --host h --template-id t1 "
            f"--template-params-file {tmp_path}/params.json",
            config=self._config(),
        )
        assert ok is True, reason

    def test_escaping_path_denied(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        ok, reason = check_infra_op_wrapper(
            "infra-run-scoped-command --host h --template-id t1 "
            "--template-params-file /etc/passwd",
            config=self._config(),
        )
        assert ok is False
        assert "template-params-file" in reason

    def test_symlink_escape_denied(self, tmp_path, monkeypatch):
        outside = tmp_path.parent / "outside-infra-test-dir"
        outside.mkdir(exist_ok=True)
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        monkeypatch.setenv("TMPDIR", str(scratch))
        link = scratch / "escape-link"
        target = outside / "params.json"
        target.write_text("{}")
        os.symlink(target, link)
        ok, reason = check_infra_op_wrapper(
            f"infra-run-scoped-command --host h --template-id t1 "
            f"--template-params-file {link}",
            config=self._config(),
        )
        assert ok is False


class TestIsInfraParamsFilePathContained:
    def test_no_flag_present_returns_true(self):
        assert is_infra_params_file_path_contained("infra-rotate-token --service a") is True

    def test_contained_under_tmpdir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert is_infra_params_file_path_contained(
            f"infra-run-scoped-command --template-params-file {tmp_path}/x.json"
        ) is True

    def test_home_only_not_contained_denies(self, monkeypatch, tmp_path):
        # lr-f8649f: $HOME is no longer a scratch boundary for this check --
        # a value resolving under $HOME alone (no $TMPDIR set, and this
        # synthetic tmp_path does not match the real uid-home fallback) is
        # denied exactly like any other out-of-scratch location.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("TMPDIR", raising=False)
        assert is_infra_params_file_path_contained(
            f"infra-run-scoped-command --template-params-file {tmp_path}/x.json"
        ) is False

    def test_not_contained_denies(self, monkeypatch, tmp_path):
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        assert is_infra_params_file_path_contained(
            "infra-run-scoped-command --template-params-file /etc/shadow"
        ) is False


# ---------------------------------------------------------------------------
# Optional ack env-assignment prefix — admitted ONLY on run_scoped_command
# shapes with admits_ack_prefix=True, and ONLY when InfraOpsConfig.ack_env_var
# is configured (reference: ASHFORD_APPROVAL_ACKED_BY, lr-77c9f9).
# ---------------------------------------------------------------------------


class TestAckEnvAssignmentPrefix:
    def test_ack_prefix_admitted_when_configured(self):
        cfg = InfraOpsConfig(
            op_wrappers=(_run_scoped_command_json_wrapper(),),
            ack_env_var="INFRA_APPROVAL_ACKED_BY",
        )
        ok, reason = check_infra_op_wrapper(
            'INFRA_APPROVAL_ACKED_BY=operator-1 infra-run-scoped-command '
            '--host h --template-id t1 --template-params-json {"a":"b"}',
            config=cfg,
        )
        assert ok is True, reason

    def test_ack_prefix_denied_when_not_configured(self):
        cfg = InfraOpsConfig(op_wrappers=(_run_scoped_command_json_wrapper(),))
        ok, reason = check_infra_op_wrapper(
            'INFRA_APPROVAL_ACKED_BY=operator-1 infra-run-scoped-command '
            '--host h --template-id t1 --template-params-json {"a":"b"}',
            config=cfg,
        )
        assert ok is False

    def test_ack_prefix_not_admitted_on_wrapper_without_flag(self):
        cfg = InfraOpsConfig(
            op_wrappers=(_install_binary_wrapper(),),
            ack_env_var="INFRA_APPROVAL_ACKED_BY",
        )
        ok, reason = check_infra_op_wrapper(
            "INFRA_APPROVAL_ACKED_BY=operator-1 infra-install-binary "
            "--host h --package p --version 1",
            config=cfg,
        )
        assert ok is False

    def test_ack_value_with_metacharacter_denied(self):
        cfg = InfraOpsConfig(
            op_wrappers=(_run_scoped_command_json_wrapper(),),
            ack_env_var="INFRA_APPROVAL_ACKED_BY",
        )
        ok, reason = check_infra_op_wrapper(
            'INFRA_APPROVAL_ACKED_BY=$(whoami) infra-run-scoped-command '
            '--host h --template-id t1 --template-params-json {"a":"b"}',
            config=cfg,
        )
        assert ok is False


# ---------------------------------------------------------------------------
# Fixed lore read/audit-write subset -- narrow by design, not caller-
# configurable (see module docstring FLAGGED LOSSY-COLLAPSE / SCOPE POINTS).
# ---------------------------------------------------------------------------


class TestFixedLoreSubset:
    def test_lore_observe_admitted(self):
        ok, reason = check_infra_op_wrapper("lore observe some finding")
        assert ok is True, reason

    def test_lore_task_comment_admitted(self):
        ok, reason = check_infra_op_wrapper("lore task comment lr-1 hello")
        assert ok is True, reason

    def test_lore_task_show_admitted(self):
        ok, reason = check_infra_op_wrapper("lore task show lr-1")
        assert ok is True, reason

    def test_lore_task_list_admitted(self):
        ok, reason = check_infra_op_wrapper("lore task list")
        assert ok is True, reason

    def test_lore_search_admitted(self):
        ok, reason = check_infra_op_wrapper("lore search widget")
        assert ok is True, reason

    def test_lore_task_close_denied(self):
        # No task close/create/update -- reference lr-b3de6c's explicit
        # narrowing, preserved exactly.
        ok, reason = check_infra_op_wrapper("lore task close lr-1")
        assert ok is False

    def test_lore_task_create_denied(self):
        ok, reason = check_infra_op_wrapper("lore task create --title x")
        assert ok is False

    def test_bare_lore_wildcard_denied(self):
        ok, reason = check_infra_op_wrapper("lore update")
        assert ok is False


# ---------------------------------------------------------------------------
# No git, no push/PR-transport, no Write/Edit surface at all.
# ---------------------------------------------------------------------------


class TestNoGitOrPushSurface:
    def test_bare_git_denied(self):
        ok, reason = check_infra_op_wrapper("git status")
        assert ok is False

    def test_git_push_denied(self):
        ok, reason = check_infra_op_wrapper("git push --force")
        assert ok is False

    def test_generic_push_verb_denied(self):
        ok, reason = check_infra_op_wrapper("crew_push.py --repo a/b")
        assert ok is False


# ---------------------------------------------------------------------------
# extra_verb_patterns caller-supplied additions.
# ---------------------------------------------------------------------------


class TestExtraVerbPatterns:
    def test_extra_pattern_admitted(self):
        import re

        cfg = InfraOpsConfig(extra_verb_patterns=(re.compile(r"^infra-doctor(\s|$)"),))
        ok, reason = check_infra_op_wrapper("infra-doctor --check all", config=cfg)
        assert ok is True, reason


# ---------------------------------------------------------------------------
# Fail-closed default: no config at all admits nothing beyond the fixed
# lore subset.
# ---------------------------------------------------------------------------


class TestFailClosedDefault:
    def test_no_config_denies_arbitrary_command(self):
        ok, reason = check_infra_op_wrapper("echo hello")
        assert ok is False

    def test_no_config_still_admits_fixed_lore_subset(self):
        ok, reason = check_infra_op_wrapper("lore search x")
        assert ok is True, reason
