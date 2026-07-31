"""test_provisioning_allowlist.py — per-role fragment generation (lr-4e04).

Covers: both Bash forms per verb, ONLY the role's own declared verbs (no
global leakage from other roles or from the full KNOWN_VERBS set), and the
unknown-verb refusal. Synthetic role/verb names exercise the conformance
gate (CLAUDE.md rule 6a) — this module has no notion of "the seed roles"
at all, only whatever verb tuple its caller passes in.
"""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.provisioning.allowlist import (
    UnknownVerbError,
    VERB_CONSOLE_SCRIPTS,
    generate_role_fragment,
    render_fragment_json,
)
from clagentic_loadout.provisioning.roles import KNOWN_VERBS


def test_single_verb_yields_both_bash_forms() -> None:
    fragment = generate_role_fragment("some-role", ("push",))
    assert fragment == sorted(["Bash(loadout-push:*)", "Bash(loadout-push *)"])


def test_multi_verb_role_gets_exactly_its_own_entries_no_leakage() -> None:
    fragment = generate_role_fragment("reviewer-like-role", ("git-host-api", "review-post"))
    expected = sorted(
        [
            "Bash(loadout-git-host-api:*)",
            "Bash(loadout-git-host-api *)",
            "Bash(loadout-review-post:*)",
            "Bash(loadout-review-post *)",
        ]
    )
    assert fragment == expected
    # No entry for any OTHER known verb leaked in.
    for verb, script in VERB_CONSOLE_SCRIPTS.items():
        if verb in ("git-host-api", "review-post"):
            continue
        assert f"Bash({script}:*)" not in fragment
        assert f"Bash({script} *)" not in fragment


def test_every_known_verb_has_a_console_script_mapping() -> None:
    for verb in KNOWN_VERBS:
        assert verb in VERB_CONSOLE_SCRIPTS
        assert VERB_CONSOLE_SCRIPTS[verb].startswith("loadout-")


def test_unknown_verb_raises() -> None:
    with pytest.raises(UnknownVerbError) as exc_info:
        generate_role_fragment("some-role", ("not-a-real-verb",))
    assert "not-a-real-verb" in str(exc_info.value)


def test_render_fragment_json_round_trips() -> None:
    rendered = render_fragment_json("builder", ("push",))
    parsed = json.loads(rendered)
    assert parsed == generate_role_fragment("builder", ("push",))


def test_two_disjoint_synthetic_roles_produce_disjoint_fragments() -> None:
    """Conformance (rule 6a): invented role names, no assumption that only
    the seed roles exist. Also proves there is no shared/global state a
    second role's generation call could leak into."""
    fragment_a = generate_role_fragment("zorbnaut", ("push",))
    fragment_b = generate_role_fragment("flibbertigibbet", ("merge", "release-dispatch"))
    assert set(fragment_a).isdisjoint(fragment_b)
