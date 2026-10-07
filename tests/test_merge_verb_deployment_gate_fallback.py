"""A repo with no tracked gate file at base falls back to the deployment
config.yaml's gate keys. `gate_from_deployment_config` returns an empty gate
for a merge section it cannot parse, which is safe only because the merge verb
refuses a malformed deployment config before anything merges. These tests pin
that through the real verb: merge_pr must never be reached."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH
from tests._gate_repo import init_gate_repo
from tests.test_merge_verb_pre_checks import (
    _AllowingAuthorityProvider,
    _RecordingTokenProvider,
    _base_args,
    _make_opener,
)

_MALFORMED_DEPLOYMENT_CONFIGS = {
    "merge_not_a_mapping": b"merge:\n  - just\n  - a list\n",
    "merge_scalar": b"merge: nope\n",
    "pre_checks_not_a_list": b"merge:\n  sync_tree_after_merge: false\n  pre_checks: not-a-list\n",
    "pre_checks_step_without_cmd": (
        b"merge:\n  sync_tree_after_merge: false\n  pre_checks:\n    - description: no cmd key\n"
    ),
    "non_utf8": b"merge:\n  note: \xff\xfe\n",
}


@pytest.mark.parametrize("shape", sorted(_MALFORMED_DEPLOYMENT_CONFIGS))
def test_malformed_deployment_gate_refuses_before_merge_pr(tmp_path, shape):
    repo = init_gate_repo(tmp_path, tracked_gate=None)
    (tmp_path / DEFAULT_CONFIG_RELATIVE_PATH).write_bytes(_MALFORMED_DEPLOYMENT_CONFIGS[shape])

    merge_calls: list[str] = []
    code = verb.main(
        _base_args(**{"--repo-path": str(repo.path)}),
        token_provider=_RecordingTokenProvider(),
        authority_provider=_AllowingAuthorityProvider(),
        opener=_make_opener(pr_info=repo.pr_info(), merge_calls=merge_calls),
    )

    assert code in (verb.EXIT_PRE_CHECKS_FAILED, verb.EXIT_POST_MERGE_FAILED)
    assert merge_calls == []
