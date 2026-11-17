"""`clagentic-loadout lint` and `build`: run the commands a repo profile declares.

Both verbs are one code path with a different block name, so every row runs for
each block. Entries are tiny `python -c` commands; no real linter or compiler runs.
"""

from __future__ import annotations

import json
import sys

import pytest
import yaml

from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.repo_checks import exits, store
from clagentic_loadout.repo_checks.verb import main
from clagentic_loadout.review.policy_source import PROFILE_PATH

BLOCKS = ["lint", "build"]


def _py(code: str, **extra) -> dict:
    return {"argv": [sys.executable, "-c", code], **extra}


def _exit(code: int) -> dict:
    return _py(f"import sys; sys.exit({code})")


@pytest.fixture(autouse=True)
def _reports_root(monkeypatch, tmp_path):
    monkeypatch.setenv(store.KIND.env_var, str(tmp_path.parent / f"{tmp_path.name}-check-reports"))
    monkeypatch.setenv("PASS_ME", "yes")
    monkeypatch.setenv("NOT_ME", "yes")


def _repo(tmp_path, document: dict | str | None):
    """A directory holding a profile of *document* (a mapping, raw YAML text, or None for none)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    if document is not None:
        text = document if isinstance(document, str) else yaml.safe_dump(document, sort_keys=False)
        (repo / PROFILE_PATH).parent.mkdir(parents=True)
        (repo / PROFILE_PATH).write_text(text, encoding="utf-8")
    return repo


def _profile(block: str, entries: dict) -> dict:
    return {"class": "internal", block: entries}


def _run(capsys, block: str, repo, *extra: str):
    capsys.readouterr()
    code = main(["run", "--repo-root", str(repo), *extra], block=block)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run_id(out: str) -> str:
    return out.splitlines()[0].rsplit(" ", 1)[1]


def _report(capsys, block: str, run_id: str) -> dict:
    capsys.readouterr()
    assert main(["report", run_id], block=block) == exits.EXIT_OK
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    "codes, expected_exit, ran",
    [
        ([0, 0, 0], exits.EXIT_OK, ["a", "b", "c"]),
        ([0, 3, 0], exits.EXIT_CHECK_FAILED, ["a", "b"]),
        ([5, 0, 0], exits.EXIT_CHECK_FAILED, ["a"]),
    ],
    ids=["all-pass", "second-fails", "first-fails"],
)
def test_entries_run_in_order_and_stop_at_the_first_failure(tmp_path, capsys, block, codes, expected_exit, ran):
    repo = _repo(tmp_path, _profile(block, {n: _exit(c) for n, c in zip("abc", codes, strict=True)}))

    code, out, _ = _run(capsys, block, repo)

    assert code == expected_exit
    report = _report(capsys, block, _run_id(out))
    assert [e["name"] for e in report["entries"]] == ran
    assert report["not_run"] == [n for n in "abc" if n not in ran]
    assert report["state"] == ("passed" if expected_exit == exits.EXIT_OK else "failed")
    for name in ran:
        assert f"  {name}: " in out
    assert f"run {report['run_id']} {report['state']}" in out


@pytest.mark.parametrize("block", BLOCKS)
def test_the_command_runs_in_the_repo_root_without_a_shell_and_with_the_allowlisted_env(
    tmp_path, capsys, block, monkeypatch
):
    probe = "import os, sys; print(os.getcwd(), os.environ.get('PASS_ME'), os.environ.get('NOT_ME'), sys.argv[1])"
    entry = {
        "argv": [sys.executable, "-c", probe, "x; echo $HOME"],
        "env_passthrough": ["PASS_ME"],
    }
    repo = _repo(tmp_path, _profile(block, {"probe": entry}))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    code, out, _ = _run(capsys, block, repo)

    assert code == exits.EXIT_OK
    tail = _report(capsys, block, _run_id(out))["entries"][0]["stdout_tail"]
    assert tail.strip() == f"{repo.resolve()} yes None x; echo $HOME"


@pytest.mark.parametrize("block", BLOCKS)
def test_a_timeout_ends_the_run_and_is_recorded(tmp_path, capsys, block):
    repo = _repo(
        tmp_path,
        _profile(block, {"slow": _py("import time; time.sleep(60)", timeout_seconds=0.5), "after": _exit(0)}),
    )

    code, out, err = _run(capsys, block, repo)

    assert code == exits.EXIT_CHECK_FAILED
    report = _report(capsys, block, _run_id(out))
    entry = report["entries"][0]
    assert entry["timed_out"] is True and entry["exit_code"] is None and entry["passed"] is False
    assert "TIMEOUT" in out and len(report["entries"]) == 1 and report["not_run"] == ["after"]


@pytest.mark.parametrize("block", BLOCKS)
def test_a_command_that_cannot_start_fails_the_run(tmp_path, capsys, block):
    repo = _repo(tmp_path, _profile(block, {"missing": {"argv": ["no-such-command-here"]}}))

    code, out, _ = _run(capsys, block, repo)

    assert code == exits.EXIT_CHECK_FAILED
    assert _report(capsys, block, _run_id(out))["entries"][0]["start_error"]


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    "document",
    [
        None,
        "class: internal\n",
        "class: internal\nlint: {}\nbuild: {}\n",
        "class: internal\nlint: null\nbuild: null\n",
    ],
    ids=["no-profile", "no-block", "empty-mapping", "null-block"],
)
def test_nothing_declared_exits_not_declared_runs_nothing_and_names_the_path_and_block(
    tmp_path, capsys, block, document
):
    repo = _repo(tmp_path, document)

    code, out, err = _run(capsys, block, repo)

    assert code == exits.EXIT_NOT_DECLARED
    assert PROFILE_PATH in err and block in err and str(repo.resolve()) in err
    assert out == ""
    assert not store.reports_root().exists()


@pytest.mark.parametrize("block", BLOCKS)
def test_only_the_other_blocks_entries_declared_is_not_declared(tmp_path, capsys, block):
    other = "build" if block == "lint" else "lint"
    repo = _repo(tmp_path, _profile(other, {"a": _exit(0)}))

    code, _, err = _run(capsys, block, repo)

    assert code == exits.EXIT_NOT_DECLARED and f"`{block}`" in err


@pytest.mark.parametrize("block", BLOCKS)
def test_only_runs_one_entry_and_an_unknown_name_is_usage_listing_the_declared_names(tmp_path, capsys, block):
    repo = _repo(tmp_path, _profile(block, {"a": _exit(0), "b": _exit(0)}))

    code, out, _ = _run(capsys, block, repo, "--only", "b")
    report = _report(capsys, block, _run_id(out))
    assert code == exits.EXIT_OK
    assert [e["name"] for e in report["entries"]] == ["b"] and report["only"] == "b"

    code, out, err = _run(capsys, block, repo, "--only", "nope")
    assert code == exits.EXIT_USAGE and out == ""
    assert "nope" in err and "a, b" in err


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize(
    "entries, key_path",
    [
        ("[a]", "{block} must be a mapping"),
        ("{a: x}", "{block}.a must be a mapping"),
        ("{a: {}}", "{block}.a.argv"),
        ("{a: {argv: [x, 3]}}", "{block}.a.argv"),
        ("{a: {argv: [x], timeout_seconds: 0}}", "{block}.a.timeout_seconds"),
        ("{a: {argv: [x], env_passthrough: [bad-name]}}", "{block}.a.env_passthrough"),
        ("{a: {argv: [x], shell: true}}", "{block}.a.shell"),
    ],
)
def test_a_malformed_block_exits_invalid_naming_the_key_path_before_running_anything(
    tmp_path, capsys, block, entries, key_path
):
    repo = _repo(tmp_path, f"class: internal\n{block}: {entries}\n")

    code, out, err = _run(capsys, block, repo)

    assert code == exits.EXIT_PROFILE_INVALID
    assert key_path.format(block=block) in err and out == ""
    assert not store.reports_root().exists()


@pytest.mark.parametrize("block", BLOCKS)
def test_the_report_holds_every_field_and_redacts_a_secret_shaped_token(tmp_path, capsys, block):
    repo = _repo(
        tmp_path,
        _profile(block, {"noisy": _py("import sys; print('Bearer abcdefgh123456'); print('oops', file=sys.stderr)")}),
    )

    code, out, _ = _run(capsys, block, repo)
    report = _report(capsys, block, _run_id(out))

    assert code == exits.EXIT_OK
    assert report["block"] == block and report["repo"]["path"] == str(repo.resolve())
    assert {"head_sha", "dirty", "started_at", "finished_at", "declared", "state"} <= set(report)
    entry = report["entries"][0]
    assert {"name", "argv", "exit_code", "timed_out", "stdout_tail", "stderr_tail"} <= set(entry)
    assert "abcdefgh123456" not in entry["stdout_tail"] and "[REDACTED]" in entry["stdout_tail"]
    assert entry["stderr_tail"].strip() == "oops"


@pytest.mark.parametrize("block", BLOCKS)
@pytest.mark.parametrize("run_id", ["20260101T000000Z-1", "not-an-id", "../escape"])
def test_an_unknown_or_malformed_run_id_exits_run_not_found(tmp_path, capsys, block, run_id):
    capsys.readouterr()

    assert main(["report", run_id], block=block) == exits.EXIT_RUN_NOT_FOUND
    assert run_id in capsys.readouterr().err


def test_a_run_id_of_the_other_verb_is_not_found_and_names_the_right_verb(tmp_path, capsys):
    repo = _repo(tmp_path, _profile("lint", {"a": _exit(0)}))
    _, out, _ = _run(capsys, "lint", repo)
    capsys.readouterr()

    assert main(["report", _run_id(out)], block="build") == exits.EXIT_RUN_NOT_FOUND
    assert "clagentic-loadout lint report" in capsys.readouterr().err


def test_an_unreadable_recorded_report_exits_report_unreadable(tmp_path, capsys):
    root = store.reports_root()
    run_id, run_dir = store.create_run_dir(root)
    (run_dir / store.REPORT_FILE).write_text("[1]", encoding="utf-8")
    capsys.readouterr()

    assert main(["report", run_id], block="lint") == exits.EXIT_REPORT_UNREADABLE


def test_two_runs_in_one_second_get_distinct_run_ids(tmp_path, capsys):
    repo = _repo(tmp_path, _profile("lint", {"a": _exit(0)}))

    ids = {_run_id(_run(capsys, "lint", repo)[1]) for _ in range(3)}

    assert len(ids) == 3


def test_a_store_that_cannot_be_written_exits_store_write_before_running(tmp_path, capsys, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    monkeypatch.setenv(store.KIND.env_var, str(blocker / "reports"))
    marker = tmp_path / "ran"
    repo = _repo(tmp_path, _profile("lint", {"a": _py(f"open({str(marker)!r}, 'w').close()")}))

    code, _, err = _run(capsys, "lint", repo)

    assert code == exits.EXIT_STORE_WRITE and not marker.exists() and "blocker" in err


@pytest.mark.parametrize("block", BLOCKS)
def test_the_umbrella_routes_both_verbs(tmp_path, capsys, block):
    repo = _repo(tmp_path, _profile(block, {"a": _exit(0)}))
    capsys.readouterr()

    assert umbrella_main([block, "run", "--repo-root", str(repo)]) == exits.EXIT_OK
    assert f"clagentic-loadout {block}: run " in capsys.readouterr().out
