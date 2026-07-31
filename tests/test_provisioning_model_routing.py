"""test_provisioning_model_routing.py — ROLE -> scope-tiered model_chain
config coverage (lr-71e3, re-filed from an internal deployment's own
lr-aee8).

Covers: the default/reference mapping, repo-local override (replace, not
merge), tier-boundary resolution (including the >10k-LOC escalation shape
this task's own description names as the reference case), unknown-role and
malformed-tier resolved-values errors, and the open-ended-tier-must-be-last
ordering guard. Uses SYNTHETIC role and model-id names throughout — the
conformance gate (CLAUDE.md rule 6a) requires this module to work with
invented role/model names and no lore present; nothing here depends on the
seed role taxonomy or any real provider/model literal being present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.provisioning.model_routing import (
    DEFAULT_MODEL_ROUTING,
    InvalidModelRoutingConfigError,
    load_model_routing,
    resolve_model_chain,
)


def _write_config(repo_root: Path, yaml_text: str) -> None:
    loadout_dir = repo_root / ".clagentic" / "loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def _write_legacy_config(repo_root: Path, yaml_text: str) -> None:
    loadout_dir = repo_root / ".loadout"
    loadout_dir.mkdir(parents=True, exist_ok=True)
    (loadout_dir / "config.yaml").write_text(yaml_text, encoding="utf-8")


def test_default_mapping_covers_seed_roles_single_open_ended_tier() -> None:
    for role in ("builder", "reviewer", "merger", "lead"):
        assert role in DEFAULT_MODEL_ROUTING
        tiers = DEFAULT_MODEL_ROUTING[role]
        assert len(tiers) == 1
        assert tiers[0]["max_loc"] is None
        assert tiers[0]["model_chain"]


def test_no_repo_root_returns_default_mapping() -> None:
    assert load_model_routing(None) == dict(DEFAULT_MODEL_ROUTING)


def test_no_config_file_returns_default_mapping(tmp_path: Path) -> None:
    assert load_model_routing(tmp_path) == dict(DEFAULT_MODEL_ROUTING)


def test_repo_config_with_synthetic_roles_replaces_default_entirely(tmp_path: Path) -> None:
    """CLAUDE.md rule 6a conformance: synthetic role/model names, no lore,
    no dependency on the seed role taxonomy being present at all."""
    _write_config(
        tmp_path,
        """
model_routing:
  zorbnaut:
    - max_loc: null
      model_chain: ["zorbnaut-model"]
  flibbertigibbet:
    - max_loc: 500
      model_chain: ["small-model"]
    - max_loc: null
      model_chain: ["big-model", "fallback-model"]
""",
    )
    resolved = load_model_routing(tmp_path)
    assert resolved == {
        "zorbnaut": ({"max_loc": None, "model_chain": ("zorbnaut-model",)},),
        "flibbertigibbet": (
            {"max_loc": 500, "model_chain": ("small-model",)},
            {"max_loc": None, "model_chain": ("big-model", "fallback-model")},
        ),
    }
    # The seed roles are GONE, not merged in.
    assert "builder" not in resolved


def test_reference_escalation_shape_gt_10k_loc(tmp_path: Path) -> None:
    """The reference deployment shape this task names in its own
    description: reviewer role escalates from a standard model to an
    architectural-review model chain on diffs over 10k LOC. Model ids here
    are synthetic tokens (never a real provider/model literal, rule 1) —
    the module never bakes in "gpt-high" or "claude-opus" as a default."""
    _write_config(
        tmp_path,
        """
model_routing:
  reviewer:
    - max_loc: 10000
      model_chain: ["reviewer-standard"]
    - max_loc: null
      model_chain: ["reviewer-architectural", "reviewer-standard"]
""",
    )
    assert resolve_model_chain("reviewer", 500, repo_root=tmp_path) == ("reviewer-standard",)
    assert resolve_model_chain("reviewer", 10000, repo_root=tmp_path) == ("reviewer-standard",)
    assert resolve_model_chain("reviewer", 10001, repo_root=tmp_path) == (
        "reviewer-architectural",
        "reviewer-standard",
    )
    assert resolve_model_chain("reviewer", 500_000, repo_root=tmp_path) == (
        "reviewer-architectural",
        "reviewer-standard",
    )


def test_resolve_model_chain_success_default_mapping() -> None:
    assert resolve_model_chain("builder", 42) == ("builder-default",)


def test_resolve_unknown_role_reports_resolved_values(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_routing:\n  known-role:\n    - max_loc: null\n      model_chain: [\"m\"]\n",
    )
    with pytest.raises(InvalidModelRoutingConfigError) as exc_info:
        resolve_model_chain("nonexistent-role", 10, repo_root=tmp_path)
    msg = str(exc_info.value)
    assert "nonexistent-role" in msg
    assert "known-role" in msg
    assert str(tmp_path / ".clagentic" / "loadout" / "config.yaml") in msg


def test_negative_changed_lines_raises() -> None:
    with pytest.raises(InvalidModelRoutingConfigError):
        resolve_model_chain("builder", -1)


def test_open_ended_tier_must_be_last(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        """
model_routing:
  bad-role:
    - max_loc: null
      model_chain: ["m1"]
    - max_loc: 100
      model_chain: ["m2"]
""",
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_empty_model_routing_section_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing: {}\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_role_with_empty_tier_list_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing:\n  empty-role:\n    []\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_tier_missing_model_chain_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing:\n  bad-role:\n    - max_loc: 10\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_tier_with_empty_model_chain_raises(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_routing:\n  bad-role:\n    - max_loc: null\n      model_chain: []\n",
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_tier_with_non_string_model_id_raises(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_routing:\n  bad-role:\n    - max_loc: null\n      model_chain: [123]\n",
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_tier_with_negative_max_loc_raises(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_routing:\n  bad-role:\n    - max_loc: -5\n      model_chain: [\"m\"]\n",
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_tier_with_non_int_max_loc_raises(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        "model_routing:\n  bad-role:\n    - max_loc: \"lots\"\n      model_chain: [\"m\"]\n",
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_non_list_tiers_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing:\n  bad-role: not-a-list\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_non_mapping_tier_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing:\n  bad-role:\n    - not-a-mapping\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_malformed_yaml_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "model_routing: [this is not, a mapping\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_non_mapping_top_level_document_raises(tmp_path: Path) -> None:
    _write_config(tmp_path, "- just\n- a\n- list\n")
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_invalid_role_name_token_raises(tmp_path: Path) -> None:
    _write_config(
        tmp_path,
        'model_routing:\n  "bad role with spaces":\n    - max_loc: null\n      model_chain: ["m"]\n',
    )
    with pytest.raises(InvalidModelRoutingConfigError):
        load_model_routing(tmp_path)


def test_caller_supplied_mapping_bypasses_file_lookup() -> None:
    """model_routing= lets a caller compose with an already-loaded mapping
    (mirrors provisioning.roles.resolve_role_verbs's role_verbs= param)."""
    mapping = {"custom-role": ({"max_loc": None, "model_chain": ("only-model",)},)}
    assert resolve_model_chain("custom-role", 0, model_routing=mapping) == ("only-model",)


def test_caller_supplied_mapping_missing_open_ended_tier_fails_closed() -> None:
    """A hand-built mapping (bypassing load_model_routing's own tier-order
    validation) that omits an open-ended top tier must still fail closed
    rather than silently returning an empty chain for an out-of-range
    changed_lines value."""
    mapping = {"custom-role": ({"max_loc": 100, "model_chain": ("only-model",)},)}
    with pytest.raises(InvalidModelRoutingConfigError):
        resolve_model_chain("custom-role", 500, model_routing=mapping)


def test_legacy_path_is_read_when_new_path_absent(tmp_path: Path, capsys) -> None:
    """Transitional back-compat (lr-446c35): a repo that has not yet
    migrated off .loadout/config.yaml is still read, with a one-line
    deprecation warning to stderr. Removed after the fleet migration
    (lr-a645aa)."""
    _write_legacy_config(
        tmp_path,
        'model_routing:\n  legacy-role:\n    - max_loc: null\n      model_chain: ["m"]\n',
    )

    resolved = load_model_routing(tmp_path)

    assert resolved == {"legacy-role": ({"max_loc": None, "model_chain": ("m",)},)}
    stderr = capsys.readouterr().err
    assert "deprecated" in stderr
    assert stderr.count("\n") == 1


def test_new_path_wins_when_both_present(tmp_path: Path, capsys) -> None:
    _write_legacy_config(
        tmp_path,
        'model_routing:\n  legacy-role:\n    - max_loc: null\n      model_chain: ["m"]\n',
    )
    _write_config(
        tmp_path,
        'model_routing:\n  new-role:\n    - max_loc: null\n      model_chain: ["n"]\n',
    )

    resolved = load_model_routing(tmp_path)

    assert resolved == {"new-role": ({"max_loc": None, "model_chain": ("n",)},)}
    assert capsys.readouterr().err == ""
