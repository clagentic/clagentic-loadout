"""Classification of a change as docs-only or code: the pure function, the paths a
host serves, the profile block that declares the allowlist, and the
`loadout-changeset classify` verb over real git repositories.

The allowlist is read from the base commit only, so a change that edits the
profile can never widen the list it is judged by."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from clagentic_loadout._version import get_version
from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.review import changeset_verb as cv
from clagentic_loadout.review import docs_only_policy as dop
from clagentic_loadout.review.changeset_paths import UNREADABLE_PATH, from_diff_text
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.repo_profile import ProfileFormatError, parse_profile_document
from tests._review_cli_support import make_diff
from tests._review_evaluate_support import commit_files, make_repo, run_git

PROFILE = """\
class: released-tool
review:
  docs_only:
    paths: ["docs/**/*.md", "README.md", "docs/build.sh", "docs/data.json"]
    depended_on: ["AGENTS.md", "docs/README.md"]
"""

SOURCE = dop.AllowlistSource("a" * 40, PROFILE_PATH, dop.STATE_OK)
POLICY = dop.DocsOnlyPolicy(
    paths=("docs/**/*.md", "README.md", "docs/build.sh", "docs/data.json", "AGENTS.md"),
    depended_on=("AGENTS.md", "docs/README.md"),
)


def _classify(*paths: str, policy=POLICY, modes=None):
    changed = [dop.ChangedPath(p, (modes or {}).get(p, ())) for p in paths]
    return dop.classify_changeset(changed, policy, SOURCE)


class TestProfileBlock:
    def test_the_block_parses_into_the_two_lists(self):
        profile = parse_profile_document(
            {"class": "internal", "review": {"docs_only": {"paths": ["docs/**"], "depended_on": ["x.md"]}}}
        )
        assert profile.docs_only == dop.DocsOnlyPolicy(("docs/**",), ("x.md",))

    def test_a_profile_without_the_block_declares_no_allowlist(self):
        assert parse_profile_document({"class": "internal"}).docs_only is None
        assert parse_profile_document({"class": "internal", "review": {}}).docs_only is None

    @pytest.mark.parametrize(
        "block",
        [
            "docs",
            {"other": {}},
            {"docs_only": ["docs/**"]},
            {"docs_only": {"paths": "docs/**"}},
            {"docs_only": {"paths": ["docs/**"], "extra": []}},
            {"docs_only": {"paths": [""]}},
            {"docs_only": {"paths": ["/etc/**"]}},
            {"docs_only": {"depended_on": ["../x"]}},
            {"docs_only": {"paths": [3]}},
        ],
    )
    def test_a_block_outside_the_documented_shape_makes_the_profile_invalid(self, block):
        with pytest.raises(ProfileFormatError):
            parse_profile_document({"class": "internal", "review": block})


class TestClassification:
    def test_paths_on_the_allowlist_are_docs_only(self):
        result = _classify("docs/guide.md", "README.md", "docs/sub/more.md")
        assert result.kind == "docs-only" and result.is_docs_only
        assert {item.kind for item in result.files} == {"docs"}
        assert result.source == SOURCE

    def test_one_path_off_the_allowlist_makes_the_change_code(self):
        result = _classify("docs/guide.md", "notes/other.txt")
        assert result.kind == "code"
        assert [(i.path, i.reason) for i in result.code_files()] == [
            ("notes/other.txt", "not on the docs allowlist")
        ]

    def test_a_listed_instruction_file_is_code_even_when_the_paths_glob_matches_it(self):
        result = _classify("docs/guide.md", "AGENTS.md", "docs/README.md")
        assert result.kind == "code"
        assert {i.path for i in result.code_files()} == {"AGENTS.md", "docs/README.md"}
        assert all("depended on" in i.reason for i in result.code_files())

    @pytest.mark.parametrize(
        "path,fragment",
        [
            ("docs/data.json", "configuration or data"),
            ("docs/build.sh", "script or source"),
            ("docs/Makefile", "script or source"),
            ("docs/conf.yaml", "configuration or data"),
            ("docs/pre-commit", "hook"),
            ("docs/hooks/readme.md", "hook"),
            ("docs/.hidden.md", "dotfile"),
            (".github/workflows/ci.md", "CI or host workflow"),
            (".clagentic/loadout/notes.md", "loadout configuration"),
            (".clagentic/loadout/profile.yaml", "loadout configuration"),
        ],
    )
    def test_a_built_in_code_class_wins_over_a_broad_allowlist(self, path, fragment):
        policy = dop.DocsOnlyPolicy(paths=("**",))
        result = dop.classify_changeset([dop.ChangedPath(path)], policy, SOURCE)
        assert result.kind == "code"
        assert fragment in result.files[0].reason

    @pytest.mark.parametrize("mode", ["100755", "120000", "160000"])
    def test_an_executable_symlink_or_submodule_entry_is_code(self, mode):
        result = _classify("docs/guide.md", modes={"docs/guide.md": ("100644", mode)})
        assert result.kind == "code"
        assert mode in result.files[0].reason

    @pytest.mark.parametrize("mode", ["100755", "120000", "160000"])
    def test_a_later_plain_entry_for_the_same_path_does_not_erase_a_special_mode(self, mode):
        special = dop.ChangedPath("docs/guide.md", (mode,))
        plain = dop.ChangedPath("docs/guide.md", ("100644",))

        for order in ([special, plain], [plain, special]):
            result = dop.classify_changeset(order, POLICY, SOURCE)
            assert result.kind == "code"
            [only] = result.files
            assert only.kind == "code" and mode in only.reason

    def test_a_path_is_docs_only_when_every_one_of_its_entries_is(self):
        entries = [dop.ChangedPath("docs/guide.md", ("100644",)), dop.ChangedPath("docs/guide.md")]

        result = dop.classify_changeset(entries, POLICY, SOURCE)

        assert result.is_docs_only and len(result.files) == 1

    def test_a_plain_mode_is_not_special(self):
        assert _classify("docs/guide.md", modes={"docs/guide.md": ("100644", "100644")}).is_docs_only

    @pytest.mark.parametrize("policy", [None, dop.DocsOnlyPolicy(), dop.DocsOnlyPolicy(depended_on=("x",))])
    def test_no_allowlist_means_nothing_is_docs_only(self, policy):
        result = _classify("docs/guide.md", "README.md", policy=policy)
        assert result.kind == "code"
        assert all("declares no docs allowlist" in i.reason for i in result.files)

    def test_a_change_with_no_paths_is_code(self):
        assert _classify().kind == "code"

    @pytest.mark.parametrize("path", ["", "/docs/a.md", "docs/../a.md", "docs//a.md", "docs/./a.md"])
    def test_a_path_that_is_not_repository_relative_is_code(self, path):
        assert _classify("docs/guide.md", path).kind == "code"

    def test_the_answer_does_not_depend_on_the_order_of_the_paths(self):
        forward = _classify("docs/a.md", "README.md", "docs/b.md")
        backward = _classify("docs/b.md", "README.md", "docs/a.md")
        assert forward == backward

    def test_a_leading_dot_slash_is_the_same_path(self):
        assert _classify("./docs/guide.md").is_docs_only


class TestPathsFromADiff:
    def test_each_file_of_the_diff_is_a_path(self):
        entries = from_diff_text(make_diff({"docs/a.md": 2, "src/b.py": 1}))
        assert sorted(e.path for e in entries) == ["docs/a.md", "src/b.py"]
        assert all("100644" in e.modes for e in entries)

    def test_a_rename_names_the_old_path_as_well_as_the_new_one(self):
        diff = (
            "diff --git a/src/old.py b/docs/new.md\n"
            "similarity index 100%\n"
            "rename from src/old.py\n"
            "rename to docs/new.md\n"
        )
        assert sorted(e.path for e in from_diff_text(diff)) == ["docs/new.md", "src/old.py"]

    def test_a_mode_change_is_seen_on_a_file_with_no_content_change(self):
        diff = "diff --git a/docs/a.md b/docs/a.md\nold mode 100644\nnew mode 100755\n"
        [entry] = from_diff_text(diff)
        assert entry.path == "docs/a.md" and set(entry.modes) == {"100644", "100755"}

    def test_the_mode_on_the_index_line_of_a_content_change_is_seen(self):
        diff = (
            "diff --git a/docs/a.md b/docs/a.md\n"
            "index 1111111..2222222 100755\n"
            "--- a/docs/a.md\n+++ b/docs/a.md\n@@ -1 +1 @@\n-a\n+b\n"
        )
        [entry] = from_diff_text(diff)
        assert entry.modes == ("100755",)
        assert _classify("docs/a.md", modes={"docs/a.md": entry.modes}).kind == "code"

    def test_a_quoted_name_is_code_because_it_cannot_be_read(self):
        diff = 'diff --git "a/docs/caf\\303\\251.md" "b/docs/caf\\303\\251.md"\n'
        assert [e.path for e in from_diff_text(diff)] == [UNREADABLE_PATH]
        assert _classify(UNREADABLE_PATH).kind == "code"

    def test_a_diff_without_git_headers_yields_nothing(self):
        assert from_diff_text("--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n") == []


def _repo(tmp_path, profile: str = PROFILE):
    repo = make_repo(tmp_path)
    base = commit_files(
        repo,
        {
            PROFILE_PATH: profile,
            "README.md": "readme\n",
            "docs/guide.md": "guide\n",
            "docs/README.md": "index\n",
            "AGENTS.md": "agents\n",
            "src/app.py": "x = 1\n",
        },
    )
    run_git(repo, "checkout", "-q", "-b", "feature")
    return repo, base


def _classify_verb(capsys, repo, *extra):
    capsys.readouterr()
    code = cv.main(["classify", "--repo-path", str(repo), "--base", "main", *extra])
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return code, (json.loads(lines[-1]) if lines else None), captured.err


class TestVerb:
    def test_a_change_to_paths_on_the_bases_allowlist_is_docs_only_with_its_source(
        self, tmp_path, capsys
    ):
        repo, base = _repo(tmp_path)
        commit_files(repo, {"docs/guide.md": "changed\n", "docs/new.md": "new\n", "README.md": "r2\n"})

        code, result, _ = _classify_verb(capsys, repo)

        assert code == cv.EXIT_OK
        assert result["result"] == "docs-only" == result["classification"]
        assert result["allowlist_source"] == {"base_sha": base, "path": PROFILE_PATH, "state": "ok"}
        assert result["base"] == base
        assert {f["path"] for f in result["files"]} == {"docs/guide.md", "docs/new.md", "README.md"}

    def test_one_agent_file_among_docs_makes_the_change_code(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {"docs/guide.md": "changed\n", "AGENTS.md": "changed\n"})

        code, result, _ = _classify_verb(capsys, repo)

        assert code == cv.EXIT_OK
        assert result["result"] == "code"
        [code_file] = [f for f in result["files"] if f["class"] == "code"]
        assert code_file["path"] == "AGENTS.md" and "depended on" in code_file["reason"]

    def test_a_change_that_edits_the_allowlist_is_judged_by_the_base_and_is_code(
        self, tmp_path, capsys
    ):
        repo, base = _repo(tmp_path)
        # The change widens the list to everything and adds a file the base's list
        # does not cover, so the widened list would call it documentation.
        widened = PROFILE.replace('paths: ["docs/**/*.md"', 'paths: ["**", "docs/**/*.md"')
        commit_files(repo, {PROFILE_PATH: widened, "src/extra.txt": "x\n"})

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert result["allowlist_source"]["base_sha"] == base
        reasons = {f["path"]: f["reason"] for f in result["files"]}
        assert "loadout configuration" in reasons[PROFILE_PATH]
        assert reasons["src/extra.txt"] == "not on the docs allowlist"

    def test_a_profile_edit_alone_is_code_even_if_the_widened_list_names_nothing_else(
        self, tmp_path, capsys
    ):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {PROFILE_PATH: PROFILE + "# a comment\n"})

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"

    def test_the_list_is_read_at_the_base_commit_not_the_working_tree(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {"docs/guide.md": "changed\n"})
        (repo / PROFILE_PATH).write_text("class: released-tool\n", encoding="utf-8")

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "docs-only"

    def test_a_base_with_no_profile_has_no_allowlist_so_the_change_is_code(self, tmp_path, capsys):
        repo = make_repo(tmp_path)
        base = commit_files(repo, {"docs/guide.md": "guide\n"})
        run_git(repo, "checkout", "-q", "-b", "feature")
        commit_files(repo, {"docs/guide.md": "changed\n"})

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert result["allowlist_source"] == {"base_sha": base, "path": PROFILE_PATH, "state": "missing"}

    def test_a_base_profile_without_the_block_is_reported_as_absent(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path, "class: released-tool\n")
        commit_files(repo, {"docs/guide.md": "changed\n"})

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert result["allowlist_source"]["state"] == "absent"

    def test_an_invalid_base_profile_gives_no_allowlist(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path, "class: released-tool\nbogus: 1\n")
        commit_files(repo, {"docs/guide.md": "changed\n"})

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert result["allowlist_source"]["state"] == "invalid"

    def test_only_what_the_head_adds_since_the_merge_base_is_classified(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {"docs/guide.md": "changed\n"})
        run_git(repo, "checkout", "-q", "main")
        moved = commit_files(repo, {"src/other.py": "y = 2\n"})
        run_git(repo, "checkout", "-q", "feature")

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "docs-only"
        assert result["allowlist_source"]["base_sha"] == moved
        assert [f["path"] for f in result["files"]] == ["docs/guide.md"]

    def test_a_file_made_executable_is_code(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {"docs/guide.md": "changed\n"})
        run_git(repo, "update-index", "--chmod=+x", "--", "docs/guide.md")
        run_git(repo, "commit", "-q", "-m", "chore: mode")

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert "100755" in result["files"][0]["reason"]

    def test_a_rename_out_of_code_into_docs_is_code(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        os.replace(repo / "src/app.py", repo / "docs/app.md")
        run_git(repo, "add", "-A", "--", "src", "docs")
        run_git(repo, "commit", "-q", "-m", "chore: move")

        _, result, _ = _classify_verb(capsys, repo)

        assert result["result"] == "code"
        assert {f["path"] for f in result["files"]} == {"src/app.py", "docs/app.md"}

    def test_the_output_is_deterministic(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        commit_files(repo, {"docs/b.md": "b\n", "docs/a.md": "a\n"})

        first = _classify_verb(capsys, repo)
        second = _classify_verb(capsys, repo)

        assert first == second

    def test_head_defaults_to_head_and_can_be_named(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)
        tip = commit_files(repo, {"docs/guide.md": "changed\n"})

        _, default, _ = _classify_verb(capsys, repo)
        _, named, _ = _classify_verb(capsys, repo, "--head", tip)

        assert default == named and default["head"] == tip

    def test_an_unknown_revision_exits_62_naming_it(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)

        code, result, err = _classify_verb(capsys, repo, "--head", "no-such-ref")

        assert code == cv.EXIT_IO_FAILED == 62
        assert result is None
        assert "no-such-ref" in err and str(repo) in err

    def test_a_directory_that_is_not_a_repository_exits_62(self, tmp_path, capsys):
        code, _, err = _classify_verb(capsys, tmp_path)

        assert code == 62 and str(tmp_path) in err

    def test_a_revision_that_looks_like_an_option_is_a_usage_error(self, tmp_path, capsys):
        repo, _ = _repo(tmp_path)

        code, _, err = _classify_verb(capsys, repo, "--head=--exec=x")

        assert code == cv.EXIT_USAGE == 1
        assert "--head must be a revision" in err

class TestVerbConformance:
    def test_help_and_version_exit_ok(self, capsys):
        assert cv.main(["--help"]) == 0
        assert "classify" in capsys.readouterr().out
        assert cv.main(["--version"]) == 0
        assert get_version() in capsys.readouterr().out
        assert cv.main(["classify", "--help"]) == 0
        assert "--base" in capsys.readouterr().out

    def test_a_missing_required_argument_is_a_usage_error(self, capsys):
        assert cv.main(["classify"]) == cv.EXIT_USAGE
        assert cv.main([]) == cv.EXIT_USAGE

    def test_the_umbrella_routes_to_the_verb(self, capsys):
        assert umbrella_main(["changeset", "--help"]) == 0
        assert "classify" in capsys.readouterr().out
        assert umbrella_main(["changeset", "classify", "--help"]) == 0
        assert umbrella_main(["--help"]) == 0
        assert "changeset" in capsys.readouterr().out

    def test_the_verb_is_a_console_script(self):
        from pathlib import Path

        text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text(
            encoding="utf-8"
        )
        assert 'loadout-changeset = "clagentic_loadout.review.changeset_verb:main"' in text

    def test_the_module_runs_as_a_script(self):
        src = os.path.join(os.path.dirname(__file__), "..", "src")
        done = subprocess.run(
            ["python3", "-m", "clagentic_loadout.review.changeset_verb", "--version"],
            capture_output=True,
            text=True,
            env={**os.environ, "PYTHONPATH": src},
        )
        assert done.returncode == 0 and get_version() in done.stdout
