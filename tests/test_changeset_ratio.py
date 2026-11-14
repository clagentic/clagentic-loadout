"""The advisory net test:code ratio of a change: the arithmetic, the profile keys,
the `loadout-changeset ratio` verb over real git repositories, the host source,
and the notice on push and the field on post."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.acquire.contract import RangeDiff
from clagentic_loadout.acquire.errors import AcquireFetchError
from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.repo_test.spec import parse_test_block
from clagentic_loadout.review import changeset_ratio as cr
from clagentic_loadout.review import changeset_ratio_source as source
from clagentic_loadout.review import changeset_verb as cv
from clagentic_loadout.review.churn_delta import FileDelta
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.repo_profile import parse_profile_document
from tests._review_evaluate_support import commit_files, make_repo, run_git

OVER_2 = "over the 2:1 budget; advisory only, it does not block"
WITHIN_2 = "within the 2:1 budget"
ABOVE_2_UNDER_100 = "above the 2:1 budget, but test growth is under the 100-line floor, so it is not flagged"

# test added, test removed, code added, code removed, budget, floor, ratio, over budget,
# the verdict the line ends with
ARITHMETIC_ROWS = [
    (300, 0, 100, 0, 2, 100, "3.0:1", True, OVER_2),
    (3241, 5607, 400, 0, 2, 100, "-5.9:1", False, WITHIN_2),
    (50, 0, 5, 0, 2, 100, "10.0:1", False, ABOVE_2_UNDER_100),
    (200, 0, 100, 100, 2, 100, "unbounded", True, OVER_2),
    (200, 0, 100, 0, 2, 100, "2.0:1", False, WITHIN_2),
    (201, 0, 100, 0, 2, 100, "2.0:1", True, OVER_2),
    (150, 0, 0, 40, 2, 100, "unbounded", True, OVER_2),
    (99, 0, 0, 0, 2, 100, "unbounded", False, ABOVE_2_UNDER_100),
    (100, 0, 0, 0, 2, 100, "unbounded", True, OVER_2),
    (0, 0, 0, 0, 2, 100, "n/a", False, WITHIN_2),
    (0, 400, 30, 0, 2, 100, "-13.3:1", False, WITHIN_2),
    (500, 0, 100, 0, 5, 100, "5.0:1", False, "within the 5:1 budget"),
    (
        500, 0, 100, 0, 2, 600, "5.0:1", False,
        "above the 2:1 budget, but test growth is under the 600-line floor, so it is not flagged",
    ),
    (
        150, 0, 50, 0, 2, 200, "3.0:1", False,
        "above the 2:1 budget, but test growth is under the 200-line floor, so it is not flagged",
    ),
]


@pytest.mark.parametrize(
    ("t_add", "t_rem", "c_add", "c_rem", "budget", "floor", "ratio", "over", "verdict"),
    ARITHMETIC_ROWS,
)
def test_the_ratio_and_the_over_budget_rule(
    t_add, t_rem, c_add, c_rem, budget, floor, ratio, over, verdict
):
    result = cr.compute(
        [FileDelta("tests/a.py", t_add, t_rem), FileDelta("src/a.py", c_add, c_rem)],
        cr.RatioSettings(budget, floor),
    )

    assert result["ratio"] == ratio
    assert result["over_budget"] is over
    assert result["budget"] == budget
    assert result["test"]["net"] == t_add - t_rem and result["code"]["net"] == c_add - c_rem
    assert ("does not block" in result["line"]) is over
    assert result["line"].endswith(f": {verdict}.")


def test_documentation_counts_for_neither_side():
    result = cr.compute([FileDelta("docs/a.md", 900, 0), FileDelta("README.md", 50, 0)])

    assert result["test"]["net"] == 0 == result["code"]["net"]
    assert result["over_budget"] is False


def test_a_profile_glob_makes_a_path_a_test_through_the_shared_rule():
    files = [FileDelta("spec/x.rb", 300, 0), FileDelta("lib/x.rb", 10, 0)]

    plain = cr.compute(files)
    globbed = cr.compute(files, test_globs=("spec/**",))

    assert plain["test"]["net"] == 0 and plain["code"]["net"] == 310
    assert globbed["test"]["net"] == 300 and globbed["code"]["net"] == 10
    assert globbed["over_budget"] is True


# --- the two profile keys ---------------------------------------------------


def _profile(test_block):
    return parse_profile_document({"class": "internal", "test": test_block})


def test_the_defaults_apply_without_a_profile_or_keys():
    for profile in (None, parse_profile_document({"class": "internal"}), _profile({"full": ["x", "{junit}"]})):
        settings = cr.settings_from_profile(profile)
        assert (settings.warn, settings.floor_lines, settings.warnings) == (2, 100, ())


def test_valid_keys_are_used():
    settings = cr.settings_from_profile(_profile({"ratio_warn": 3, "ratio_floor_lines": 40}))

    assert (settings.warn, settings.floor_lines, settings.warnings) == (3, 40, ())


@pytest.mark.parametrize("bad", [0, -1, "2", 2.5, True, [], {}])
def test_an_unusable_value_falls_back_with_a_warning_naming_the_key(bad):
    profile = _profile({"ratio_warn": bad, "ratio_floor_lines": bad})

    settings = cr.settings_from_profile(profile)
    result = cr.compute([FileDelta("tests/a.py", 1, 0)], settings)

    assert (settings.warn, settings.floor_lines) == (2, 100)
    assert any("test.ratio_warn" in w for w in settings.warnings)
    assert any("test.ratio_floor_lines" in w for w in settings.warnings)
    assert len(result["warnings"]) == 2


@pytest.mark.parametrize(
    ("block", "named"),
    [
        ({"ratio_warn": 0}, ["test.ratio_warn"]),
        ({"ratio_floor_lines": "x"}, ["test.ratio_floor_lines"]),
        ({"ratio_warn": 0, "ratio_floor_lines": -1}, ["test.ratio_warn", "test.ratio_floor_lines"]),
        ({"ratio_warn": 3}, []),
    ],
)
def test_unreadable_counts_still_name_an_invalid_key(block, named):
    result = source.from_host(
        _Backend(error=AcquireFetchError("HTTP 500")),
        _profile(block),
        owner="o",
        repo="r",
        base_sha="a" * 40,
        head_sha="b" * 40,
    )

    assert result["ratio"] == "unavailable"
    assert [key for key in named if any(key in w for w in result.get("warnings", []))] == named
    assert len(result.get("warnings", [])) == len(named)


def test_a_zero_value_still_makes_the_block_declared_so_its_warning_is_seen():
    assert parse_test_block({"ratio_warn": 0}).declared_keys() == ["ratio_warn"]


# --- the verb over real repositories ------------------------------------------


def _repo_with_change(tmp_path, profile_text="class: internal\n"):
    repo = make_repo(tmp_path)
    commit_files(repo, {PROFILE_PATH: profile_text, "src/a.py": "x\n"})
    run_git(repo, "checkout", "-q", "-b", "change")
    commit_files(
        repo,
        {
            "src/a.py": "x\n" + "y\n" * 10,
            "tests/test_a.py": "t\n" * 300,
            "spec/x.rb": "s\n" * 5,
            "docs/a.md": "d\n" * 50,
        },
    )
    return repo


def _run(repo, capsys, *extra):
    code = cv.main(["ratio", "--repo-path", str(repo), "--base", "main", *extra])
    return code, json.loads(capsys.readouterr().out)


def test_the_verb_states_the_ratio_of_the_change_against_the_base(tmp_path, capsys):
    repo = _repo_with_change(tmp_path)

    code, out = _run(repo, capsys)

    assert code == 0
    assert out["test"] == {"added": 300, "removed": 0, "net": 300}
    assert out["code"] == {"added": 15, "removed": 0, "net": 15}
    assert out["over_budget"] is True and "does not block" in out["line"]


def test_the_profile_at_the_base_supplies_test_paths_and_the_head_cannot_change_them(tmp_path, capsys):
    repo = _repo_with_change(
        tmp_path, "class: internal\ntest:\n  paths: ['spec/**']\n  ratio_floor_lines: 10\n"
    )
    run_git(repo, "checkout", "-q", "change")
    commit_files(repo, {PROFILE_PATH: "class: internal\ntest:\n  ratio_floor_lines: 100000\n"})

    code, out = _run(repo, capsys)

    assert code == 0
    assert out["test"]["net"] == 305
    assert out["code"]["net"] == 9  # +10 in src, -1 for the profile edit itself
    assert out["over_budget"] is True


def test_a_binary_file_counts_no_lines(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"src/a.py": "x\n"})
    run_git(repo, "checkout", "-q", "-b", "change")
    (repo / "src" / "blob.bin").write_bytes(b"\x00\x01\x02" * 100)
    run_git(repo, "add", "--", "src/blob.bin")
    run_git(repo, "commit", "-q", "-m", "chore: blob")

    _, out = _run(repo, capsys)

    assert out["code"] == {"added": 0, "removed": 0, "net": 0}


def test_a_renamed_test_file_nets_to_zero(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"tests/test_old.py": "t\n" * 50})
    run_git(repo, "checkout", "-q", "-b", "change")
    run_git(repo, "mv", "tests/test_old.py", "tests/test_new.py")
    run_git(repo, "commit", "-q", "-m", "chore: rename")

    _, out = _run(repo, capsys)

    assert out["test"] == {"added": 50, "removed": 50, "net": 0}
    assert out["code"]["net"] == 0


def test_a_branch_behind_its_base_counts_only_its_own_lines(tmp_path, capsys):
    repo = make_repo(tmp_path)
    commit_files(repo, {"src/a.py": "x\n"})
    run_git(repo, "checkout", "-q", "-b", "change")
    commit_files(repo, {"src/b.py": "b\n" * 7, "tests/test_b.py": "t\n" * 20})
    run_git(repo, "checkout", "-q", "main")
    commit_files(repo, {"src/late.py": "l\n" * 500, "tests/test_late.py": "t\n" * 900})
    run_git(repo, "checkout", "-q", "change")

    code, out = _run(repo, capsys)

    assert code == 0
    assert out["code"] == {"added": 7, "removed": 0, "net": 7}
    assert out["test"] == {"added": 20, "removed": 0, "net": 20}


def test_an_unreadable_revision_is_unavailable_with_a_reason_and_exit_zero(tmp_path, capsys):
    repo = _repo_with_change(tmp_path)

    code = cv.main(["ratio", "--repo-path", str(repo), "--base", "no-such-rev"])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["ratio"] == "unavailable" and out["reason"] and out["over_budget"] is False
    assert out["test"] is None and out["code"] is None


def test_the_umbrella_routes_the_subcommand_and_help_names_it(tmp_path, capsys):
    repo = _repo_with_change(tmp_path)

    assert umbrella_main(["changeset", "ratio", "--repo-path", str(repo), "--base", "main"]) == 0
    assert json.loads(capsys.readouterr().out)["test"]["net"] == 300
    assert cv.main(["ratio", "--help"]) == 0
    assert "--base" in capsys.readouterr().out


def test_a_revision_that_looks_like_an_option_is_a_usage_error(tmp_path, capsys):
    assert cv.main(["ratio", "--repo-path", str(tmp_path), "--base", "--output=x"]) == cv.EXIT_USAGE


# --- the host source ---------------------------------------------------------------


class _Backend:
    def __init__(self, span=None, error=None):
        self.span, self.error = span, error

    def fetch_range_diff(self, *, owner, repo, base_sha, head_sha):
        if self.error:
            raise self.error
        return self.span


DIFF = """\
diff --git a/src/a.py b/src/a.py
--- a/src/a.py
+++ b/src/a.py
@@ -1,1 +1,2 @@
 keep
+new
diff --git a/tests/test_a.py b/tests/test_a.py
new file mode 100644
--- /dev/null
+++ b/tests/test_a.py
@@ -0,0 +1,3 @@
+a
+b
+c
"""


def test_the_host_compare_gives_the_same_object():
    backend = _Backend(RangeDiff("a" * 40, "b" * 40, True, DIFF))

    result = source.from_host(
        backend, None, owner="o", repo="r", base_sha="a" * 40, head_sha="b" * 40
    )

    assert result["test"]["net"] == 3 and result["code"]["net"] == 1
    assert result["over_budget"] is False


@pytest.mark.parametrize(
    "backend",
    [
        _Backend(error=AcquireFetchError("HTTP 500")),
        _Backend(RangeDiff("a", "b", False)),
        object(),
    ],
)
def test_a_host_compare_that_fails_is_unavailable_with_a_reason(backend):
    result = source.from_host(backend, None, owner="o", repo="r", base_sha="a" * 40, head_sha="b" * 40)

    assert result["ratio"] == "unavailable" and result["reason"]
    assert result["over_budget"] is False
