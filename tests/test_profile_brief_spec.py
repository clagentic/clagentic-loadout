"""`loadout-profile brief --spec` and `--out`: the sealed spec carried in the brief.

Every row goes through the real `main()` of both verbs: the spec is sealed by
`loadout-spec seal` into the per-test specs root and read back by the brief."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.spec import model as spec_model
from clagentic_loadout.spec import store as spec_store
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


@pytest.mark.parametrize("failure", ["not-found", "tampered", "unusable-root", "root-inside-repo"])
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
    elif failure == "root-inside-repo":
        root = repo / "specs-inside"
        monkeypatch.setenv(spec_verb.spec_store.SPECS_DIR_ENV_VAR, str(root))
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


def _seal_sized(repo: Path, goal_bytes: int) -> str:
    """Seal a spec whose goal is *goal_bytes* long, at a fixed time and head, so two of
    them differ only in what the size changes."""
    draft = spec_model.new_draft("planner-role")
    draft["goal"] = "g" * goal_bytes
    draft["acceptance_criteria"] = [{"id": "ac-1", "given": "g", "when": "w", "then": "t"}]
    sealed = spec_model.seal(
        draft, head_sha=run_git(repo, "rev-parse", "HEAD"), sealed_at="2026-01-01T00:00:00Z"
    )
    spec_store.write_sealed(spec_store.specs_root(), sealed)
    return sealed.spec_hash


def _reference_argv(repo: Path, spec_hash: str, *extra: str) -> list[str]:
    return ["--role", "builder", "--repo-path", str(repo), "--spec", spec_hash, *extra]


def test_the_reference_form_indexes_the_spec_and_every_show_line_prints_its_field(
    tmp_path, capsys
):
    repo = profile_repo(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    document = json.loads(_stored(spec_hash).read_text(encoding="ascii"))

    code, result, err = _brief(capsys, *_reference_argv(repo, spec_hash, "--spec-form", "reference"))

    assert code == pv.EXIT_OK, err
    section = result["spec"]
    assert "document" not in section
    assert list(section) == [
        "spec_hash", "head_sha", "sealed_at", "role", "supersedes", "show", "fields",
    ]
    assert section["show"] == f"loadout-spec show {spec_hash}"
    header = {"head_sha", "sealed_at", "role", "supersedes", "schema_version"}
    assert [f["name"] for f in section["fields"]] == [k for k in document if k not in header]
    for entry in section["fields"]:
        assert entry["bytes"] == len(json.dumps(document[entry["name"]], ensure_ascii=False).encode())
        assert entry["show"] == f"loadout-spec show {spec_hash} --field {entry['name']}"
        assert spec_verb.main(shlex.split(entry["show"])[1:]) == 0
        assert json.loads(capsys.readouterr().out) == {
            "spec_hash": spec_hash, entry["name"]: document[entry["name"]],
        }
    assert spec_verb.main(shlex.split(section["show"])[1:]) == 0


def test_the_reference_brief_does_not_grow_with_the_spec(tmp_path, capsys):
    repo = profile_repo(tmp_path)
    _, plain, _ = _brief(capsys, "--role", "builder", "--repo-path", str(repo))
    small, large = _seal_sized(repo, 1000), _seal_sized(repo, 45_000)
    briefs = {}
    for name, spec_hash in (("small", small), ("large", large)):
        code, briefs[name], err = _brief(
            capsys, *_reference_argv(repo, spec_hash, "--spec-form", "reference")
        )
        assert code == pv.EXIT_OK, err
        assert len(json.dumps(briefs[name])) - len(json.dumps(plain)) <= 8 * 1024

    def masked(brief: dict, spec_hash: str) -> str:
        section = dict(brief["spec"])
        fields = [{**f, "bytes": 0} for f in section["fields"]]
        text = json.dumps({**section, "fields": fields, "spec_hash": ""})
        return text.replace(spec_hash, "")

    assert masked(briefs["small"], small) == masked(briefs["large"], large)
    sizes = {name: {f["name"]: f["bytes"] for f in b["spec"]["fields"]} for name, b in briefs.items()}
    assert sizes["small"]["goal"] < 1100 and sizes["large"]["goal"] > 45_000


@pytest.mark.parametrize("form", [None, "document"])
def test_the_document_form_is_the_default_and_keeps_the_section_it_always_had(
    tmp_path, capsys, form
):
    repo = profile_repo(tmp_path)
    spec_hash = _seal_spec(tmp_path, repo, capsys)
    document = json.loads(_stored(spec_hash).read_text(encoding="ascii"))
    _, plain, _ = _brief(capsys, "--role", "builder", "--repo-path", str(repo))
    expected = json.dumps(
        {
            **plain,
            "spec": {
                "spec_hash": spec_hash, "head_sha": document["head_sha"],
                "sealed_at": document["sealed_at"], "role": document["role"],
                "supersedes": document["supersedes"], "document": document,
            },
        }
    )
    extra = [] if form is None else ["--spec-form", form]

    capsys.readouterr()
    code = pv.main(["brief", *_reference_argv(repo, spec_hash, *extra)])

    assert code == pv.EXIT_OK and capsys.readouterr().out == expected + "\n"


@pytest.mark.parametrize("argv", [["--spec-form", "reference"], ["--spec-form", "document"]])
def test_a_spec_form_without_a_spec_is_a_usage_error(tmp_path, capsys, argv):
    repo = profile_repo(tmp_path)

    code, result, err = _brief(capsys, "--role", "builder", "--repo-path", str(repo), *argv)

    assert code == pv.EXIT_USAGE and result is None and "--spec-form" in err
