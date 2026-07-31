"""test_push_crew_identity.py — unit tests for
clagentic_loadout.push.crew_identity (lr-f145d2, platform-gate fix
lr-f145d2-followup).

Covers: registry-membership + platform gate (is_recognized_crew_caller)
against `github_app.callers`; derivation of (name, email) from
`github_app.slugs.<caller>` on platform=github; fail-closed
(CrewBotIdentityNotResolvableError) for a recognized-but-unresolvable
caller ON GITHUB; the external-user-safety default (no `github_app`
section configured at all -> never recognized); and the PLATFORM GATE
ITSELF -- a recognized crew caller on Forgejo is NEVER treated as
"recognized" by is_recognized_crew_caller, because Forgejo has no
App-bot-slug concept for this module to derive from at all.

CORRECTED, NOT WEAKENED (coordinator-flagged defect, see push/verb.py's
_resolve_effective_bot_identity docstring for the full incident account):
an earlier revision of this file asserted
`resolve_crew_bot_identity(..., PLATFORM_FORGEJO, ...)` raises
CrewBotIdentityNotResolvableError, and separately never exercised
is_recognized_crew_caller with a platform argument at all (the function
didn't take one). That shape let push.verb call is_recognized_crew_caller
platform-BLIND -- True for any recognized caller regardless of platform --
enter tier 2 unconditionally, and then hit the resolver's raise on EVERY
Forgejo push from a recognized caller, converting the overwhelming
majority of this deployment's actual push traffic into a hard
EXIT_AUTHOR_MISMATCH failure. The fix moves the platform condition INTO
is_recognized_crew_caller (see that function's own docstring). The tests
below assert the CORRECTED contract: is_recognized_crew_caller(caller,
PLATFORM_FORGEJO, ...) is False for a recognized caller (so push.verb
never enters tier 2 for Forgejo at all), while
resolve_crew_bot_identity(..., PLATFORM_FORGEJO, ...) still raises as a
DEFENSIVE invariant check for a caller that bypasses the gate (retained,
not weakened -- see that function's own docstring for why this is
"should never be reachable in practice", not "expected to fire routinely").
"""

from __future__ import annotations

import yaml
import pytest

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.push.crew_identity import (
    CrewBotIdentityNotResolvableError,
    is_recognized_crew_caller,
    resolve_crew_bot_identity,
)


def _write_user_config(tmp_path, content: dict) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestIsRecognizedCrewCaller:
    def test_no_config_file_at_all_is_never_recognized(self, tmp_path):
        assert is_recognized_crew_caller("amos", PLATFORM_GITHUB, config_root=tmp_path) is False

    def test_no_github_app_section_is_never_recognized(self, tmp_path):
        _write_user_config(tmp_path, {"credentials": {}})
        assert is_recognized_crew_caller("amos", PLATFORM_GITHUB, config_root=tmp_path) is False

    def test_no_callers_key_is_never_recognized(self, tmp_path):
        _write_user_config(tmp_path, {"github_app": {"slugs": {"amos": "some-app"}}})
        assert is_recognized_crew_caller("amos", PLATFORM_GITHUB, config_root=tmp_path) is False

    def test_caller_present_in_callers_list_is_recognized_on_github(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "some-app"}, "callers": ["amos", "peaches"]}},
        )
        assert is_recognized_crew_caller("amos", PLATFORM_GITHUB, config_root=tmp_path) is True

    def test_caller_absent_from_callers_list_is_not_recognized(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "some-app"}, "callers": ["peaches"]}},
        )
        assert is_recognized_crew_caller("amos", PLATFORM_GITHUB, config_root=tmp_path) is False

    def test_none_or_empty_caller_is_never_recognized(self, tmp_path):
        _write_user_config(
            tmp_path, {"github_app": {"callers": ["amos"]}}
        )
        assert is_recognized_crew_caller(None, PLATFORM_GITHUB, config_root=tmp_path) is False
        assert is_recognized_crew_caller("", PLATFORM_GITHUB, config_root=tmp_path) is False
        assert is_recognized_crew_caller("   ", PLATFORM_GITHUB, config_root=tmp_path) is False

    def test_recognized_caller_on_forgejo_is_never_recognized(self, tmp_path):
        """THE PLATFORM-GATE FIX ITSELF: a caller present in
        github_app.callers -- deployment-wide, not platform-scoped -- must
        NOT be treated as a "recognized crew caller" for identity-derivation
        purposes when the push targets Forgejo, because this module has no
        slug-to-identity derivation for that platform at all. Before this
        fix, this same (caller, config) combination returned True
        regardless of platform, and push.verb had no way to avoid entering
        tier 2 for a Forgejo push -- see this module's own docstring for
        the incident this test exists to prevent recurring."""
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "some-builder-app"}, "callers": ["amos"]}},
        )
        assert is_recognized_crew_caller("amos", PLATFORM_FORGEJO, config_root=tmp_path) is False


class TestResolveCrewBotIdentityGithub:
    def test_derives_name_and_email_from_configured_slug(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "some-builder-app"}, "callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity("amos", PLATFORM_GITHUB, config_root=tmp_path)
        assert name == "some-builder-app[bot]"
        assert email == "some-builder-app[bot]@users.noreply.github.com"

    def test_falls_back_to_single_global_slug(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slug": "global-app", "callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity("amos", PLATFORM_GITHUB, config_root=tmp_path)
        assert name == "global-app[bot]"
        assert email == "global-app[bot]@users.noreply.github.com"

    def test_no_slug_for_caller_raises_not_resolvable(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"peaches": "reviewer-app"}, "callers": ["amos", "peaches"]}},
        )
        with pytest.raises(CrewBotIdentityNotResolvableError):
            resolve_crew_bot_identity("amos", PLATFORM_GITHUB, config_root=tmp_path)


class TestResolveCrewBotIdentityProviderVerifiedSlug:
    """lr-43c8d7: resolve_crew_bot_identity's provider_verified_app_slug
    keyword -- a tier ABOVE github_app.slugs config. Non-vacuity: the
    provider slug and the config slug must DIFFER for these tests to prove
    anything (see this task's own dispatch brief)."""

    def test_provider_slug_wins_over_config_slug_when_they_differ(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "config-app"}, "callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity(
            "amos", PLATFORM_GITHUB, config_root=tmp_path,
            provider_verified_app_slug="provider-app",
        )
        assert name == "provider-app[bot]"
        assert email == "provider-app[bot]@users.noreply.github.com"

    def test_config_fallback_still_works_when_provider_supplies_nothing(self, tmp_path):
        """The provider param defaults to None -- config resolution must be
        completely unaffected (byte-identical to before this task)."""
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "config-app"}, "callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity("amos", PLATFORM_GITHUB, config_root=tmp_path)
        assert name == "config-app[bot]"
        assert email == "config-app[bot]@users.noreply.github.com"

    def test_config_fallback_when_provider_supplies_empty_string(self, tmp_path):
        """The real, reachable gatekeeper case: a role with no App-slug
        binding configured returns an EMPTY app_slug, not an absent
        parameter -- this must fall through to config, not raise or use an
        empty slug literally."""
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "config-app"}, "callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity(
            "amos", PLATFORM_GITHUB, config_root=tmp_path,
            provider_verified_app_slug="",
        )
        assert name == "config-app[bot]"

    def test_provider_slug_rescues_otherwise_unresolvable_caller(self, tmp_path):
        """No github_app.slugs entry at all for this caller -- config alone
        would raise CrewBotIdentityNotResolvableError (see
        TestResolveCrewBotIdentityGithub.test_no_slug_for_caller_raises_not_resolvable).
        A provider-supplied slug resolves it anyway."""
        _write_user_config(
            tmp_path,
            {"github_app": {"callers": ["amos"]}},
        )
        name, email = resolve_crew_bot_identity(
            "amos", PLATFORM_GITHUB, config_root=tmp_path,
            provider_verified_app_slug="provider-only-app",
        )
        assert name == "provider-only-app[bot]"
        assert email == "provider-only-app[bot]@users.noreply.github.com"

    def test_whitespace_only_provider_slug_treated_as_not_supplied(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "config-app"}, "callers": ["amos"]}},
        )
        name, _email = resolve_crew_bot_identity(
            "amos", PLATFORM_GITHUB, config_root=tmp_path,
            provider_verified_app_slug="   ",
        )
        assert name == "config-app[bot]"


class TestResolveCrewBotIdentityForgejoDefensiveGuard:
    """resolve_crew_bot_identity itself still refuses a non-GitHub platform
    -- retained as a DEFENSIVE invariant check for a caller that bypasses
    push.verb's own is_recognized_crew_caller(caller, platform) gate, not
    as the mechanism push.verb relies on to avoid entering tier 2 for
    Forgejo (that is is_recognized_crew_caller's job now -- see
    TestIsRecognizedCrewCaller.test_recognized_caller_on_forgejo_is_never_recognized
    above). push.verb itself never reaches this raise in normal operation
    after the platform-gate fix."""

    def test_forgejo_platform_still_raises_as_defensive_invariant(self, tmp_path):
        _write_user_config(
            tmp_path,
            {"github_app": {"slugs": {"amos": "some-builder-app"}, "callers": ["amos"]}},
        )
        with pytest.raises(CrewBotIdentityNotResolvableError):
            resolve_crew_bot_identity("amos", PLATFORM_FORGEJO, config_root=tmp_path)
