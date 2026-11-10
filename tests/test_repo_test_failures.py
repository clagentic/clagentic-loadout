"""`clagentic-loadout test report --failures`, and `id_scheme` in `--summary`."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text, write_run

from clagentic_loadout.repo_test import exits, failures, liveness, store
from clagentic_loadout.repo_test.junit import ID_SCHEME, FailedCase
from clagentic_loadout.repo_test.run_cmd import execute_run
from clagentic_loadout.repo_test.verb import main

RUN = "20260101T000000Z-10"
OTHER = "20260102T000000Z-11"

_SRC = str(Path(__file__).resolve().parents[1] / "src")
_GOOD_ENOUGH_TO_WAIT = 20.0

# A test command that waits for a marker file, then writes a one-test report.
_WAIT_THEN_PASS = (
    "import os, sys, time\n"
    "go, junit = sys.argv[1], sys.argv[2]\n"
    "end = time.monotonic() + 30\n"
    "while not os.path.exists(go) and time.monotonic() < end:\n"
    "    time.sleep(0.05)\n"
    "open(junit, 'w').write(\"<testsuite><testcase classname='a' name='t'/></testsuite>\")\n"
)


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    # The detached supervisor is a fresh interpreter; it needs the same import path.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, [_SRC, os.environ.get("PYTHONPATH", "")])))
    return root


def _report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["report", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _bounded_excerpt(text: str, message: str = "m") -> str:
    (record,) = failures.bound_failures([FailedCase("m.T::t", "failure", message, text)])
    return record["excerpt"]


def test_a_failure_prints_its_id_kind_message_and_excerpt(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "<testcase classname='m.T' name='test_ok'/>"
        "<testcase classname='m.T' name='test_a'>"
        "<failure message='assert 1 == 2'>Traceback\n  line 3\nAssertionError</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::test_a"], junit_body=body)

    code, out, _ = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_OK
    assert json.loads(out) == [
        {
            "id": "m.T::test_a",
            "kind": "failure",
            "message": "assert 1 == 2",
            "excerpt": "Traceback\n  line 3\nAssertionError",
        }
    ]


def test_an_error_is_reported_as_an_error(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    body = "<testcase classname='m.T' name='t'><error message='boom'>KeyError</error></testcase>"
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert [(r["kind"], r["message"], r["excerpt"]) for r in json.loads(out)] == [("error", "boom", "KeyError")]


def test_cases_sharing_an_id_each_get_a_record_in_report_order(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    body = (
        "<testcase classname='m.T' name='t'><failure message='first'/></testcase>"
        "<testcase classname='m.T' name='u'><failure message='other'/></testcase>"
        "<testcase classname='m.T' name='t'><error message='teardown'/></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t", "m.T::t", "m.T::u"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert [(r["id"], r["kind"], r["message"]) for r in json.loads(out)] == [
        ("m.T::t", "failure", "first"),
        ("m.T::u", "failure", "other"),
        ("m.T::t", "error", "teardown"),
    ]


def test_a_passing_run_prints_an_empty_list(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, RUN, junit_body="<testcase classname='m.T' name='t'/>")

    code, out, _ = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_OK and json.loads(out) == []


def test_huge_failure_text_is_cut_with_a_marker_stating_what_was_dropped(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    size = 5_000_000
    body = (
        "<testcase classname='m.T' name='t'>"
        f"<failure message='{'m' * 3000}'>{'x' * size}</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    code, out, _ = _report(capsys, RUN, "--failures")

    (record,) = json.loads(out)
    cap = failures.EXCERPT_CAP_CHARS
    assert code == exits.EXIT_OK
    assert record["excerpt"] == "x" * cap + f"...[truncated: {size - cap} characters dropped]"
    assert record["message"] == "m" * failures.MESSAGE_CAP_CHARS + "...[truncated: 2000 characters dropped]"
    assert len(out) < failures.TOTAL_CAP_CHARS


def test_the_total_cap_holds_over_many_failed_cases(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    count = 200
    body = "".join(
        f"<testcase classname='m.T' name='t{i}'><failure message='m'>{'y' * 3000}</failure></testcase>"
        for i in range(count)
    )
    write_run(reports, RUN, failing=[f"m.T::t{i}" for i in range(count)], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    records = json.loads(out)
    marker, shown = records[-1], records[:-1]
    assert "id" not in marker and marker["omitted_cases"] == count - len(shown) > 0
    assert sum(len(r["id"]) + len(r["message"]) + len(r["excerpt"]) for r in shown) < failures.TOTAL_CAP_CHARS * 1.2
    assert len(out) < failures.TOTAL_CAP_CHARS * 2


def test_a_token_shaped_value_in_a_failure_is_redacted(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "s3cr3tT0kenValue99"
    body = (
        "<testcase classname='m.T' name='t'>"
        f"<failure message='GET https://user:{secret}@host.example.test/x failed'>"
        f"url=https://host.example.test/?token={secret}\n"
        f"Authorization: Bearer {secret}"
        "</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert secret not in out
    (record,) = json.loads(out)
    assert "[REDACTED]" in record["message"] and "[REDACTED]" in record["excerpt"]


@pytest.mark.parametrize(
    "userinfo",
    ["https://user:hunter2pw@host.example.test/x", "HTTPS://User:hunter2pw@host.example.test/x",
     "HtTpS://uSeR:hunter2pw@host.example.test/x"],
)
def test_url_userinfo_in_any_case_is_redacted_through_the_verb(
    reports: Path, capsys: pytest.CaptureFixture[str], userinfo: str
) -> None:
    body = (
        "<testcase classname='m.T' name='t'>"
        f"<failure message='fetch {userinfo}'>{userinfo}</failure></testcase>"
    )
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    assert "hunter2pw" not in out and "[REDACTED]" in out


def test_a_secret_straddling_the_excerpt_cap_leaves_no_part_behind(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A long bearer value collapses to a short marker, which moves a later URL
    # to a position the raw text does not have: redaction happens on the whole
    # value first, and the cut is made on what is left.
    head = "Authorization: Bearer " + "A" * 15_000 + "\n"
    redacted_head = len("Authorization: [REDACTED]\n")
    lead = "f " * ((failures.EXCERPT_CAP_CHARS - redacted_head - 6) // 2)
    text = head + lead + "https://user:" + "p" * 40 + "@host.example.test/x\n" + "tail " * 100
    body = f"<testcase classname='m.T' name='t'><failure message='m'>{text}</failure></testcase>"
    write_run(reports, RUN, failing=["m.T::t"], junit_body=body)

    _, out, _ = _report(capsys, RUN, "--failures")

    (record,) = json.loads(out)
    assert "ppp" not in out and "AAAA" not in out
    assert record["excerpt"].startswith("Authorization: [REDACTED]")
    assert len(record["excerpt"].split("...[truncated")[0]) <= failures.EXCERPT_CAP_CHARS


def test_a_url_split_by_an_escape_sequence_is_redacted_once_the_escape_is_stripped() -> None:
    for text in (
        "https://us\x1b[0mer:topsecretpw@host.example.test/x",
        "ht\x1b[1mtps://user:topsecretpw@host.example.test/x",
        "https://user:topsecret\x1b[0mpw@host.example.test/x",
    ):
        excerpt = _bounded_excerpt(text)
        assert "topsecret" not in excerpt and "\x1b" not in excerpt


def test_a_control_character_inside_the_bearer_keyword_does_not_defeat_redaction() -> None:
    for text in ("Bea\x07rer abcdefghijklmnop1234", "B\x00earer abcdefghijklmnop1234", "Bearer\x7f abcdefghijklmnop1234"):
        excerpt = _bounded_excerpt(text, message=text)
        assert "abcdefghijklmnop" not in excerpt and "\x07" not in excerpt


def test_a_value_over_the_ceiling_emits_only_the_counted_marker() -> None:
    size = failures.REDACTION_CEILING_CHARS + 1
    secret_text = "https://user:hunter2pw@host.example.test/ " + "x" * size

    (record,) = failures.bound_failures([FailedCase(secret_text, "failure", secret_text, secret_text)])

    expected = f"[omitted: value of {len(secret_text)} characters exceeds the 8 MiB redaction ceiling]"
    assert record == {"id": expected, "kind": "failure", "message": expected, "excerpt": expected}


def test_an_over_ceiling_marker_never_exceeds_the_total_cap() -> None:
    over = "y" * (failures.REDACTION_CEILING_CHARS + 1)
    # Nine records of exactly 4000 characters, then one leaving a budget of 10,
    # so the over-ceiling message that follows is cut to what remains.
    cases = [FailedCase("a", "failure", "m", "x" * (failures.EXCERPT_CAP_CHARS - 2)) for _ in range(9)]
    cases.append(FailedCase("a", "failure", "m", "x" * (failures.EXCERPT_CAP_CHARS - 12)))
    cases.append(FailedCase("b", "failure", over, "e"))

    records = failures.bound_failures(cases)

    counted = sum(
        len(record[key].split("...[truncated")[0])
        for record in records
        if "id" in record
        for key in ("id", "message", "excerpt")
    )
    assert counted <= failures.TOTAL_CAP_CHARS
    assert records[-1]["message"] == "[omitted:"


def test_a_value_exactly_at_the_ceiling_is_still_redacted_and_cut() -> None:
    text = "x" * failures.REDACTION_CEILING_CHARS

    excerpt = _bounded_excerpt(text)

    cap = failures.EXCERPT_CAP_CHARS
    assert excerpt == "x" * cap + f"...[truncated: {len(text) - cap} characters dropped]"


def test_an_unrunnable_run_is_refused_naming_the_run(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, RUN, state=store.STATE_UNRUNNABLE)

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def test_a_complete_run_with_no_recorded_report_is_refused_naming_the_run(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN, failing=["m.T::t"])

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def test_a_symlinked_report_is_refused(reports: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    run_dir = write_run(reports, RUN, failing=["m.T::t"])
    target = tmp_path / "elsewhere.xml"
    target.write_text("<testsuites/>")
    (run_dir / store.JUNIT_FILE).symlink_to(target)

    code, out, err = _report(capsys, RUN, "--failures")

    assert code == exits.EXIT_DIFF_REFUSED and out == "" and RUN in err


def _detached_gated_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[dict, Path]:
    script = tmp_path / "command.py"
    script.write_text(_WAIT_THEN_PASS)
    go = tmp_path / "go"
    repo = make_repo(
        tmp_path / "repo", profile=profile_text(full=[sys.executable, str(script), str(go), "{junit}"])
    )
    assert main(["run", "full", "--detach", "--repo-root", str(repo)]) == exits.EXIT_OK
    return json.loads(capsys.readouterr().out), go


def test_failures_of_a_running_run_exit_still_running_and_name_the_state(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started, go = _detached_gated_run(tmp_path, capsys)
    try:
        code, out, err = _report(capsys, started["run_id"], "--failures")
    finally:
        go.write_text("")

    assert (code, out) == (exits.EXIT_STILL_RUNNING, "")
    assert "still running" in err and started["run_id"] in err
    main(["report", started["run_id"], "--wait", str(_GOOD_ENOUGH_TO_WAIT)])


def test_failures_of_an_interrupted_run_are_refused_naming_the_state(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    started, go = _detached_gated_run(tmp_path, capsys)
    try:
        os.kill(started["pid"], signal.SIGKILL)
        deadline = time.monotonic() + _GOOD_ENOUGH_TO_WAIT
        while liveness.is_alive(liveness.capture_identity(started["pid"])) and time.monotonic() < deadline:
            time.sleep(0.05)

        code, out, err = _report(capsys, started["run_id"], "--failures")
    finally:
        go.write_text("")

    assert (code, out) == (exits.EXIT_DIFF_REFUSED, "")
    assert "interrupted" in err and started["run_id"] in err


def test_failures_is_exclusive_with_the_other_report_forms(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN)
    write_run(reports, OTHER)

    for other in (["--summary"], ["--failing-ids"], ["--diff", OTHER]):
        code, out, _ = _report(capsys, RUN, "--failures", *other)
        assert code == exits.EXIT_USAGE and out == ""


def test_help_documents_failures(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["report", "--help"])

    assert code == exits.EXIT_OK
    assert "--failures" in capsys.readouterr().out


def test_summary_carries_the_id_scheme_and_null_for_an_old_run(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, RUN)
    write_run(reports, OTHER, id_scheme=ID_SCHEME)

    _, old_out, _ = _report(capsys, RUN)
    _, new_out, _ = _report(capsys, OTHER)

    assert json.loads(old_out)["id_scheme"] is None
    assert "id_basis" not in json.loads(old_out)
    assert json.loads(new_out)["id_scheme"] == ID_SCHEME


def test_a_boolean_id_scheme_in_a_hand_edited_record_shows_null(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = write_run(reports, RUN, id_scheme=ID_SCHEME)
    result_path = run_dir / store.RESULT_FILE
    result = json.loads(result_path.read_text())
    result["id_scheme"] = True
    result_path.write_text(json.dumps(result))

    _, out, _ = _report(capsys, RUN)

    assert json.loads(out)["id_scheme"] is None


def test_summary_carries_id_basis_when_the_record_has_one(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_dir = write_run(reports, RUN, id_scheme=ID_SCHEME)
    result_path = run_dir / store.RESULT_FILE
    result = json.loads(result_path.read_text())
    result["id_basis"] = "collapsed"
    result_path.write_text(json.dumps(result))

    _, out, _ = _report(capsys, RUN)

    assert json.loads(out)["id_basis"] == "collapsed"


def test_a_recorded_run_with_a_real_failure_can_be_read_back(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", git_init=False)
    (repo / "runner.py").write_text(
        "import sys\n"
        "body = \"<testcase classname='s' name='t'><failure message='bad'>detail</failure></testcase>\"\n"
        "open(sys.argv[1], 'w').write('<testsuites><testsuite>' + body + '</testsuite></testsuites>')\n"
        "sys.exit(1)\n"
    )
    outcome = execute_run(
        mode="full",
        paths=[],
        repo_root=repo,
        base=None,
        timeout_seconds=60,
        env={store.REPORTS_DIR_ENV_VAR: str(reports)},
    )

    code, out, _ = _report(capsys, outcome.summary["run_id"], "--failures")

    assert code == exits.EXIT_OK
    assert json.loads(out) == [{"id": "s::t", "kind": "failure", "message": "bad", "excerpt": "detail"}]
