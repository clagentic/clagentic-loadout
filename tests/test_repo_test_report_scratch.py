"""The JUnit report is written to a per-run scratch path a differently-privileged
test process can reach, then copied into the private run record."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text

from clagentic_loadout.repo_test import exits, report_scratch, store
from clagentic_loadout.repo_test.verb import main

# Records what the command saw of its report path, then writes a one-case report.
_PROBE = """\
import json, os, stat, sys
junit = sys.argv[1]
parent = os.path.dirname(junit)
seen = {
    "junit": junit,
    "dir_mode": stat.S_IMODE(os.stat(parent).st_mode),
    "file_mode": stat.S_IMODE(os.stat(junit).st_mode),
}
with open("seen.json", "w") as handle:
    handle.write(json.dumps(seen))
with open(junit, "w") as handle:
    handle.write("<testsuite><testcase classname='c' name='t'/></testsuite>")
"""

# Drops to an unprivileged uid (as a repo's own test bootstrap may) before writing.
_DROP_THEN_WRITE = (
    "import os, sys; os.setgid(65534); os.setuid(65534); "
    "open(sys.argv[1], 'w').write('<testsuite><testcase classname=\"c\" name=\"t\"/></testsuite>')"
)


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


@pytest.fixture
def scratch_base(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    base = tmp_path / "scratch-base"
    base.mkdir()
    monkeypatch.setattr(report_scratch, "_scratch_base", lambda: base)
    return base


def _probe_repo(root: Path) -> Path:
    # Declared argv entries must be one line, so the probe script lives in the repo.
    repo = make_repo(root, profile=profile_text(full=[sys.executable, "probe.py", "{junit}"]))
    (repo / "probe.py").write_text(_PROBE)
    return repo


def _run(capsys: pytest.CaptureFixture[str], repo: Path, *extra: str) -> tuple[int, dict]:
    code = main(["run", "full", "--repo-root", str(repo), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_the_command_gets_a_scratch_report_path_outside_the_private_run_record(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _probe_repo(tmp_path / "repo")

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_OK
    seen = json.loads((repo / "seen.json").read_text())
    assert Path(seen["junit"]).parent.parent == scratch_base
    assert not Path(seen["junit"]).is_relative_to(reports)
    assert seen["dir_mode"] == 0o733
    assert seen["file_mode"] == 0o666
    assert summary["counts"]["collected"] == 1
    stored = reports / summary["run_id"] / store.JUNIT_FILE
    assert stored.is_file()
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert list(scratch_base.iterdir()) == []


def test_the_scratch_is_removed_when_the_command_fails_without_a_report(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, "-c", "raise SystemExit(3)", "{junit}"]))

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_UNRUNNABLE
    assert "never wrote its report" in summary["problem"]
    assert str(scratch_base) in summary["problem"]
    assert "exited 3" in summary["problem"]
    assert list(scratch_base.iterdir()) == []


def test_the_scratch_is_removed_on_timeout(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(full=[sys.executable, "-c", "import time; time.sleep(60)", "{junit}"]),
    )

    code, summary = _run(capsys, repo, "--timeout", "0.5")

    assert code == exits.EXIT_UNRUNNABLE
    assert "timed out" in summary["problem"]
    assert list(scratch_base.iterdir()) == []


def test_a_report_replaced_by_a_symlink_is_refused_not_followed(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("<testsuite><testcase classname='c' name='leak'/></testsuite>")
    code_text = "import os, sys; os.remove(sys.argv[1]); os.symlink(sys.argv[2], sys.argv[1])"
    repo = make_repo(
        tmp_path / "repo",
        profile=profile_text(full=[sys.executable, "-c", code_text, "{junit}", str(secret)]),
    )

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_UNRUNNABLE
    assert "no readable report" in summary["problem"]
    assert not (reports / summary["run_id"] / store.JUNIT_FILE).exists()
    assert list(scratch_base.iterdir()) == []


def test_a_scratch_directory_that_cannot_be_removed_is_stated_in_the_problem(
    tmp_path: Path, reports: Path, scratch_base: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _probe_repo(tmp_path / "repo")

    def refuse(path: object) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(report_scratch.shutil, "rmtree", refuse)

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_OK
    assert "could not remove the report scratch directory" in summary["problem"]


def test_a_parent_without_the_search_bit_for_others_is_not_traversable(tmp_path: Path) -> None:
    private = tmp_path / "private"
    private.mkdir()
    child = private / "child"
    child.mkdir()
    child.chmod(0o755)
    private.chmod(0o700)
    try:
        assert report_scratch._others_can_traverse(child) is False
        assert report_scratch._others_can_traverse(Path("/")) is True
    finally:
        private.chmod(0o755)


@pytest.mark.skipif(os.geteuid() != 0, reason="switching user needs a root caller")
def test_a_command_that_drops_to_an_unprivileged_user_still_yields_a_report(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The real base selection is used: it must land somewhere the dropped user reaches.
    if not report_scratch._others_can_traverse(report_scratch._scratch_base()):
        pytest.skip("no temp location reachable by an unprivileged user on this host")
    repo = make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, "-c", _DROP_THEN_WRITE, "{junit}"]))

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_OK
    assert summary["counts"] == {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}
