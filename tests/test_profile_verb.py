"""`loadout-profile`: validate a repo profile, and draft one for a repo with none."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout._version import get_version
from clagentic_loadout.cli import EXIT_UMBRELLA_USAGE
from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.review.policy_source import PROFILE_PATH
from tests._review_evaluate_support import commit_files, make_repo

VALID = "class: released-tool\npaths:\n  '.state/**': {class: internal}\n"


def _run(capsys, *argv) -> tuple[int, dict | None, str]:
    capsys.readouterr()
    code = pv.main(list(argv))
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines and lines[-1].startswith("{") else None), captured.err


def test_check_reports_a_valid_profile_with_its_class_rules_and_overrides(tmp_path, capsys):
    repo = make_repo(tmp_path)
    (repo / PROFILE_PATH).parent.mkdir(parents=True)
    (repo / PROFILE_PATH).write_text(VALID, encoding="utf-8")

    code, result, _ = _run(capsys, "check", "--repo-path", str(repo))

    assert code == pv.EXIT_OK
    assert result["result"] == "valid" and result["class"] == "released-tool"
    assert result["paths"] == [{"glob": ".state/**", "class": "internal"}]
    assert result["rules"] and result["deny"] == {"names": 0, "hosts": 0, "users": 0}


def test_check_without_a_ref_reads_the_working_tree_and_with_a_ref_the_revision(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {PROFILE_PATH: "class: internal\n"})
    (repo / PROFILE_PATH).write_text("class: released-tool\n", encoding="utf-8")

    _, working, _ = _run(capsys, "check", "--repo-path", str(repo))
    _, committed, _ = _run(capsys, "check", "--repo-path", str(repo), "--ref", "HEAD")

    assert (working["class"], committed["class"]) == ("released-tool", "internal")
    assert committed["at"] == "HEAD"


def test_check_with_no_profile_exits_60_and_says_how_to_draft_one(tmp_path, capsys):
    repo = make_repo(tmp_path)

    code, result, err = _run(capsys, "check", "--repo-path", str(repo))

    assert code == pv.EXIT_PROFILE_MISSING == 60
    assert result["result"] == "missing"
    assert "loadout-profile init" in err and str(repo) in err


def test_check_with_an_invalid_profile_exits_61_naming_the_problem(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {PROFILE_PATH: "class: internal\nowner: x\n"})

    code, result, err = _run(capsys, "check", "--repo-path", str(repo), "--ref", "HEAD")

    assert code == pv.EXIT_PROFILE_INVALID == 61
    assert result["result"] == "invalid" and "owner" in result["problem"]
    assert "owner" in err


def test_check_of_a_ref_that_cannot_be_read_exits_62_naming_the_path_and_the_ref(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"a": "1\n"})

    code, _, err = _run(capsys, "check", "--repo-path", str(repo), "--ref", "no-such-ref")

    assert code == pv.EXIT_IO_FAILED == 62
    assert "no-such-ref" in err and str(repo) in err


def test_init_writes_a_draft_the_check_then_accepts(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"pyproject.toml": "x\n", ".agentstate/a.yaml": "k: v\n", "src/a.py": "x\n"})

    code, result, err = _run(capsys, "init", "--repo-path", str(repo))

    assert code == pv.EXIT_OK and result["result"] == "written"
    written = (repo / PROFILE_PATH).read_text(encoding="utf-8")
    assert written.startswith("# DRAFT") and "pyproject.toml" in written
    assert "review it" in err
    code, checked, _ = _run(capsys, "check", "--repo-path", str(repo))
    assert code == pv.EXIT_OK
    assert checked["class"] == "released-tool"
    assert checked["paths"] == [{"glob": ".agentstate/**", "class": "internal"}]


def test_init_for_a_tree_with_no_publishing_signal_drafts_an_internal_profile(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"notes.md": "x\n"})

    _run(capsys, "init", "--repo-path", str(repo))

    _, checked, _ = _run(capsys, "check", "--repo-path", str(repo))
    assert checked["class"] == "internal"


def test_init_never_overwrites_an_existing_profile_without_force(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {PROFILE_PATH: "class: internal\n", "pyproject.toml": "x\n"})

    code, _, err = _run(capsys, "init", "--repo-path", str(repo))

    assert code == pv.EXIT_EXISTS == 63
    assert "--force" in err and str(repo / PROFILE_PATH) in err
    assert (repo / PROFILE_PATH).read_text(encoding="utf-8") == "class: internal\n"

    code, _, _ = _run(capsys, "init", "--repo-path", str(repo), "--force")
    assert code == pv.EXIT_OK
    assert (repo / PROFILE_PATH).read_text(encoding="utf-8").startswith("# DRAFT")


def test_init_stdout_prints_the_draft_and_writes_nothing(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"pyproject.toml": "x\n"})

    capsys.readouterr()
    code = pv.main(["init", "--repo-path", str(repo), "--stdout"])

    assert code == pv.EXIT_OK
    assert capsys.readouterr().out.startswith("# DRAFT")
    assert not (repo / PROFILE_PATH).exists()


def test_init_reads_the_tree_of_the_ref_not_the_working_tree(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"notes.md": "x\n"})
    (repo / "pyproject.toml").write_text("x\n", encoding="utf-8")

    capsys.readouterr()
    pv.main(["init", "--repo-path", str(repo), "--stdout"])

    assert "class: internal" in capsys.readouterr().out


def test_init_in_a_repository_with_no_commit_exits_62(tmp_path, capsys):
    repo = make_repo(tmp_path)

    code, _, err = _run(capsys, "init", "--repo-path", str(repo))

    assert code == pv.EXIT_IO_FAILED
    assert str(repo) in err and "HEAD" in err


def test_a_ref_that_looks_like_an_option_is_a_usage_error(tmp_path, capsys):
    code, _, err = _run(capsys, "check", "--repo-path", str(tmp_path), "--ref=--output=x")

    assert code == pv.EXIT_USAGE
    assert "--output=x" in err


def test_a_missing_subcommand_is_a_usage_error_and_help_and_version_exit_zero(capsys):
    assert pv.main([]) == pv.EXIT_USAGE
    capsys.readouterr()
    assert pv.main(["--help"]) == 0
    assert "init" in capsys.readouterr().out
    assert pv.main(["--version"]) == 0
    assert get_version() in capsys.readouterr().out


def test_the_umbrella_routes_profile_to_the_verb(capsys):
    assert umbrella_main(["profile", "--help"]) == 0
    assert "check" in capsys.readouterr().out


def test_the_verb_binds_no_caller_because_it_mints_no_credential():
    flags = {opt for action in pv._build_arg_parser()._actions for opt in action.option_strings}
    assert not flags & {"--caller", "--role"}


@pytest.mark.parametrize(
    "code", [pv.EXIT_PROFILE_MISSING, pv.EXIT_PROFILE_INVALID, pv.EXIT_IO_FAILED, pv.EXIT_EXISTS]
)
def test_the_verbs_own_exit_codes_stay_clear_of_the_umbrellas_usage_code(code):
    assert code != EXIT_UMBRELLA_USAGE and code > 1
