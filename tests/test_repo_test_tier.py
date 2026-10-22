"""`clagentic-loadout test run tier NAME`: named tiers from the profile, recorded and diffed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from _repo_test_support import make_repo, profile_text, write_run

from clagentic_loadout.repo_test import exits, store
from clagentic_loadout.repo_test.spec import TestBlockError, parse_test_block
from clagentic_loadout.repo_test.verb import main
from clagentic_loadout.review import repo_profile as rp

RUNNER = [sys.executable, "runner.py", "{junit}"]
# Writes a report holding one failing case, so the tier's own result is visible.
MARKER_TIER = [
    sys.executable,
    "-c",
    "import sys; open(sys.argv[1], 'w').write("
    "\"<testsuite><testcase classname='tier' name='ran'><failure message='x'/></testcase></testsuite>\")"
    "; sys.exit(1)",
    "{junit}",
]


@pytest.fixture
def reports(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "reports"
    monkeypatch.setenv(store.REPORTS_DIR_ENV_VAR, str(root))
    return root


def _main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_declared_tier_runs_only_that_tier_and_is_recorded(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(tiers={"integration": MARKER_TIER}))

    code, out, _ = _main(capsys, "run", "tier", "integration", "--repo-root", str(repo))

    summary = json.loads(out)
    assert code == exits.EXIT_TESTS_FAILED
    assert summary["mode"] == "tier" and summary["tier"] == "integration"
    run = store.load_run(reports, summary["run_id"])
    assert run.result["failing_ids"] == ["tier::ran"]
    assert run.result["command"] == MARKER_TIER


def test_non_tier_runs_carry_a_null_tier(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, out, _ = _main(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert json.loads(out)["tier"] is None


def test_an_undeclared_tier_exits_72_naming_the_key_path(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, out, err = _main(capsys, "run", "tier", "integration", "--repo-root", str(repo))

    assert code == exits.EXIT_NOT_DECLARED and out == ""
    assert "test.tiers.integration" in err
    assert not reports.exists()


@pytest.mark.parametrize("name", ["Integration", "1st", "has space", "a" * 33, "-x", "../x"])
def test_a_malformed_tier_name_is_a_usage_error(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    repo = make_repo(tmp_path / "repo")

    code, out, err = _main(capsys, "run", "--repo-root", str(repo), "tier", "--", name)

    assert code == exits.EXIT_USAGE and out == ""
    assert repr(name) in err
    assert not reports.exists()


def test_tier_takes_exactly_one_name_and_other_modes_take_none(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(tiers={"integration": RUNNER}))

    assert _main(capsys, "run", "tier", "--repo-root", str(repo))[0] == exits.EXIT_USAGE
    assert _main(capsys, "run", "tier", "integration", "other", "--repo-root", str(repo))[0] == exits.EXIT_USAGE
    assert not reports.exists()


def test_existing_modes_ignore_declared_tiers(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(tiers={"integration": MARKER_TIER}))

    code, out, _ = _main(capsys, "run", "full", "--repo-root", str(repo))

    assert code == exits.EXIT_OK
    assert json.loads(out)["counts"]["failed"] == 0


def test_a_profile_with_only_tiers_is_valid_and_runs_its_tier(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = make_repo(
        tmp_path / "repo",
        profile="class: internal\ntest:\n  tiers:\n    e2e: " + json.dumps(RUNNER) + "\n",
    )

    code, out, _ = _main(capsys, "run", "tier", "e2e", "--repo-root", str(repo))

    assert code == exits.EXIT_OK and json.loads(out)["tier"] == "e2e"


def test_tiers_parse_and_are_listed_as_a_declared_key() -> None:
    parsed = parse_test_block({"tiers": {"integration": ["run", "{junit}"], "e2e_slow-1": ["x", "{junit}"]}})

    assert parsed.tier_argv("integration") == ("run", "{junit}")
    assert parsed.tier_argv("missing") is None
    assert parsed.declared_keys() == ["tiers"]
    assert parse_test_block({"tiers": {}}).declared_keys() == []


@pytest.mark.parametrize(
    "block,fragment",
    [
        ({"tiers": ["integration"]}, "test.tiers must be a mapping"),
        ({"tiers": {"Integration": ["x", "{junit}"]}}, "test.tiers.Integration"),
        ({"tiers": {3: ["x", "{junit}"]}}, "test.tiers.3"),
        ({"tiers": {"integration": "run {junit}"}}, "test.tiers.integration must be a non-empty list"),
        ({"tiers": {"integration": ["run"]}}, "test.tiers.integration must carry the {junit}"),
        ({"tiers": {"integration": ["run", "{junit}", "{base}"]}}, "test.tiers.integration may not use {base}"),
        ({"tiers": {"integration": ["run", "{junit}", 4]}}, "test.tiers.integration[2]"),
    ],
)
def test_a_malformed_tier_is_refused_naming_the_key_path(block: object, fragment: str) -> None:
    with pytest.raises(TestBlockError) as caught:
        parse_test_block(block)

    assert fragment in str(caught.value)


def test_the_profile_names_the_tier_key_when_it_is_bad() -> None:
    with pytest.raises(rp.ProfileFormatError, match=r"test\.tiers\.integration"):
        rp.parse_profile_document({"class": "internal", "test": {"tiers": {"integration": ["run"]}}})


def test_the_summary_reports_the_tier_and_older_records_read_as_null(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", mode="tier", tier="integration")
    write_run(reports, "20260102T000000Z-11")

    _, out, _ = _main(capsys, "report", "20260101T000000Z-10")
    assert json.loads(out)["tier"] == "integration" and json.loads(out)["mode"] == "tier"
    _, out, _ = _main(capsys, "report", "20260102T000000Z-11")
    assert json.loads(out)["tier"] is None


def test_diff_compares_two_runs_of_the_same_tier(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", mode="tier", tier="integration", failing=["t::old", "t::both"])
    write_run(reports, "20260102T000000Z-11", mode="tier", tier="integration", failing=["t::new", "t::both"])

    code, out, _ = _main(capsys, "report", "20260102T000000Z-11", "--diff", "20260101T000000Z-10")

    assert code == exits.EXIT_OK
    diff = json.loads(out)
    assert diff["head_only"] == ["t::new"] and diff["base_only"] == ["t::old"] and diff["shared_count"] == 1


def test_diff_refuses_a_tier_run_against_a_non_tier_run_either_way(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", mode="tier", tier="integration")
    write_run(reports, "20260102T000000Z-11")

    for head, base in [("20260101T000000Z-10", "20260102T000000Z-11"), ("20260102T000000Z-11", "20260101T000000Z-10")]:
        code, out, err = _main(capsys, "report", head, "--diff", base)
        assert code == exits.EXIT_DIFF_REFUSED and out == ""
        assert "'integration'" in err and "not a tier run" in err


def test_diff_refuses_two_different_tiers(reports: Path, capsys: pytest.CaptureFixture[str]) -> None:
    write_run(reports, "20260101T000000Z-10", mode="tier", tier="integration")
    write_run(reports, "20260102T000000Z-11", mode="tier", tier="e2e")

    code, out, err = _main(capsys, "report", "20260102T000000Z-11", "--diff", "20260101T000000Z-10")

    assert code == exits.EXIT_DIFF_REFUSED and out == ""
    assert "'integration'" in err and "'e2e'" in err


def test_non_tier_diff_is_unchanged_for_records_with_no_tier_field(
    reports: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    write_run(reports, "20260101T000000Z-10", failing=["t::a"])
    write_run(reports, "20260102T000000Z-11", failing=["t::a"])

    code, out, _ = _main(capsys, "report", "20260102T000000Z-11", "--diff", "20260101T000000Z-10")

    assert code == exits.EXIT_OK and json.loads(out)["shared_count"] == 1


def test_an_acceptance_run_is_recorded_and_diffed_against_a_base_run_of_the_same_tier(
    tmp_path: Path, reports: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path / "repo", profile=profile_text(tiers={"integration": MARKER_TIER}))
    ids = []
    for pid in (101, 102):
        # Two runs in one process would otherwise share a run id.
        monkeypatch.setattr(store, "new_run_id", lambda pid=pid: f"20260101T00000{pid - 100}Z-{pid}")
        _, out, _ = _main(capsys, "run", "tier", "integration", "--repo-root", str(repo))
        ids.append(json.loads(out)["run_id"])

    code, out, _ = _main(capsys, "report", ids[1], "--diff", ids[0])

    assert code == exits.EXIT_OK
    assert json.loads(out)["shared_count"] == 1
