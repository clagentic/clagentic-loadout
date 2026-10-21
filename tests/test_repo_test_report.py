"""`clagentic-loadout test report`: reading recorded runs by id, read-only."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _repo_test_support import tree_snapshot, write_run

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.verb import main

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_A2 = "a" * 7 + "c" * 33


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    root.mkdir()
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _report(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(["report", *argv])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_latest_is_the_newest_finished_run_and_summary_is_the_default(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)
    write_run(reports, "20260102T000000Z-11", head_sha=SHA_B, failing=["t::a"])
    write_run(reports, "20260103T000000Z-12", head_sha=SHA_B, finished=False)

    code, out, _ = _report(capsys)

    summary = json.loads(out)
    assert code == exits.EXIT_OK
    assert summary["run_id"] == "20260102T000000Z-11"
    assert summary["head_sha"] == SHA_B and summary["dirty"] is False and summary["commit_known"]
    assert summary["failing_id_count"] == 1
    assert summary["repo"]["path"] == "/repos/alpha"


def test_latest_is_filtered_by_repo_path_name_or_remote(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", repo_path="/repos/alpha", remote_url="https://h.example.test/o/alpha.git")
    write_run(reports, "20260102T000000Z-11", repo_path="/repos/beta", remote_url="https://h.example.test/o/beta.git")

    for wanted, expected in [
        ("alpha", "20260101T000000Z-10"),
        ("/repos/beta", "20260102T000000Z-11"),
        ("https://h.example.test/o/alpha.git", "20260101T000000Z-10"),
    ]:
        code, out, _ = _report(capsys, "latest", "--repo", wanted)
        assert code == exits.EXIT_OK and json.loads(out)["run_id"] == expected

    code, _, err = _report(capsys, "latest", "--repo", "gamma")
    assert code == exits.EXIT_NO_RUNS and "'gamma'" in err


def test_the_run_id_may_follow_the_options(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", repo_path="/repos/alpha")
    write_run(reports, "20260102T000000Z-11", repo_path="/repos/beta")

    code, out, _ = _report(capsys, "--repo", "alpha", "latest")
    assert code == exits.EXIT_OK and json.loads(out)["run_id"] == "20260101T000000Z-10"

    code, out, _ = _report(capsys, "--failing-ids", "20260102T000000Z-11")
    assert code == exits.EXIT_OK and out == ""


def test_an_explicit_run_id_is_loaded_and_a_missing_one_is_named(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10")

    assert _report(capsys, "20260101T000000Z-10")[0] == exits.EXIT_OK
    code, _, err = _report(capsys, "20260101T000000Z-99")
    assert code == exits.EXIT_BAD_RUN_ID and "20260101T000000Z-99" in err


@pytest.mark.parametrize(
    "run_id",
    ["../escape", "..", "/etc", "20260101T000000Z-10/../..", "20260101T000000Z-10\n", "x", "20260101T000000Z-"],
)
def test_a_run_id_outside_the_grammar_is_refused_before_it_touches_a_path(
    reports: Path, capsys: pytest.CaptureFixture[str], run_id: str
) -> None:
    code, _, err = _report(capsys, "--", run_id)

    assert code == exits.EXIT_BAD_RUN_ID
    assert "is not a run id" in err


def test_a_run_directory_that_is_a_symlink_is_refused(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "elsewhere").mkdir()
    elsewhere = write_run(tmp_path / "elsewhere", "20260101T000000Z-10")
    (reports / "20260101T000000Z-10").symlink_to(elsewhere, target_is_directory=True)

    code, _, err = _report(capsys, "20260101T000000Z-10")

    assert code == exits.EXIT_BAD_RUN_ID and "symlink" in err


def test_an_unfinished_run_is_not_a_report(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", finished=False)

    code, _, err = _report(capsys, "20260101T000000Z-10")

    assert code == exits.EXIT_BAD_RUN_ID and "not finished" in err


def test_a_missing_reports_root_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(tmp_path / "never-created"))

    code, out, err = _report(capsys)

    assert code == exits.EXIT_NO_RUNS and out == ""
    assert str(tmp_path / "never-created") in err
    assert not (tmp_path / "never-created").exists()


def test_failing_ids_are_printed_sorted_one_per_line(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", failing=["z::t", "a::t"])

    code, out, _ = _report(capsys, "--failing-ids")

    assert code == exits.EXIT_OK and out == "a::t\nz::t\n"


def test_an_unrunnable_run_refuses_failing_ids_and_diff_instead_of_reporting_an_empty_set(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", state=store.STATE_UNRUNNABLE)
    write_run(reports, "20260102T000000Z-11")

    code, out, err = _report(capsys, "20260101T000000Z-10", "--failing-ids")
    assert code == exits.EXIT_DIFF_REFUSED and out == "" and "unrunnable" in err

    code, _, err = _report(capsys, "20260102T000000Z-11", "--diff", "20260101T000000Z-10")
    assert code == exits.EXIT_DIFF_REFUSED and "unrunnable" in err


def test_sha_selects_the_newest_clean_run_for_that_commit_by_full_sha_or_prefix(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)
    write_run(reports, "20260102T000000Z-11", head_sha=SHA_A)
    write_run(reports, "20260103T000000Z-12", head_sha=SHA_B)

    for sha in (SHA_A, SHA_A[:7], SHA_A[:12].upper()):
        code, out, _ = _report(capsys, "latest", "--sha", sha)
        assert code == exits.EXIT_OK and json.loads(out)["run_id"] == "20260102T000000Z-11"


def test_sha_never_falls_back_to_latest_and_names_what_exists(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)
    write_run(reports, "20260102T000000Z-11", head_sha=None, dirty=None)

    code, out, err = _report(capsys, "latest", "--sha", SHA_B)

    assert code == exits.EXIT_SHA_NO_MATCH and out == ""
    assert SHA_A[:12] in err and "commit unknown" in err


def test_sha_skips_dirty_runs_and_runs_that_recorded_no_commit(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A, dirty=True)
    write_run(reports, "20260102T000000Z-11", head_sha=None, dirty=None)

    code, _, err = _report(capsys, "--sha", SHA_A)

    assert code == exits.EXIT_SHA_NO_MATCH and "dirty" in err


def test_an_ambiguous_sha_prefix_is_refused(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)
    write_run(reports, "20260102T000000Z-11", head_sha=SHA_A2)

    code, _, err = _report(capsys, "--sha", SHA_A[:7])

    assert code == exits.EXIT_SHA_NO_MATCH and "ambiguous" in err


@pytest.mark.parametrize("sha", ["abc", "xyz1234", "a" * 41])
def test_a_malformed_sha_is_a_usage_error(reports: Path, capsys: pytest.CaptureFixture[str], sha: str) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)

    assert _report(capsys, "--sha", sha)[0] == exits.EXIT_USAGE


def test_repo_and_sha_cannot_be_combined_with_an_explicit_run_id(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A)

    assert _report(capsys, "20260101T000000Z-10", "--sha", SHA_A)[0] == exits.EXIT_USAGE


def test_diff_lists_head_only_base_only_and_the_shared_count(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    remote = "https://h.example.test/o/alpha.git"
    write_run(reports, "20260101T000000Z-10", failing=["t::both", "t::old"], remote_url=remote)
    write_run(reports, "20260102T000000Z-11", failing=["t::both", "t::new"], remote_url=remote)

    code, out, _ = _report(capsys, "20260102T000000Z-11", "--diff", "20260101T000000Z-10")

    assert code == exits.EXIT_OK
    assert json.loads(out) == {
        "head_run": "20260102T000000Z-11",
        "base_run": "20260101T000000Z-10",
        "head_only": ["t::new"],
        "base_only": ["t::old"],
        "shared_count": 1,
        "repo_check": "same",
    }


def test_diff_refuses_runs_from_different_repositories(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", remote_url="https://h.example.test/o/alpha.git")
    write_run(reports, "20260102T000000Z-11", remote_url="https://h.example.test/o/beta.git")
    write_run(reports, "20260103T000000Z-12", repo_path="/repos/alpha")
    write_run(reports, "20260104T000000Z-13", repo_path="/repos/gamma")

    for head, base in [("20260102T000000Z-11", "20260101T000000Z-10"), ("20260104T000000Z-13", "20260103T000000Z-12")]:
        code, out, err = _report(capsys, head, "--diff", base)
        assert code == exits.EXIT_DIFF_REFUSED and out == ""
        assert "different repositories" in err


def test_diff_against_a_run_with_no_recorded_repo_is_marked_unverified(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", repo_path=None, failing=["t::a"])
    write_run(reports, "20260102T000000Z-11", failing=["t::a"])

    code, out, _ = _report(capsys, "20260102T000000Z-11", "--diff", "20260101T000000Z-10")

    assert code == exits.EXIT_OK and json.loads(out)["repo_check"] == "unverified"


def test_diff_base_must_be_an_explicit_existing_run_id(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10")

    assert _report(capsys, "--diff", "latest")[0] == exits.EXIT_USAGE
    code, _, err = _report(capsys, "--diff", "20260101T000000Z-77")
    assert code == exits.EXIT_BAD_RUN_ID and "20260101T000000Z-77" in err


def test_report_changes_nothing_under_the_reports_root_or_beside_it(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", head_sha=SHA_A, failing=["t::a"])
    write_run(reports, "20260102T000000Z-11", head_sha=SHA_B)
    before = tree_snapshot(tmp_path)

    for argv in (
        [],
        ["latest", "--sha", SHA_A],
        ["--failing-ids"],
        ["20260102T000000Z-11", "--diff", "20260101T000000Z-10"],
        ["--sha", "c" * 40],
        ["../x"],
    ):
        _report(capsys, *argv)

    assert tree_snapshot(tmp_path) == before
