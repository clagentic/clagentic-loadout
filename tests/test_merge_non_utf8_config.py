"""A repo config file that is not valid UTF-8 is a config error every reader
reports in its own terms, never an uncaught UnicodeDecodeError."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge.gate_config import InvalidMergeGateConfigError, load_required_reviewer_roles
from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.post_merge_config import load_post_merge_steps
from clagentic_loadout.merge.pre_checks_config import load_pre_checks
from clagentic_loadout.merge.repo_gate_runtime import load_repo_gate_at_base
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH
from tests._gate_repo import init_gate_repo


def _bad_config(tmp_path):
    path = tmp_path / DEFAULT_CONFIG_RELATIVE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"merge:\n  note: \xff\xfe\n")


@pytest.mark.parametrize("loader", [load_post_merge_steps, load_pre_checks])
def test_step_loaders_report_a_non_utf8_config(tmp_path, loader):
    _bad_config(tmp_path)
    with pytest.raises(PostMergeConfigError):
        loader(tmp_path)


def test_gate_loaders_report_a_non_utf8_config(tmp_path):
    _bad_config(tmp_path)
    with pytest.raises(InvalidMergeGateConfigError):
        load_required_reviewer_roles(tmp_path)


def test_a_non_utf8_deployment_file_does_not_crash_the_runtime_gate_load(tmp_path):
    repo = init_gate_repo(tmp_path, tracked_gate={"required_reviewer_roles": ["reviewer"]})
    _bad_config(tmp_path)
    gate = load_repo_gate_at_base(tmp_path, base_sha=repo.base_sha, base_branch="main")
    # The deployment file names where the git tree is, so an unreadable one
    # leaves nothing to read the base commit from: a warned fallback, no crash.
    assert gate.reviewer_roles == ()
    assert any("could not be read as YAML" in warning for warning in gate.warnings)
