"""The JUnit report is written to a per-run scratch path a differently-privileged
test process can reach, then copied into the private run record."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text

from clagentic_loadout.repo_test import exits, report_scratch, store
from clagentic_loadout.repo_test.verb import main

_OWN = frozenset({os.geteuid()})

# Records what the command saw of its report path, then writes a one-case report.
_PROBE = """\
import json, os, stat, sys
junit = sys.argv[1]
parent = os.path.dirname(junit)
seen = {
    "junit": junit,
    "dir_mode": stat.S_IMODE(os.stat(parent).st_mode),
    "existed_before": os.path.exists(junit),
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
    assert seen["dir_mode"] == 0o1733
    assert seen["existed_before"] is False
    assert summary["counts"]["collected"] == 1
    assert summary["report_verification"]["status"] == "verified"
    assert summary["report_verification"]["owner_uid"] == os.geteuid()
    assert summary["report_verification"]["writable_by_others"] is False
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
    code_text = (
        "import os, sys; open(sys.argv[1], 'w').close(); os.remove(sys.argv[1]); os.symlink(sys.argv[2], sys.argv[1])"
    )
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

    assert code == exits.EXIT_OK, summary.get("problem")
    assert summary["counts"] == {"collected": 1, "passed": 1, "failed": 0, "skipped": 0}


def test_a_report_that_grows_past_the_cap_after_the_size_check_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / "scratch"
    directory.mkdir()
    source = directory / "junit.xml"
    source.write_bytes(b"x" * 100)
    scratch = report_scratch.ReportScratch(directory, source)
    dest = tmp_path / "stored.xml"
    monkeypatch.setattr(report_scratch, "MAX_JUNIT_BYTES", 50)
    real_fstat = os.fstat

    def small_when_checked(fd: int) -> object:
        real = real_fstat(fd)
        return type(
            "Info", (), {"st_mode": real.st_mode, "st_size": 10, "st_nlink": real.st_nlink, "st_uid": real.st_uid}
        )()

    monkeypatch.setattr(report_scratch.os, "fstat", small_when_checked)

    with pytest.raises(report_scratch.JunitError) as caught:
        report_scratch.collect_report(scratch, dest, _OWN)

    assert "grew past the 50 byte limit" in str(caught.value)
    assert str(source) in str(caught.value)
    assert not dest.exists()


def test_a_report_exactly_at_the_cap_is_stored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    directory = tmp_path / "scratch"
    directory.mkdir()
    source = directory / "junit.xml"
    source.write_bytes(b"y" * 50)
    dest = tmp_path / "stored.xml"
    monkeypatch.setattr(report_scratch, "MAX_JUNIT_BYTES", 50)

    report_scratch.collect_report(report_scratch.ReportScratch(directory, source), dest, _OWN)

    assert dest.read_bytes() == b"y" * 50


def _scratch_with_report(tmp_path: Path, mode: int = 0o644) -> report_scratch.ReportScratch:
    directory = tmp_path / "scratch"
    directory.mkdir()
    source = directory / "junit.xml"
    source.write_text("<testsuite><testcase classname='c' name='t'/></testsuite>")
    source.chmod(mode)
    return report_scratch.ReportScratch(directory, source)


def test_a_report_owned_by_a_user_the_command_was_not_seen_as_is_refused(tmp_path: Path) -> None:
    scratch = _scratch_with_report(tmp_path)
    dest = tmp_path / "stored.xml"

    with pytest.raises(report_scratch.ReportUntrusted) as caught:
        report_scratch.collect_report(scratch, dest, frozenset({os.geteuid() + 1}))

    assert f"owned by user id {os.geteuid()}" in str(caught.value)
    assert not dest.exists()


def test_a_report_writable_by_others_is_refused(tmp_path: Path) -> None:
    scratch = _scratch_with_report(tmp_path, mode=0o666)
    dest = tmp_path / "stored.xml"

    with pytest.raises(report_scratch.ReportUntrusted) as caught:
        report_scratch.collect_report(scratch, dest, _OWN)

    assert "writable by any user" in str(caught.value)
    assert not dest.exists()


def test_a_report_linked_from_another_name_is_refused(tmp_path: Path) -> None:
    scratch = _scratch_with_report(tmp_path)
    os.link(scratch.junit_path, tmp_path / "other-name")
    dest = tmp_path / "stored.xml"

    with pytest.raises(report_scratch.ReportUntrusted) as caught:
        report_scratch.collect_report(scratch, dest, _OWN)

    assert "2 links" in str(caught.value)
    assert not dest.exists()


def test_a_group_writable_report_from_the_command_is_accepted(tmp_path: Path) -> None:
    scratch = _scratch_with_report(tmp_path, mode=0o664)

    verified = report_scratch.collect_report(scratch, tmp_path / "stored.xml", _OWN)

    assert verified["status"] == "verified"


@pytest.mark.skipif(os.geteuid() != 0, reason="creating a file as another user needs a root caller")
def test_a_report_created_by_another_user_during_the_run_is_refused_not_trusted(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # The stranger runs outside the command's session, as a user the command never
    # becomes, and creates the report name first; the command then writes into it.
    base = report_scratch._scratch_base()
    if not report_scratch._others_can_traverse(base):
        pytest.skip("no temp location reachable by an unprivileged user on this host")
    existing = set(base.glob("loadout-junit-*"))
    go = tmp_path / "go"
    script = (
        "import os, sys, time\n"
        "while not os.path.exists(sys.argv[2]): time.sleep(0.01)\n"
        "open(sys.argv[1], 'w').write(\"<testsuite><testcase classname='c' name='t'/></testsuite>\")\n"
    )
    repo = make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, "run.py", "{junit}", str(go)]))
    (repo / "run.py").write_text(script)
    forge = (
        "import os, sys; os.setgid(65534); os.setuid(65534); "
        "open(sys.argv[1], 'w').write(\"<testsuite><testcase classname='c' name='forged'/></testsuite>\"); "
        "os.chmod(sys.argv[1], 0o644)"
    )

    def stranger() -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found = list(set(base.glob("loadout-junit-*")) - existing)
            if found:
                subprocess.run([sys.executable, "-c", forge, str(found[0] / "junit.xml")], check=True)
                go.write_text("go")
                return
            time.sleep(0.005)

    thread = threading.Thread(target=stranger)
    thread.start()
    try:
        code, summary = _run(capsys, repo)
    finally:
        go.write_text("go")
        thread.join()

    assert code == exits.EXIT_UNRUNNABLE
    assert "owned by user id 65534" in summary["problem"]
    assert summary["report_verification"]["status"] == "refused"
    assert not (reports / summary["run_id"] / store.JUNIT_FILE).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="switching user needs a root caller")
def test_a_report_rewritten_in_place_by_a_stranger_is_refused(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A command that leaves its report world-writable (umask 0) can be rewritten by
    # anyone; the run is refused rather than trusted.
    code_text = (
        "import os, sys; os.umask(0); "
        "open(sys.argv[1], 'w').write(\"<testsuite><testcase classname='c' name='t'/></testsuite>\")"
    )
    repo = make_repo(tmp_path / "repo", profile=profile_text(full=[sys.executable, "-c", code_text, "{junit}"]))

    code, summary = _run(capsys, repo)

    assert code == exits.EXIT_UNRUNNABLE
    assert "writable by any user" in summary["problem"]
    assert summary["report_verification"]["status"] == "refused"


def test_a_fifo_at_the_report_name_is_refused_without_blocking(tmp_path: Path) -> None:
    directory = tmp_path / "scratch"
    directory.mkdir()
    source = directory / "junit.xml"
    os.mkfifo(source)
    scratch = report_scratch.ReportScratch(directory, source)
    dest = tmp_path / "stored.xml"
    outcome: list[BaseException] = []

    def collect() -> None:
        try:
            report_scratch.collect_report(scratch, dest, _OWN)
        except BaseException as exc:  # noqa: BLE001 - the thread hands any outcome back to the assertion
            outcome.append(exc)

    worker = threading.Thread(target=collect, daemon=True)
    worker.start()
    worker.join(timeout=10)

    try:
        assert not worker.is_alive(), "collecting a FIFO report blocked"
    finally:
        if worker.is_alive():
            # Unblock the stuck open so the thread can end.
            fd = os.open(source, os.O_WRONLY | os.O_NONBLOCK)
            os.close(fd)
            worker.join(timeout=5)
    assert len(outcome) == 1
    assert isinstance(outcome[0], report_scratch.ReportUntrusted)
    assert "not a regular file" in str(outcome[0])
    assert not dest.exists()


def test_a_run_whose_report_name_holds_a_fifo_is_refused_in_bounded_time(
    tmp_path: Path, reports: Path, scratch_base: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo", profile=profile_text(full=[sys.executable, "mk.py", "{junit}"])
    )
    (repo / "mk.py").write_text("import os, sys; os.mkfifo(sys.argv[1])")
    started = time.monotonic()

    code, summary = _run(capsys, repo)

    assert time.monotonic() - started < 30
    assert code != exits.EXIT_OK
    assert "not a regular file" in json.dumps(summary)
