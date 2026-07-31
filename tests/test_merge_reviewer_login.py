"""test_merge_reviewer_login.py — tests for
clagentic_loadout.merge.reviewer_login (lr-2f1378).

Coverage (per the task's required tests):
  (a) bare name on forgejo resolves to the bare login.
  (b) bare name on github resolves to <slug>[bot] via
      resolve_github_app_slug(caller=reviewer_name).
  (c) a bare name with no configured slug on github fails closed with a
      clear "no slug configured" error (ReviewerLoginNotConfiguredError),
      never a silent skip.
  (d) an unrecognized platform value raises ValueError.

resolve_reviewer_login has no config_root/env parameters of its own -- it
calls resolve_github_app_slug with zero config overrides, mirroring
review.github_backend.resolve_own_login's own zero-config-param call shape
(both resolve against the real os.environ / default user config root). Every
GitHub-path test here monkeypatches this module's own imported
`resolve_github_app_slug` name directly (the SAME isolation pattern
test_review_github_backend.py's `_configure_app_slug` helper already uses)
rather than setenv/delenv on the real process environment -- this test
machine may have a real ~/.config/clagentic/loadout/config.yaml or env var
configured for other crew tooling, and a setenv/delenv-based test would
silently pass or fail depending on that ambient, untracked machine state.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.transport.github_app_config import GithubAppSlugNotConfiguredError


def _configure_app_slug(monkeypatch, slug: str) -> None:
    """Test helper mirroring test_review_github_backend.py's own
    _configure_app_slug: patches this module's imported resolve_github_app_slug
    name so a test's expected slug is returned deterministically, without
    touching the real process environment or ~/.config/clagentic/loadout/
    config.yaml."""
    monkeypatch.setattr(
        "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
        lambda **kwargs: slug,
    )


def _configure_app_slug_unconfigured(monkeypatch) -> None:
    def _raise(**kwargs):
        raise GithubAppSlugNotConfiguredError("no slug configured")

    monkeypatch.setattr(
        "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug", _raise
    )


class TestForgejoBareName:
    def test_bare_name_resolves_to_itself(self):
        assert resolve_reviewer_login("peaches", PLATFORM_FORGEJO) == "peaches"

    def test_bare_name_never_consults_github_app_config(self, monkeypatch):
        # Forgejo path must not even look at the GitHub App slug config --
        # a slug resolver that would raise if called proves it is never
        # invoked on this platform branch.
        def _fail_if_called(**kwargs):
            raise AssertionError("resolve_github_app_slug must not be called on forgejo")

        monkeypatch.setattr(
            "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
            _fail_if_called,
        )
        assert resolve_reviewer_login("peaches", PLATFORM_FORGEJO) == "peaches"


class TestGithubBareName:
    def test_bare_name_resolves_via_configured_slug(self, monkeypatch):
        _configure_app_slug(monkeypatch, "clagentic-reviewer")
        assert (
            resolve_reviewer_login("peaches", PLATFORM_GITHUB)
            == "clagentic-reviewer[bot]"
        )

    def test_unconfigured_slug_fails_closed(self, monkeypatch):
        _configure_app_slug_unconfigured(monkeypatch)
        with pytest.raises(ReviewerLoginNotConfiguredError) as exc_info:
            resolve_reviewer_login("peaches", PLATFORM_GITHUB)
        assert "peaches" in str(exc_info.value)


class TestUnrecognizedPlatform:
    def test_raises_value_error(self):
        with pytest.raises(ValueError):
            resolve_reviewer_login("peaches", "not-a-real-platform")
