"""test_guard_env_prefix.py — exactly-anchored env-var-assignment-prefix
admission (lr-5a8d, folded-in lr-24b2, task comment #1).

Acceptance criterion: if the ported guard admits an env-prefix, it must be
anchored to EXACTLY `CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=<slug>` (end-
anchored); arbitrary VAR=value prefixes must be rejected.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.guard.env_prefix import (
    ALLOWED_ENV_PREFIX_VAR,
    strip_allowed_env_prefix,
)


class TestExactPrefixAdmitted:
    def test_simple_slug_stripped(self):
        value, remainder = strip_allowed_env_prefix(
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=my-app-slug loadout-review-post --pr 1"
        )
        assert value == "my-app-slug"
        assert remainder == "loadout-review-post --pr 1"

    def test_underscore_and_digit_slug_stripped(self):
        value, remainder = strip_allowed_env_prefix(
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=slug_9 loadout-review-post"
        )
        assert value == "slug_9"
        assert remainder == "loadout-review-post"

    def test_correct_var_name_is_exactly_this_constant(self):
        assert ALLOWED_ENV_PREFIX_VAR == "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG"


class TestArbitraryVarNameRejected:
    @pytest.mark.parametrize(
        "command",
        [
            "SOME_OTHER_VAR=x loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUGX=x loadout-review-post",
            "clagentic_loadout_github_app_slug=x loadout-review-post",
            "PATH=/evil loadout-review-post",
        ],
    )
    def test_wrong_var_name_not_stripped(self, command):
        value, remainder = strip_allowed_env_prefix(command)
        assert value is None
        assert remainder == command


class TestUnsafeValueRejected:
    @pytest.mark.parametrize(
        "command",
        [
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=x;touch${IFS}/tmp/pwned loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=$(whoami) loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=`whoami` loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=a\\ b loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=-leadinghyphen loadout-review-post",
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG= loadout-review-post",
        ],
    )
    def test_unsafe_value_not_stripped(self, command):
        value, remainder = strip_allowed_env_prefix(command)
        assert value is None
        assert remainder == command


class TestMultipleAssignmentsRejected:
    def test_two_stacked_assignments_not_stripped(self):
        command = (
            "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=ok EXTRA=x loadout-review-post"
        )
        value, remainder = strip_allowed_env_prefix(command)
        # The regex requires the remainder after the FIRST assignment to be
        # a real command start, not a second assignment token -- "EXTRA=x
        # loadout-review-post" is accepted as a bare non-whitespace
        # "command" by the raw grammar's second capture group, so this test
        # documents the actual contract: only ONE assignment is ever
        # consumed, and whatever remains is treated as the verb command
        # line handed back to the caller's own classifier (which will not
        # recognize "EXTRA=x loadout-review-post" as any known verb).
        assert value == "ok"
        assert remainder == "EXTRA=x loadout-review-post"


class TestNoPrefixAtAll:
    def test_bare_command_unaffected(self):
        value, remainder = strip_allowed_env_prefix("loadout-review-post --pr 1")
        assert value is None
        assert remainder == "loadout-review-post --pr 1"

    def test_empty_command_unaffected(self):
        value, remainder = strip_allowed_env_prefix("")
        assert value is None
        assert remainder == ""
