"""At push time the consumers of a breaking interface change are printed from the
consumer map. Reporting only: nothing here can fail a push."""

from __future__ import annotations

import io

import pytest

from clagentic_loadout.interface.consumers import CONSUMERS_FILE_ENV_VAR
from clagentic_loadout.push import interface_report
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.transport import provider_config
from tests._gate_repo import git
from tests._support.interface_fixtures import (
    ADDED_INTERFACE,
    BASE_INTERFACE,
    RENAMED_INTERFACE,
    interface_repo,
    write_consumers,
)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.delenv(CONSUMERS_FILE_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")


def _repo(tmp_path, head):
    repo, base, _head = interface_repo(tmp_path, BASE_INTERFACE, head)
    git(repo, "remote", "add", "origin", "https://git-host.example.test/some-owner/some-repo.git")
    return repo, base


def test_a_breaking_change_prints_its_consumers_from_the_map(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    lines = interface_report.notice_lines(repo, [base])

    assert lines[0] == "push: BREAKING INTERFACE CHANGE (verb tool-one); consumers per the consumer map:"
    assert "push:   app-one consumes tool-one (confirmed 2026-10-01)" in lines
    assert "push:   app-two consumes tool-one (confirmed 2026-09-01)" in lines


def test_the_first_base_ref_that_resolves_is_used(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    assert interface_report.notice_lines(repo, ["no-such-ref", base])


@pytest.mark.parametrize("head", [ADDED_INTERFACE, BASE_INTERFACE], ids=["additions", "unchanged"])
def test_a_change_that_does_not_break_prints_nothing(tmp_path, monkeypatch, head):
    repo, base = _repo(tmp_path, head)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    assert interface_report.notice_lines(repo, [base]) == []


def test_an_unavailable_map_is_one_line_saying_so(tmp_path):
    repo, base = _repo(tmp_path, RENAMED_INTERFACE)

    lines = interface_report.notice_lines(repo, [base])

    assert len(lines) == 1 and "consumer map is unavailable" in lines[0]


def test_a_base_that_does_not_resolve_prints_nothing(tmp_path, monkeypatch):
    repo, _base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    assert interface_report.notice_lines(repo, ["no-such-ref"]) == []


def test_an_invalid_interface_file_is_named_not_raised(tmp_path):
    repo, base = _repo(tmp_path, "schema_version: 9\n")

    lines = interface_report.notice_lines(repo, [base])

    assert len(lines) == 1 and "interface diff not computed" in lines[0]


def test_print_notice_writes_each_line_to_the_stream(tmp_path, monkeypatch):
    repo, base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))
    stream = io.StringIO()

    interface_report.print_notice(repo, [base], stream)

    assert stream.getvalue().count("\n") == 4


def test_the_push_verification_step_prints_the_notice_and_its_outcome_is_unchanged(
    tmp_path, monkeypatch, capsys
):
    repo, base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    body, outcome, tested = push_verb._run_verification_outcome(
        repo, body="body", skip=False, base_refs=[base]
    )

    assert "push: BREAKING INTERFACE CHANGE" in capsys.readouterr().err
    assert (body, outcome, tested) == ("body", push_verb.VERIFICATION_NONE, None)


def test_the_push_verification_step_without_base_refs_prints_no_notice(tmp_path, monkeypatch, capsys):
    repo, _base = _repo(tmp_path, RENAMED_INTERFACE)
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(write_consumers(tmp_path)))

    push_verb._run_verification_outcome(repo, body="body", skip=False)

    assert "BREAKING" not in capsys.readouterr().err
