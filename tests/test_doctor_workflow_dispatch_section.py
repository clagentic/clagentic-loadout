"""loadout-doctor's repo config check knows the `workflow_dispatch` section."""

from __future__ import annotations

from clagentic_loadout.doctor.checks import check_repo_loadout_schema


def _write(repo_root, text: str) -> None:
    cfg = repo_root / ".clagentic" / "loadout"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(text, encoding="utf-8")


def test_valid_section_passes_and_is_not_unknown(tmp_path):
    _write(tmp_path, "workflow_dispatch:\n  - {workflow: build.yml, refs: [main]}\n")
    result = check_repo_loadout_schema(tmp_path)
    assert result.ok is True
    assert result.resolved["errors"] == []
    assert result.resolved["unknown_sections"] == []


def test_malformed_section_fails_with_key_path(tmp_path):
    _write(tmp_path, "workflow_dispatch:\n  - {workflow: build.yml, refs: []}\n")
    result = check_repo_loadout_schema(tmp_path)
    assert result.ok is False
    assert any(
        err.startswith("workflow_dispatch:") and "workflow_dispatch[0].refs" in err
        for err in result.resolved["errors"]
    )


def test_absent_section_is_fine(tmp_path):
    _write(tmp_path, "roles:\n  builder:\n    - push\n")
    assert check_repo_loadout_schema(tmp_path).ok is True
