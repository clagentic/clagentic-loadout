"""`loadout-review keep-evidence` through the CLI entry point, with a synthetic review
records root and evidence root."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.verdict_record.review_evidence import REVIEW_EVIDENCE_DIR_ENV_VAR
from clagentic_loadout.verdict_record.review_record import (
    REVIEW_RECORDS_DIR_ENV_VAR,
    build_review_record,
    review_records_root,
    write_review_record,
)

HEAD = "b" * 40
OTHER_HEAD = "c" * 40
REPO = "some-owner/some-repo"
DOT_REPO = "some-owner/.github"
REFUSED = 64
BINARY = bytes(range(256)) + b"\r\n\x00tail"


class Area:
    def __init__(self, root: Path, evidence: Path) -> None:
        self.root = root
        self.evidence = evidence

    def file(self, name: str, data: bytes = b"data\n", directory: str = "in") -> str:
        path = self.root / directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)

    def record(self, comment_id, *, role="reviewer", at, repo=REPO, pr=3, head=HEAD) -> None:
        record = build_review_record(
            comment_id=comment_id, repo=repo, pr=pr, head_sha=head, role=role,
            policy_base_sha=None, spec_hash=None, engines=[], evaluated=[],
            open_findings=[], advisory=[], dropped=[], chunks=None,
        )
        record["recorded_at"] = at
        write_review_record(review_records_root(), record)

    def keep(self, *files: str, capsys, repo=REPO, head=HEAD, role="reviewer", extra=(), config_root=None):
        argv = [
            "keep-evidence", "--repo", repo, "--pr", "3", "--head-sha", head, "--role", role,
            *extra, *[arg for path in files for arg in ("--file", path)],
        ]
        capsys.readouterr()
        code = review_cli.main(argv, config_root=config_root)
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err


@pytest.fixture
def area(tmp_path) -> Area:
    return Area(tmp_path, Path(os.environ[REVIEW_EVIDENCE_DIR_ENV_VAR]))


def test_an_explicit_comment_id_keeps_byte_identical_copies_and_reports_each(area, capsys):
    files = {"findings.json": b'{"a": 1}\n', "records.txt": BINARY, "scan.json": b""}
    paths = [area.file(name, data) for name, data in files.items()]

    code, result, _ = area.keep(*paths, capsys=capsys, extra=("--comment-id", "4242"))

    assert code == 0
    directory = area.evidence / HEAD / "4242"
    assert result["result"] == "kept" and result["directory"] == str(directory)
    assert result["comment_id"] == "4242" and result["comment_id_source"] == "argument"
    assert result["files"] == [
        {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in files.items()
    ]
    for name, data in files.items():
        assert (directory / name).read_bytes() == data
    assert sorted(p.name for p in directory.parent.iterdir()) == ["4242"]


RECORDS = [
    # (comment id, role, recorded_at, repo, pr, head)
    ("101", "reviewer", "2099-01-01T00:00:01Z", REPO, 3, HEAD),
    ("102", "reviewer", "2099-01-01T00:00:09Z", REPO, 3, HEAD),
    ("201", "security", "2099-01-01T00:00:05Z", REPO, 3, HEAD),
    ("301", "reviewer", "2099-01-01T00:00:30Z", REPO, 3, OTHER_HEAD),
    ("302", "reviewer", "2099-01-01T00:00:31Z", REPO, 4, HEAD),
    ("303", "reviewer", "2099-01-01T00:00:32Z", "some-owner/other", 3, HEAD),
]


@pytest.mark.parametrize(
    "role,expected",
    [("reviewer", "102"), ("security", "201"), ("merger", None)],
    ids=["newest-of-the-role", "other-role", "no-record-for-the-role"],
)
def test_without_a_comment_id_the_newest_matching_review_record_names_it(
    area, capsys, role, expected
):
    for comment_id, record_role, at, repo, pr, head in RECORDS:
        area.record(comment_id, role=record_role, at=at, repo=repo, pr=pr, head=head)
    path = area.file("findings.json")

    code, result, err = area.keep(path, capsys=capsys, role=role)

    if expected is None:
        assert code == REFUSED and result == {}
        for part in (str(review_records_root()), REPO, "pr 3", role, HEAD):
            assert part in err
        assert not area.evidence.exists()
        return
    assert code == 0
    assert result["comment_id"] == expected and result["comment_id_source"] == "review record"
    assert (area.evidence / HEAD / expected / "findings.json").is_file()


def test_a_file_that_is_not_a_record_is_not_a_match_and_does_not_stop_the_lookup(area, capsys):
    area.record("101", at="2099-01-01T00:00:01Z")
    (review_records_root() / HEAD / "999.json").write_text("{not json", encoding="utf-8")

    code, result, _ = area.keep(area.file("a.txt"), capsys=capsys)

    assert code == 0 and result["comment_id"] == "101"


@pytest.mark.parametrize(
    "case", ["missing-file", "unreadable-name", "duplicate-name", "directory"],
)
def test_an_unusable_file_is_refused_before_anything_is_written(area, capsys, case):
    good = area.file("good.txt")
    bad = {
        "missing-file": str(area.root / "in" / "none.txt"),
        "unreadable-name": "..",
        "duplicate-name": area.file("good.txt", b"other", directory="elsewhere"),
        "directory": str(area.root / "in"),
    }[case]

    code, result, err = area.keep(good, bad, capsys=capsys, extra=("--comment-id", "5"))

    assert code == REFUSED and result == {}
    assert (bad if case in ("missing-file", "directory") else "good.txt" if case == "duplicate-name" else "..") in err
    assert not area.evidence.exists()


def test_a_rerun_replaces_the_earlier_copy_as_a_whole(area, capsys):
    first = area.file("one.txt", b"first")
    code, _, _ = area.keep(first, area.file("two.txt"), capsys=capsys, extra=("--comment-id", "5"))
    assert code == 0

    code, _, _ = area.keep(area.file("one.txt", b"second", directory="again"), capsys=capsys, extra=("--comment-id", "5"))

    directory = area.evidence / HEAD / "5"
    assert code == 0 and [p.name for p in directory.iterdir()] == ["one.txt"]
    assert (directory / "one.txt").read_bytes() == b"second"
    assert [p.name for p in directory.parent.iterdir()] == ["5"]


def test_a_replacement_that_fails_leaves_the_earlier_copy_in_place(area, capsys, monkeypatch):
    area.keep(area.file("one.txt", b"first"), capsys=capsys, extra=("--comment-id", "5"))
    real_rename = os.rename

    def failing(src, dst, *args, **kwargs):
        if str(src).endswith(".new"):
            raise OSError(28, "No space left on device")
        return real_rename(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", failing)
    code, result, err = area.keep(
        area.file("one.txt", b"second", directory="again"), area.file("extra.txt"),
        capsys=capsys, extra=("--comment-id", "5"),
    )

    directory = area.evidence / HEAD / "5"
    assert code == REFUSED and result == {} and "No space left" in err
    assert [p.name for p in directory.iterdir()] == ["one.txt"]
    assert (directory / "one.txt").read_bytes() == b"first"
    assert [p.name for p in directory.parent.iterdir()] == ["5"]


@pytest.mark.parametrize(
    "env,setting,expected",
    [
        (True, True, "from-env"),
        (False, True, "from-setting"),
        (False, False, "default"),
    ],
    ids=["environment-first", "then-the-setting", "then-the-default"],
)
def test_the_evidence_root_follows_the_environment_then_the_setting_then_the_default(
    area, capsys, monkeypatch, tmp_path, env, setting, expected
):
    config = tmp_path / "config"
    config.mkdir()
    if setting:
        (config / "config.yaml").write_text(
            f"review_evidence:\n  dir: {tmp_path / 'from-setting'}\n", encoding="utf-8"
        )
    if env:
        monkeypatch.setenv(REVIEW_EVIDENCE_DIR_ENV_VAR, str(tmp_path / "from-env"))
    else:
        monkeypatch.delenv(REVIEW_EVIDENCE_DIR_ENV_VAR)
    root = {
        "from-env": tmp_path / "from-env", "from-setting": tmp_path / "from-setting",
        "default": config / "state" / "review-evidence",
    }[expected]

    code, result, _ = area.keep(
        area.file("a.txt"), capsys=capsys, extra=("--comment-id", "9"), config_root=config
    )

    assert code == 0 and result["directory"] == str(root / HEAD / "9")


def test_a_repository_whose_name_starts_with_a_dot_is_accepted(area, capsys):
    area.record("77", at="2099-01-01T00:00:01Z", repo=DOT_REPO)

    code, result, _ = area.keep(area.file("a.txt"), capsys=capsys, repo=DOT_REPO)

    assert code == 0 and result["comment_id"] == "77" and result["comment_id_source"] == "review record"


@pytest.mark.parametrize(
    "repo", ["some-owner/.", "some-owner/..", "./some-repo", "../x", "some-repo", "a/b/c", "a/", "a/b c"]
)
def test_a_repository_that_is_not_owner_slash_name_is_refused(area, capsys, repo):
    code, _, err = area.keep(area.file("a.txt"), capsys=capsys, repo=repo, extra=("--comment-id", "1"))

    assert code == REFUSED and "--repo" in err and not area.evidence.exists()


@pytest.mark.parametrize(
    "extra,head",
    [(("--comment-id", "12x"), HEAD), (("--comment-id", "../x"), HEAD), (("--comment-id", "1"), "abc")],
    ids=["non-digit-id", "path-id", "short-head"],
)
def test_a_comment_id_or_head_that_could_reach_a_path_is_refused(area, capsys, extra, head):
    code, _, err = area.keep(area.file("a.txt"), capsys=capsys, extra=extra, head=head)

    assert code == REFUSED and not area.evidence.exists()
