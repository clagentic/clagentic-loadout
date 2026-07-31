"""test_repo_config.py — unit tests for clagentic_loadout.repo_config
(lr-446c35, operator-ratified 2026-07-11; wrapper-hop discovery lr-18f46a).

Covers:
  - DEFAULT_CONFIG_RELATIVE_PATH points at the new .clagentic/loadout/
    config.yaml home; LEGACY_CONFIG_RELATIVE_PATH is the pre-migration path.
  - resolve_repo_config_path resolution order: new path wins when present
    (regardless of whether legacy also exists); legacy path is read (with a
    one-line deprecation warning to stderr) only when new is absent and
    legacy exists; neither present returns the new path unconditionally,
    with no warning.
  - the `warn=False` opt-out silences the deprecation warning.
  - config_relative_path/legacy_relative_path overrides are honored (mainly
    exercised indirectly by each section-owning loader's own tests, but
    covered directly here too).
  - resolve_repo_config_root / resolve_repo_config_path bounded wrapper-hop
    (lr-18f46a): a config file one level above a repo's own git top-level
    (the wrapper layout) is found; a repo whose OWN git top-level carries
    its own config file never climbs (self-contained dead end); no config
    anywhere in the single-hop chain leaves the ORIGINAL repo_root's default
    behavior unchanged; a --repo-path-style caller override (an explicit
    repo_root argument) still wins over the hop.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_MARKER,
    LEGACY_CONFIG_RELATIVE_PATH,
    find_git_top_level_down_hop,
    resolve_repo_config_path,
    resolve_repo_config_root,
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


class TestConstants:
    def test_default_path_is_the_clagentic_home(self):
        assert DEFAULT_CONFIG_RELATIVE_PATH == ".clagentic/loadout/config.yaml"

    def test_legacy_path_is_the_pre_migration_home(self):
        assert LEGACY_CONFIG_RELATIVE_PATH == ".loadout/config.yaml"

    def test_legacy_marker_is_the_bare_legacy_dir_name(self):
        assert LEGACY_CONFIG_MARKER == ".loadout"


class TestResolveRepoConfigPathNeitherPresent:
    def test_returns_new_path_unconditionally(self, tmp_path):
        resolved = resolve_repo_config_path(tmp_path)
        assert resolved == tmp_path / DEFAULT_CONFIG_RELATIVE_PATH

    def test_no_warning_when_neither_present(self, tmp_path, capsys):
        resolve_repo_config_path(tmp_path)
        assert capsys.readouterr().err == ""


class TestResolveRepoConfigPathNewOnly:
    def test_returns_new_path(self, tmp_path):
        new_dir = tmp_path / ".clagentic" / "loadout"
        new_dir.mkdir(parents=True)
        (new_dir / "config.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(tmp_path)

        assert resolved == new_dir / "config.yaml"

    def test_no_warning(self, tmp_path, capsys):
        new_dir = tmp_path / ".clagentic" / "loadout"
        new_dir.mkdir(parents=True)
        (new_dir / "config.yaml").write_text("wait: {}\n")

        resolve_repo_config_path(tmp_path)

        assert capsys.readouterr().err == ""


class TestResolveRepoConfigPathLegacyOnly:
    def test_returns_legacy_path(self, tmp_path):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(tmp_path)

        assert resolved == legacy_dir / "config.yaml"

    def test_one_line_deprecation_warning_to_stderr(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        legacy_path = legacy_dir / "config.yaml"
        legacy_path.write_text("wait: {}\n")

        resolve_repo_config_path(tmp_path)

        stderr = capsys.readouterr().err
        assert stderr.count("\n") == 1
        assert str(legacy_path) in stderr
        assert "deprecated" in stderr
        assert str(tmp_path / DEFAULT_CONFIG_RELATIVE_PATH) in stderr

    def test_warn_false_suppresses_the_warning(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(tmp_path, warn=False)

        assert resolved == legacy_dir / "config.yaml"
        assert capsys.readouterr().err == ""


class TestResolveRepoConfigPathBothPresent:
    def test_new_path_wins(self, tmp_path):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("wait: {}\n")
        new_dir = tmp_path / ".clagentic" / "loadout"
        new_dir.mkdir(parents=True)
        (new_dir / "config.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(tmp_path)

        assert resolved == new_dir / "config.yaml"

    def test_no_warning_when_new_path_wins(self, tmp_path, capsys):
        legacy_dir = tmp_path / ".loadout"
        legacy_dir.mkdir()
        (legacy_dir / "config.yaml").write_text("wait: {}\n")
        new_dir = tmp_path / ".clagentic" / "loadout"
        new_dir.mkdir(parents=True)
        (new_dir / "config.yaml").write_text("wait: {}\n")

        resolve_repo_config_path(tmp_path)

        assert capsys.readouterr().err == ""


class TestResolveRepoConfigPathOverrides:
    def test_custom_config_relative_path_honored(self, tmp_path):
        alt_dir = tmp_path / "alt"
        alt_dir.mkdir()
        (alt_dir / "custom.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(
            tmp_path, config_relative_path="alt/custom.yaml"
        )

        assert resolved == alt_dir / "custom.yaml"

    def test_custom_legacy_relative_path_honored(self, tmp_path):
        legacy_alt_dir = tmp_path / "legacy-alt"
        legacy_alt_dir.mkdir()
        (legacy_alt_dir / "custom.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(
            tmp_path,
            config_relative_path="does-not-exist.yaml",
            legacy_relative_path="legacy-alt/custom.yaml",
        )

        assert resolved == legacy_alt_dir / "custom.yaml"

    def test_str_repo_root_accepted(self, tmp_path):
        resolved = resolve_repo_config_path(str(tmp_path))
        assert resolved == Path(tmp_path) / DEFAULT_CONFIG_RELATIVE_PATH


# ---------------------------------------------------------------------------
# Bounded wrapper-hop discovery (lr-18f46a)
# ---------------------------------------------------------------------------


class TestResolveRepoConfigRootWrapperHop:
    def test_wrapper_hop_found(self, tmp_path):
        """Wrapper carries the config; the inner git repo (its own git top
        level) does not -- the hop must climb exactly one level and find it."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        config_dir = wrapper / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        (config_dir / "config.yaml").write_text("wait: {}\n")

        resolved_root = resolve_repo_config_root(repo, DEFAULT_CONFIG_RELATIVE_PATH)

        assert resolved_root == wrapper.resolve()

    def test_wrapper_hop_found_via_resolve_repo_config_path(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        config_dir = wrapper / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.yaml"
        config_file.write_text("wait: {}\n")

        resolved = resolve_repo_config_path(repo)

        assert resolved == config_file

    def test_self_contained_repo_with_own_config_never_climbs(self, tmp_path):
        """The git top-level (== repo_root here) already has its OWN config
        file -- a self-contained repo is a dead end, even though a DIFFERENT
        config also exists one level up in the wrapper."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)

        own_config_dir = repo / ".clagentic" / "loadout"
        own_config_dir.mkdir(parents=True)
        own_config_file = own_config_dir / "config.yaml"
        own_config_file.write_text("wait: {}\n")

        wrapper_config_dir = wrapper / ".clagentic" / "loadout"
        wrapper_config_dir.mkdir(parents=True)
        (wrapper_config_dir / "config.yaml").write_text("wait: {}\n")

        resolved = resolve_repo_config_path(repo)

        assert resolved == own_config_file

    def test_no_config_anywhere_in_the_chain_returns_original_root(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)

        resolved = resolve_repo_config_path(repo)

        assert resolved == repo.resolve() / DEFAULT_CONFIG_RELATIVE_PATH

    def test_no_config_anywhere_returns_repo_root_unhopped(self, tmp_path):
        """resolve_repo_config_root's own no-match contract: falls back to
        the ORIGINAL repo_root, never the git top-level or wrapper, when
        neither candidate carries the file."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)

        resolved_root = resolve_repo_config_root(repo, DEFAULT_CONFIG_RELATIVE_PATH)

        assert resolved_root == repo.resolve()

    def test_repo_path_override_wins_over_hop(self, tmp_path):
        """A --repo-path-style caller override is just a different repo_root
        argument -- passing the wrapper directly (instead of the inner repo)
        makes step 1 itself find the config, without ever needing the hop."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        config_dir = wrapper / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.yaml"
        config_file.write_text("wait: {}\n")

        resolved = resolve_repo_config_path(wrapper)

        assert resolved == config_file

    def test_non_git_directory_with_no_config_is_unaffected(self, tmp_path):
        """A plain (non-git) directory with no config anywhere -- the hop's
        find_git_top_level lookup fails closed (no git repo found) and
        resolve_repo_config_root falls back to repo_root, matching the
        pre-lr-18f46a no-git-anchor behavior."""
        plain_dir = tmp_path / "plain"
        plain_dir.mkdir()

        resolved = resolve_repo_config_path(plain_dir)

        assert resolved == plain_dir / DEFAULT_CONFIG_RELATIVE_PATH

    def test_legacy_config_found_via_wrapper_hop(self, tmp_path):
        """The hop also finds the LEGACY path at the wrapper, applying the
        same deprecation-warning fallback as the un-hopped case."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        legacy_dir = wrapper / ".loadout"
        legacy_dir.mkdir()
        legacy_file = legacy_dir / "config.yaml"
        legacy_file.write_text("wait: {}\n")

        resolved = resolve_repo_config_path(repo, warn=False)

        assert resolved == legacy_file

    def test_git_top_level_itself_with_own_config_is_used_without_hop(self, tmp_path):
        """repo_root is a SUBDIRECTORY of the git repo (not the top level
        itself); the git top level (not repo_root, not the wrapper) carries
        the config -- step 2 of the hop must find it."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        subdir = repo / "src"
        subdir.mkdir()
        config_dir = repo / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "config.yaml"
        config_file.write_text("wait: {}\n")

        resolved = resolve_repo_config_path(subdir)

        assert resolved == config_file


# ---------------------------------------------------------------------------
# Bounded DOWN-hop discovery (lr-c17040 SECONDARY, mirroring lr-18f46a's
# UP-hop for a caller that genuinely needs a LOCAL repo root resolved from a
# wrapper cwd with no .git of its own).
# ---------------------------------------------------------------------------


class TestResolveRepoConfigRootResolvedContract:
    """lr-329d27 (deferred review nit from lr-18f46a): every return path of
    `resolve_repo_config_root` must yield a RESOLVED Path, uniformly -- not
    conditionally on whether repo_root existed on disk at call time."""

    def test_existing_repo_root_with_no_match_returns_resolved_path(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)

        resolved_root = resolve_repo_config_root(repo, DEFAULT_CONFIG_RELATIVE_PATH)

        assert resolved_root == repo.resolve()
        assert resolved_root.is_absolute()

    def test_nonexistent_repo_root_with_no_match_returns_resolved_path(self, tmp_path):
        missing = tmp_path / "does-not-exist"

        resolved_root = resolve_repo_config_root(missing, DEFAULT_CONFIG_RELATIVE_PATH)

        assert resolved_root == missing.resolve()
        assert resolved_root.is_absolute()

    def test_nonexistent_repo_root_matches_existing_repo_root_semantics(self, tmp_path):
        """Same relative shape, one existing on disk and one not -- both
        must return an equally-resolved Path (the inconsistency this task
        fixes: an existing repo_root used to resolve, a missing one did
        not)."""
        existing = tmp_path / "existing"
        existing.mkdir()
        missing = tmp_path / "missing"

        resolved_existing = resolve_repo_config_root(existing, DEFAULT_CONFIG_RELATIVE_PATH)
        resolved_missing = resolve_repo_config_root(missing, DEFAULT_CONFIG_RELATIVE_PATH)

        assert resolved_existing == existing.resolve()
        assert resolved_missing == missing.resolve()
        assert resolved_existing.is_absolute()
        assert resolved_missing.is_absolute()


class TestFindGitTopLevelDownHop:
    def test_finds_the_single_contained_repo(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)

        found = find_git_top_level_down_hop(wrapper)

        assert found == repo.resolve()

    def test_returns_none_when_start_is_itself_a_repo(self, tmp_path):
        """A caller already inside a repo should use find_git_top_level
        directly -- this hop is for the 'not in a repo at all' case only."""
        repo = tmp_path / "repo"
        _init_repo(repo)

        assert find_git_top_level_down_hop(repo) is None

    def test_returns_none_when_start_is_a_subdir_of_a_repo(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        subdir = repo / "src"
        subdir.mkdir()

        assert find_git_top_level_down_hop(subdir) is None

    def test_returns_none_with_zero_contained_repos(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        wrapper.mkdir()
        (wrapper / "not-a-repo").mkdir()

        assert find_git_top_level_down_hop(wrapper) is None

    def test_returns_none_with_multiple_contained_repos_ambiguous(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        _init_repo(wrapper / "repo-a")
        _init_repo(wrapper / "repo-b")

        assert find_git_top_level_down_hop(wrapper) is None

    def test_never_descends_more_than_one_level(self, tmp_path):
        """A repo nested two levels down (wrapper/outer/repo, where 'outer'
        is a plain, non-repo directory) must NOT be found -- the hop is
        bounded to exactly one level, never a recursive descent."""
        wrapper = tmp_path / "wrapper"
        outer = wrapper / "outer"
        repo = outer / "repo"
        _init_repo(repo)

        assert find_git_top_level_down_hop(wrapper) is None

    def test_skips_dotdirs_as_candidates(self, tmp_path):
        wrapper = tmp_path / "wrapper"
        wrapper.mkdir()
        dotdir_repo = wrapper / ".hidden-repo"
        _init_repo(dotdir_repo)

        assert find_git_top_level_down_hop(wrapper) is None

    def test_nonexistent_start_returns_none(self, tmp_path):
        assert find_git_top_level_down_hop(tmp_path / "does-not-exist") is None

    def test_start_that_is_a_file_returns_none(self, tmp_path):
        a_file = tmp_path / "a-file"
        a_file.write_text("x")
        assert find_git_top_level_down_hop(a_file) is None
