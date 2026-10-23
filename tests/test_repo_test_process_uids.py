"""The watcher keeps the user ids a command's session ran as."""

from __future__ import annotations

import os
import subprocess
import sys

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
