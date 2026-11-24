"""`loadout-review stage`, `teardown` and `check-drops` through the CLI entry point,
on both platforms, against a synthetic host serving one pull request with modified,
added, deleted, binary, unreadable and path-escaping files."""

from __future__ import annotations

import hashlib
import json
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import github_app_config, provider_config
from tests._review_cli_support import Env, identity_provider, make_diff
from tests._support.review_host import ReviewHost

MODIFIED = "src/mod.py"
ADDED = "docs/new.md"
DELETED = "old.txt"
BINARY = "img.bin"
UNREADABLE = "broken.py"
ESCAPES = ("../escape.txt", "/abs.txt")
HEAD_BYTES = {MODIFIED: b"print('head')\n", ADDED: b"# new\n"}
REPO = "some-owner/some-repo"
DOT_REPO = "some-owner/.github"
REFUSED = 64


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path

    def files(self) -> list:
        gone = "removed" if self.host.platform == "github" else "deleted"
        return [
            MODIFIED,
            {"filename": ADDED, "status": "added"},
            {"filename": DELETED, "status": gone},
            BINARY,
            UNREADABLE,
            *ESCAPES,
        ]

    def call(self, command: str, *argv: str, capsys, repo: str = REPO, with_host: bool = True):
        base = [command]
        if with_host:
            base += ["--caller", "reviewer", "--repo", repo, *self.host.platform_args()]
        capsys.readouterr()
        code = review_cli.main(
            [*base, *argv],
            token_provider=self.env.token_provider,
            opener=self.host.opener,
            identity_provider=identity_provider("reviewer"),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def stage(self, out: Path, *, capsys, repo: str = REPO):
        return self.call("stage", "--out", str(out), capsys=capsys, repo=repo)

    def write(self, name: str, document) -> str:
        path = self.root / name
        path.write_text(
            document if isinstance(document, str) else json.dumps(document), encoding="utf-8"
        )
        return str(path)


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    host = ReviewHost(
        request.param, diff=make_diff({MODIFIED: 1}), base_sha=env.base_sha, head_files={}
    )
    world = World(env, host, tmp_path)
    host.files = world.files()
    host.pull["changed_files"] = len(host.files)
    host.head_files = {**HEAD_BYTES, BINARY: b"\x89PNG\x00\x01", UNREADABLE: 500}
    return world


def _manifest(payload: dict) -> dict:
    return json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))


def _paths(entries: list[dict]) -> dict[str, str]:
    return {entry["path"]: entry.get("sha256", entry.get("reason")) for entry in entries}


def test_stage_writes_head_copies_and_a_manifest_naming_every_changed_file(world, tmp_path, capsys):
    out = tmp_path / "stage"
    code, payload, _ = world.stage(out, capsys=capsys)

    assert code == 0
    assert payload["head_sha"] == world.host.head_sha
    manifest = _manifest(payload)
    assert Path(payload["manifest"]).parent == out.resolve()
    assert manifest["schema"] == "loadout-review-stage/1"
    assert (manifest["repo"], manifest["pr"], manifest["platform"]) == (
        REPO, world.host.number, world.host.platform,
    )
    assert manifest["head_sha"] == world.host.head_sha
    assert manifest["base_sha"]
    # Each changed file that exists at the head is written at its repository path
    # with the head bytes, and the recorded sha256 is that of the written copy.
    for path, data in HEAD_BYTES.items():
        written = (out / path).read_bytes()
        assert written == data
        assert _paths(manifest["files"])[path] == hashlib.sha256(written).hexdigest()
        assert (out / path).stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH) == 0
    assert set(_paths(manifest["files"])) == set(HEAD_BYTES)
    assert _paths(manifest["deleted"]).keys() == {DELETED}
    assert not (out / DELETED).exists()
    assert "staging_gap" not in manifest and "staging_gap" not in payload


def test_stage_pins_the_head_the_host_reports_at_the_time_of_the_call(world, tmp_path, capsys):
    first = world.stage(tmp_path / "one", capsys=capsys)[1]["head_sha"]
    moved = "9" * 40
    world.host.pull["head"]["sha"] = moved
    world.host.head_sha = moved
    world.host.head_files = {path: data for path, data in world.host.head_files.items()}

    code, payload, _ = world.stage(tmp_path / "two", capsys=capsys)

    assert first != moved and code == 0 and payload["head_sha"] == moved
    assert _manifest(payload)["head_sha"] == moved


def test_stage_lists_what_it_cannot_copy_under_missing_and_writes_nothing_outside(
    world, tmp_path, capsys
):
    out = tmp_path / "area" / "stage"
    code, payload, _ = world.stage(out, capsys=capsys)

    assert code == 0
    missing = _paths(_manifest(payload)["missing"])
    assert set(missing) == {BINARY, UNREADABLE, *ESCAPES}
    assert "binary" in missing[BINARY]
    assert "unreadable" in missing[UNREADABLE]
    assert all("outside the output directory" in missing[path] for path in ESCAPES)
    # Every other file is staged, and nothing is written outside the directory.
    assert {entry["path"] for entry in _manifest(payload)["files"]} == set(HEAD_BYTES)
    assert not (tmp_path / "area" / "escape.txt").exists()
    assert not (tmp_path / "escape.txt").exists()
    assert {p.name for p in (tmp_path / "area").iterdir()} == {"stage"}


@pytest.mark.parametrize(
    "files,count,reason",
    [
        ([MODIFIED], 3, "the host reports 3 changed file(s) but the stage accounts for 1"),
        ([], 0, "no file copy was staged"),
    ],
    ids=["host-count-differs", "nothing-staged"],
)
def test_a_staging_gap_is_reported_and_the_exit_code_stays_zero(
    world, tmp_path, capsys, files, count, reason
):
    world.host.files = files
    world.host.pull["changed_files"] = count

    code, payload, _ = world.stage(tmp_path / "stage", capsys=capsys)

    assert code == 0
    gap = payload["staging_gap"]
    assert reason in gap["reason"]
    assert gap["host"] == count and gap["staged"] == len(files)
    assert _manifest(payload)["staging_gap"] == gap


@pytest.mark.parametrize("kind", ["non-empty-dir", "file"])
def test_stage_never_replaces_an_existing_path(world, tmp_path, capsys, kind):
    out = tmp_path / "taken"
    if kind == "file":
        out.write_text("keep", encoding="utf-8")
    else:
        out.mkdir()
        (out / "keep.txt").write_text("keep", encoding="utf-8")
    before = world.host.requests[:]

    code, payload, err = world.stage(out, capsys=capsys)

    assert code == REFUSED and payload == {}
    assert str(out.resolve()) in err
    assert world.host.requests == before
    assert out.read_text(encoding="utf-8") == "keep" if kind == "file" else (
        [p.name for p in out.iterdir()] == ["keep.txt"]
    )


def test_stage_accepts_an_existing_empty_directory(world, tmp_path, capsys):
    out = tmp_path / "empty"
    out.mkdir()

    code, payload, _ = world.stage(out, capsys=capsys)

    assert code == 0 and (out / MODIFIED).is_file()


def test_teardown_removes_the_stage_and_a_second_call_reports_it_gone(world, tmp_path, capsys):
    out = tmp_path / "stage"
    manifest = world.stage(out, capsys=capsys)[1]["manifest"]

    code, first, _ = world.call("teardown", "--manifest", manifest, capsys=capsys, with_host=False)
    assert code == 0 and first["already_gone"] is False and first["removed_directory"] is True
    assert not out.exists()

    code, second, _ = world.call("teardown", "--manifest", manifest, capsys=capsys, with_host=False)
    assert code == 0 and second["already_gone"] is True
    assert second["directory"] == str(out.resolve())


@pytest.mark.parametrize("kind", ["unlisted-file", "symlinked-parent"])
def test_teardown_leaves_what_it_must_not_touch(world, tmp_path, capsys, kind):
    out = tmp_path / "stage"
    manifest = world.stage(out, capsys=capsys)[1]["manifest"]
    if kind == "unlisted-file":
        (out / "mine.txt").write_text("keep", encoding="utf-8")
        left, survivor = ["mine.txt"], out / "mine.txt"
    else:
        outside = tmp_path / "outside"
        outside.mkdir()
        survivor = outside / "mod.py"
        survivor.write_text("keep", encoding="utf-8")
        (out / "src" / "mod.py").chmod(0o600)
        (out / "src" / "mod.py").unlink()
        (out / "src").rmdir()
        (out / "src").symlink_to(outside, target_is_directory=True)
        left = [MODIFIED]

    code, payload, err = world.call("teardown", "--manifest", manifest, capsys=capsys, with_host=False)

    assert code == 0 and payload["remaining"] == left
    assert survivor.read_text(encoding="utf-8") == "keep"
    if kind == "unlisted-file":
        assert not (out / MODIFIED).exists()


def test_teardown_refuses_what_is_not_the_manifest_of_its_own_directory(world, tmp_path, capsys):
    out = tmp_path / "stage"
    manifest = Path(world.stage(out, capsys=capsys)[1]["manifest"])
    moved = tmp_path / "elsewhere"
    moved.mkdir()
    copied = moved / manifest.name
    copied.write_bytes(manifest.read_bytes())
    not_a_manifest = world.write("other.json", {"schema": "something-else", "dir": str(out)})

    for target, reason in ((copied, "not the directory that holds it"), (Path(not_a_manifest), "not a stage manifest")):
        code, _, err = world.call("teardown", "--manifest", str(target), capsys=capsys, with_host=False)
        assert code == REFUSED
        assert str(target.resolve()) in err and reason in err
    assert (out / MODIFIED).is_file() and manifest.is_file() and copied.is_file()


def _drop(path, *, line=3, **extra):
    return {"file": path, "line": line, "rule_id": "R1", "message": "m", "reason": "r", **extra}


@pytest.mark.parametrize(
    "entry,proven,reason",
    [
        (lambda head: _drop(MODIFIED, read_at=head), True, None),
        (lambda head: _drop(DELETED, read_at=head), True, None),
        (lambda head: _drop(MODIFIED, read_at="c" * 40), False, "read at another revision (" + "c" * 40 + ")"),
        (lambda head: _drop(MODIFIED), False, "no read_at"),
        (lambda head: _drop(BINARY, read_at=head), False, "no head copy"),
        (lambda head: _drop("never/staged.py", read_at=head), False, "no head copy"),
    ],
    ids=["copy", "deleted", "other-revision", "no-read-at", "missing-file", "unstaged-file"],
)
def test_check_drops_marks_each_drop_proven_or_names_the_reason(
    world, tmp_path, capsys, entry, proven, reason
):
    payload = world.stage(tmp_path / "stage", capsys=capsys)[1]
    dropped = world.write("dropped.json", [entry(payload["head_sha"])])

    code, report, err = world.call(
        "check-drops", "--manifest", payload["manifest"], "--dropped", dropped,
        capsys=capsys, with_host=False,
    )

    assert code == 0
    (item,) = report["drops"]
    assert item["position"] == 1 and item["id"].endswith(":3:R1")
    assert item["proven"] is proven and item.get("reason") == reason
    assert (item["id"] in err) is (not proven)


def test_check_drops_lists_the_gaps_of_the_stage_and_of_the_drops_and_findings(
    world, tmp_path, capsys
):
    payload = world.stage(tmp_path / "stage", capsys=capsys)[1]
    dropped = world.write("dropped.json", {"dropped": [_drop("never/staged.py", read_at="1" * 40)]})
    findings = world.write(
        "findings.json", {"findings": [{"file": "other/missing.py"}, {"file": MODIFIED}]}
    )

    code, report, _ = world.call(
        "check-drops", "--manifest", payload["manifest"], "--dropped", dropped,
        "--findings", findings, capsys=capsys, with_host=False,
    )

    assert code == 0
    gaps = {gap["path"] for gap in report["gaps"]}
    assert gaps == {BINARY, UNREADABLE, *ESCAPES, "never/staged.py", "other/missing.py"}
    assert report["unproven_count"] == 1


@pytest.mark.parametrize(
    "case",
    ["manifest-missing", "manifest-not-json", "dropped-missing", "dropped-malformed", "findings-malformed"],
)
def test_check_drops_refuses_an_unreadable_or_malformed_input_naming_the_path(
    world, tmp_path, capsys, case
):
    payload = world.stage(tmp_path / "stage", capsys=capsys)[1]
    manifest = payload["manifest"]
    dropped = world.write("dropped.json", [_drop(MODIFIED)])
    findings = None
    if case == "manifest-missing":
        manifest = str(tmp_path / "no-such.json")
    elif case == "manifest-not-json":
        manifest = world.write("bad.json", "{not json")
    elif case == "dropped-missing":
        dropped = str(tmp_path / "no-such-dropped.json")
    elif case == "dropped-malformed":
        dropped = world.write("dropped.json", [{"file": MODIFIED, "rule_id": "R1"}])
    else:
        findings = world.write("findings.json", {"findings": "nope"})
    argv = ["--manifest", manifest, "--dropped", dropped] + (["--findings", findings] if findings else [])

    code, report, err = world.call("check-drops", *argv, capsys=capsys, with_host=False)

    assert code == REFUSED and report == {}
    culprit = {
        "manifest-missing": manifest, "manifest-not-json": manifest, "dropped-missing": dropped,
        "dropped-malformed": dropped, "findings-malformed": findings,
    }[case]
    assert str(Path(culprit).resolve()) in err


def test_a_repository_whose_name_starts_with_a_dot_stages_tears_down_and_checks_drops(
    world, tmp_path, capsys
):
    out = tmp_path / "stage"
    code, payload, _ = world.stage(out, capsys=capsys, repo=DOT_REPO)
    assert code == 0 and _manifest(payload)["repo"] == DOT_REPO
    dropped = world.write("dropped.json", [_drop(MODIFIED, read_at=payload["head_sha"])])

    code, report, _ = world.call(
        "check-drops", "--manifest", payload["manifest"], "--dropped", dropped,
        capsys=capsys, with_host=False,
    )
    assert code == 0 and report["drops"][0]["proven"] is True
    code, result, _ = world.call(
        "teardown", "--manifest", payload["manifest"], capsys=capsys, with_host=False
    )
    assert code == 0 and result["removed_directory"] is True and not out.exists()


@pytest.mark.parametrize("subcommand", ["stage", "teardown", "check-drops"])
@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_the_new_subcommands_answer_help_and_version(subcommand, flag, capsys):
    code = review_cli.main([subcommand, flag])

    assert code == 0
    assert capsys.readouterr().out.strip()
