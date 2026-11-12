"""merge.tree_paths: a file's path inside a git tree is lexical, never a symlink target."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.tree_paths import lexical_tree_relative_path


@pytest.mark.parametrize(
    ("make_file", "expected"),
    [
        ("plain", ".clagentic/loadout/config.yaml"),
        ("link_to_dev_null", ".clagentic/loadout/config.yaml"),
        ("link_outside_tree", ".clagentic/loadout/config.yaml"),
        ("link_to_other_in_tree_path", ".clagentic/loadout/config.yaml"),
        ("above_the_tree", None),
    ],
)
def test_the_relative_path_is_lexical_and_ignores_where_a_symlink_points(tmp_path, make_file, expected):
    tree = tmp_path / "tree"
    config = tree / ".clagentic" / "loadout" / "config.yaml"
    config.parent.mkdir(parents=True)
    (tree / "other.yaml").write_text("x\n", encoding="utf-8")
    outside = tmp_path / "outside.yaml"
    outside.write_text("x\n", encoding="utf-8")
    if make_file == "plain":
        config.write_text("x\n", encoding="utf-8")
    elif make_file == "link_to_dev_null":
        config.symlink_to("/dev/null")
    elif make_file == "link_outside_tree":
        config.symlink_to(outside)
    elif make_file == "link_to_other_in_tree_path":
        config.symlink_to(tree / "other.yaml")
    else:
        config = tmp_path / "config.yaml"

    assert lexical_tree_relative_path(config, tree) == expected
