"""test_task_id_guard.py — unit tests for clagentic_loadout.task_id_guard
(lr-4005f5).

CONFORMANCE (CLAUDE.md rule 6): every test uses a SYNTHETIC pattern
(r"\\bWIDGET-\\d+\\b") and invented identifiers -- no test depends on the real
internal lr-XXXXXX task-id shape, proving the guard genuinely works off a
configurable pattern rather than a hidden assumption about tracker shape.
"""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.task_id_guard import (
    CONFIG_KEY_TASK_ID_MODE,
    CONFIG_KEY_TASK_ID_PATTERN,
    CONFIG_SECTION_PUSH,
    DEFAULT_MODE_WHEN_PATTERN_CONFIGURED,
    MODE_BLOCK,
    MODE_OFF,
    MODE_WARN,
    InvalidTaskIdGuardConfigError,
    TaskIdGuardViolation,
    check_task_id_guard,
    load_task_id_guard_config,
)

_SYNTHETIC_PATTERN = r"\bWIDGET-\d+\b"


def _write_config(tmp_path, content: dict) -> None:
    config_dir = tmp_path / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestConfigNoOpByDefault:
    """Hard acceptance criterion: no configured pattern -> the guard is a
    strict no-op. Never fails closed on an unconfigured deployment."""

    def test_no_repo_root_returns_no_op_default(self):
        cfg = load_task_id_guard_config(None)
        assert cfg.pattern is None
        assert cfg.mode == MODE_OFF
        assert cfg.source == "default"

    def test_no_config_file_returns_no_op_default(self, tmp_path):
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.pattern is None

    def test_config_file_with_no_push_section_returns_no_op_default(self, tmp_path):
        _write_config(tmp_path, {"merge": {"authorized_roles": ["merger"]}})
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.pattern is None

    def test_push_section_with_no_pattern_key_returns_no_op_default(self, tmp_path):
        _write_config(tmp_path, {"push": {"scratch_patterns": ["a-*"]}})
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.pattern is None


class TestConfigExplicitOptIn:
    def test_pattern_configured_defaults_to_block_mode(self, tmp_path):
        """Operator-pinned decision: once a pattern is set, mode defaults to
        block -- never silently weakened to warn/off."""
        _write_config(tmp_path, {"push": {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN}})
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.pattern == _SYNTHETIC_PATTERN
        assert cfg.mode == MODE_BLOCK
        assert cfg.mode == DEFAULT_MODE_WHEN_PATTERN_CONFIGURED
        assert cfg.source != "default"

    def test_explicit_warn_mode_overrides_block_default(self, tmp_path):
        _write_config(
            tmp_path,
            {"push": {
                CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN,
                CONFIG_KEY_TASK_ID_MODE: MODE_WARN,
            }},
        )
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.mode == MODE_WARN

    def test_explicit_off_mode_is_legitimate(self, tmp_path):
        """A deployment that wants the pattern recorded but this surface
        inert is a legitimate configuration."""
        _write_config(
            tmp_path,
            {"push": {
                CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN,
                CONFIG_KEY_TASK_ID_MODE: MODE_OFF,
            }},
        )
        cfg = load_task_id_guard_config(tmp_path)
        assert cfg.mode == MODE_OFF


_OTHER_PATTERN = r"\bGADGET-\d+\b"

#: Row marker: load with no repo root at all (distinct from a repo root with no config).
_NO_REPO_ROOT = object()

#: (repo-local push section or None (or _NO_REPO_ROOT), user-level push section or None,
#: expected pattern, expected mode, expected source is the user-level file)
_PRECEDENCE_ROWS = [
    pytest.param(None, None, None, MODE_OFF, False, id="nothing-anywhere-is-a-no-op"),
    pytest.param(
        None, {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN},
        _SYNTHETIC_PATTERN, MODE_BLOCK, True, id="user-level-only-fills-the-absence",
    ),
    pytest.param(
        {"scratch_patterns": ["a-*"]}, {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN},
        _SYNTHETIC_PATTERN, MODE_BLOCK, True, id="repo-local-without-a-pattern-falls-through",
    ),
    pytest.param(
        {CONFIG_KEY_TASK_ID_PATTERN: _OTHER_PATTERN, CONFIG_KEY_TASK_ID_MODE: MODE_WARN},
        {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN, CONFIG_KEY_TASK_ID_MODE: MODE_BLOCK},
        _OTHER_PATTERN, MODE_WARN, False, id="repo-local-pattern-and-mode-win",
    ),
    pytest.param(
        {CONFIG_KEY_TASK_ID_PATTERN: _OTHER_PATTERN},
        {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN, CONFIG_KEY_TASK_ID_MODE: MODE_WARN},
        _OTHER_PATTERN, MODE_BLOCK, False, id="mode-never-mixed-across-sources",
    ),
    pytest.param(
        None, {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN, CONFIG_KEY_TASK_ID_MODE: MODE_WARN},
        _SYNTHETIC_PATTERN, MODE_WARN, True, id="user-level-mode-is-honored",
    ),
    pytest.param(
        _NO_REPO_ROOT, {CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN},
        _SYNTHETIC_PATTERN, MODE_BLOCK, True, id="user-level-works-with-no-repo-root",
    ),
]


class TestConfigUserLevelFallback:
    @pytest.mark.parametrize(
        "repo_push, user_push, want_pattern, want_mode, want_user_source", _PRECEDENCE_ROWS
    )
    def test_precedence_table(
        self, tmp_path, repo_push, user_push, want_pattern, want_mode, want_user_source
    ):
        user_root = tmp_path / "user-config-root"
        user_root.mkdir()
        if repo_push is _NO_REPO_ROOT:
            repo_root = None
        else:
            repo_root = tmp_path / "repo"
            repo_root.mkdir()
        if repo_push is not None and repo_push is not _NO_REPO_ROOT:
            _write_config(repo_root, {"push": repo_push})
        if user_push is not None:
            (user_root / "config.yaml").write_text(
                yaml.safe_dump({"push": user_push}), encoding="utf-8"
            )

        cfg = load_task_id_guard_config(repo_root, config_root=user_root)

        assert cfg.pattern == want_pattern
        assert cfg.mode == want_mode
        if want_pattern is None:
            assert cfg.source == "default"
        elif want_user_source:
            assert cfg.source == str(user_root / "config.yaml")
        else:
            assert cfg.source == str(repo_root / ".clagentic" / "loadout" / "config.yaml")

    def test_invalid_user_level_pattern_names_the_user_level_file(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"push": {CONFIG_KEY_TASK_ID_PATTERN: "["}}), encoding="utf-8"
        )
        with pytest.raises(InvalidTaskIdGuardConfigError, match="config.yaml"):
            load_task_id_guard_config(None, config_root=tmp_path)

    def test_violation_message_names_the_actual_source(self):
        with pytest.raises(TaskIdGuardViolation) as exc_info:
            check_task_id_guard(
                "feat: WIDGET-9 leak",
                field="PR title",
                pattern=_SYNTHETIC_PATTERN,
                mode=MODE_BLOCK,
                source="/synthetic/user-root/config.yaml",
            )
        assert "/synthetic/user-root/config.yaml" in str(exc_info.value)
        assert ".clagentic/loadout/config.yaml" not in str(exc_info.value)


class TestConfigMalformedRaises:
    def test_pattern_not_a_string_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {CONFIG_KEY_TASK_ID_PATTERN: 5}})
        with pytest.raises(InvalidTaskIdGuardConfigError):
            load_task_id_guard_config(tmp_path)

    def test_pattern_empty_string_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {CONFIG_KEY_TASK_ID_PATTERN: ""}})
        with pytest.raises(InvalidTaskIdGuardConfigError):
            load_task_id_guard_config(tmp_path)

    def test_pattern_invalid_regex_raises(self, tmp_path):
        _write_config(tmp_path, {"push": {CONFIG_KEY_TASK_ID_PATTERN: "["}})
        with pytest.raises(InvalidTaskIdGuardConfigError):
            load_task_id_guard_config(tmp_path)

    def test_mode_not_a_valid_choice_raises(self, tmp_path):
        _write_config(
            tmp_path,
            {"push": {
                CONFIG_KEY_TASK_ID_PATTERN: _SYNTHETIC_PATTERN,
                CONFIG_KEY_TASK_ID_MODE: "sometimes",
            }},
        )
        with pytest.raises(InvalidTaskIdGuardConfigError):
            load_task_id_guard_config(tmp_path)

    def test_malformed_yaml_raises(self, tmp_path):
        config_dir = tmp_path / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "config.yaml").write_text("push: [unterminated", encoding="utf-8")
        with pytest.raises(InvalidTaskIdGuardConfigError):
            load_task_id_guard_config(tmp_path)


class TestConfigConstants:
    def test_section_and_key_names(self):
        assert CONFIG_SECTION_PUSH == "push"
        assert CONFIG_KEY_TASK_ID_PATTERN == "task_id_guard_pattern"
        assert CONFIG_KEY_TASK_ID_MODE == "task_id_guard_mode"

    def test_default_mode_is_block(self):
        assert DEFAULT_MODE_WHEN_PATTERN_CONFIGURED == MODE_BLOCK


class TestCheckNoOp:
    def test_none_pattern_is_a_no_op_regardless_of_mode(self):
        assert check_task_id_guard(
            "feat: WIDGET-1 leaked", field="PR title", pattern=None, mode=MODE_BLOCK
        ) is None

    def test_none_value_is_a_no_op(self):
        assert check_task_id_guard(
            None, field="PR title", pattern=_SYNTHETIC_PATTERN, mode=MODE_BLOCK
        ) is None

    def test_mode_off_is_a_no_op_even_with_a_match(self):
        assert check_task_id_guard(
            "feat: WIDGET-1 leaked", field="PR title", pattern=_SYNTHETIC_PATTERN, mode=MODE_OFF
        ) is None

    def test_no_match_is_a_no_op_in_block_mode(self):
        assert check_task_id_guard(
            "feat: add a thing", field="PR title", pattern=_SYNTHETIC_PATTERN, mode=MODE_BLOCK
        ) is None

    def test_no_match_is_a_no_op_in_warn_mode(self):
        assert check_task_id_guard(
            "feat: add a thing", field="PR title", pattern=_SYNTHETIC_PATTERN, mode=MODE_WARN
        ) is None


class TestCheckBlockMode:
    def test_match_raises_violation_naming_field_value_and_config_key(self):
        with pytest.raises(TaskIdGuardViolation) as exc_info:
            check_task_id_guard(
                "feat: fix WIDGET-42 leak",
                field="PR title",
                pattern=_SYNTHETIC_PATTERN,
                mode=MODE_BLOCK,
            )
        message = str(exc_info.value)
        assert "PR title" in message
        assert "WIDGET-42" in message
        assert CONFIG_KEY_TASK_ID_PATTERN in message
        assert CONFIG_KEY_TASK_ID_MODE in message
        exc = exc_info.value
        assert exc.field == "PR title"
        assert exc.matched == "WIDGET-42"
        assert exc.value == "feat: fix WIDGET-42 leak"

    def test_commit_subject_field_name_is_reported_verbatim(self):
        with pytest.raises(TaskIdGuardViolation) as exc_info:
            check_task_id_guard(
                "WIDGET-7: id-leading subject",
                field="branch commit subject (deadbeef)",
                pattern=_SYNTHETIC_PATTERN,
                mode=MODE_BLOCK,
            )
        assert "branch commit subject (deadbeef)" in str(exc_info.value)


class TestCheckWarnMode:
    def test_match_returns_warning_message_never_raises(self):
        warning = check_task_id_guard(
            "feat: fix WIDGET-42 leak",
            field="PR title",
            pattern=_SYNTHETIC_PATTERN,
            mode=MODE_WARN,
        )
        assert warning is not None
        assert "WIDGET-42" in warning
        assert "PR title" in warning


class TestTrailerAndPrReferenceFormsUnaffected:
    """Hard acceptance criteria: the body `Task: <id>` trailer is out of
    scope by construction (this module never sees a PR body -- only a title
    or a commit subject is ever passed in), and the `(#NN)` trailing
    Conventional Commits PR-reference form never matches a
    tracker-id-shaped pattern."""

    def test_pr_reference_suffix_does_not_match_a_tracker_shaped_pattern(self):
        assert check_task_id_guard(
            "feat(auth): add x (#123)",
            field="PR title",
            pattern=_SYNTHETIC_PATTERN,
            mode=MODE_BLOCK,
        ) is None

    def test_trailer_grammar_itself_is_never_inspected_by_this_module(self):
        """This module has no body-parsing entrypoint at all -- proven by
        showing a value shaped like a trailer line still only matches on the
        SAME configured pattern applied to a title/subject-shaped string,
        never a body."""
        with pytest.raises(TaskIdGuardViolation):
            check_task_id_guard(
                "Task: WIDGET-99",  # a title/subject string that HAPPENS to
                # look like a trailer -- still matches, because this module
                # has no body/trailer awareness at all; the exemption for
                # the real Task: trailer lives in the CALLER never handing
                # the body to this function in the first place.
                field="PR title",
                pattern=_SYNTHETIC_PATTERN,
                mode=MODE_BLOCK,
            )
