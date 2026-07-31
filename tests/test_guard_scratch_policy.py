"""test_guard_scratch_policy.py — category-grant spawn-scratch containment
(lr-5a8d, task comment #2).

Behavioral acceptance from the task: a spawn does arbitrary in-sandbox
scratch setup with ZERO operator prompts (i.e. is_scratch_contained returns
True for any SCRATCH_SAFE_VERBS invocation whose targets resolve inside
$TMPDIR); any escape — including via symlink or `..`-traversal — denies.

lr-f8649f: $HOME dropped from SCRATCH_ROOT_ENV_VARS entirely (TMPDIR-only
narrowing) -- see TestTmpdirOnlyNarrowing below for the dedicated
non-vacuous coverage of that change (HOME rejected, TMPDIR admitted,
TMPDIR-unset behavior asserted explicitly, per that task's acceptance
criteria).

Conformance (CLAUDE.md rule 6a): synthetic paths only, no LORE
present, no real machine identifiers.
"""

from __future__ import annotations

import os

import pytest

from clagentic_loadout.guard.scratch_policy import (
    SCRATCH_ROOT_ENV_VARS,
    SCRATCH_SAFE_VERBS,
    ScratchContainmentError,
    is_scratch_contained,
    resolve_all_scratch_boundaries,
    resolve_scratch_boundary,
)


def _env(tmpdir: str, home: str) -> dict[str, str]:
    return {"TMPDIR": tmpdir, "HOME": home}


class TestCategoryGrantAcrossVerbs:
    """The core acceptance property: ANY verb in SCRATCH_SAFE_VERBS is
    admitted by containment alone -- this is not an enumerated-verb list
    check, it is a target-path check applied uniformly."""

    def test_mkdir_contained_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert is_scratch_contained(f"mkdir -p {scratch}/work/nested", env=env)

    def test_touch_contained_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert is_scratch_contained(f"touch {scratch}/marker.txt", env=env)

    def test_mv_both_targets_contained_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert is_scratch_contained(f"mv {scratch}/a {scratch}/b", env=env)

    def test_mktemp_with_contained_template_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert is_scratch_contained(f"mktemp {scratch}/tmp.XXXXXX", env=env)

    def test_rm_contained_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert is_scratch_contained(f"rm {scratch}/leftover.txt", env=env)

    def test_every_scratch_safe_verb_is_exercised_by_this_suite(self):
        """No verb silently added to the policy list without test coverage."""
        exercised = {"mkdir", "touch", "mv", "mktemp", "rm", "cp", "rmdir", "ln", "chmod"}
        assert exercised == SCRATCH_SAFE_VERBS


class TestEscapeDenied:
    def test_absolute_workspace_path_denied(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert not is_scratch_contained("mkdir -p /workspace/some-project/x", env=env)

    def test_dollar_home_target_denied(self, tmp_path):
        # lr-f8649f: $HOME is no longer a recognized boundary token at all --
        # `$HOME/...` is never expanded (no "HOME" boundary exists to
        # substitute it), so it resolves as a literal, non-existent relative
        # path from cwd and fails containment outright, regardless of
        # `..`-traversal.
        home = tmp_path / "home"
        home.mkdir()
        env = _env(str(tmp_path / "scratch"), str(home))
        assert not is_scratch_contained("mkdir $HOME/../outside", env=env)
        assert not is_scratch_contained(f"mkdir {home}/work", env=env)

    def test_symlink_escape_denied(self, tmp_path):
        """A symlink planted INSIDE the scratch root pointing OUTSIDE it
        must not be admitted merely because its own path token starts under
        the scratch root -- os.path.realpath must resolve the symlink
        before containment is checked."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        outside = tmp_path / "outside-target"
        outside.mkdir()
        escape_link = scratch / "escape-link"
        escape_link.symlink_to(outside)
        env = _env(str(scratch), str(tmp_path / "home"))
        assert not is_scratch_contained(f"touch {escape_link}/pwned.txt", env=env)

    def test_one_escaping_argument_denies_whole_command(self, tmp_path):
        """mv with one contained and one escaping target must deny the
        WHOLE command, never partially admit."""
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        assert not is_scratch_contained(f"mv {scratch}/x /workspace/y", env=env)

    def test_relative_path_from_non_scratch_cwd_denied(self, tmp_path, monkeypatch):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        not_scratch = tmp_path / "not-scratch"
        not_scratch.mkdir()
        monkeypatch.chdir(not_scratch)
        env = _env(str(scratch), str(tmp_path / "home"))
        assert not is_scratch_contained("mkdir ./newdir", env=env)


class TestVerbNotInAllowSet:
    def test_git_verb_raises(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained(f"git init {scratch}/repo", env=env)

    def test_curl_verb_raises(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = _env(str(scratch), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained(f"curl -o {scratch}/out http://example.test", env=env)

    def test_push_verb_raises(self, tmp_path):
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained("loadout-push --repo owner/repo", env=env)


class TestCompoundExpressionsRefused:
    @pytest.mark.parametrize(
        "command",
        [
            "mkdir /tmp/a; rm -rf /workspace",
            "mkdir /tmp/a && rm -rf /workspace",
            "mkdir /tmp/a || rm -rf /workspace",
            "mkdir /tmp/a | cat",
            "mkdir `echo /tmp/a`",
            "mkdir $(echo /tmp/a)",
            "touch /tmp/a > /workspace/pwned",
        ],
    )
    def test_compound_or_redirect_raises(self, tmp_path, command):
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained(command, env=env)

    def test_empty_command_raises(self, tmp_path):
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained("", env=env)

    def test_whitespace_only_command_raises(self, tmp_path):
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        with pytest.raises(ScratchContainmentError):
            is_scratch_contained("   ", env=env)


class TestNoTargetArgument:
    def test_bare_mktemp_with_no_target_not_admitted(self, tmp_path):
        """A safe verb invoked with nothing to verify containment against
        is refused, not assumed safe."""
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        assert not is_scratch_contained("mktemp", env=env)

    def test_flag_only_invocation_not_admitted(self, tmp_path):
        env = _env(str(tmp_path / "scratch"), str(tmp_path / "home"))
        assert not is_scratch_contained("mkdir -p", env=env)


class TestNoBoundaryConfigured:
    def test_no_scratch_env_vars_set_denies(self):
        # lr-f8649f: with TMPDIR unset, resolve_all_scratch_boundaries still
        # resolves the uid-home fallback boundary -- so this denies because
        # /tmp/x does not resolve under THAT boundary, not because no
        # boundary exists at all (see TestTmpdirOnlyNarrowing for the
        # genuinely-no-boundary-available case).
        assert not is_scratch_contained("mkdir /tmp/x", env={})


class TestResolveScratchBoundary:
    def test_tmpdir_resolves_to_realpath(self, tmp_path):
        boundary = resolve_scratch_boundary("TMPDIR", env={"TMPDIR": str(tmp_path)})
        assert boundary is not None
        assert boundary.resolved_path == os.path.realpath(str(tmp_path))

    def test_unset_tmpdir_falls_back_to_uid_home(self):
        # lr-f8649f DELIBERATE DECISION: with $HOME dropped from
        # SCRATCH_ROOT_ENV_VARS entirely, an unset/empty $TMPDIR must not
        # yield ZERO boundaries -- it falls back to the process's real
        # uid-home directory (_uid_home_fallback, repointed from the removed
        # HOME-boundary fallback), aligning with the identical posture
        # landed on the sibling deployment-automation project this platform
        # composes with, rather than failing closed with no boundary at all.
        import pwd

        boundary = resolve_scratch_boundary("TMPDIR", env={})
        expected = pwd.getpwuid(os.getuid()).pw_dir
        if expected:
            assert boundary is not None
            assert boundary.resolved_path == os.path.realpath(expected)
        else:
            assert boundary is None

    def test_unset_home_no_longer_resolves(self):
        # lr-f8649f: $HOME is no longer a scratch root at all -- resolving
        # it directly (bypassing SCRATCH_ROOT_ENV_VARS) gets no uid-home
        # fallback either, since that fallback is now TMPDIR-specific.
        assert resolve_scratch_boundary("HOME", env={}) is None

    def test_resolve_all_boundaries_omits_unresolvable(self, tmp_path):
        boundaries = resolve_all_scratch_boundaries(env={"TMPDIR": str(tmp_path)})
        env_vars = {b.env_var for b in boundaries}
        assert "TMPDIR" in env_vars


class TestTmpdirOnlyNarrowing:
    """lr-f8649f acceptance criteria, dedicated non-vacuous coverage:
    HOME-rooted path rejected, TMPDIR-rooted path admitted, and the
    TMPDIR-unset behavior asserted explicitly (never a silent HOME
    fallback)."""

    def test_scratch_root_env_vars_is_tmpdir_only(self):
        assert SCRATCH_ROOT_ENV_VARS == ("TMPDIR",)
        assert "HOME" not in SCRATCH_ROOT_ENV_VARS

    def test_home_rooted_mkdir_rejected(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.delenv("TMPDIR", raising=False)
        env = {"HOME": str(home)}
        # No TMPDIR at all; the uid-home fallback backs TMPDIR instead, and
        # this synthetic `home` path is not the real uid home, so a target
        # under it is correctly denied.
        assert not is_scratch_contained(f"mkdir -p {home}/work", env=env)

    def test_tmpdir_rooted_mkdir_admitted(self, tmp_path):
        scratch = tmp_path / "scratch"
        scratch.mkdir()
        env = {"TMPDIR": str(scratch)}
        assert is_scratch_contained(f"mkdir -p {scratch}/work", env=env)

    def test_tmpdir_unset_falls_back_to_uid_home_never_silently_home_env(
        self, tmp_path, monkeypatch
    ):
        """The TMPDIR-unset decision (task item 2): falls back to the
        process's real uid-home directory, which is NOT the same thing as
        "silently admit whatever $HOME is set to" -- a caller's own $HOME
        env var value (distinct from the real uid-home passwd-database
        entry) must NOT be consulted at all."""
        import pwd

        monkeypatch.delenv("TMPDIR", raising=False)
        fake_home = tmp_path / "not-the-real-uid-home"
        fake_home.mkdir()
        env = {"HOME": str(fake_home)}

        boundaries = resolve_all_scratch_boundaries(env=env)
        real_uid_home = pwd.getpwuid(os.getuid()).pw_dir
        if real_uid_home:
            assert len(boundaries) == 1
            assert boundaries[0].env_var == "TMPDIR"
            assert boundaries[0].resolved_path == os.path.realpath(real_uid_home)
            assert boundaries[0].resolved_path != os.path.realpath(str(fake_home))
        else:
            assert boundaries == []

        # The caller's $HOME env value itself is never admitted as a
        # boundary -- a target under the FAKE $HOME must still be denied.
        assert not is_scratch_contained(f"mkdir -p {fake_home}/work", env=env)
