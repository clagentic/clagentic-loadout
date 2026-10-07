"""Optional display labels for a profile's engines, and the strict JSON decode
the acquire backend shares with the tolerant parse every write path uses."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.review.profile_config import ReviewProfileError, load_review_profile
from clagentic_loadout.transport import git_host_api


def _load(tmp_path, **entry):
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": {"reviewer": {"carrier": ["c"], **entry}}}}),
        encoding="utf-8",
    )
    return load_review_profile("reviewer", config_root=tmp_path)


def test_labels_default_to_none(tmp_path):
    profile = _load(tmp_path)

    assert profile.carrier_model is None and profile.fallback_model is None


def test_labels_are_read_and_stripped(tmp_path):
    profile = _load(tmp_path, fallback=["f"], carrier_model=" big ", fallback_model="small")

    assert (profile.carrier_model, profile.fallback_model) == ("big", "small")


@pytest.mark.parametrize("value", ["", "   ", "two\nlines", "x" * 101, 5, ["a"]])
def test_a_bad_label_is_refused(tmp_path, value):
    with pytest.raises(ReviewProfileError, match="fallback_model"):
        _load(tmp_path, fallback=["f"], fallback_model=value)


def test_a_repo_level_label_is_ignored_like_a_repo_level_command(tmp_path, capsys):
    repo = tmp_path / "repo"
    (repo / ".clagentic" / "loadout").mkdir(parents=True)
    (repo / ".clagentic" / "loadout" / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": {"reviewer": {"fallback_model": "hostile"}}}}),
        encoding="utf-8",
    )
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": {"reviewer": {"carrier": ["c"]}}}}),
        encoding="utf-8",
    )

    profile = load_review_profile("reviewer", config_root=tmp_path, repo_root=repo)

    assert profile.fallback_model is None
    assert "fallback_model" in capsys.readouterr().err


def test_decode_json_body_is_strict_where_parse_json_body_is_tolerant():
    assert git_host_api.decode_json_body(b'{"a": 1}') == {"a": 1}
    assert git_host_api.parse_json_body(b"not json") == {}
    with pytest.raises(ValueError):
        git_host_api.decode_json_body(b"not json")
    with pytest.raises(ValueError):
        git_host_api.decode_json_body(b"\xff\xfe")
    with pytest.raises(ValueError):
        git_host_api.decode_json_body(b"")
