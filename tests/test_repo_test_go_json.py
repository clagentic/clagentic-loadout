"""The go-test-json report format: the converter, the profile key, a recorded run and
the report verbs over it."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from _go_test2json_fixtures import ATTR, BENCHFAIL, ISSUE23920, PANIC, TIMEOUT
from _repo_test_support import make_repo, profile_text, write_run

from clagentic_loadout.repo_test import exits, go_test_json, report_cmd, store
from clagentic_loadout.repo_test.detach import _job_of, _prepared_of
from clagentic_loadout.repo_test.go_test_json import (
    NO_PACKAGE_ID,
    GoStreamError,
    parse_failed_cases,
    parse_go_stream,
)
from clagentic_loadout.repo_test.run_cmd import PreparedRun, RunOutcome, execute_run
from clagentic_loadout.repo_test.spec import TestBlockError, parse_test_block
from clagentic_loadout.repo_test.verb import main
from clagentic_loadout.review.profile_brief import declared_tests

GO = "go-test-json"


def _events(*events: dict[str, str]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _ev(action: str, test: str = "", package: str = "p", **more: str) -> dict[str, str]:
    event = {"Action": action, "Package": package, **more}
    if test:
        event["Test"] = test
    return event


def _stream(tmp_path: Path, text: str | bytes) -> Path:
    path = tmp_path / "go-test.jsonl"
    path.write_bytes(text if isinstance(text, bytes) else text.encode())
    return path


MIXED = (
    "go: downloading something\n"
    + _events(
        _ev("run", "A"),
        _ev("pass", "A"),
        _ev("run", "B"),
        _ev("skip", "B"),
        _ev("run", "C"),
        _ev("output", "C", Output="boom\n"),
        _ev("fail", "C"),
        _ev("pass"),
    )
    + "a test binary printed this\n{not json\n[1, 2]\n"
    + _events(_ev("fail"))
)

# (name, stream, passed, skipped, failing ids)
SUMMARY_ROWS = [
    ("attr and subtests pass", ATTR, 2, 0, ()),
    ("colons in subtest names", ISSUE23920, 3, 0, ()),
    ("panic in a test then the package fails", PANIC, 0, 0, (NO_PACKAGE_ID, "TestPanic")),
    ("timeout leaves a test open", TIMEOUT, 0, 0, (NO_PACKAGE_ID, "Test")),
    ("a benchmark that fails", BENCHFAIL, 0, 0, (NO_PACKAGE_ID, "BenchmarkFoo")),
    ("pass skip fail and noise lines", MIXED, 1, 1, ("p", "p::C")),
    (
        "count=2 yields two cases",
        _events(_ev("run", "X"), _ev("fail", "X"), _ev("run", "X"), _ev("pass", "X")),
        1,
        0,
        ("p::X",),
    ),
    (
        "output after the end does not reopen a test",
        _events(_ev("run", "X"), _ev("pass", "X"), _ev("output", "X", Output="late\n")),
        1,
        0,
        (),
    ),
    (
        "a run on an open test fails the earlier run",
        _events(_ev("run", "X"), _ev("run", "X"), _ev("pass", "X")),
        1,
        0,
        ("p::X",),
    ),
    (
        "output with no terminal action fails",
        _events(_ev("output", "X", Output="x\n")),
        0,
        0,
        ("p::X",),
    ),
    (
        "same test name in two packages stays two ids",
        _events(_ev("fail", "T", "a"), _ev("fail", "T", "b")),
        0,
        0,
        ("a::T", "b::T"),
    ),
    (
        "a package with no tests is not a case",
        _events(_ev("skip"), _ev("pass", package="q")),
        0,
        0,
        (),
    ),
    (
        "a failed build is a package failure",
        _events(_ev("build-output", Output="x.go:1: bad\n", ImportPath="p", Package=""), _ev("fail", FailedBuild="p")),
        0,
        0,
        ("p",),
    ),
]


@pytest.mark.parametrize(("name", "text", "passed", "skipped", "ids"), SUMMARY_ROWS, ids=[row[0] for row in SUMMARY_ROWS])
def test_summary_of_a_stream(tmp_path: Path, name: str, text: str, passed: int, skipped: int, ids: tuple[str, ...]) -> None:
    summary = parse_go_stream(_stream(tmp_path, text))

    assert summary.failing_ids == ids
    assert summary.failed == len(summary.failing_ids)
    assert (summary.passed, summary.skipped) == (passed, skipped)
    assert summary.collected == passed + skipped + len(ids)


def test_a_streams_failure_text_is_its_output_events(tmp_path: Path) -> None:
    cases = parse_failed_cases(_stream(tmp_path, PANIC))

    by_id = {case.test_id: case for case in cases}
    assert set(by_id) == {"TestPanic", NO_PACKAGE_ID}
    assert "panic: oops [recovered]" in by_id["TestPanic"].text
    assert by_id["TestPanic"].kind == "failure"
    assert by_id[NO_PACKAGE_ID].kind == "error" and "FAIL\tcommand-line-arguments" in by_id[NO_PACKAGE_ID].text


def test_a_build_failure_carries_the_build_output(tmp_path: Path) -> None:
    text = _events(
        _ev("build-output", Output="x.go:1: bad\n", ImportPath="p", Package=""), _ev("fail", FailedBuild="p")
    )

    (case,) = parse_failed_cases(_stream(tmp_path, text))

    assert case.test_id == "p" and "x.go:1: bad" in case.text and case.message.startswith("build failed")


@pytest.mark.parametrize("content", [b"", b"plain text\nmore\n", b"{\"Output\":\"no action\"}\n", b"{bad json\n"])
def test_a_stream_without_an_event_is_not_a_pass(tmp_path: Path, content: bytes) -> None:
    with pytest.raises(GoStreamError, match="no go test event"):
        parse_go_stream(_stream(tmp_path, content))


def test_a_missing_stream_is_a_stated_problem(tmp_path: Path) -> None:
    with pytest.raises(GoStreamError, match="no go test event stream"):
        parse_go_stream(tmp_path / "absent.jsonl")


# (cap attribute, value, stream builder) for a stream that passes it
def _many_tests(count: int) -> str:
    return _events(*(_ev("pass", f"T{n}") for n in range(count)))


CAP_ROWS = [
    ("MAX_TRACKED_TESTS", 3, _many_tests(4), "more than 3 tests"),
    ("MAX_FAILING_IDS", 2, _events(*(_ev("fail", f"T{n}") for n in range(3))), "more than 2 failed cases"),
    ("MAX_STREAM_BYTES", 50, _many_tests(10), "byte limit"),
]


@pytest.mark.parametrize(("cap", "value", "text", "message"), CAP_ROWS, ids=[row[0] for row in CAP_ROWS])
def test_a_stream_past_a_cap_is_refused_with_the_cap_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cap: str, value: int, text: str, message: str
) -> None:
    monkeypatch.setattr(go_test_json, cap, value)

    with pytest.raises(GoStreamError, match=message):
        parse_go_stream(_stream(tmp_path, text))


def test_an_over_long_line_is_skipped_and_counted_without_being_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(go_test_json, "MAX_LINE_BYTES", 200)
    long_line = json.dumps(_ev("fail", "L" * 5000)) + "\n"
    unterminated = json.dumps(_ev("fail", "M" * 5000))
    text = _events(_ev("pass", "A")) + long_line + _events(_ev("pass", "B")) + unterminated

    summary = parse_go_stream(_stream(tmp_path, text))

    assert summary.passed == 2 and summary.failed == 0 and summary.skipped_for_size == 2


def test_an_event_with_an_over_long_name_is_skipped_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(go_test_json, "MAX_NAME_CHARS", 20)

    summary = parse_go_stream(_stream(tmp_path, _events(_ev("pass", "A"), _ev("fail", "N" * 40))))

    assert summary.passed == 1 and summary.failed == 0 and summary.skipped_for_size == 1


def test_a_line_exactly_at_the_cap_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    line = json.dumps(_ev("pass", "A")) + "\n"
    monkeypatch.setattr(go_test_json, "MAX_LINE_BYTES", len(line))

    assert parse_go_stream(_stream(tmp_path, line)).passed == 1


def test_failure_text_is_cut_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(go_test_json, "CASE_TEXT_CAP_CHARS", 30)
    text = _events(_ev("output", "X", Output="a" * 25 + "\n"), _ev("output", "X", Output="b" * 25), _ev("fail", "X"))

    (case,) = parse_failed_cases(_stream(tmp_path, text))

    assert case.text.startswith("a" * 25) and "[output cut while the stream was read]" in case.text
    assert len(case.text.split("\n[output cut")[0]) == 30


def test_the_total_failure_text_is_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(go_test_json, "TOTAL_TEXT_CAP_CHARS", 10)
    text = _events(
        _ev("output", "X", Output="x" * 8), _ev("fail", "X"), _ev("output", "Y", Output="y" * 8), _ev("fail", "Y")
    )

    cases = parse_failed_cases(_stream(tmp_path, text))

    assert sum(len(case.text.split("\n[output cut")[0]) for case in cases) == 10


# (block, key named in the refusal or None)
PROFILE_ROWS = [
    ({"full": ["go", "test", "-json", "./..."], "report_format": GO}, None),
    ({"full": ["go", "test", "-json"], "files": ["go", "test", "-json"], "tiers": {"slow": ["go", "test"]}, "report_format": GO}, None),
    ({"full": ["x", "{junit}"], "report_format": "junit"}, None),
    ({"full": ["x", "{junit}"]}, None),
    ({"full": ["x"], "report_format": "junit"}, "test.full"),
    ({"full": ["x"]}, "test.full"),
    ({"full": ["x"], "tiers": {"slow": ["y"]}, "report_format": "junit"}, "test.full"),
    ({"full": ["x"], "report_format": "tap"}, "test.report_format"),
    ({"full": ["x"], "report_format": 3}, "test.report_format"),
]


@pytest.mark.parametrize(("block", "refused"), PROFILE_ROWS)
def test_report_format_in_the_profile_block(block: dict[str, object], refused: str | None) -> None:
    if refused is None:
        declared = parse_test_block(block)
        assert declared.format == block.get("report_format", "junit")
        return
    with pytest.raises(TestBlockError, match=refused):
        parse_test_block(block)


def test_the_declared_commands_and_the_brief_carry_the_format() -> None:
    declared = parse_test_block({"full": ["go", "test", "-json"], "report_format": GO})

    assert declared.report_format == GO and "report_format" in declared.declared_keys()
    assert declared_tests(declared)["report_format"] == GO  # type: ignore[index]
    assert "report_format" not in (declared_tests(parse_test_block({"full": ["x", "{junit}"]})) or {})


def test_a_detached_job_keeps_the_format(tmp_path: Path) -> None:
    prepared = PreparedRun(
        run_id="20260101T000000Z-1",
        run_dir=tmp_path / "run",
        root=tmp_path,
        mode="full",
        tier=None,
        template=("go", "test", "-json"),
        files=(),
        base_sha=None,
        timeout_seconds=None,
        recorded={},
        report_format=GO,
    )

    assert _prepared_of(_job_of(prepared)).report_format == GO


STREAM_SCRIPT = (
    "import sys\n"
    "sys.stdout.write(open('stream.jsonl').read())\n"
    "sys.stdout.flush()\n"
    "sys.stderr.write('to stderr\\n')\n"
    "sys.exit(1)\n"
)


def _go_repo(tmp_path: Path, stream: str, script: str = STREAM_SCRIPT) -> Path:
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(
            full=[sys.executable, "stream.py"], impacted=None, files=None, report_format=GO
        ),
        git_init=False,
    )
    (repo / "stream.py").write_text(script)
    (repo / "stream.jsonl").write_text(stream)
    return repo


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _run(repo: Path, reports: Path) -> RunOutcome:
    return execute_run(
        mode="full", paths=[], repo_root=repo, base=None, timeout_seconds=60,
        env={store.REPORTS_DIR_ENV_VAR: str(reports)},
    )


def test_a_go_run_is_recorded_and_readable(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _go_repo(tmp_path, MIXED)

    outcome = _run(repo, reports)

    run_dir = reports / outcome.summary["run_id"]
    result = json.loads((run_dir / store.RESULT_FILE).read_text())
    assert outcome.exit_code == exits.EXIT_TESTS_FAILED
    assert result["report_format"] == GO and result["state"] == store.STATE_COMPLETE
    assert result["counts"] == {"collected": 4, "passed": 1, "failed": 2, "skipped": 1}
    assert result["failing_ids"] == ["p", "p::C"] and result["id_scheme"] == 2
    assert (run_dir / store.STREAM_FILE).read_text() == MIXED
    assert (run_dir / store.LOG_FILE).read_text() == "to stderr\n"
    assert not (run_dir / store.JUNIT_FILE).exists()

    run = store.load_run(reports, outcome.summary["run_id"])
    (failure,) = [item for item in report_cmd.failures_of(reports, run) if item["id"] == "p::C"]
    assert "boom" in json.dumps(failure)
    assert main(["report", run.run_id, "--summary"]) == exits.EXIT_OK
    assert json.loads(capsys.readouterr().out)["report_format"] == GO
    assert main(["report", run.run_id, "--failing-ids"]) == exits.EXIT_OK
    assert "p::C" in capsys.readouterr().out


def test_two_go_runs_diff(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = iter(["20260101T000000Z-10", "20260102T000000Z-11"])
    monkeypatch.setattr(store, "new_run_id", lambda: next(ids))
    repo = _go_repo(tmp_path, MIXED)
    first = _run(repo, reports).summary["run_id"]
    (repo / "stream.jsonl").write_text(_events(_ev("run", "A"), _ev("pass", "A"), _ev("pass")))
    second = _run(repo, reports)

    code = main(["report", second.summary["run_id"], "--diff", first])

    diff = json.loads(capsys.readouterr().out)
    assert code == exits.EXIT_OK and diff["base_only"] == ["p", "p::C"] and diff["head_only"] == []
    assert "report_format_note" not in diff


def test_a_go_run_with_no_event_is_unrunnable(tmp_path: Path, reports: Path) -> None:
    repo = _go_repo(tmp_path, "just text\n", script="print('hello')\n")

    outcome = _run(repo, reports)

    result = json.loads((reports / outcome.summary["run_id"] / store.RESULT_FILE).read_text())
    assert outcome.exit_code == exits.EXIT_UNRUNNABLE
    assert result["state"] == store.STATE_UNRUNNABLE and result["counts"]["collected"] == 0
    assert "no go test event" in result["problem"]
    assert "hello" in result["failure_log_tail"]


def test_a_junit_run_is_recorded_as_before(tmp_path: Path, reports: Path) -> None:
    repo = make_repo(tmp_path / "repo", git_init=False)
    (repo / "runner.py").write_text(
        "import sys\n"
        "print('out', flush=True)\n"
        "print('err', file=sys.stderr, flush=True)\n"
        "open(sys.argv[1], 'w').write('<testsuites><testsuite><testcase name=\"t\"/></testsuite></testsuites>')\n"
    )

    outcome = _run(repo, reports)

    run_dir = reports / outcome.summary["run_id"]
    result = json.loads((run_dir / store.RESULT_FILE).read_text())
    assert (run_dir / store.LOG_FILE).read_text() == "out\nerr\n"
    assert "report_format" not in result and not (run_dir / store.STREAM_FILE).exists()
    assert result["counts"]["passed"] == 1


def test_a_diff_across_formats_notes_it_and_still_prints(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = "https://h.example.test/o/alpha.git"
    old, new = "20260101T000000Z-10", "20260102T000000Z-11"
    write_run(reports, old, failing=["m.T::a"], remote_url=remote, id_scheme=2)
    write_run(reports, new, failing=["pkg::A"], remote_url=remote, id_scheme=2, report_format=GO)

    code = main(["report", new, "--diff", old])

    diff = json.loads(capsys.readouterr().out)
    assert code == exits.EXIT_OK
    assert "ids are not comparable across report formats" in diff["report_format_note"]
    assert "go-test-json" in diff["report_format_note"] and "junit" in diff["report_format_note"]
    assert diff["head_only"] == ["pkg::A"] and diff["base_only"] == ["m.T::a"]
