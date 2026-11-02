"""`clagentic-loadout test`: a run states, and checks, the repository and head it ran on;
report lookup cannot hand back another repository's run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from _repo_test_support import git, make_repo, write_run

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.verb import main


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    # Run ids are second-and-pid granular; runs in one test process need distinct ones.
    ticks = iter(range(10, 100))
    monkeypatch.setattr(store, "new_run_id", lambda: f"20260101T0000{next(ticks):02d}Z-1")
    return root


def _main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _run_ids(root: Path) -> list[str]:
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def test_run_prints_where_it_ran_on_stderr_first_and_last_and_in_the_summary(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "alpha")
    head = git(repo, "rev-parse", "HEAD")

    code, out, err = _main(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    summary = json.loads(out)
    assert summary["repo"]["name"] == "alpha"
    assert summary["repo"]["path"] == str(repo.resolve())
    assert summary["head_sha"] == head
    assert summary["dirty"] is False
    lines = err.strip().splitlines()
    for line in (lines[0], lines[-1]):
        assert summary["run_id"] in line
        assert "repo=alpha" in line and head in line and "dirty=false" in line
    assert "started" in lines[0] and "finished" in lines[-1]


def test_run_outside_a_git_work_tree_states_an_unknown_head(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "plain", git_init=False)

    code, out, err = _main(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert json.loads(out)["head_sha"] is None
    assert "head=unknown" in err.splitlines()[0]


def test_expect_repo_mismatch_refuses_naming_both_and_creates_no_run(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "alpha")

    code, out, err = _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-repo", "beta")

    assert code == exits.EXIT_REPO_MISMATCH
    assert out == ""
    assert "'beta'" in err and "alpha" in err
    assert _run_ids(reports) == []


def test_expect_repo_that_matches_by_name_or_path_runs(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "alpha")

    for wanted in ("alpha", str(repo)):
        assert _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-repo", wanted)[0] == exits.EXIT_OK
    assert len(_run_ids(reports)) == 2


def test_expect_head_matches_by_full_sha_and_prefix_and_refuses_otherwise(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "alpha")
    head = git(repo, "rev-parse", "HEAD")
    other = ("0" if head[0] != "0" else "1") + head[1:]

    for wanted in (head, head[:7].upper()):
        assert _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-head", wanted)[0] == exits.EXIT_OK
    before = _run_ids(reports)

    code, _, err = _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-head", other)
    assert code == exits.EXIT_REPO_MISMATCH
    assert other in err and head in err
    assert _run_ids(reports) == before


def test_expect_head_with_a_malformed_value_is_a_usage_error(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "alpha")

    assert _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-head", "abc")[0] == exits.EXIT_USAGE
    assert _run_ids(reports) == []


def test_expect_head_refuses_a_root_that_is_not_a_git_work_tree(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "plain", git_init=False)

    code, _, err = _main(capsys, "run", "full", "--repo-root", str(repo), "--expect-head", "a" * 40)

    assert code == exits.EXIT_REPO_MISMATCH
    assert "unknown" in err
    assert _run_ids(reports) == []


def _record_two_repos(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> tuple[Path, Path]:
    alpha, beta = make_repo(tmp_path / "alpha"), make_repo(tmp_path / "beta")
    for repo in (alpha, beta):
        assert _main(capsys, "run", "full", "--repo-root", str(repo))[0] == exits.EXIT_OK
    return alpha, beta


def test_latest_inside_a_git_work_tree_is_scoped_to_that_repository(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    alpha, beta = _record_two_repos(tmp_path, capsys)

    monkeypatch.chdir(alpha)
    code, out, err = _main(capsys, "report")
    assert code == exits.EXIT_OK
    assert json.loads(out)["repo"]["name"] == "alpha"
    assert "scoped" in err and str(alpha.resolve()) in err

    # beta ran last, so an unscoped lookup would have returned it.
    monkeypatch.chdir(beta)
    assert json.loads(_main(capsys, "report", "latest")[1])["repo"]["name"] == "beta"
    monkeypatch.chdir(alpha)
    sha = git(alpha, "rev-parse", "HEAD")
    assert json.loads(_main(capsys, "report", "--sha", sha[:12])[1])["repo"]["name"] == "alpha"


def test_latest_in_a_git_work_tree_with_no_run_for_it_does_not_return_another_repository(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _record_two_repos(tmp_path, capsys)
    gamma = make_repo(tmp_path / "gamma")

    monkeypatch.chdir(gamma)
    code, out, _ = _main(capsys, "report")

    assert code == exits.EXIT_NO_RUNS
    assert out == ""


def test_latest_outside_git_with_two_repositories_refuses_naming_both(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _record_two_repos(tmp_path, capsys)

    code, out, err = _main(capsys, "report", "latest")

    assert code == exits.EXIT_REPO_MISMATCH
    assert out == ""
    assert "alpha" in err and "beta" in err and "--repo" in err
    assert _main(capsys, "report", "latest", "--repo", "alpha")[0] == exits.EXIT_OK


def test_latest_outside_git_with_one_repository_on_record_returns_it(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", repo_path="/repos/alpha")
    write_run(reports, "20260101T000001Z-11", repo_path="/repos/alpha")

    code, out, _ = _main(capsys, "report")

    assert code == exits.EXIT_OK
    assert json.loads(out)["run_id"] == "20260101T000001Z-11"


def test_an_explicit_run_id_with_repo_is_an_assertion(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", repo_path="/repos/alpha")

    assert _main(capsys, "report", "20260101T000000Z-10", "--repo", "alpha")[0] == exits.EXIT_OK
    code, out, err = _main(capsys, "report", "20260101T000000Z-10", "--repo", "beta")
    assert code == exits.EXIT_REPO_MISMATCH
    assert out == ""
    assert "alpha" in err and "beta" in err
    assert _main(capsys, "report", "20260101T000000Z-10", "--sha", "a" * 40)[0] == exits.EXIT_USAGE
