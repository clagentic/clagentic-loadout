"""test_merge_ci_malformed_entries.py — an unreadable check-run or commit-status
entry makes the CI gate refuse as gate-fact-unavailable, naming the head SHA. It
is never counted toward "the list is complete" and then dropped, which would let
unreadable CI evidence stand as an empty pass."""

from __future__ import annotations

import pytest

from clagentic_loadout.merge import github_backend, verb
from clagentic_loadout.merge.errors import GateFactUnavailableError
from tests._support.ci_gate import HEAD_SHA, check_run, forgejo_ci_opener, github_ci_opener, status_entry
from tests._support.fakes import json_resp
from tests.test_merge_verb_ci_check_runs import _run


def _without(entry: dict, key: str) -> dict:
    return {k: v for k, v in entry.items() if k != key}


_GOOD = check_run("test", run_id=1, suite_id=10)

_MALFORMED_CHECK_RUNS = {
    "null": None,
    "non-object": "failure",
    "missing name": _without(_GOOD, "name"),
    "missing status": _without(_GOOD, "status"),
    "missing id": _without(_GOOD, "id"),
    "missing suite": _without(_GOOD, "check_suite"),
    "suite without id": {**_GOOD, "check_suite": {}},
    "completed without conclusion": {**_GOOD, "conclusion": None},
    "non-string conclusion": {**_GOOD, "conclusion": 3},
}


class TestMalformedCheckRuns:
    @pytest.mark.parametrize("entry", _MALFORMED_CHECK_RUNS.values(), ids=_MALFORMED_CHECK_RUNS.keys())
    def test_fetch_refuses_naming_the_head(self, entry):
        opener = github_ci_opener(check_runs=[entry])
        with pytest.raises(GateFactUnavailableError) as exc:
            github_backend.fetch_ci_status("o", "r", HEAD_SHA, token="tok", opener=opener)
        assert HEAD_SHA in str(exc.value)

    def test_total_count_one_with_a_single_null_refuses_the_merge(self, capsys):
        merge_calls: list = []
        opener = github_ci_opener(check_runs=[None], merge_calls=merge_calls)

        assert _run(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE

        assert HEAD_SHA in capsys.readouterr().err
        assert merge_calls == []

    def test_a_malformed_entry_among_readable_ones_refuses(self):
        runs = [_GOOD, None, check_run("lint", run_id=2, suite_id=10)]
        merge_calls: list = []
        opener = github_ci_opener(check_runs=runs, merge_calls=merge_calls)

        assert _run(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert merge_calls == []

    def test_a_malformed_entry_on_a_later_page_refuses(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=10) for i in range(33)]
        runs.append(None)
        merge_calls: list = []
        opener = github_ci_opener(check_runs=runs, check_runs_page_size=30, merge_calls=merge_calls)

        assert _run(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert merge_calls == []

    @pytest.mark.parametrize("body", [{"check_runs": [_GOOD]}, {"total_count": "1", "check_runs": [_GOOD]}])
    def test_a_response_without_an_integer_total_refuses(self, body):
        base = github_ci_opener()

        def opener(req, timeout=30):
            if req.full_url.split("?")[0].endswith("/check-runs"):
                return json_resp(200, body)
            return base(req, timeout=timeout)

        with pytest.raises(GateFactUnavailableError, match="total_count"):
            github_backend.fetch_ci_status("o", "r", HEAD_SHA, token="tok", opener=opener)

    def test_a_response_without_a_run_list_refuses(self):
        base = github_ci_opener()

        def opener(req, timeout=30):
            if req.full_url.split("?")[0].endswith("/check-runs"):
                return json_resp(200, {"total_count": 1})
            return base(req, timeout=timeout)

        with pytest.raises(GateFactUnavailableError, match="no run list"):
            github_backend.fetch_ci_status("o", "r", HEAD_SHA, token="tok", opener=opener)


_MALFORMED_STATUSES = {
    "null": None,
    "non-object": "success",
    "missing state": {"context": "ci"},
    "non-string state": {"context": "ci", "state": 3},
    "non-string context": {"context": 3, "state": "success"},
}


class TestMalformedStatuses:
    @pytest.mark.parametrize("entry", _MALFORMED_STATUSES.values(), ids=_MALFORMED_STATUSES.keys())
    def test_github_refuses_the_merge(self, entry, capsys):
        merge_calls: list = []
        opener = github_ci_opener(state="success", statuses=[entry], merge_calls=merge_calls)

        assert _run(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE

        assert HEAD_SHA in capsys.readouterr().err
        assert merge_calls == []

    @pytest.mark.parametrize("entry", _MALFORMED_STATUSES.values(), ids=_MALFORMED_STATUSES.keys())
    def test_forgejo_refuses_the_merge(self, entry, capsys):
        merge_calls: list = []
        opener = forgejo_ci_opener(state="success", statuses=[entry], merge_calls=merge_calls)

        assert _run(opener, platform="forgejo") == verb.EXIT_GATE_FACT_UNAVAILABLE

        assert HEAD_SHA in capsys.readouterr().err
        assert merge_calls == []

    def test_a_malformed_entry_beside_a_readable_one_refuses(self):
        opener = forgejo_ci_opener(
            state="success", statuses=[status_entry("build / test", "success"), None]
        )
        assert _run(opener, platform="forgejo") == verb.EXIT_GATE_FACT_UNAVAILABLE

    def test_a_statuses_field_that_is_not_a_list_refuses(self):
        base = github_ci_opener()

        def opener(req, timeout=30):
            if req.full_url.split("?")[0].endswith("/status"):
                return json_resp(200, {"state": "success", "statuses": {"state": "success"}})
            return base(req, timeout=timeout)

        with pytest.raises(GateFactUnavailableError, match="not a list"):
            github_backend.fetch_ci_status("o", "r", HEAD_SHA, token="tok", opener=opener)

    def test_a_readable_entry_with_the_forgejo_status_spelling_is_read(self):
        entry = {"context": "build / test", "status": "failure"}
        opener = forgejo_ci_opener(state="failure", statuses=[entry])
        assert _run(opener, platform="forgejo") == verb.EXIT_CI_STATUS_FAILED
