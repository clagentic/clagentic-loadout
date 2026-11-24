"""`loadout-profile brief --spec` and `--out`: the sealed spec carried in the brief.

Every row goes through the real `main()` of both verbs: the spec is sealed by
`loadout-spec seal` into the per-test specs root and read back by the brief."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.spec import verb as spec_verb
from tests._review_evaluate_support import commit_files, run_git
from tests._support.roles_groups import profile_repo, write_groups

ROLES = ["builder", "planner", "reviewer", "troubleshooter", "invented-undeclared-role"]


def _brief(capsys, *argv):
    capsys.readouterr()
    code = pv.main(["brief", *argv])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines else None), captured.err


def _seal_spec(tmp_path: Path, repo: Path, capsys, *, head: str | None = None) -> str:
    """Seal a small spec for *repo* and return its hash."""
    capsys.readouterr()
    assert spec_verb.main(["new", "--role", "planner-role", "--repo-path", str(repo)]) == 0
    draft = json.loads(capsys.readouterr().out)
    draft["goal"] = "Add a thing."
    draft["acceptance_criteria"] = [{"id": "ac-1", "given": "g", "when": "w", "then": "t"}]
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")
    argv = ["seal", "--file", str(path), "--repo-path", str(repo)]
    if head is not None:
        argv += ["--head", head]
    assert spec_verb.main(argv) == 0
    return json.loads(capsys.readouterr().out)["spec_hash"]


def _stored(spec_hash: str) -> Path:
    return next(Path(spec_verb.spec_store.specs_root()).glob(f"*/{spec_hash}.json"))


def test_a_builder_brief_carries_the_envelope_unchanged_and_the_sealed_spec(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    base_argv = ["--role", "builder", "--repo-path", str(repo)]
    _, plain, _ = _brief(capsys, *base_argv)

    code, result, err = _brief(capsys, *base_argv, "--spec", spec_hash)

    assert code == pv.EXIT_OK, err
    assert {k: v for k, v in result.items() if k != "spec"} == plain
    section = result["spec"]
    document = json.loads(_stored(spec_hash).read_text(encoding="ascii"))
    assert section["document"] == document
    assert section["spec_hash"] == spec_hash
    assert section["head_sha"] == document["head_sha"] == run_git(repo, "rev-parse", "HEAD")
    assert section["sealed_at"] == document["sealed_at"]
    assert section["role"] == "planner-role" and section["supersedes"] is None
    assert "note" not in section
    assert set(plain) >= {"class", "class_rules", "paths", "deny", "tool_packages", "tests", "guidance", "rules"}


@pytest.mark.parametrize("role", ROLES)
def test_every_role_class_gets_the_spec_section_and_its_own_shaping_unchanged(
    tmp_path, capsys, role
):
    repo = profile_repo(tmp_path)
    groups = write_groups(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    argv = ["--role", role, "--repo-path", str(repo), "--groups-file", str(groups)]
    _, plain, _ = _brief(capsys, *argv)

    code, result, err = _brief(capsys, *argv, "--spec", spec_hash)

    assert code == pv.EXIT_OK, err
    assert "spec" not in plain
    assert result["spec"]["spec_hash"] == spec_hash
    assert {k: v for k, v in result.items() if k != "spec"} == plain


@pytest.mark.parametrize("bad", ["abc", "E" * 64, "e" * 63, "../x"])
def test_a_spec_that_is_not_a_hash_is_a_usage_error_naming_the_value(tmp_path, capsys, bad):
    repo = profile_repo(tmp_path)

    code, result, err = _brief(capsys, "--role", "builder", "--repo-path", str(repo), "--spec", bad)

    assert code == pv.EXIT_USAGE and result is None and repr(bad) in err


def _tamper(artifact: Path) -> None:
    document = json.loads(artifact.read_text(encoding="ascii"))
    document["goal"] = "Something else entirely."
    artifact.write_text(json.dumps(document, sort_keys=True, separators=(",", ":")), encoding="ascii")


@pytest.mark.parametrize("failure", ["not-found", "tampered", "unusable-root"])
def test_an_unreadable_spec_exits_64_naming_the_specs_root_and_the_hash(
    tmp_path, capsys, monkeypatch, failure
):
    repo = profile_repo(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    root = spec_verb.spec_store.specs_root()
    asked = spec_hash
    if failure == "not-found":
        asked = "e" * 64
    elif failure == "tampered":
        _tamper(_stored(spec_hash))
    else:
        unusable = tmp_path / "a-file-not-a-directory"
        unusable.write_text("x", encoding="utf-8")
        monkeypatch.setenv(spec_verb.spec_store.SPECS_DIR_ENV_VAR, str(unusable))
        root = unusable

    code, result, err = _brief(
        capsys, "--role", "builder", "--repo-path", str(repo), "--spec", asked
    )

    assert code == pv.EXIT_SPEC_UNREADABLE == 64 and result is None
    assert str(root) in err and asked in err


@pytest.mark.parametrize("ref_at", ["head", "moved", "absent"])
def test_a_ref_other_than_the_spec_head_is_noted_never_refused(tmp_path, capsys, ref_at):
    repo = profile_repo(tmp_path)
    sealed_at = run_git(repo, "rev-parse", "HEAD")
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    moved = commit_files(repo, {"other.txt": "y\n"})
    argv = ["--role", "builder", "--repo-path", str(repo), "--spec", spec_hash]
    if ref_at != "absent":
        argv += ["--ref", sealed_at if ref_at == "head" else moved]

    code, result, err = _brief(capsys, *argv)

    assert code == pv.EXIT_OK, err
    if ref_at == "moved":
        assert sealed_at in result["spec"]["note"] and moved in result["spec"]["note"]
    else:
        assert "note" not in result["spec"]


@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_out_writes_the_brief_to_a_new_file_and_never_replaces_one(tmp_path, capsys, role):
    repo = profile_repo(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    target = tmp_path / "brief.json"
    argv = ["--role", role, "--repo-path", str(repo), "--spec", spec_hash]
    _, printed, _ = _brief(capsys, *argv)

    code, line, err = _brief(capsys, *argv, "--out", str(target))

    assert code == pv.EXIT_OK, err
    assert line == {"result": "written", "path": str(target)}
    assert json.loads(target.read_text(encoding="utf-8")) == printed
    before = target.read_bytes()
    code, line, err = _brief(capsys, *argv, "--out", str(target))
    assert code == pv.EXIT_IO_FAILED and line is None and str(target) in err
    assert target.read_bytes() == before


def test_a_large_spec_is_readable_through_the_out_file(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    capsys.readouterr()
    assert spec_verb.main(["new", "--role", "planner-role", "--repo-path", str(repo)]) == 0
    draft = json.loads(capsys.readouterr().out)
    draft["goal"] = "Add a thing."
    draft["acceptance_criteria"] = [
        {"id": f"ac-{n}", "given": "g" * 80, "when": "w" * 80, "then": "t" * 80} for n in range(150)
    ]
    path = tmp_path / "draft.json"
    path.write_text(json.dumps(draft), encoding="utf-8")
    assert spec_verb.main(["seal", "--file", str(path), "--repo-path", str(repo)]) == 0
    spec_hash = json.loads(capsys.readouterr().out)["spec_hash"]
    assert _stored(spec_hash).stat().st_size > 40 * 1024
    target = tmp_path / "brief.json"

    code, line, err = _brief(
        capsys, "--role", "builder", "--repo-path", str(repo), "--spec", spec_hash, "--out", str(target)
    )

    assert code == pv.EXIT_OK, err
    assert line["path"] == str(target)
    written = json.loads(target.read_text(encoding="utf-8"))
    assert len(written["spec"]["document"]["acceptance_criteria"]) == 150


@pytest.mark.parametrize("name", [".invented-profile-repo", "plain-repo"])
def test_a_dot_named_repository_completes(tmp_path, capsys, name):
    repo = profile_repo(tmp_path, name=name)
    groups = write_groups(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)

    for extra in ([], ["--repo", name]):
        code, result, err = _brief(
            capsys,
            "--role", "builder", "--repo-path", str(repo), "--groups-file", str(groups),
            "--spec", spec_hash, *extra,
        )
        assert code == pv.EXIT_OK, err
        assert result["spec"]["spec_hash"] == spec_hash
