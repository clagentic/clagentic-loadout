"""test_guard_credential_paths.py — Read/Glob/Bash credential-path denial
(lr-fd279d, port of the reference deployment's guard-credentials.py;
lr-5a8d epic, slice 1).

Conformance (CLAUDE.md rule 6a): synthetic paths only, no LORE present, no
real machine/operator identifiers — protected-prefix tests use an invented
"/synthetic-home/" prefix rather than a hardcoded "/root/" assumption
(the whole point of this module vs. the reference deployment).
"""

from __future__ import annotations

from clagentic_loadout.guard.credential_paths import (
    DEFAULT_BASH_CREDENTIAL_PATTERNS,
    DEFAULT_CREDENTIAL_DENY_LIST,
    check_bash_command,
    check_glob_call,
    check_read_path,
    is_valid_bak_path,
)


# ---------------------------------------------------------------------------
# Read — happy paths
# ---------------------------------------------------------------------------


class TestCheckReadPathAllows:
    def test_allows_ordinary_source_path(self):
        ok, reason = check_read_path("/workspace/some-project/src/module.py")
        assert ok, reason

    def test_allows_ordinary_tmp_path(self):
        ok, reason = check_read_path("/tmp/some-agent-log.txt")
        assert ok, reason

    def test_empty_path_allowed_noop(self):
        ok, reason = check_read_path("")
        assert ok, reason


# ---------------------------------------------------------------------------
# Read — deny paths (deny-list substring)
# ---------------------------------------------------------------------------


class TestCheckReadPathDeniesDenyList:
    def test_netrc_denied(self):
        ok, reason = check_read_path("/synthetic-home/.netrc")
        assert not ok
        assert "netrc" in reason.lower()

    def test_git_credentials_denied(self):
        ok, reason = check_read_path("/synthetic-home/.git-credentials")
        assert not ok

    def test_inject_credentials_denied(self):
        ok, reason = check_read_path("/workspace/scripts/inject_credentials.py")
        assert not ok


# ---------------------------------------------------------------------------
# Read — protected home prefixes (caller-supplied, not hardcoded /root/)
# ---------------------------------------------------------------------------


class TestCheckReadPathProtectedPrefixes:
    def test_path_under_protected_prefix_denied(self):
        ok, reason = check_read_path(
            "/synthetic-home/some-other-file.txt",
            protected_home_prefixes=("/synthetic-home",),
        )
        assert not ok
        assert "protected prefix" in reason

    def test_path_outside_protected_prefix_allowed(self):
        ok, reason = check_read_path(
            "/workspace/project/file.py",
            protected_home_prefixes=("/synthetic-home",),
        )
        assert ok, reason

    def test_no_protected_prefixes_configured_never_denies_on_that_basis(self):
        # A caller that supplies no protected_home_prefixes gets no
        # home-directory protection at all -- this module never assumes one.
        ok, reason = check_read_path("/synthetic-home/anything.txt")
        assert ok, reason

    def test_explicit_exact_allow_overrides_protected_prefix(self):
        ok, reason = check_read_path(
            "/synthetic-home/.config/app/settings.json",
            protected_home_prefixes=("/synthetic-home",),
            allowed_exact_paths=("/synthetic-home/.config/app/settings.json",),
        )
        assert ok, reason

    def test_bak_prefix_allow_overrides_protected_prefix(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        bak_path = base + "20260101"
        ok, reason = check_read_path(
            bak_path,
            protected_home_prefixes=(str(tmp_path),),
            allowed_bak_prefixes=(base,),
        )
        assert ok, reason

    def test_bak_directory_as_prefix_attack_denied(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        (tmp_path / "settings.json.bak-evil").mkdir()
        bad_path = base + "evil/anything"
        ok, reason = check_read_path(
            bad_path,
            protected_home_prefixes=(str(tmp_path),),
            allowed_bak_prefixes=(base,),
        )
        assert not ok

    def test_bak_traversal_attack_denied(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        bad_path = base + "../../etc/shadow"
        ok, reason = check_read_path(
            bad_path,
            protected_home_prefixes=(str(tmp_path),),
            allowed_bak_prefixes=(base,),
        )
        assert not ok


# ---------------------------------------------------------------------------
# is_valid_bak_path — direct unit coverage (lr-9c39 tightened suffix rules)
# ---------------------------------------------------------------------------


class TestIsValidBakPath:
    def test_valid_timestamp_suffix_nonexistent_path_allowed(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        assert is_valid_bak_path(base + "9999999999", base) is True

    def test_directory_suffix_rejected(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        (tmp_path / "settings.json.bak-evil").mkdir()
        assert is_valid_bak_path(base + "evil/anything", base) is False

    def test_traversal_suffix_rejected(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        assert is_valid_bak_path(base + "../../etc/shadow", base) is False

    def test_similar_but_different_name_rejected(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        assert is_valid_bak_path(str(tmp_path / "settings.json.backup"), base) is False

    def test_existing_directory_rejected(self, tmp_path):
        base = str(tmp_path / "settings.json.bak-")
        real_dir = tmp_path / "settings.json.bak-realdir"
        real_dir.mkdir()
        assert is_valid_bak_path(str(real_dir), base) is False


# ---------------------------------------------------------------------------
# Glob
# ---------------------------------------------------------------------------


class TestCheckGlobCall:
    def test_allows_legitimate_pattern(self):
        ok, reason = check_glob_call("**/*.py", "/workspace/some-project")
        assert ok, reason

    def test_denies_netrc_pattern(self):
        ok, reason = check_glob_call("*.netrc", "")
        assert not ok

    def test_denies_path_under_protected_prefix(self):
        ok, reason = check_glob_call(
            "**/*.py", "/synthetic-home", protected_home_prefixes=("/synthetic-home",)
        )
        assert not ok

    def test_denies_pattern_containing_git_credentials(self):
        ok, reason = check_glob_call("**/.git-credentials", "")
        assert not ok


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------


class TestCheckBashCommand:
    def test_allows_ordinary_command(self):
        ok, reason = check_bash_command("git log --oneline -10")
        assert ok, reason

    def test_allows_netrc_as_cli_flag(self):
        # curl --netrc is a legitimate auth flag, not a credential-file path
        # reference — the path-anchored pattern must not false-positive here.
        ok, reason = check_bash_command("curl --netrc https://example.invalid/api")
        assert ok, reason

    def test_denies_cat_of_netrc_file(self):
        ok, reason = check_bash_command("cat /synthetic-home/.netrc")
        assert not ok

    def test_denies_env_var_expansion_of_netrc(self):
        ok, reason = check_bash_command('grep -r token "$HOME/.netrc"')
        assert not ok

    def test_denies_inject_credentials_invocation(self):
        ok, reason = check_bash_command("python3 /workspace/scripts/inject_credentials.py")
        assert not ok

    def test_empty_command_allowed_noop(self):
        ok, reason = check_bash_command("")
        assert ok, reason


# ---------------------------------------------------------------------------
# Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_default_deny_list_covers_required_entries(self):
        required = [".netrc", "netrc", ".git-credentials", "git-credentials", "inject_credentials"]
        for entry in required:
            assert entry in DEFAULT_CREDENTIAL_DENY_LIST

    def test_default_bash_patterns_nonempty(self):
        assert len(DEFAULT_BASH_CREDENTIAL_PATTERNS) >= 3
