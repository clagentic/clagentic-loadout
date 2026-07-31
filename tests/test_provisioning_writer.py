"""test_provisioning_writer.py — idempotent in-place JSON merge (lr-4e04).

Covers: file-creation when absent, no duplication on repeat merge, existing
unrelated entries/keys preserved byte-for-byte (never reordered/removed),
refusal on an incompatible existing shape, and (lr-3dfe) the write-path
hardening: symlink refusal, atomic-write failure safety, and explicit file
mode.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest import mock

import pytest

from clagentic_loadout.provisioning.writer import (
    SETTINGS_FILE_MODE,
    SettingsWriteError,
    merge_fragment_into_settings,
)


def test_creates_file_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "settings.json"
    result = merge_fragment_into_settings(target, ["Bash(loadout-push:*)", "Bash(loadout-push *)"])
    assert result == ["Bash(loadout-push:*)", "Bash(loadout-push *)"]
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc == {"permissions": {"allow": ["Bash(loadout-push:*)", "Bash(loadout-push *)"]}}


def test_repeat_merge_is_idempotent_no_duplicates(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    fragment = ["Bash(loadout-push:*)", "Bash(loadout-push *)"]
    merge_fragment_into_settings(target, fragment)
    result = merge_fragment_into_settings(target, fragment)
    assert result == fragment
    assert len(result) == len(set(result))


def test_existing_unrelated_keys_and_entries_preserved(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(
        json.dumps(
            {
                "someOtherTopLevelKey": "untouched",
                "permissions": {
                    "allow": ["Bash(some-other-tool:*)"],
                    "deny": ["Bash(rm -rf /:*)"],
                },
            }
        ),
        encoding="utf-8",
    )
    result = merge_fragment_into_settings(target, ["Bash(loadout-merge:*)", "Bash(loadout-merge *)"])
    assert result == [
        "Bash(some-other-tool:*)",
        "Bash(loadout-merge:*)",
        "Bash(loadout-merge *)",
    ]
    doc = json.loads(target.read_text(encoding="utf-8"))
    assert doc["someOtherTopLevelKey"] == "untouched"
    assert doc["permissions"]["deny"] == ["Bash(rm -rf /:*)"]


def test_second_roles_fragment_does_not_disturb_first_roles_entries(tmp_path: Path) -> None:
    """Two DIFFERENT roles' fragments merged in sequence -- proves the
    per-role generation + merge sequence composes without one role's
    provisioning clobbering another's (the property that makes the
    rejected 'one global fragment' shape unnecessary)."""
    target = tmp_path / "settings.json"
    merge_fragment_into_settings(target, ["Bash(loadout-push:*)", "Bash(loadout-push *)"])
    result = merge_fragment_into_settings(target, ["Bash(loadout-merge:*)", "Bash(loadout-merge *)"])
    assert set(result) == {
        "Bash(loadout-push:*)",
        "Bash(loadout-push *)",
        "Bash(loadout-merge:*)",
        "Bash(loadout-merge *)",
    }


def test_non_object_top_level_raises(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(SettingsWriteError):
        merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])


def test_permissions_not_an_object_raises(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"permissions": "not-an-object"}), encoding="utf-8")
    with pytest.raises(SettingsWriteError):
        merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])


def test_allow_not_a_list_raises(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text(json.dumps({"permissions": {"allow": "not-a-list"}}), encoding="utf-8")
    with pytest.raises(SettingsWriteError):
        merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])


def test_malformed_json_raises(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    target.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(SettingsWriteError):
        merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])


def test_symlink_target_refused_with_resolved_path_in_error(tmp_path: Path) -> None:
    real_file = tmp_path / "real-settings.json"
    real_file.write_text(json.dumps({"permissions": {"allow": []}}), encoding="utf-8")
    symlink_target = tmp_path / "settings.json"
    symlink_target.symlink_to(real_file)

    with pytest.raises(SettingsWriteError) as exc_info:
        merge_fragment_into_settings(symlink_target, ["Bash(loadout-push:*)"])

    message = str(exc_info.value)
    assert str(symlink_target) in message
    assert str(real_file.resolve()) in message
    # Refusal means the symlink's target is never touched.
    assert json.loads(real_file.read_text(encoding="utf-8")) == {
        "permissions": {"allow": []}
    }


def test_symlink_to_nonexistent_target_also_refused(tmp_path: Path) -> None:
    """A dangling symlink must be refused too -- `.exists()` would report
    False for it (since it follows the link to a target that isn't
    there), so the guard must not be based on existence."""
    dangling_target = tmp_path / "nowhere.json"
    symlink_target = tmp_path / "settings.json"
    symlink_target.symlink_to(dangling_target)

    with pytest.raises(SettingsWriteError) as exc_info:
        merge_fragment_into_settings(symlink_target, ["Bash(loadout-push:*)"])

    assert str(symlink_target) in str(exc_info.value)
    assert not dangling_target.exists()


def test_atomic_write_leaves_original_intact_on_mid_merge_failure(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    original_doc = {"permissions": {"allow": ["Bash(existing:*)"]}}
    target.write_text(json.dumps(original_doc), encoding="utf-8")

    with mock.patch("os.replace", side_effect=OSError("simulated mid-write failure")):
        with pytest.raises(OSError):
            merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])

    # Original file is untouched -- not partial, not corrupt, not merged.
    assert json.loads(target.read_text(encoding="utf-8")) == original_doc

    # No leftover temp file in the target directory.
    leftover = [p for p in tmp_path.iterdir() if p != target]
    assert leftover == []


def test_atomic_write_creates_no_file_on_failure_when_target_absent(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"

    with mock.patch("os.replace", side_effect=OSError("simulated mid-write failure")):
        with pytest.raises(OSError):
            merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])

    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_written_file_has_explicit_restrictive_mode(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    merge_fragment_into_settings(target, ["Bash(loadout-push:*)"])

    mode = stat.S_IMODE(os.stat(target).st_mode)
    assert mode == SETTINGS_FILE_MODE


def test_happy_path_merge_result_unchanged_by_hardening(tmp_path: Path) -> None:
    """Regression: the RESULT of a valid-path, no-symlink, successful merge
    is byte-for-byte identical to pre-hardening behavior -- only the write
    mechanism (atomic temp-file + rename) and failure-mode safety change."""
    target = tmp_path / "nested" / "settings.json"
    fragment = ["Bash(loadout-push:*)", "Bash(loadout-push *)"]
    result = merge_fragment_into_settings(target, fragment)

    assert result == fragment
    assert target.read_text(encoding="utf-8") == (
        json.dumps(
            {"permissions": {"allow": fragment}},
            indent=2,
        )
        + "\n"
    )
