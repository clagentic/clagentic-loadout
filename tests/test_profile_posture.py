"""The repo profile's risk posture: its vocabulary, what is reported as missing, and
every caller that reads it (the evaluator through the gates, `loadout-profile
check` and `brief`)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.review import profile_posture as pp
from clagentic_loadout.review.repo_profile import parse_profile_document
from clagentic_loadout.review import profile_verb as pv
from clagentic_loadout.review.evaluate_render import render_text
from clagentic_loadout.review.evaluation import EvaluationContext, evaluate_findings
from clagentic_loadout.review.policy_reader import GitPolicyReader
from clagentic_loadout.review.policy_source import PROFILE_PATH, STAKES_PATH
from tests._review_evaluate_support import HEAD, commit_files, facts, finding, make_repo

REPO_ROOT = Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 24, 9, 30, tzinfo=dt.timezone.utc)
TODAY = NOW.date().isoformat()
FULL = {
    "exposure": "internal_authenticated",
    "data": "internal",
    "visibility": "private",
    "merge_control": "review_required",
    "confirmed_at": TODAY,
}
SERIOUS = facts(integrity="full", precondition="none")


def profile_text(drop: tuple[str, ...] = (), **overrides) -> str:
    values = {**FULL, **overrides}
    return "class: internal\n" + "".join(f"{k}: {v}\n" for k, v in values.items() if k not in drop)


class HostLikeReader:
    """A policy reader over an in-memory tree, the shape the host API gives the
    reviewer and the merge gate when no checkout is named."""

    def __init__(self, files: dict[str, str]) -> None:
        self._files = files

    def list_paths(self, base: str) -> list[str]:
        return sorted(self._files)

    def read_text(self, base: str, path: str) -> str:
        return self._files[path]


def evaluate_at(reader, base: str = "a" * 40, *, visibility: str = "private"):
    context = EvaluationContext(policy_reader=reader, policy_base_sha=base, visibility=visibility)
    return evaluate_findings([finding(fact=SERIOUS)], HEAD, context)


def stakes_of(verdict) -> dict:
    return verdict.blocking[0].stakes.as_dict()


class TestVocabulary:
    @pytest.mark.parametrize(
        "value, expected",
        [
            (dt.date(2026, 10, 24), dt.date(2026, 10, 24)),
            (dt.datetime(2026, 10, 24, 23, 0), dt.date(2026, 10, 24)),
            ("2026-10-24", dt.date(2026, 10, 24)),
            ("2026-10-24T01:02:03Z", dt.date(2026, 10, 24)),
            ("yesterday", None),
            (20261024, None),
            (True, None),
            (None, None),
        ],
    )
    def test_confirmed_at_forms(self, value, expected):
        assert pp.parse_confirmed_at(value) == expected

    def test_the_clock_is_utc_and_a_naive_moment_is_taken_as_utc(self):
        assert pp.today_utc(dt.datetime(2026, 10, 24, 23, 59)) == dt.date(2026, 10, 24)
        offset = dt.timezone(dt.timedelta(hours=5))
        assert pp.today_utc(dt.datetime(2026, 10, 25, 2, 0, tzinfo=offset)) == dt.date(2026, 10, 24)

    def test_a_fully_stated_confirmed_posture_resolves_with_no_items(self):
        claims = pp.resolve_posture(FULL, now=NOW, profile_path="p.yaml")
        assert (claims.exposure, claims.data_class, claims.merge_control) == (
            "internal",
            "internal",
            "controlled",
        )
        assert claims.declared_visibility == "private" and claims.items == ()

    def test_items_come_in_a_fixed_order_with_a_reason_and_a_fix(self):
        claims = pp.resolve_posture({}, now=NOW, profile_path="p.yaml")
        assert [item.key for item in claims.items] == list(pp.POSTURE_KEY_ORDER)
        assert all(item.reason and "p.yaml" in item.fix for item in claims.items)
        assert claims.items[0].as_dict() == {
            "key": "exposure",
            "reason": "missing",
            "fix": "set `exposure: <internet | internal_authenticated | local_or_ci_only>` in p.yaml",
        }

    def test_a_non_string_value_is_unrecognised_not_a_crash(self):
        claims = pp.resolve_posture({**FULL, "exposure": ["internet"]}, now=NOW, profile_path="p")
        assert claims.exposure is None
        assert [item.key for item in claims.items] == ["exposure"]


class TestShippedExample:
    def test_the_example_profile_states_a_complete_posture_in_the_documented_vocabulary(self):
        example = REPO_ROOT / ".clagentic" / "loadout" / "profile.yaml.example"
        profile = parse_profile_document(yaml.safe_load(example.read_text(encoding="utf-8")))
        confirmed = pp.parse_confirmed_at(profile.posture_values()["confirmed_at"])
        assert confirmed is not None
        claims = pp.resolve_posture(
            profile.posture_values(),
            now=dt.datetime.combine(confirmed, dt.time(), tzinfo=dt.timezone.utc),
            profile_path=PROFILE_PATH,
        )
        assert claims.items == ()
        assert claims.exposure is not None and claims.merge_control is not None


class TestCheckVerb:
    """loadout-profile check reports completeness and never fails on it."""

    def _check(self, capsys, repo, *extra, now=NOW):
        capsys.readouterr()
        code = pv.main(["check", "--repo-path", str(repo), *extra], now=now)
        captured = capsys.readouterr()
        lines = captured.out.strip().splitlines()
        return code, json.loads(lines[-1]), captured.err

    def test_a_repo_with_no_posture_keys_lists_the_missing_keys_and_exits_zero(
        self, tmp_path, capsys
    ):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: "class: internal\n"})

        code, result, err = self._check(capsys, repo)

        assert code == pv.EXIT_OK
        assert result["result"] == "valid"
        assert [item["key"] for item in result["completeness"]] == list(pp.POSTURE_KEY_ORDER)
        assert all(item["reason"] and item["fix"] for item in result["completeness"])
        assert err.count("incomplete profile:") == 5

    def test_a_complete_profile_has_no_items_and_says_nothing_on_stderr(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: profile_text()})

        code, result, err = self._check(capsys, repo)

        assert (code, result["completeness"], err) == (pv.EXIT_OK, [], "")

    def test_an_expired_confirmation_names_the_expiry_and_still_exits_zero(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: profile_text(confirmed_at="2026-04-01")})

        code, result, _ = self._check(capsys, repo)

        assert code == pv.EXIT_OK
        (item,) = result["completeness"]
        assert item["key"] == "confirmed_at"
        assert "expired" in item["reason"] and "180" in item["reason"]

    def test_the_age_is_measured_against_the_injected_clock(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: profile_text(confirmed_at="2026-10-01")})
        _, soon, _ = self._check(capsys, repo, now=NOW)
        _, later, _ = self._check(capsys, repo, now=NOW + dt.timedelta(days=200))
        assert soon["completeness"] == []
        assert [item["key"] for item in later["completeness"]] == ["confirmed_at"]

    def test_a_retired_stakes_file_is_an_item_naming_where_the_keys_go(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: profile_text(), STAKES_PATH: "profiles: {}\n"})

        code, result, _ = self._check(capsys, repo)

        assert code == pv.EXIT_OK
        (item,) = result["completeness"]
        assert item["key"] == "stakes.yaml" and "retired" in item["reason"]
        assert "profile.yaml" in item["fix"]

    def test_the_same_items_come_from_a_ref_and_the_working_tree(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: "class: internal\n"})
        (repo / PROFILE_PATH).write_text(profile_text(), encoding="utf-8")

        _, working, _ = self._check(capsys, repo)
        _, committed, _ = self._check(capsys, repo, "--ref", "HEAD")

        assert working["completeness"] == []
        assert len(committed["completeness"]) == 5

    def test_a_missing_profile_still_exits_60_and_carries_the_items(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {"a": "1\n"})

        code, result, _ = self._check(capsys, repo, "--ref", "HEAD")

        assert code == pv.EXIT_PROFILE_MISSING
        assert result["completeness"][0]["key"] == "profile"

    def test_an_invalid_profile_still_exits_61_and_carries_the_file_item_only(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: "class: internal\nowner: x\n"})

        code, result, _ = self._check(capsys, repo, "--ref", "HEAD")

        assert code == pv.EXIT_PROFILE_INVALID
        assert [item["key"] for item in result["completeness"]] == ["profile"]

    def test_an_unrecognised_posture_value_leaves_the_profile_valid(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: profile_text(exposure="sealed")})

        code, result, _ = self._check(capsys, repo)

        assert code == pv.EXIT_OK and result["result"] == "valid"
        assert [item["key"] for item in result["completeness"]] == ["exposure"]


class TestBriefNeverCarriesPosture:
    def _brief(self, capsys, repo, role):
        capsys.readouterr()
        code = pv.main(["brief", "--role", role, "--repo-path", str(repo)])
        out = capsys.readouterr().out.strip().splitlines()
        return code, json.loads(out[-1])

    @pytest.mark.parametrize("role", ["builder", "planner", "reviewer", "researcher"])
    def test_no_posture_key_or_value_in_a_brief_of_a_repo_that_has_them(
        self, tmp_path, capsys, role
    ):
        repo = make_repo(tmp_path)
        commit_files(
            repo,
            {PROFILE_PATH: profile_text(exposure="internet", data="regulated_or_customer")},
        )

        code, result = self._brief(capsys, repo, role)

        assert code == pv.EXIT_OK
        text = json.dumps(result)
        for key in pp.POSTURE_KEY_ORDER:
            assert f'"{key}"' not in text
        for value in ("internet", "regulated_or_customer", "review_required", TODAY):
            assert value not in text


class TestEvaluatorStakes:
    """The evaluator through the gates reads posture from the base only."""

    def test_posture_at_the_base_is_the_stakes_with_the_profile_as_source(self, tmp_path):
        repo = make_repo(tmp_path)
        base = commit_files(repo, {PROFILE_PATH: profile_text(confirmed_at=pp.today_utc().isoformat())})
        verdict = evaluate_at(GitPolicyReader(repo), base)
        stakes = stakes_of(verdict)
        assert (stakes["exposure"], stakes["data_class"], stakes["merge_control"]) == (
            "internal",
            "internal",
            "controlled",
        )
        assert stakes["exposure_source"] == stakes["merge_control_source"] == "profile"

    def test_a_head_only_posture_edit_is_not_applied(self, tmp_path):
        repo = make_repo(tmp_path)
        today = pp.today_utc().isoformat()
        base = commit_files(
            repo, {PROFILE_PATH: profile_text(exposure="internet", confirmed_at=today)}
        )
        commit_files(
            repo,
            {PROFILE_PATH: profile_text(exposure="local_or_ci_only", confirmed_at=today)},
        )
        assert stakes_of(evaluate_at(GitPolicyReader(repo), base))["exposure"] == "exposed"

    def test_an_expired_posture_is_the_worst_case_through_the_host_shaped_reader(self):
        stale = profile_text(confirmed_at="2025-01-01")
        verdict = evaluate_at(HostLikeReader({PROFILE_PATH: stale}))
        stakes = stakes_of(verdict)
        assert (stakes["exposure"], stakes["data_class"], stakes["merge_control"]) == (
            "exposed",
            "regulated",
            "uncontrolled",
        )
        assert verdict.degraded == ()

    def test_a_retired_stakes_file_gives_worst_case_values_in_the_fence_text(self):
        reader = HostLikeReader(
            {
                STAKES_PATH: "profile: s\nprofiles:\n  s:\n    exposure: internal\n"
                "    data_class: public\n    merge_control: controlled\n"
            }
        )
        rendering = render_text(evaluate_at(reader))
        assert (
            "stakes: exposure=exposed (unstated (worst case)), data_class=regulated "
            "(unstated (worst case)), merge_control=uncontrolled (unstated (worst case))"
        ) in rendering

    def test_the_fence_text_keeps_its_shape_with_a_profile_source(self):
        today = pp.today_utc().isoformat()
        reader = HostLikeReader({PROFILE_PATH: profile_text(confirmed_at=today)})
        rendering = render_text(evaluate_at(reader))
        assert (
            "stakes: exposure=internal (profile), data_class=internal (profile), "
            "merge_control=controlled (profile)"
        ) in rendering

    def test_a_tree_marker_still_overrides_the_profile_to_exposed(self):
        today = pp.today_utc().isoformat()
        reader = HostLikeReader(
            {PROFILE_PATH: profile_text(confirmed_at=today), "src/ingress.yaml": "k: v\n"}
        )
        stakes = stakes_of(evaluate_at(reader))
        assert (stakes["exposure"], stakes["exposure_source"]) == ("exposed", "tree marker")

    def test_a_declared_public_raises_but_a_declared_private_does_not_lower_the_host(self):
        today = pp.today_utc().isoformat()
        declared_public = HostLikeReader({PROFILE_PATH: profile_text(visibility="public", confirmed_at=today)})
        declared_private = HostLikeReader({PROFILE_PATH: profile_text(visibility="private", confirmed_at=today)})
        assert stakes_of(evaluate_at(declared_public, visibility="private"))["exposure"] == "exposed"
        assert stakes_of(evaluate_at(declared_private, visibility="public"))["exposure"] == "exposed"
