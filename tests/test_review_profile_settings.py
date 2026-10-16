"""The deployment's repo profile settings come from the user-level config."""

from __future__ import annotations

import pytest
import yaml

from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review.profile_settings import ProfileSettings, load_profile_settings


def _config(root, review_section) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "config.yaml").write_text(yaml.safe_dump({"review": review_section}), encoding="utf-8")


def test_nothing_configured_is_the_defaults(tmp_path):
    assert load_profile_settings(tmp_path / "absent") == ProfileSettings()
    _config(tmp_path, {"profiles": {}})
    assert load_profile_settings(tmp_path) == ProfileSettings()
    assert ProfileSettings().require is False


def test_a_denial_list_and_require_are_read(tmp_path):
    _config(
        tmp_path,
        {
            "repo_profile": {
                "require": True,
                "deny": {"names": ["agent-alpha"], "hosts": ["host-one.example.test"]},
            }
        },
    )

    settings = load_profile_settings(tmp_path)

    assert settings.require is True
    assert settings.deny == rp.ProfileDeny(
        names=("agent-alpha",), hosts=("host-one.example.test",)
    )


def test_a_repository_config_is_never_a_source(tmp_path):
    root = tmp_path / "user"
    root.mkdir()
    repo_cfg = tmp_path / "repo" / ".clagentic" / "loadout"
    repo_cfg.mkdir(parents=True)
    (repo_cfg / "config.yaml").write_text(
        yaml.safe_dump({"review": {"repo_profile": {"require": True}}}), encoding="utf-8"
    )

    assert load_profile_settings(root) == ProfileSettings()


@pytest.mark.parametrize(
    "section, key_path",
    [
        ("text", "review.repo_profile must be a mapping"),
        ({"strict": True}, "review.repo_profile.strict"),
        ({"require": "yes"}, "review.repo_profile.require must be true or false"),
        ({"require": 1}, "review.repo_profile.require must be true or false"),
        ({"deny": ["a"]}, "review.repo_profile.deny must be a mapping"),
        ({"deny": {"names": [""]}}, "review.repo_profile.deny.names[0]"),
        ({"deny": {"emails": ["a"]}}, "review.repo_profile.deny.emails"),
    ],
)
def test_malformed_settings_are_refused_naming_the_key_path(tmp_path, section, key_path):
    _config(tmp_path, {"repo_profile": section})

    with pytest.raises(rp.ProfileFormatError) as caught:
        load_profile_settings(tmp_path)

    assert key_path in str(caught.value)
