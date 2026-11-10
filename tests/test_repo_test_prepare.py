"""The optional `test.prepare` argv: run before the tests, and a failure of it ends
the run as unrunnable without running them."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
import yaml
from _repo_test_support import make_repo, profile_text

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.spec import TestBlockError, parse_test_block
from clagentic_loadout.repo_test.verb import main
from clagentic_loadout.review import profile_brief
from clagentic_loadout.review import repo_profile as rp

_SRC = str(Path(__file__).resolve().parents[1] / "src")

# Writes a passing report only when the prepare step's marker exists, and leaves
# its own marker so a test can tell the command ran at all.
_TESTS_NEED_MARKER = (
    "import os, sys\n"
    "open('tests-ran.txt', 'w').write('x')\n"
    "if os.path.exists('prepared.txt'):\n"
    "    open(sys.argv[1], 'w').write('<testsuite><testcase classname=\"a\" name=\"t\"/></testsuite>')\n"
)
_PREPARE_WRITES_MARKER = "open('prepared.txt', 'w').write('x'); print('prepared ok')"
_PREPARE_EXITS_3 = "import sys; print('dependency resolution failed https://u:hunter2secret@h.example.test/'); sys.exit(3)"
_PREPARE_KILLED = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH", "")])))
    return root


def _call(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _repo(tmp_path: Path, prepare: list[str] | None) -> Path:
    (tmp_path / "command.py").write_text(_TESTS_NEED_MARKER)
    keys: dict[str, list[str] | str] = {"full": [sys.executable, str(tmp_path / "command.py"), "{junit}"]}
    if prepare is not None:
        keys["prepare"] = prepare
    return make_repo(tmp_path / "repo", profile=profile_text(**keys))


def _py(source: str) -> list[str]:
    return [sys.executable, "-c", source]


def _result(reports: Path, run_id: str) -> dict:
    return json.loads((reports / run_id / store.RESULT_FILE).read_text())


def test_a_successful_prepare_runs_before_the_tests_and_is_recorded(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, _py(_PREPARE_WRITES_MARKER))

    code, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    summary = json.loads(out)
    assert (code, summary["state"]) == (exits.EXIT_OK, "complete")
    assert summary["prepare"]["exit_code"] == 0
    assert summary["prepare"]["command"] == _py(_PREPARE_WRITES_MARKER)
    assert (reports / summary["run_id"] / store.PREPARE_LOG_FILE).read_text().strip() == "prepared ok"
    _, out, _ = _call(capsys, "report", summary["run_id"], "--summary")
    assert json.loads(out)["prepare"]["exit_code"] == 0


@pytest.mark.parametrize("detach", [False, True])
def test_a_prepare_that_exits_nonzero_stops_the_run_and_names_the_step(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], detach: bool
) -> None:
    repo = _repo(tmp_path, _py(_PREPARE_EXITS_3))

    args = ["run", "full", "--repo-root", str(repo)] + (["--detach"] if detach else [])
    code, out, _ = _call(capsys, *args)
    run_id = json.loads(out)["run_id"]
    if detach:
        assert code == exits.EXIT_OK
    else:
        assert code == exits.EXIT_UNRUNNABLE
    _, out, _ = _call(capsys, "report", run_id, "--wait", "30", "--summary")

    summary = json.loads(out)
    assert summary["state"] == "unrunnable"
    assert "prepare step" in summary["problem"] and "exited 3" in summary["problem"]
    assert summary["prepare"]["exit_code"] == 3
    assert "dependency resolution failed" in summary["failure_log_tail"]
    assert "hunter2secret" not in summary["failure_log_tail"]
    assert not (repo / "tests-ran.txt").exists()


@pytest.mark.parametrize("detach", [False, True])
def test_a_prepare_killed_by_a_signal_names_the_signal(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], detach: bool
) -> None:
    repo = _repo(tmp_path, _py(_PREPARE_KILLED))

    args = ["run", "full", "--repo-root", str(repo)] + (["--detach"] if detach else [])
    _, out, _ = _call(capsys, *args)
    _, out, _ = _call(capsys, "report", json.loads(out)["run_id"], "--wait", "30", "--summary")

    summary = json.loads(out)
    assert summary["state"] == "unrunnable"
    assert "prepare step" in summary["problem"] and "killed by signal 9 (SIGKILL)" in summary["problem"]
    assert not (repo / "tests-ran.txt").exists()


def test_a_prepare_program_that_does_not_exist_is_named(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, ["no-such-prepare-program"])

    code, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    summary = json.loads(out)
    assert (code, summary["state"]) == (exits.EXIT_UNRUNNABLE, "unrunnable")
    assert "prepare step" in summary["problem"] and "no-such-prepare-program" in summary["problem"]
    assert not (repo / "tests-ran.txt").exists()


def test_a_profile_without_prepare_records_no_prepare(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _repo(tmp_path, None)
    (repo / "prepared.txt").write_text("")

    _, out, _ = _call(capsys, "run", "full", "--repo-root", str(repo))

    summary = json.loads(out)
    assert summary["state"] == "complete"
    assert "prepare" not in summary
    assert "prepare" not in _result(reports, summary["run_id"])
    assert not (reports / summary["run_id"] / store.PREPARE_LOG_FILE).exists()


@pytest.mark.parametrize(
    "value,fragment",
    [
        ("make deps", "test.prepare must be a non-empty list"),
        ([], "test.prepare must be a non-empty list"),
        (["run", 3], "test.prepare[1]"),
        (["run", ""], "test.prepare[1]"),
    ],
)
def test_an_invalid_prepare_is_refused_like_any_other_test_key(value: object, fragment: str) -> None:
    with pytest.raises(TestBlockError) as caught:
        parse_test_block({"full": ["run", "{junit}"], "prepare": value})
    assert fragment in str(caught.value)

    with pytest.raises(rp.ProfileFormatError, match=r"test\.prepare"):
        rp.parse_profile_document({"class": "internal", "test": {"prepare": value}})


def test_prepare_parses_without_a_junit_placeholder_and_the_profile_readers_accept_it() -> None:
    document = yaml.safe_load("class: internal\ntest:\n  prepare: [./bin/prepare, --frozen]\n")

    profile = rp.parse_profile_document(document)

    assert profile.test is not None and profile.test.prepare == ("./bin/prepare", "--frozen")
    assert profile.test.declared_keys() == ["prepare"]
    assert profile_brief.declared_tests(profile.test) == {"prepare": ["./bin/prepare", "--frozen"]}
