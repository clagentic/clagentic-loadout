"""test_guard_dispatch_config.py — the user-level reader that feeds a
deployment's extra trivial directory names to guard.dispatch_discipline."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.guard.dispatch_config import load_trivial_dir_segments
from clagentic_loadout.guard.dispatch_discipline import (
    DEFAULT_TRIVIAL_DIR_SEGMENTS,
    is_trivial_path,
)


def _write(tmp_path, content) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


def test_no_config_yields_just_the_default(tmp_path):
    assert load_trivial_dir_segments(config_root=tmp_path) == DEFAULT_TRIVIAL_DIR_SEGMENTS


def test_configured_segments_extend_the_default(tmp_path):
    _write(tmp_path, {"guard": {"trivial_dir_segments": [".crew", ".lore"]}})
    assert load_trivial_dir_segments(config_root=tmp_path) == frozenset(
        {"docs", ".crew", ".lore"}
    )


def test_configured_segments_drive_the_predicate(tmp_path):
    _write(tmp_path, {"guard": {"trivial_dir_segments": [".crew"]}})
    segments = load_trivial_dir_segments(config_root=tmp_path)
    assert is_trivial_path("/p/.crew/x.yaml", segments) is True
    assert is_trivial_path("/p/.lore/x", segments) is False
    assert is_trivial_path("/p/docs/x", segments) is True


@pytest.mark.parametrize("bad", ["x", 3, {"a": 1}, None])
def test_non_list_value_is_ignored(tmp_path, bad):
    _write(tmp_path, {"guard": {"trivial_dir_segments": bad}})
    assert load_trivial_dir_segments(config_root=tmp_path) == DEFAULT_TRIVIAL_DIR_SEGMENTS


def test_bad_entries_are_dropped_good_ones_kept(tmp_path):
    _write(tmp_path, {"guard": {"trivial_dir_segments": [".ok", "", "  ", 5, "a/b"]}})
    assert load_trivial_dir_segments(config_root=tmp_path) == frozenset({"docs", ".ok"})
