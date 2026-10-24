"""A profile is read once per command: a revision that moves while a command runs
cannot make one document describe two commits."""

from __future__ import annotations

import datetime as dt
import json

import pytest
import yaml

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError
from clagentic_loadout.review.policy_source import PROFILE_OK, PROFILE_PATH
from clagentic_loadout.review.profile_brief import (
    ProfileReadError,
    completeness_of,
    read_snapshot,
)
from clagentic_loadout.spec import verb as spec_verb
from tests._review_evaluate_support import commit_files, make_repo, run_git

NOW = dt.datetime(2026, 10, 24, 12, 0, tzinfo=dt.timezone.utc)
MOVING = "moving"
COMPLETE = {
    "class": "released-tool",
    "exposure": "internal_authenticated",
    "data": "internal",
    "visibility": "private",
    "merge_control": "review_required",
    "confirmed_at": "2026-10-24",
}


@pytest.fixture
def moving_repo(tmp_path):
    """A repository whose branch ``moving`` is at a commit with a complete profile
    (``first``), and a later commit on main with no profile at all (``second``)."""
    repo = make_repo(tmp_path)
    first = commit_files(repo, {PROFILE_PATH: yaml.safe_dump(COMPLETE)})
    run_git(repo, "branch", MOVING)
    run_git(repo, "rm", "-q", "--", PROFILE_PATH)
    run_git(repo, "commit", "-q", "-m", "chore: drop the profile")
    second = run_git(repo, "rev-parse", "HEAD")
    return repo, first, second


@pytest.fixture
def advance_after_first_read(monkeypatch, moving_repo):
    """Move ``moving`` to the second commit right after the first tree listing,
    which is the point a later read of the name would see a different commit."""
    repo, _first, second = moving_repo
    original = GitPolicyReader.list_paths
    moved = []

    def list_then_move(self, base):
        paths = original(self, base)
        if not moved:
            moved.append(True)
            run_git(repo, "update-ref", f"refs/heads/{MOVING}", second)
        return paths

    monkeypatch.setattr(GitPolicyReader, "list_paths", list_then_move)
    return moved


def test_resolve_commit_names_one_full_commit(moving_repo):
    repo, first, _second = moving_repo
    assert GitPolicyReader(repo).resolve_commit(MOVING) == first
    with pytest.raises(PolicyReadError):
        GitPolicyReader(repo).resolve_commit("no-such-ref")


def test_a_snapshot_reads_the_commit_the_ref_named_at_the_start(
    moving_repo, advance_after_first_read
):
    repo, first, _second = moving_repo
    snapshot = read_snapshot(repo, MOVING)
    assert advance_after_first_read
    assert snapshot.commit == first
    assert snapshot.state == PROFILE_OK and snapshot.profile is not None
    assert completeness_of(snapshot, now=NOW) == ()


def test_an_unresolvable_ref_is_a_read_error_naming_it(moving_repo):
    repo, _first, _second = moving_repo
    with pytest.raises(ProfileReadError, match="no-such-ref"):
        read_snapshot(repo, "no-such-ref")


def test_spec_new_takes_the_brief_and_the_completeness_from_one_commit(
    moving_repo, advance_after_first_read, capsys
):
    repo, _first, _second = moving_repo
    capsys.readouterr()
    code = spec_verb.main(
        ["new", "--role", "some-role", "--repo-path", str(repo), "--ref", MOVING], now=NOW
    )
    out = capsys.readouterr().out
    assert code == 0
    draft = json.loads(out)
    assert advance_after_first_read
    # The brief is the first commit's, so the completeness must be too: complete.
    assert draft["class_rules"]["class"] == "released-tool"
    assert draft["profile_completeness"]["items"] == []


def test_profile_check_reads_the_listing_and_the_file_at_one_commit(
    moving_repo, advance_after_first_read, capsys
):
    repo, _first, _second = moving_repo
    capsys.readouterr()
    code = pv.main(["check", "--repo-path", str(repo), "--ref", MOVING], now=NOW)
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip().splitlines()[-1])
    assert advance_after_first_read
    assert code == pv.EXIT_OK and result["result"] == "valid"
    # The report still names the revision the caller asked for.
    assert result["at"] == MOVING
    assert result["completeness"] == []


def test_profile_brief_reads_the_listing_and_the_file_at_one_commit(
    moving_repo, advance_after_first_read, capsys
):
    repo, _first, _second = moving_repo
    capsys.readouterr()
    code = pv.main(
        ["brief", "--role", "builder", "--repo-path", str(repo), "--ref", MOVING], now=NOW
    )
    captured = capsys.readouterr()
    assert advance_after_first_read
    assert code == pv.EXIT_OK, captured.err
    assert json.loads(captured.out.strip().splitlines()[-1])["at"] == MOVING
