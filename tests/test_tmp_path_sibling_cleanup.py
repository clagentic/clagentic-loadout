"""The conftest removes a test's tmp_path and siblings only when setup, call and
teardown all passed; a failure in any phase keeps both for diagnosis."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_INNER_TESTS = """
import pytest


def _populate(tmp_path, suffix):
    (tmp_path / "marker").write_text(suffix)
    sibling = tmp_path.parent / (tmp_path.name + "-" + suffix)
    sibling.mkdir()
    (sibling / "marker").write_text("x")


@pytest.fixture
def failing_teardown():
    yield
    raise RuntimeError("teardown failure")


@pytest.fixture
def failing_setup(tmp_path):
    _populate(tmp_path, "setupfail")
    raise RuntimeError("setup failure")


def test_all_phases_pass(tmp_path):
    _populate(tmp_path, "passed")


def test_body_passes_teardown_fails(tmp_path, failing_teardown):
    _populate(tmp_path, "teardownfail")


def test_body_fails(tmp_path):
    _populate(tmp_path, "bodyfail")
    assert False


def test_setup_fails(failing_setup):
    pass
"""


def test_tmp_path_and_siblings_follow_all_phases_passed(pytester: pytest.Pytester) -> None:
    pytester.makeconftest(_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_inner=_INNER_TESTS)
    basetemp = pytester.path / "bt"

    result = pytester.runpytest_inprocess(f"--basetemp={basetemp}", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=1, errors=2)

    def survivors(suffix: str) -> tuple[bool, bool]:
        sibling = any(basetemp.glob(f"*-{suffix}/marker"))
        own = any(
            marker.read_text() == suffix
            for marker in basetemp.glob("*/marker")
            if not marker.parent.name.endswith(f"-{suffix}")
        )
        return own, sibling

    assert survivors("setupfail") == (True, True)
    assert survivors("bodyfail") == (True, True)
    assert survivors("teardownfail") == (True, True)
    assert survivors("passed") == (False, False)
