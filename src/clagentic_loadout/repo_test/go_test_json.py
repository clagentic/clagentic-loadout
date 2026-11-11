"""repo_test.go_test_json — counts and failing test ids from a ``go test -json``
event stream, the ``go-test-json`` report format.

The stream is one JSON object per line (a test event). The fields read here are
``Action``, ``Package`` and ``Test``, plus ``Output`` and ``FailedBuild`` for a
failure's text. Every other field, and every ``Action`` not named below, is
ignored. The shapes were taken from the Go project's own ``cmd/internal/test2json``
reference streams (``testdata/*.json``) at the go1.25.13 release: ``start``,
``run``, ``pause``, ``cont``, ``pass``, ``fail``, ``skip``, ``output`` and ``attr``
events, where a package-level event has no ``Test`` and those streams carry no
``Package`` (``go test`` adds it). ``bench`` and the build events
(``build-output``, with an ``ImportPath``, and a package-level ``fail`` carrying
``FailedBuild``) are not in that testdata; they are read defensively: a build
event that is absent changes nothing.

Reading rules:

* A line that is not a JSON object with a string ``Action`` is skipped, never
  fatal (a test binary may print to the same stream). A line longer than
  MAX_LINE_BYTES, and an event whose package and test names together pass
  MAX_NAME_CHARS, is skipped and counted in ``GoStreamSummary.skipped_for_size``.
* A test is one id per ``(Package, Test)``: ``<package>::<test>``, or just
  ``<test>`` when the event names no package. A ``/`` inside a subtest name cannot
  be told from nesting, so two distinct tests can share an id; the ids are a
  multiset (see ``repo_test.junit``), so both are counted.
* ``pass`` and ``bench`` end a test as passed, ``skip`` as skipped and ``fail`` as
  failed. A test that ran or printed output and never reached one of those is
  counted as failed: the process ended under it (a panic, a timeout, a kill).
  Output arriving after a test has ended does not reopen it; a new ``run`` event
  does, so ``-count=N`` yields N cases.
* A ``fail`` event with no ``Test`` is a package-level failure (a build failure, a
  ``TestMain`` failure, a panic outside a test). It is a failing id of its own,
  ``<package>``, and counts as one failed case. A package-level ``pass`` or
  ``skip`` (a package with no tests) is not a case.
* The same ``fail`` rule means a package with a failing test reports two failing
  ids: the test and its package.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test.junit import FailedCase, JunitSummary

# Same whole-stream bound as the JUnit reader.
MAX_STREAM_BYTES = 256 * 1024 * 1024
# The Go test2json converter splits output into events of about 1 KiB, so a line
# this long is not an event.
MAX_LINE_BYTES = 1024 * 1024
# Longest package plus test name an event may carry; a longer event is skipped
# (and counted), so memory held per tracked test is bounded.
MAX_NAME_CHARS = 512
# Distinct (package, test) pairs tracked at once.
MAX_TRACKED_TESTS = 200_000
# Failing ids kept: result.json holds them all and is read back under an 8 MiB limit.
MAX_FAILING_IDS = 10_000
# Characters of one failed case's output kept while reading.
CASE_TEXT_CAP_CHARS = 6_000
# Characters of case output kept across all failed cases of one read.
TOTAL_TEXT_CAP_CHARS = 4 * 1024 * 1024

NO_PACKAGE_ID = "(unnamed package)"

_TERMINAL = {"pass": "passed", "bench": "passed", "skip": "skipped", "fail": "failed"}
_OPENING = frozenset({"run", "pause", "cont", "output"})

_OPEN = 0
_CLOSED = 1

KIND_FAILURE = "failure"
KIND_ERROR = "error"


class GoStreamError(Exception):
    """The stream is missing, too large, holds no event, or names more tests than
    the caps allow."""


@dataclass(frozen=True)
class GoStreamSummary(JunitSummary):
    """The JUnit summary shape, plus how many lines or events were skipped for size."""

    skipped_for_size: int = 0


def case_id(package: str, test: str) -> str:
    return f"{package}::{test}" if package else test


def package_id(package: str) -> str:
    return package or NO_PACKAGE_ID


def _lines(path: Path) -> Iterator[bytes | None]:
    """Each line of the file, or None for a line over MAX_LINE_BYTES (its bytes are
    read and dropped, never held). Raises GoStreamError when the file is missing,
    cannot be read, or passes MAX_STREAM_BYTES."""
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise GoStreamError(f"no go test event stream at the path the run recorded: {exc}") from exc
    read = 0
    with handle:
        while True:
            try:
                line = handle.readline(MAX_LINE_BYTES + 1)
                read += len(line)
                oversized = len(line) > MAX_LINE_BYTES and not line.endswith(b"\n")
                while oversized and read <= MAX_STREAM_BYTES:
                    chunk = handle.readline(MAX_LINE_BYTES)
                    read += len(chunk)
                    if not chunk or chunk.endswith(b"\n"):
                        break
            except OSError as exc:
                raise GoStreamError(f"the go test event stream cannot be read: {exc}") from exc
            if read > MAX_STREAM_BYTES:
                raise GoStreamError(f"the go test event stream is over the {MAX_STREAM_BYTES} byte limit")
            if not line:
                return
            yield None if oversized else line


class EventReader:
    """Iterates the events of a stream and counts the lines and events it skipped
    for size (``skipped``) and the events it read (``events``)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self.skipped = 0
        self.events = 0

    def __iter__(self) -> Iterator[dict[str, Any]]:
        for line in _lines(self._path):
            if line is None:
                self.skipped += 1
                continue
            event = _decode(line)
            if event is None:
                continue
            if len(_text(event, "Package")) + len(_text(event, "Test")) > MAX_NAME_CHARS:
                self.skipped += 1
                continue
            self.events += 1
            yield event


def _decode(line: bytes) -> dict[str, Any] | None:
    stripped = line.strip()
    if not stripped.startswith(b"{"):
        return None
    try:
        event = json.loads(stripped)
    except (ValueError, RecursionError):
        return None
    if not isinstance(event, dict) or not isinstance(event.get("Action"), str):
        return None
    return event


def _text(event: dict[str, Any], key: str) -> str:
    value = event.get(key)
    return value if isinstance(value, str) else ""


class _Tracker:
    """The state of the cases of a stream, event by event."""

    def __init__(self) -> None:
        self.state: dict[tuple[str, str], int] = {}
        self.passed = 0
        self.skipped = 0
        # Which run of a (package, test) is current: 1 for the first, one more per
        # later run, so -count=N output can be told apart per run.
        self.instance: dict[tuple[str, str], int] = {}
        # (kind, id, package, test, failed_build, instance) per failed case, in the
        # order each failed.
        self.failed: list[tuple[str, str, str, str, str, int]] = []

    def _fail(self, kind: str, case_id: str, package: str, test: str, failed_build: str = "") -> None:
        if len(self.failed) >= MAX_FAILING_IDS:
            raise GoStreamError(
                f"the go test event stream reports more than {MAX_FAILING_IDS} failed cases, "
                "over the limit this reader keeps"
            )
        self.failed.append((kind, case_id, package, test, failed_build, self.instance.get((package, test), 0)))

    def feed(self, event: dict[str, Any]) -> None:
        action = event["Action"]
        package = sys.intern(_text(event, "Package"))
        test = _text(event, "Test")
        if not test:
            if action == "fail":
                self._fail(KIND_ERROR, package_id(package), package, "", _text(event, "FailedBuild"))
            return
        key = (package, test)
        current = self.state.get(key)
        if action == "run":
            if current == _OPEN:
                self._fail(KIND_ERROR, case_id(package, test), package, test)
            self._track(key)
        elif action in _OPENING:
            if current is None:
                self._track(key)
        elif action in _TERMINAL:
            if current == _CLOSED:
                return
            self.state[key] = _CLOSED
            if len(self.state) > MAX_TRACKED_TESTS:
                self._too_many()
            outcome = _TERMINAL[action]
            if outcome == "passed":
                self.passed += 1
            elif outcome == "skipped":
                self.skipped += 1
            else:
                self._fail(KIND_FAILURE, case_id(package, test), package, test)

    def _track(self, key: tuple[str, str]) -> None:
        self.state[key] = _OPEN
        self.instance[key] = self.instance.get(key, 0) + 1
        if len(self.state) > MAX_TRACKED_TESTS:
            self._too_many()

    @staticmethod
    def _too_many() -> None:
        raise GoStreamError(f"the go test event stream names more than {MAX_TRACKED_TESTS} tests")

    def finish(self) -> None:
        """Tests still open at the end of the stream never reached a result."""
        for (package, test), state in self.state.items():
            if state == _OPEN:
                self._fail(KIND_ERROR, case_id(package, test), package, test)


def _run(path: Path) -> tuple[_Tracker, EventReader]:
    reader = EventReader(path)
    tracker = _Tracker()
    for event in reader:
        tracker.feed(event)
    if reader.events == 0:
        raise GoStreamError(
            "the command wrote no go test event to its standard output "
            "(is it `go test -json`, and did it build?)"
        )
    tracker.finish()
    return tracker, reader


def parse_go_stream(path: Path) -> GoStreamSummary:
    tracker, reader = _run(path)
    ids = sorted(case[1] for case in tracker.failed)
    return GoStreamSummary(
        collected=tracker.passed + tracker.skipped + len(ids),
        passed=tracker.passed,
        failed=len(ids),
        skipped=tracker.skipped,
        failing_ids=tuple(ids),
        skipped_for_size=reader.skipped,
    )


def _failure_message(kind: str, failed_build: str, package: str, test: str) -> str:
    if failed_build:
        return f"build failed: {failed_build}"
    if kind == KIND_ERROR and test:
        return "the test never reached a result"
    if not test:
        return "package failed"
    return ""


def parse_failed_cases(path: Path) -> list[FailedCase]:
    """One entry per failed case, in the order each failed, so cases sharing an id
    each keep their own entry. The text of a test is its ``Output`` events; the
    text of a package is its package-level output, and for a build failure also
    the ``build-output`` of the package that failed to build. The text is neither
    redacted nor bounded to the caps of ``repo_test.failures``; it is cut at
    CASE_TEXT_CAP_CHARS per case and TOTAL_TEXT_CAP_CHARS in all."""
    tracker, _ = _run(path)
    # Output is kept per run of a test, so under -count=N a failed case carries
    # only its own run's output. A second tracker replays the stream to say which
    # run each Output event belongs to.
    wanted_tests = {(package, test, run) for _, _, package, test, _, run in tracker.failed if test}
    wanted_packages = {package for _, _, package, test, _, _ in tracker.failed if not test}
    wanted_builds = {build for _, _, _, _, build, _ in tracker.failed if build}
    replay = _Tracker()
    chunks: dict[tuple[str, str, int], list[str]] = {}
    used: dict[tuple[str, str, int], int] = {}
    builds: dict[str, list[str]] = {}
    build_used: dict[str, int] = {}
    cut: set[Any] = set()
    budget = TOTAL_TEXT_CAP_CHARS

    def keep(store: dict[Any, list[str]], sizes: dict[Any, int], key: Any, text: str) -> None:
        nonlocal budget
        if not text:
            return
        room = max(min(CASE_TEXT_CAP_CHARS - sizes.get(key, 0), budget), 0)
        if room < len(text):
            cut.add(key)
        if room == 0:
            return
        piece = text[:room]
        store.setdefault(key, []).append(piece)
        sizes[key] = sizes.get(key, 0) + len(piece)
        budget -= len(piece)

    for event in EventReader(path):
        replay.feed(event)
        action = event["Action"]
        if action == "build-output":
            build = _text(event, "ImportPath")
            if build in wanted_builds:
                keep(builds, build_used, build, _text(event, "Output"))
            continue
        if action != "output":
            continue
        package, test = _text(event, "Package"), _text(event, "Test")
        if test:
            run = replay.instance.get((package, test), 0)
            if (package, test, run) in wanted_tests:
                keep(chunks, used, (package, test, run), _text(event, "Output"))
        elif package in wanted_packages:
            keep(chunks, used, (package, "", 0), _text(event, "Output"))

    cases: list[FailedCase] = []
    for kind, failed_id, package, test, failed_build, run in tracker.failed:
        parts = list(chunks.get((package, test, run), ()))
        if failed_build:
            parts = list(builds.get(failed_build, ())) + parts
        text = "".join(parts)
        if (package, test, run) in cut or (failed_build and failed_build in cut):
            text += "\n[output cut while the stream was read]"
        cases.append(
            FailedCase(
                test_id=failed_id,
                kind=kind,
                message=_failure_message(kind, failed_build, package, test),
                text=text,
            )
        )
    return cases


__all__ = [
    "CASE_TEXT_CAP_CHARS",
    "MAX_FAILING_IDS",
    "MAX_LINE_BYTES",
    "MAX_NAME_CHARS",
    "MAX_STREAM_BYTES",
    "MAX_TRACKED_TESTS",
    "NO_PACKAGE_ID",
    "TOTAL_TEXT_CAP_CHARS",
    "GoStreamError",
    "GoStreamSummary",
    "parse_failed_cases",
    "parse_go_stream",
]
