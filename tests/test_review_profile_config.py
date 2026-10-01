"""Tests for review.profile_config: host defaults, repo-level overrides, and
the refusal to let a repo choose which command runs."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.review.profile_config import (
    ReviewProfileError,
    load_review_profile,
    load_run_root_override,
)


def _user(tmp_path, profiles: dict, **section) -> object:
    root = tmp_path / "user"
    root.mkdir()
    (root / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": profiles, **section}}), encoding="utf-8"
    )
    return root


def _repo(tmp_path, profiles: dict):
    repo = tmp_path / "repo"
    (repo / ".clagentic" / "loadout").mkdir(parents=True)
    (repo / ".clagentic" / "loadout" / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": profiles}}), encoding="utf-8"
    )
    return repo


def test_defaults_and_argv_list(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": ["engine", "--flag"]}})

    profile = load_review_profile("reviewer", config_root=root)

    assert profile.carrier == ("engine", "--flag")
    assert profile.fallback is None
    assert profile.chunk_lines == 600
    assert profile.max_attempts == 3


def test_string_argv_is_shell_split(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": "engine --opt 'two words'"}})

    assert load_review_profile("reviewer", config_root=root).carrier == (
        "engine", "--opt", "two words",
    )


def test_missing_profile_names_the_config_key(tmp_path):
    root = _user(tmp_path, {"other": {"carrier": ["x"]}})

    with pytest.raises(ReviewProfileError, match="review.profiles.reviewer"):
        load_review_profile("reviewer", config_root=root)


@pytest.mark.parametrize(
    "bad",
    [{"carrier": []}, {"carrier": [1]}, {"carrier": ["x"], "chunk_lines": 0},
     {"carrier": ["x"], "timeout_seconds": -1}, {"carrier": ["x"], "max_attempts": 1.5},
     {"carrier": ["x"], "parallel": True}],
)
def test_malformed_values_are_refused(tmp_path, bad):
    root = _user(tmp_path, {"reviewer": bad})

    with pytest.raises(ReviewProfileError):
        load_review_profile("reviewer", config_root=root)


def test_repo_level_may_override_bounds(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": ["x"], "chunk_lines": 600}})
    repo = _repo(tmp_path, {"reviewer": {"chunk_lines": 50, "timeout_seconds": 9}})

    profile = load_review_profile("reviewer", config_root=root, repo_root=repo)

    assert profile.chunk_lines == 50
    assert profile.timeout_seconds == 9.0


def test_repo_level_cannot_choose_the_command(tmp_path, capsys):
    root = _user(tmp_path, {"reviewer": {"carrier": ["trusted-engine"]}})
    repo = _repo(tmp_path, {"reviewer": {"carrier": ["evil"], "fallback": ["evil"]}})

    profile = load_review_profile("reviewer", config_root=root, repo_root=repo)

    assert profile.carrier == ("trusted-engine",)
    assert profile.fallback is None
    assert "honored from the user-level config only" in capsys.readouterr().err


def test_repo_level_rulebook_must_stay_inside_the_repo(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": ["x"]}})
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    repo = _repo(tmp_path, {"reviewer": {"rulebook": "../outside.md"}})

    with pytest.raises(ReviewProfileError, match="outside the repository"):
        load_review_profile("reviewer", config_root=root, repo_root=repo)


def test_repo_level_rulebook_inside_the_repo_is_read(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": ["x"]}})
    repo = _repo(tmp_path, {"reviewer": {"rulebook": "RULES.md"}})
    (repo / "RULES.md").write_text("be kind", encoding="utf-8")

    assert load_review_profile("reviewer", config_root=root, repo_root=repo).rulebook_text == "be kind"


def test_unreadable_rulebook_is_a_profile_error(tmp_path):
    root = _user(tmp_path, {"reviewer": {"carrier": ["x"], "rulebook": str(tmp_path / "nope.md")}})

    with pytest.raises(ReviewProfileError, match="cannot read rulebook"):
        load_review_profile("reviewer", config_root=root)


def test_run_root_override(tmp_path):
    root = _user(tmp_path, {}, run_root=str(tmp_path / "runs"))

    assert load_run_root_override(config_root=root) == tmp_path / "runs"
    assert load_run_root_override(config_root=tmp_path / "missing") is None
