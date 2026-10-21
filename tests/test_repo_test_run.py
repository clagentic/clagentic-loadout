"""`clagentic-loadout test run`: profile-driven runs, recorded under the reports root."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest
from _repo_test_support import git, make_repo, profile_text

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.verb import main


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, dict | None, str]:
    code = main(["run", *argv])
    captured = capsys.readouterr()
    return code, (json.loads(captured.out) if captured.out.strip() else None), captured.err


def test_full_run_records_a_passing_run_with_provenance(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    head = git(repo, "rev-parse", "HEAD")

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert summary["state"] == "complete"
    assert summary["counts"] == {"collected": 3, "passed": 2, "failed": 0, "skipped": 1}
    run = store.load_run(reports, summary["run_id"])
    assert run.head_sha == head
    assert run.dirty is False
    assert run.repo["path"] == str(repo.resolve())
    assert run.result["failing_ids"] == []
    assert stat.S_IMODE(reports.stat().st_mode) == 0o700


def test_a_failing_test_gives_the_failed_exit_and_lists_its_id(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    (repo / "fail.txt").write_text("x")

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_TESTS_FAILED
    assert summary["failing_id_count"] == 1
    run = store.load_run(reports, summary["run_id"])
    assert run.result["failing_ids"] == ["suite.alpha::test_broken"]
    assert run.dirty is True


def test_files_mode_appends_the_named_files_to_the_declared_command(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, summary, _ = _run(capsys, "files", "test_good.py", "test_bad.py", "--repo-root", str(repo))

    assert code == exits.EXIT_TESTS_FAILED
    run = store.load_run(reports, summary["run_id"])
    assert run.result["failing_ids"] == ["suite.files::test_bad.py"]
    assert run.result["command"][-2:] == ["test_good.py", "test_bad.py"]


def test_options_and_file_paths_may_come_in_any_order(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, summary, _ = _run(capsys, "files", "--repo-root", str(repo), "test_good.py", "--timeout", "60", "test_bad.py")

    assert code == exits.EXIT_TESTS_FAILED
    assert summary["command"][-2:] == ["test_good.py", "test_bad.py"]


@pytest.mark.parametrize("raw", ["-x", "missing.py", "../outside.py"])
def test_files_mode_refuses_options_missing_files_and_paths_outside_the_root(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], raw: str
) -> None:
    repo = make_repo(tmp_path / "repo")
    (tmp_path / "outside.py").write_text("")

    code, summary, err = _run(capsys, "files", "--repo-root", str(repo), "--", raw)

    assert code == exits.EXIT_USAGE and summary is None
    assert repr(raw) in err
    assert not reports.exists()


def test_files_mode_needs_a_path_and_the_other_modes_take_none(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    assert _run(capsys, "files", "--repo-root", str(repo))[0] == exits.EXIT_USAGE
    assert _run(capsys, "full", "test_good.py", "--repo-root", str(repo))[0] == exits.EXIT_USAGE


def test_impacted_substitutes_the_resolved_base_sha_and_records_it(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")

    code, summary, _ = _run(capsys, "impacted", "--base", "main", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    run = store.load_run(reports, summary["run_id"])
    assert run.provenance["base_sha"] == base


def test_impacted_uses_the_profiles_default_base(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(base="main"))

    code, summary, _ = _run(capsys, "impacted", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert store.load_run(reports, summary["run_id"]).provenance["base_sha"] == git(repo, "rev-parse", "main")


def test_impacted_without_any_base_names_both_places_to_set_it(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, _, err = _run(capsys, "impacted", "--repo-root", str(repo))

    assert code == exits.EXIT_NOT_DECLARED
    assert "--base" in err and "test.base" in err


def test_an_unresolvable_base_is_refused_with_the_value(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, _, err = _run(capsys, "impacted", "--base", "no-such-rev", "--repo-root", str(repo))

    assert code == exits.EXIT_USAGE
    assert "'no-such-rev'" in err


def test_a_mode_the_profile_does_not_declare_is_refused_naming_the_key(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(files=None))

    code, _, err = _run(capsys, "files", "test_good.py", "--repo-root", str(repo))

    assert code == exits.EXIT_NOT_DECLARED
    assert "test.files" in err
    assert not reports.exists()


def test_no_profile_no_test_block_and_an_invalid_profile_are_refused_cleanly(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    assert _run(capsys, "full", "--repo-root", str(bare))[0] == exits.EXIT_PROFILE

    no_block = make_repo(tmp_path / "no-block", profile="class: internal\n")
    code, _, err = _run(capsys, "full", "--repo-root", str(no_block))
    assert code == exits.EXIT_NOT_DECLARED and "`test` block" in err

    invalid = make_repo(tmp_path / "invalid", profile="class: internal\ntest: {full: [run]}\n")
    code, _, err = _run(capsys, "full", "--repo-root", str(invalid))
    assert code == exits.EXIT_PROFILE and "test.full" in err

    assert _run(capsys, "full", "--repo-root", str(tmp_path / "nope"))[0] == exits.EXIT_USAGE
    assert not reports.exists()


def test_a_command_that_writes_no_report_is_unrunnable_and_shows_its_output(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(full=[sys.executable, "-c", "print('collection exploded')", "{junit}"]),
    )

    code, summary, err = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_UNRUNNABLE
    assert summary["state"] == "unrunnable"
    assert "collection exploded" in err
    assert store.load_run(reports, summary["run_id"]).state == "unrunnable"


def test_a_missing_executable_is_unrunnable_and_names_it(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(full=["no-such-binary-here", "{junit}"]))

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_UNRUNNABLE
    assert "no-such-binary-here" in summary["problem"]


def test_a_timeout_ends_the_command_and_records_an_unrunnable_run(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(full=[sys.executable, "-c", "import time; time.sleep(60)", "{junit}"]),
    )

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo), "--timeout", "0.5")

    assert code == exits.EXIT_UNRUNNABLE
    assert "timed out" in summary["problem"]


def test_a_repo_outside_a_git_work_tree_runs_with_null_commit_fields(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "plain", git_init=False)

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    run = store.load_run(reports, summary["run_id"])
    assert run.head_sha is None and run.dirty is None
    assert run.repo["is_git"] is False and run.repo["path"] == str(repo.resolve())


def test_the_remote_url_is_recorded_without_credentials(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", "https://user:secret-value@git.example.test:8443/o/r.git")

    _, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    run = store.load_run(reports, summary["run_id"])
    assert run.repo["remote_url"] == "https://git.example.test:8443/o/r.git"
    assert "secret-value" not in json.dumps(dict(run.provenance))


def test_a_malformed_remote_is_recorded_as_null_and_the_run_proceeds(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")
    git(repo, "remote", "add", "origin", "https://[")

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert store.load_run(reports, summary["run_id"]).repo["remote_url"] is None


def test_the_child_exit_code_is_never_the_verbs_exit_code(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(
            full=[sys.executable, "-c", "import sys; open(sys.argv[1], 'w').write('<testsuite/>'); sys.exit(64)", "{junit}"]
        ),
    )

    code, summary, _ = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_TESTS_FAILED
    assert summary["command_exit_code"] == 64
    assert summary["failing_id_count"] == 0
    assert "exited 64" in summary["problem"]


def test_an_unwritable_reports_root_is_a_clean_store_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where the reports root should be")
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(blocker / "reports"))
    repo = make_repo(tmp_path / "repo")

    code, _, err = _run(capsys, "full", "--repo-root", str(repo))

    assert code == exits.EXIT_STORE_WRITE
    assert "cannot record run" in err
