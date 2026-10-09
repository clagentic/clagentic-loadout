"""Tests for the deprecated `push.crew_identity` alias of
`push.agent_identity`: every old name must keep importing and must be the
SAME object as its renamed counterpart (so an existing `except` clause
keeps matching), and importing the alias must warn. Delete this file with
the alias module in the following release.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from clagentic_loadout.push import agent_identity


def _fresh_alias_import():
    sys.modules.pop("clagentic_loadout.push.crew_identity", None)
    return importlib.import_module("clagentic_loadout.push.crew_identity")


def test_alias_reexports_the_renamed_objects_by_identity():
    with pytest.warns(DeprecationWarning):
        alias = _fresh_alias_import()
    assert alias.CrewBotIdentityNotResolvableError is agent_identity.AgentBotIdentityNotResolvableError
    assert alias.is_recognized_crew_caller is agent_identity.is_recognized_agent_caller
    assert alias.resolve_crew_bot_identity is agent_identity.resolve_agent_bot_identity


def test_alias_exports_exactly_the_old_names():
    with pytest.warns(DeprecationWarning):
        alias = _fresh_alias_import()
    assert sorted(alias.__all__) == [
        "CrewBotIdentityNotResolvableError",
        "is_recognized_crew_caller",
        "resolve_crew_bot_identity",
    ]
