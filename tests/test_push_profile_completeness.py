"""loadout-push reports what the repo profile is missing while it runs its
verification step, and the verdict records it leaves point at the same items.
Reporting only: a push is never refused or changed by an incomplete profile."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.push import profile_report, verb
from clagentic_loadout.push.remote_readback import RemoteReadback
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.profile_posture import CompletenessItem
from clagentic_loadout.verdict_record.store import records_at_head, records_root
from tests._review_evaluate_support import posture_profile
from tests._support.push_verb import (  # noqa: F401, F811  (fixtures reused, not redefined)
    RecordingTokenProvider as _RecordingTokenProvider,
    forgejo_create_opener as _forgejo_create_opener,
    isolate_user_config_root,
    repo_with_remote,
    run_main as _run_main,
)

PY = sys.executable
INCOMPLETE_LINE = "push: incomplete repo profile: "


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _write_verify(repo) -> None:
    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    entries = [{"name": "unit", "argv": [PY, "-c", "pass"], "timeout_seconds": 30}]
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"push": {"verify": entries}}), encoding="utf-8"
    )
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".clagentic/loadout/config.yaml\n", encoding="utf-8")


def _commit_profile(repo, text: str) -> None:
    target = repo / PROFILE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    _git(repo, "add", "--", PROFILE_PATH)
    _git(repo, "commit", "-q", "-m", "chore: profile")


@pytest.fixture(autouse=True)
def _readback_from_the_bare_remote(repo_with_remote, monkeypatch):
    _repo, remote = repo_with_remote

    def read(remote_name, branch, project_root, token=None):
        listed = subprocess.run(
            ["git", "ls-remote", str(remote), f"refs/heads/{branch}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        return RemoteReadback(remote_head_sha=listed[0], remote=remote_name, ref=f"refs/heads/{branch}")

    monkeypatch.setattr(verb, "read_remote_head", read)


def _create(repo, monkeypatch):
    return _run_main(
        [
            "--repo-path", str(repo), "--platform", "forgejo",
            "--title", "feat: t", "--body-stdin",
        ],
        token_provider=_RecordingTokenProvider(),
        opener=_forgejo_create_opener(),
        stdin_text=json.dumps({"body": "some body"}),
        monkeypatch=monkeypatch,
    )


class TestCreatePath:
    def test_an_incomplete_profile_is_printed_and_the_records_point_at_the_same_items(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        _commit_profile(repo, "class: internal\n")
        _write_verify(repo)

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        err = capsys.readouterr().err
        printed = [line for line in err.splitlines() if line.startswith(INCOMPLETE_LINE)]
        assert [line[len(INCOMPLETE_LINE):].split(":")[0] for line in printed] == [
            "exposure",
            "data",
            "visibility",
            "merge_control",
            "confirmed_at",
        ]
        pushed = _git(remote, "rev-parse", "refs/heads/feature")
        (record,) = records_at_head(records_root(), pushed).records
        assert record.evidence_ref == (
            "profile-completeness:exposure,data,visibility,merge_control,confirmed_at"
        )

    def test_the_profile_is_read_once_and_the_records_carry_the_items_that_were_printed(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        _commit_profile(repo, "class: internal\n")
        _write_verify(repo)
        reads = []
        real = profile_report.read_items

        def read_once_then_report_complete(project_root):
            reads.append(project_root)
            return real(project_root) if len(reads) == 1 else ()

        monkeypatch.setattr(profile_report, "read_items", read_once_then_report_complete)

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert len(reads) == 1
        assert INCOMPLETE_LINE in capsys.readouterr().err
        pushed = _git(remote, "rev-parse", "refs/heads/feature")
        (record,) = records_at_head(records_root(), pushed).records
        assert record.evidence_ref == (
            "profile-completeness:exposure,data,visibility,merge_control,confirmed_at"
        )

    def test_a_complete_profile_prints_nothing_and_leaves_no_evidence(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        _commit_profile(repo, posture_profile(visibility="public"))
        _write_verify(repo)

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert INCOMPLETE_LINE not in capsys.readouterr().err
        pushed = _git(remote, "rev-parse", "refs/heads/feature")
        (record,) = records_at_head(records_root(), pushed).records
        assert record.evidence_ref is None

    def test_a_missing_profile_is_reported_and_the_push_still_succeeds(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_verify(repo)

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert f"{INCOMPLETE_LINE}profile: no profile at {PROFILE_PATH}" in capsys.readouterr().err

    def test_the_report_is_made_with_no_verification_declared_too(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _commit_profile(repo, "class: internal\n")

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert INCOMPLETE_LINE in capsys.readouterr().err


class TestReportHelpers:
    def test_the_evidence_names_the_keys_in_report_order_and_is_none_when_complete(self):
        items = (CompletenessItem("a", "r", "f"), CompletenessItem("b", "r", "f"))
        assert profile_report.evidence_ref(items) == "profile-completeness:a,b"
        assert profile_report.evidence_ref(()) is None

    def test_a_directory_with_no_profile_reports_the_missing_file_first(self, tmp_path):
        items = profile_report.read_items(tmp_path / "absent")
        assert items[0].key == "profile" and len(items) == 6
