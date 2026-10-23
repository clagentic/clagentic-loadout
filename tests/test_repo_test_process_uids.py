"""The watcher keeps the user ids a command's session ran as."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from clagentic_loadout.repo_test import process_uids
from clagentic_loadout.repo_test.process_uids import UidWatcher


def test_the_watcher_sees_the_user_id_of_the_command_and_its_child() -> None:
    code = "import subprocess, sys, time; subprocess.run([sys.executable, '-c', 'import time; time.sleep(0.3)'])"
    proc = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    watcher = UidWatcher(proc.pid)
    watcher.start()
    proc.wait()

    assert os.geteuid() in watcher.stop()


def test_the_watcher_returns_nothing_for_a_process_that_does_not_exist() -> None:
    watcher = UidWatcher(2**22 + 12345)
    watcher.start()

    assert watcher.stop() == frozenset()


def test_a_stable_session_is_scanned_host_wide_far_less_often_than_the_fast_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans = []
    monkeypatch.setattr(process_uids, "_session_members", lambda session: scans.append(session) or [session])
    monkeypatch.setattr(process_uids, "_uids_of", lambda pid: {1000})
    watcher = UidWatcher(777)
    watcher.start()
    time.sleep(0.6)
    watcher.stop()

    # Fixed-rate scanning every fourth tick would be well over a hundred scans.
    assert 1 <= len(scans) <= 20


def test_a_process_that_appears_late_in_a_stable_session_is_still_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    started = time.monotonic()

    def members(session: int) -> list[int]:
        late = time.monotonic() - started > 0.4
        return [session, 888] if late else [session]

    monkeypatch.setattr(process_uids, "_session_members", members)
    monkeypatch.setattr(process_uids, "_uids_of", lambda pid: {4242} if pid == 888 else {1000})
    watcher = UidWatcher(777)
    watcher.start()
    time.sleep(0.8)

    assert watcher.stop() == frozenset({1000, 4242})


def test_a_known_process_changing_user_resets_a_backed_off_scan_interval(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scans: list[int] = []
    uid = {"value": 1000}
    monkeypatch.setattr(process_uids, "_session_members", lambda session: scans.append(session) or [session])
    monkeypatch.setattr(process_uids, "_uids_of", lambda pid: {uid["value"]})
    watcher = UidWatcher(777)
    watcher._sample()
    watcher._scan_wait = process_uids._FULL_SCAN_MAX
    watcher._next_scan = time.monotonic() + 3600
    before = len(scans)

    uid["value"] = 65534
    watcher._sample()

    assert len(scans) == before + 1
    assert watcher._scan_wait == process_uids._FULL_SCAN_MIN


def test_a_known_pid_that_left_the_session_contributes_no_user_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = {777: 777, 888: 777}
    uids = {777: {1000}, 888: {1000}}
    monkeypatch.setattr(process_uids, "_session_members", lambda session: [777, 888])
    monkeypatch.setattr(process_uids, "_uids_of", lambda pid: uids[pid])
    monkeypatch.setattr(process_uids, "_session_of", lambda pid: sessions[pid])
    watcher = UidWatcher(777)
    watcher._sample()
    assert 888 in watcher._known

    sessions[888] = 4000
    uids[888] = {31337}
    watcher._sample()

    assert 888 not in watcher._known
    assert watcher._uids == {1000}


def test_a_known_process_that_switches_user_is_seen_at_the_fast_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    started = time.monotonic()
    monkeypatch.setattr(process_uids, "_session_members", lambda session: [session])
    monkeypatch.setattr(
        process_uids, "_uids_of", lambda pid: {65534} if time.monotonic() - started > 0.3 else {0}
    )
    watcher = UidWatcher(777)
    watcher.start()
    time.sleep(0.4)

    assert 65534 in watcher.stop()
