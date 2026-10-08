"""The conftest removes a test's tmp_path siblings only when setup, call and
teardown all passed, matching what pytest's own retention policy keeps."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_CONFTEST = Path(__file__).resolve().parent / "conftest.py"

_INNER_TESTS = """
import pytest


@pytest.fixture
def failing_teardown():
    yield
    raise RuntimeError("teardown failure")


@pytest.fixture
def failing_setup():
    raise RuntimeError("setup failure")


def _sibling(tmp_path, suffix):
    sibling = tmp_path.parent / (tmp_path.name + "-" + suffix)
    sibling.mkdir()
    (sibling / "marker").write_text("x")


def test_all_phases_pass(tmp_path):
    _sibling(tmp_path, "passed")


def test_body_passes_teardown_fails(tmp_path, failing_teardown):
    _sibling(tmp_path, "teardownfail")


def test_body_fails(tmp_path):
    _sibling(tmp_path, "bodyfail")
    assert False


def test_setup_fails(tmp_path, failing_setup):
    pass
"""


def test_siblings_survive_any_failed_phase(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\ntmp_path_retention_policy = failed\n")
    pytester.makeconftest(_CONFTEST.read_text(encoding="utf-8"))
    pytester.makepyfile(test_inner=_INNER_TESTS)
    basetemp = pytester.path / "bt"

    result = pytester.runpytest_inprocess(f"--basetemp={basetemp}", "-p", "no:cacheprovider")
    result.assert_outcomes(passed=2, failed=1, errors=2)

    leftovers = sorted(p.parent.name.rsplit("-", 1)[1] for p in basetemp.glob("*-*/marker"))
    assert leftovers == ["bodyfail", "teardownfail"]
