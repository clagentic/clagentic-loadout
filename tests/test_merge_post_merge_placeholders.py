"""`{merged_sha}` / `{merge_tree}` substitution in post_merge_steps argv.

Unit level: `run_post_merge_steps(..., placeholders=...)`. The verb-level rows (both
verbs, both tree cases) live in test_merge_tool_merge_tree.py beside their harness.
"""

from __future__ import annotations

import json
import shlex
import sys

import pytest

from clagentic_loadout.merge import post_merge

_PY = sys.executable
_SHA = "0123456789abcdef0123456789abcdef01234567"
_TREE = "/trees/merged"
_VALUES = {post_merge.PLACEHOLDER_MERGED_SHA: _SHA, post_merge.PLACEHOLDER_MERGE_TREE: _TREE}
_DUMP = "import json,sys;open(sys.argv[1],'w').write(json.dumps(sys.argv[2:]))"


def _run(tmp_path, cmd, *, placeholders=_VALUES, **step):
    out = tmp_path / "argv.json"
    if isinstance(cmd, str):
        full = f"{_PY} -c {shlex.quote(_DUMP)} {out} {cmd}"
    else:
        full = [_PY, "-c", _DUMP, str(out), *cmd]
    post_merge.run_post_merge_steps(
        [{"cmd": full, "on_failure": "fail", **step}], tmp_path, placeholders=placeholders
    )
    return json.loads(out.read_text())


@pytest.mark.parametrize("form", ["string", "list"])
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        pytest.param(["COMMIT={merged_sha}"], [f"COMMIT={_SHA}"], id="merged-sha"),
        pytest.param(["{merge_tree}/bin"], [f"{_TREE}/bin"], id="merge-tree"),
        pytest.param(["{merged_sha}:{merge_tree}"], [f"{_SHA}:{_TREE}"], id="both-in-one-token"),
        pytest.param(["plain", "--flag=1"], ["plain", "--flag=1"], id="no-placeholder"),
        pytest.param(["{print $1}"], ["{print $1}"], id="awk-braces"),
        pytest.param(["{print}", "{}", "{1}"], ["{print}", "{}", "{1}"], id="brace-lookalikes"),
    ],
)
def test_substitution_table(tmp_path, form, tokens, expected):
    cmd = " ".join(shlex.quote(t) for t in tokens) if form == "string" else tokens
    assert _run(tmp_path, cmd) == expected


def test_a_substituted_value_is_not_expanded_again(tmp_path):
    values = {**_VALUES, post_merge.PLACEHOLDER_MERGE_TREE: "{merged_sha}"}
    assert _run(tmp_path, ["{merge_tree}"], placeholders=values) == ["{merged_sha}"]


def test_unknown_placeholder_is_reported_and_the_step_still_runs(tmp_path, capsys):
    argv = _run(tmp_path, ["--x={nope}", "{merged_sha}"])
    assert argv == ["--x={nope}", _SHA]
    err = capsys.readouterr().err
    assert (
        "post_merge_steps[0]: unknown placeholder '{nope}' in argv token '--x={nope}'; "
        "left unchanged (documented names: {merge_tree}, {merged_sha})"
    ) in err


def test_no_placeholders_supplied_leaves_every_token_alone(tmp_path, capsys):
    assert _run(tmp_path, ["{merged_sha}", "{nope}"], placeholders=None) == ["{merged_sha}", "{nope}"]
    assert "unknown placeholder" not in capsys.readouterr().err


def test_verify_argv_is_substituted(tmp_path):
    marker = tmp_path / "verify.json"
    code = f"import json,sys;open({str(marker)!r},'w').write(json.dumps(sys.argv[1:]))"
    post_merge.run_post_merge_steps(
        [{"cmd": [_PY, "-c", "pass"], "verify": [_PY, "-c", code, "{merged_sha}"]}],
        tmp_path,
        placeholders=_VALUES,
    )
    assert json.loads(marker.read_text()) == [_SHA]


def test_liveness_probe_argv_is_not_substituted(tmp_path):
    step = {
        "cmd": [_PY, "-c", "pass"],
        "detaches": True,
        "liveness_probe": {"cmd": ["echo", "{merged_sha}"], "poll_interval_seconds": 0.01, "max_polls": 2},
    }
    with pytest.raises(post_merge.PostMergeLivenessError) as exc:
        post_merge.run_post_merge_steps([step], tmp_path, placeholders=_VALUES)
    assert "{merged_sha}" in str(exc.value)
