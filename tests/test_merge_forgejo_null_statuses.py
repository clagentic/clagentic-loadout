"""test_merge_forgejo_null_statuses.py — Forgejo encodes a commit with no commit
statuses as `statuses: null`. The strict parser reads that null as an empty set
only when `total_count` is the int 0 and `state` is the empty string; every other
null-statuses shape stays unreadable and the merge is refused."""

from __future__ import annotations

import copy

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.ci_payload import (
    parse_forgejo_combined_status,
    parse_github_combined_status,
)
from clagentic_loadout.merge.errors import GateFactUnavailableError
from tests._support.ci_gate import (
    HEAD_SHA,
    forgejo_ci_opener,
    github_ci_opener,
    run_merge,
    status_entry,
)
from tests._support.fakes import json_resp

#: Verbatim response of `GET /commits/{sha}/status` on a Forgejo repository
#: without a CI runner, captured from the live host for the head 1c48c3db of a
#: reviewed PR in a runner-less repository on 2026-10-13.
CAPTURED_NO_CI_RESPONSE = {
    "state": "",
    "sha": "",
    "total_count": 0,
    "statuses": None,
    "repository": None,
    "commit_url": "",
    "url": "",
}

_WHERE = "owner/repo"


def _parse(body):
    return parse_forgejo_combined_status(body, where=_WHERE, head_sha=HEAD_SHA)


def _with(**changes):
    body = copy.deepcopy(CAPTURED_NO_CI_RESPONSE)
    for key, value in changes.items():
        if value is _DROP:
            del body[key]
        else:
            body[key] = value
    return body


_DROP = object()


def _serving(body, merge_calls):
    base = forgejo_ci_opener(merge_calls=merge_calls)

    def opener(req, timeout=15):
        if req.get_method() == "GET" and req.full_url.split("?")[0].endswith("/status"):
            return json_resp(200, body)
        return base(req, timeout=timeout)

    return opener


class TestEstablishedEmpty:
    def test_the_captured_response_parses_as_an_empty_status_set(self):
        parsed = _parse(CAPTURED_NO_CI_RESPONSE)
        assert parsed.state == ""
        assert parsed.statuses == ()

    def test_the_captured_response_passes_the_gate_as_no_runner_by_design(self):
        merge_calls: list = []
        opener = _serving(CAPTURED_NO_CI_RESPONSE, merge_calls)

        assert run_merge(opener, platform="forgejo") == verb.EXIT_OK
        assert len(merge_calls) == 1


class TestNullStatusesStaysUnreadable:
    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_with(total_count=_DROP), id="total_count-missing"),
            pytest.param(_with(total_count=2), id="total_count-nonzero"),
            pytest.param(_with(total_count=True), id="total_count-bool"),
            pytest.param(_with(total_count=False), id="total_count-false"),
            pytest.param(_with(total_count="0"), id="total_count-string"),
            pytest.param(_with(total_count=None), id="total_count-null"),
            pytest.param(_with(total_count=0.0), id="total_count-float"),
            pytest.param(_with(state="pending"), id="state-pending"),
            pytest.param(_with(state="success"), id="state-success"),
        ],
    )
    def test_parser_refuses_naming_the_head(self, body):
        with pytest.raises(GateFactUnavailableError, match=HEAD_SHA):
            _parse(body)

    @pytest.mark.parametrize(
        "body",
        [
            pytest.param(_with(total_count=_DROP), id="total_count-missing"),
            pytest.param(_with(total_count=2), id="total_count-nonzero"),
            pytest.param(_with(total_count=True), id="total_count-bool"),
            pytest.param(_with(state="pending"), id="state-pending"),
        ],
    )
    def test_merge_is_refused_and_nothing_merges(self, body):
        merge_calls: list = []
        opener = _serving(body, merge_calls)

        assert run_merge(opener, platform="forgejo") == verb.EXIT_GATE_FACT_UNAVAILABLE
        assert merge_calls == []


#: Response of `GET /commits/{sha}/status` on GitHub for a commit with no commit
#: statuses, captured from the live host for clagentic/clagentic-loadout main
#: 52bbc457c5e45d447a93ddb01f8b10130c58526c on 2026-10-13. GitHub returns an
#: empty list (not null) and a rollup state of "pending". The `repository`
#: object is cut to its identifying fields; every other key is verbatim.
CAPTURED_GITHUB_NO_STATUS_RESPONSE = {
    "state": "pending",
    "statuses": [],
    "sha": "52bbc457c5e45d447a93ddb01f8b10130c58526c",
    "total_count": 0,
    "repository": {
        "id": 1318404012,
        "name": "clagentic-loadout",
        "full_name": "clagentic/clagentic-loadout",
        "private": False,
        "fork": False,
    },
    "commit_url": (
        "https://api.github.com/repos/clagentic/clagentic-loadout/commits/"
        "52bbc457c5e45d447a93ddb01f8b10130c58526c"
    ),
    "url": (
        "https://api.github.com/repos/clagentic/clagentic-loadout/commits/"
        "52bbc457c5e45d447a93ddb01f8b10130c58526c/status"
    ),
}


class TestGithubNoStatusUnchanged:
    def test_the_captured_response_parses_as_an_empty_status_set(self):
        parsed = parse_github_combined_status(
            CAPTURED_GITHUB_NO_STATUS_RESPONSE, where=_WHERE, head_sha=HEAD_SHA
        )
        assert parsed.statuses == ()
        assert parsed.state == "pending"

    def test_the_captured_response_passes_the_gate_as_no_runner_by_design(self):
        merge_calls: list = []
        base = github_ci_opener(merge_calls=merge_calls)

        def opener(req, timeout=30):
            if req.get_method() == "GET" and req.full_url.split("?")[0].endswith("/status"):
                return json_resp(200, CAPTURED_GITHUB_NO_STATUS_RESPONSE)
            return base(req, timeout=timeout)

        assert run_merge(opener, platform="github") == verb.EXIT_OK
        assert len(merge_calls) == 1

    def test_github_null_statuses_is_still_unreadable(self):
        body = {**CAPTURED_GITHUB_NO_STATUS_RESPONSE, "state": "", "statuses": None}
        with pytest.raises(GateFactUnavailableError, match=HEAD_SHA):
            parse_github_combined_status(body, where=_WHERE, head_sha=HEAD_SHA)


class TestPresentStatusSetKeepsEveryRule:
    def test_a_failing_status_still_refuses(self):
        opener = forgejo_ci_opener(state="failure", statuses=[status_entry("ci", "failure")])
        assert run_merge(opener, platform="forgejo") == verb.EXIT_CI_STATUS_FAILED

    def test_an_empty_list_is_still_an_empty_set(self):
        assert _parse(_with(statuses=[])).statuses == ()

    def test_a_non_list_non_null_statuses_is_unreadable(self):
        with pytest.raises(GateFactUnavailableError, match=HEAD_SHA):
            _parse(_with(statuses={}))
