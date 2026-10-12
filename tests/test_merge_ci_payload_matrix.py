"""test_merge_ci_payload_matrix.py — every field the CI gate reads, on every
endpoint it reads it from, refuses as gate-fact-unavailable when it is missing,
null, of the wrong type (including the falsy wrong types False/0/[]/{}) or out of
range. Each row drives the merge verb end to end and asserts the refusal names
the head SHA and that no merge call was made, so no malformed response can turn
into an empty pass.

Rows are generated from one description per endpoint: a readable payload and
the (path, accepted type) of each field the gate reads."""

from __future__ import annotations

import copy
import inspect

import pytest

from clagentic_loadout.merge import forgejo_backend, github_backend, verb
from clagentic_loadout.merge.ci_status import CiStatusResult
from tests._support.ci_gate import (
    HEAD_SHA,
    check_run,
    combined_status,
    forgejo_ci_opener,
    github_ci_opener,
    run_merge,
    status_entry,
)
from tests._support.fakes import json_resp

_MISSING = object()

_STRING, _INT, _LIST, _DICT = (str,), (int,), (list,), (dict,)
_STRING_OR_NULL = (str, type(None))

#: Replacement values tried for every field. Each is skipped for a field that
#: legitimately accepts it.
_BAD_VALUES = {
    "missing": _MISSING,
    "null": None,
    "string": "x",
    "integer": 3,
    "false": False,
    "zero": 0,
    "empty-list": [],
    "empty-object": {},
}


def _accepts(accepted: tuple, value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, accepted)


def _mutated(payload, path: tuple, value):
    out = copy.deepcopy(payload)
    if not path:
        return value
    node = out
    for key in path[:-1]:
        node = node[key]
    if value is _MISSING:
        del node[path[-1]]
    else:
        node[path[-1]] = value
    return out


_GOOD_STATUS = combined_status("success", [status_entry("ci", "success")])
_GOOD_CHECK_RUNS = {"total_count": 1, "check_runs": [check_run("test", run_id=1, suite_id=10)]}

#: (path, accepted types) of each field the gate reads, per payload.
_STATUS_FIELDS = [
    ((), _DICT),
    (("state",), _STRING),
    (("statuses",), _LIST),
    (("statuses", 0), _DICT),
    (("statuses", 0, "state"), _STRING),
    (("statuses", 0, "context"), _STRING),
]
_CHECK_RUN_FIELDS = [
    ((), _DICT),
    (("total_count",), _INT),
    (("check_runs",), _LIST),
    (("check_runs", 0), _DICT),
    (("check_runs", 0, "name"), _STRING),
    (("check_runs", 0, "status"), _STRING),
    (("check_runs", 0, "id"), _INT),
    (("check_runs", 0, "check_suite"), _DICT),
    (("check_runs", 0, "check_suite", "id"), _INT),
    (("check_runs", 0, "conclusion"), _STRING_OR_NULL),
]

#: Values that are the right type but outside the readable range.
_OUT_OF_RANGE = [
    ("github-check-runs", ("total_count",), -1, "negative total_count"),
    ("github-check-runs", ("total_count",), 0, "zero total_count beside a listed run"),
    ("github-check-runs", ("check_runs", 0, "name"), "", "empty name"),
    ("github-check-runs", ("check_runs", 0, "conclusion"), None, "completed without a conclusion"),
    ("github-status", (), {}, "empty body"),
    ("forgejo-status", (), {}, "empty body"),
    ("github-check-runs", (), {}, "empty body"),
    ("github-status", ("statuses", 0), {}, "empty entry"),
    ("forgejo-status", ("statuses", 0), {}, "empty entry"),
    ("github-check-runs", ("check_runs", 0), {}, "empty entry"),
    ("github-check-runs", ("check_runs", 0, "check_suite"), {}, "empty suite"),
]

_PAYLOADS = {
    "github-status": _GOOD_STATUS,
    "github-check-runs": _GOOD_CHECK_RUNS,
    "forgejo-status": _GOOD_STATUS,
}
_FIELDS = {
    "github-status": _STATUS_FIELDS,
    "github-check-runs": _CHECK_RUN_FIELDS,
    "forgejo-status": _STATUS_FIELDS,
}


def _rows():
    for endpoint, fields in _FIELDS.items():
        for path, accepted in fields:
            for label, value in _BAD_VALUES.items():
                if value is _MISSING and (not path or isinstance(path[-1], int)):
                    # A whole body cannot be absent, and deleting a list element
                    # is a shorter list, not a malformed field.
                    continue
                if value is not _MISSING and _accepts(accepted, value):
                    continue
                if value is None and type(None) in accepted:
                    continue
                dotted = ".".join(str(p) for p in path) or "body"
                yield pytest.param(endpoint, path, value, id=f"{endpoint}:{dotted}:{label}")
    for endpoint, path, value, label in _OUT_OF_RANGE:
        yield pytest.param(endpoint, path, value, id=f"{endpoint}:{'.'.join(map(str, path))}:{label}")


def _opener(endpoint: str, body, merge_calls: list):
    """A host serving *body* for the endpoint under test and readable payloads
    for everything else."""
    if endpoint == "forgejo-status":
        base = forgejo_ci_opener(state="success", merge_calls=merge_calls)
        suffix = "/status"
    elif endpoint == "github-status":
        base = github_ci_opener(merge_calls=merge_calls, check_runs=[])
        suffix = "/status"
    else:
        base = github_ci_opener(merge_calls=merge_calls)
        suffix = "/check-runs"

    def opener(req, timeout=30):
        if req.get_method() == "GET" and req.full_url.split("?")[0].endswith(suffix):
            return json_resp(200, body)
        return base(req, timeout=timeout)

    return opener


def _platform(endpoint: str) -> str:
    return "forgejo" if endpoint.startswith("forgejo") else "github"


class TestMalformedPayloadMatrix:
    @pytest.mark.parametrize("endpoint, path, value", _rows())
    def test_unreadable_field_refuses_naming_the_head_and_merges_nothing(
        self, endpoint, path, value, capsys
    ):
        merge_calls: list = []
        body = _mutated(_PAYLOADS[endpoint], path, value)
        opener = _opener(endpoint, body, merge_calls)

        code = run_merge(opener, platform=_platform(endpoint))

        assert code == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert HEAD_SHA in capsys.readouterr().err
        assert merge_calls == []

    @pytest.mark.parametrize("endpoint", list(_PAYLOADS))
    def test_the_unmutated_payload_is_readable(self, endpoint):
        merge_calls: list = []
        opener = _opener(endpoint, _PAYLOADS[endpoint], merge_calls)

        assert run_merge(opener, platform=_platform(endpoint)) == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_a_forgejo_entry_spelling_its_result_status_is_read(self):
        entry = {"context": "build / test", "status": "failure"}
        opener = forgejo_ci_opener(state="failure", statuses=[entry])
        assert run_merge(opener, platform="forgejo") == verb.EXIT_CI_STATUS_FAILED

    def test_a_forgejo_entry_with_a_non_string_status_spelling_refuses(self):
        entry = {"context": "build / test", "status": False}
        opener = forgejo_ci_opener(state="success", statuses=[entry])
        assert run_merge(opener, platform="forgejo") == verb.EXIT_GATE_FACT_UNAVAILABLE

    def test_a_malformed_entry_among_readable_ones_refuses(self):
        runs = [check_run("test", run_id=1, suite_id=10), None]
        merge_calls: list = []
        opener = github_ci_opener(check_runs=runs, merge_calls=merge_calls)

        assert run_merge(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert merge_calls == []

    def test_a_malformed_entry_on_a_later_page_refuses(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=10) for i in range(33)]
        runs.append(None)
        merge_calls: list = []
        opener = github_ci_opener(check_runs=runs, check_runs_page_size=30, merge_calls=merge_calls)

        assert run_merge(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert merge_calls == []

    def test_a_later_page_without_a_run_list_refuses(self):
        runs = [check_run(f"job-{i}", run_id=i + 1, suite_id=10) for i in range(40)]
        base = github_ci_opener(check_runs=runs)

        def opener(req, timeout=30):
            if "page=2" in req.full_url:
                return json_resp(200, {"total_count": 40})
            return base(req, timeout=timeout)

        assert run_merge(opener) == verb.EXIT_GATE_FACT_UNAVAILABLE


class TestNoDefaultsAnywhere:
    def test_ci_status_result_cannot_be_built_without_arguments(self):
        with pytest.raises(TypeError):
            CiStatusResult()  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "function",
        [
            github_backend.fetch_ci_status,
            github_backend._read_check_runs,
            forgejo_backend.fetch_ci_status,
        ],
        ids=lambda f: f"{f.__module__.rsplit('.', 1)[-1]}.{f.__name__}",
    )
    def test_backends_never_index_a_ci_payload(self, function):
        source = inspect.getsource(function)
        assert ".get(" not in source
        assert "body[" not in source
