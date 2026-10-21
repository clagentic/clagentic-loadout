"""The profile's `test` block: its strict schema, and its place in the profile."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.repo_test.spec import TestBlockError, parse_test_block
from clagentic_loadout.review import repo_profile as rp


def _profile(test_block: object) -> dict:
    return {"class": "internal", "test": test_block}


def test_a_full_block_parses_with_every_key() -> None:
    block = {
        "full": ["run", "{junit}"],
        "impacted": ["run", "{junit}", "{base}"],
        "files": ["run", "{junit}"],
        "base": "origin/main",
    }

    parsed = parse_test_block(block)

    assert parsed.full == ("run", "{junit}")
    assert parsed.impacted == ("run", "{junit}", "{base}")
    assert parsed.base == "origin/main"
    assert parsed.declared_keys() == ["full", "impacted", "files", "base"]


def test_duplicate_argv_entries_are_kept() -> None:
    parsed = parse_test_block({"full": ["run", "-v", "-v", "{junit}"]})

    assert parsed.full == ("run", "-v", "-v", "{junit}")


@pytest.mark.parametrize(
    "block,fragment",
    [
        ({"fulll": ["x", "{junit}"]}, "test.fulll"),
        ({"full": "run {junit}"}, "test.full must be a non-empty list"),
        ({"full": []}, "test.full must be a non-empty list"),
        ({"full": ["run"]}, "{junit}"),
        ({"full": ["run", "{junit}", "{base}"]}, "only impacted"),
        ({"full": ["run", 3, "{junit}"]}, "test.full[1]"),
        ({"full": ["run", "a\nb", "{junit}"]}, "test.full[1]"),
        ({"impacted": ["run", "{junit}"], "base": "-rf"}, "test.base"),
        ("full", "test must be a mapping"),
    ],
)
def test_a_malformed_block_is_refused_naming_the_key(block: object, fragment: str) -> None:
    with pytest.raises(TestBlockError) as caught:
        parse_test_block(block)

    assert fragment in str(caught.value)


def test_the_profile_carries_the_block_and_names_the_key_when_it_is_bad() -> None:
    good = rp.parse_profile_document(yaml.safe_load("class: internal\ntest:\n  full: [run, '{junit}']\n"))
    assert good.test is not None and good.test.full == ("run", "{junit}")

    with pytest.raises(rp.ProfileFormatError, match=r"test\.full"):
        rp.parse_profile_document(_profile({"full": ["run"]}))


def test_the_shipped_example_and_this_repositorys_own_profile_declare_test_commands() -> None:
    from pathlib import Path

    loadout_dir = Path(__file__).resolve().parent.parent / ".clagentic" / "loadout"
    for name in ("profile.yaml.example", "profile.yaml"):
        profile = rp.parse_profile_document(yaml.safe_load((loadout_dir / name).read_text(encoding="utf-8")))
        assert profile.test is not None and profile.test.full is not None, name


def test_a_profile_without_a_block_has_no_test_declaration() -> None:
    assert rp.parse_profile_document({"class": "internal"}).test is None
    assert rp.parse_profile_document({"class": "internal", "test": {}}).test is None
