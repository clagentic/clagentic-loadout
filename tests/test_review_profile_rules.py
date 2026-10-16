"""The repo profile's class rules, applied to a diff as findings with facts."""

from __future__ import annotations

import pytest

from clagentic_loadout.review import profile_rules as pr
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review.chunking import plan_chunks
from clagentic_loadout.review.profile_settings import ProfileSettings
from clagentic_loadout.review.evaluate import evaluate
from clagentic_loadout.review.facts import STATE_COMPLETE, parse_facts
from clagentic_loadout.review.policy_source import (
    PROFILE_INVALID,
    PROFILE_MISSING,
    PROFILE_OK,
    PROFILE_UNREAD,
    PolicySource,
)

NAME = "agent-alpha"
HOST = "host-one.example.test"
USER = "someone-local"
#: An account name that is not a placeholder. The test lines below spell home
#: paths with it through a variable, so this file carries no literal home path.
ACCOUNT = "dana"


def _diff(files: dict[str, list[str]]) -> str:
    parts = []
    for name, lines in files.items():
        parts += [
            f"diff --git a/{name} b/{name}",
            "new file mode 100644",
            "--- /dev/null",
            f"+++ b/{name}",
            f"@@ -0,0 +1,{len(lines)} @@",
            *(f"+{line}" for line in lines),
        ]
    return "\n".join(parts) + "\n"


def _policy(profile: rp.RepoProfile | None, state: str = PROFILE_OK, problem=None) -> PolicySource:
    return PolicySource((), stk.Stakes(), (), profile, state, problem)


def _findings(files, profile, deployment_deny=None):
    settings = ProfileSettings(deny=deployment_deny or rp.ProfileDeny())
    return pr.profile_findings(_policy(profile), plan_chunks(_diff(files)), settings)


def _released(**fields) -> rp.RepoProfile:
    return rp.RepoProfile(repo_class=rp.CLASS_RELEASED_TOOL, **fields)


def _rules(found) -> list[tuple[str, str, int]]:
    return [(f["rule_id"], f["file"], f["line"]) for f in found]


class TestReleasedTool:
    def test_a_denied_name_on_an_added_line_is_reported_under_the_profile_rule(self):
        found = _findings(
            {"src/a.py": ["x = 1", f"# written for {NAME}"]},
            _released(deny=rp.ProfileDeny(names=(NAME,))),
        )

        assert _rules(found) == [(pr.RULE_DENIED, "src/a.py", 2)]
        assert NAME in found[0]["message"]
        assert "released-tool" in found[0]["message"]

    @pytest.mark.parametrize(
        "line",
        [f"host = '{HOST}'", f"url=https://{HOST}/x", f"{HOST.upper()}"],
    )
    def test_a_denied_host_is_found_however_it_is_embedded(self, line):
        found = _findings({"a.cfg": [line]}, _released(deny=rp.ProfileDeny(hosts=(HOST,))))
        assert [f["rule_id"] for f in found] == [pr.RULE_DENIED]

    @pytest.mark.parametrize(
        "line, hit",
        [
            ("ALPHA = 1  # agent-alpha", True),
            ("agent_alpha_helper = 1", False),
            ("AGENT-ALPHA-SETTINGS", True),
            ("agent-alphabet", False),
            ("preagent-alpha", False),
        ],
    )
    def test_a_name_is_a_whole_token_not_a_substring_of_a_longer_word(self, line, hit):
        found = _findings({"a.txt": [line]}, _released(deny=rp.ProfileDeny(names=(NAME,))))
        assert bool(found) is hit

    def test_a_username_is_denied_like_a_name(self):
        found = _findings({"a.txt": [f"owner: {USER}"]}, _released(deny=rp.ProfileDeny(users=(USER,))))
        assert _rules(found) == [(pr.RULE_DENIED, "a.txt", 1)]
        assert "username" in found[0]["message"].lower()

    def test_the_deployment_list_applies_beside_the_profiles_own(self):
        found = _findings(
            {"a.txt": [HOST, NAME]},
            _released(deny=rp.ProfileDeny(hosts=(HOST,))),
            rp.ProfileDeny(names=(NAME,)),
        )
        assert sorted(f["line"] for f in found) == [1, 2]

    def test_with_no_list_anywhere_only_the_host_path_rule_applies(self):
        found = _findings({"a.txt": [NAME, HOST]}, _released())
        assert found == []

    @pytest.mark.parametrize(
        "line",
        [
            f"root = '/home/{ACCOUNT}/project'",
            f"p = /Users/{ACCOUNT}/Library",
            f"C:\\Users\\{ACCOUNT}\\src",
        ],
    )
    def test_an_absolute_home_path_is_reported(self, line):
        found = _findings({"a.sh": [line]}, _released())
        assert [f["rule_id"] for f in found] == [pr.RULE_HOST_PATH]

    def test_any_other_host_path_prefix_a_deployment_lists_is_reported_where_it_starts_a_path(self):
        profile = _released(deny=rp.ProfileDeny(hosts=("/srv/data/",)))
        assert _rules(_findings({"a.sh": ["cd /srv/data/work", "cd /srv/database"]}, profile)) == [
            (pr.RULE_DENIED, "a.sh", 1)
        ]

    @pytest.mark.parametrize(
        "line",
        [
            "path = '/home/user/project'",
            "/home/username/x",
            "/home/someone/x",
            "/home/<user>/x",
            "/home/${USER}/x",
            "/Users/Alice/x",
            "/root/work",
            "~/home/dana",
            "see $HOME/home/dana",
            "https://example.test/home/dana",
            "src/home/dana/x.py",
            "/rooted/path",
        ],
    )
    def test_a_placeholder_user_a_relative_or_url_path_is_not_a_host_path(self, line):
        assert _findings({"a.sh": [line]}, _released()) == []

    def test_a_removed_or_context_line_is_not_reported_only_added_lines_are(self):
        diff = "\n".join(
            [
                "diff --git a/a.txt b/a.txt",
                "--- a/a.txt",
                "+++ b/a.txt",
                "@@ -1,3 +1,3 @@",
                f" kept {NAME}",
                f"-removed {NAME}",
                "+fine",
                " tail",
            ]
        ) + "\n"
        found = pr.profile_findings(
            _policy(_released(deny=rp.ProfileDeny(names=(NAME,)))), plan_chunks(diff)
        )
        assert found == []

    def test_a_deployment_denial_list_never_applies_without_a_profile_in_force(self):
        settings = ProfileSettings(deny=rp.ProfileDeny(names=(NAME,)))
        policy = _policy(None, PROFILE_INVALID, "bad")
        found = pr.profile_findings(policy, plan_chunks(_diff({"a.txt": [NAME]})), settings)
        assert [f["rule_id"] for f in found] == [pr.RULE_INVALID]

    def test_the_profile_file_itself_may_list_what_it_forbids(self):
        found = _findings(
            {pr.PROFILE_PATH: [f"names: [{NAME}]"]}, _released(deny=rp.ProfileDeny(names=(NAME,)))
        )
        assert found == []

    def test_an_internal_override_exempts_its_paths(self):
        profile = _released(
            paths=((".state/**", rp.CLASS_INTERNAL),), deny=rp.ProfileDeny(names=(NAME,))
        )
        found = _findings({".state/a.yaml": [NAME], "src/a.py": [NAME]}, profile)
        assert _rules(found) == [(pr.RULE_DENIED, "src/a.py", 1)]

    def test_each_hit_carries_the_line_fingerprint_so_it_can_be_linked(self):
        (finding,) = _findings({"a.txt": [NAME]}, _released(deny=rp.ProfileDeny(names=(NAME,))))
        assert finding["fingerprint"].startswith("fp2:")

    def test_one_line_with_two_denied_values_gives_two_findings(self):
        found = _findings(
            {"a.txt": [f"{NAME} {HOST}"]},
            _released(deny=rp.ProfileDeny(names=(NAME,), hosts=(HOST,))),
        )
        assert len(found) == 2 and len({f["message"] for f in found}) == 2

    def test_a_very_long_value_is_shortened_in_the_message(self):
        long_name = "n" * 150
        (finding,) = _findings(
            {"a.txt": [long_name]}, _released(deny=rp.ProfileDeny(names=(long_name,)))
        )
        assert len(finding["message"]) < 200 and "..." in finding["message"]

    def test_hits_beyond_the_cap_are_counted_in_one_further_finding(self):
        lines = [f"{NAME} {n}" for n in range(pr.MAX_RULE_FINDINGS + 5)]
        found = _findings({"a.txt": lines}, _released(deny=rp.ProfileDeny(names=(NAME,))))
        assert len(found) == pr.MAX_RULE_FINDINGS + 1
        assert found[-1]["rule_id"] == pr.RULE_MORE
        assert found[-1]["message"].startswith("5 further")


class TestInternal:
    def test_released_tool_rules_do_not_apply_to_an_internal_tree(self):
        profile = rp.RepoProfile(
            repo_class=rp.CLASS_INTERNAL, deny=rp.ProfileDeny(names=(NAME,))
        )
        assert _findings({"a.txt": [NAME, f"/home/{ACCOUNT}/x"]}, profile) == []


class TestMissingAndInvalid:
    def test_a_missing_profile_is_one_finding_on_the_profile_path_by_default(self):
        chunks = plan_chunks(_diff({"a": ["x"]}))
        for settings in (None, ProfileSettings()):
            found = pr.profile_findings(_policy(None, PROFILE_MISSING), chunks, settings)
            assert _rules(found) == [(pr.RULE_MISSING, pr.PROFILE_PATH, 1)]
            assert "loadout-profile init" in found[0]["message"]

    def test_an_invalid_profile_is_one_finding_naming_the_problem_for_any_settings(self):
        for settings in (None, ProfileSettings()):
            found = pr.profile_findings(
                _policy(None, PROFILE_INVALID, "class is required"),
                plan_chunks(_diff({"a": ["x"]})),
                settings,
            )
            assert _rules(found) == [(pr.RULE_INVALID, pr.PROFILE_PATH, 1)]
            assert "class is required" in found[0]["message"]

    def test_an_unread_policy_states_nothing_because_it_is_already_degraded(self):
        chunks = plan_chunks(_diff({"a": ["x"]}))
        assert pr.profile_findings(_policy(None, PROFILE_UNREAD), chunks) == []

    def test_a_missing_profile_reports_even_for_a_diff_with_no_added_lines(self):
        assert pr.profile_findings(_policy(None, PROFILE_MISSING), []) != []


class TestFacts:
    """Every finding states complete facts, and the evaluator scores them: a
    missing or invalid profile can never block; a rule hit is held at low."""

    @staticmethod
    def _verdict(found, block_at):
        return evaluate(found, stk.Stakes(), (), "b" * 40, block_at=block_at)

    def test_every_finding_states_complete_facts(self):
        found = [
            *_findings(
                {"a.txt": [NAME, f"/home/{ACCOUNT}/x"]},
                _released(deny=rp.ProfileDeny(names=(NAME,))),
            ),
            pr._missing_finding(),
            pr._invalid_finding("bad"),
        ]
        assert len(found) == 4
        for item in found:
            assert parse_facts(item["facts"]).state == STATE_COMPLETE

    @pytest.mark.parametrize("block_at", ["low", "medium", "high", "critical"])
    def test_a_missing_or_invalid_profile_never_blocks_at_any_threshold(self, block_at):
        for item in (pr._missing_finding(), pr._invalid_finding("bad")):
            verdict = self._verdict([item], block_at)
            assert verdict.outcome == "CLEAN"
            assert verdict.findings[0].severity == "none" and not verdict.findings[0].blocking

    def test_a_rule_hit_is_advisory_by_default_and_blocks_only_at_the_lowest_threshold(self):
        found = _findings({"a.txt": [NAME]}, _released(deny=rp.ProfileDeny(names=(NAME,))))
        assert self._verdict(found, "high").outcome == "CLEAN"
        assert self._verdict(found, "medium").outcome == "CLEAN"
        assert self._verdict(found, "low").outcome == "BLOCKED"
        assert self._verdict(found, "high").findings[0].severity == "low"

    def test_a_rule_hit_is_never_a_security_floor_finding(self):
        found = _findings({"a.txt": [NAME]}, _released(deny=rp.ProfileDeny(names=(NAME,))))
        verdict = self._verdict(found, "high")
        assert verdict.findings[0].floor_category is False
