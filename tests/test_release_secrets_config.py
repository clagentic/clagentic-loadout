"""
test_release_secrets_config.py — tests for
clagentic_loadout.release.secrets_config (lr-51d4, Wave A slice 6).

Coverage:
  - read_role_env_file: happy path, mode-600 enforcement, path-traversal
    guard on the role/name token, malformed-line and missing-key failures,
    export-prefix and quoted-value parsing.
  - DEFAULT_CONFIG_ROOT rebases the source's ~/.config/crew/agents/ convention
    onto ~/.config/clagentic/loadout/roles/ (tome #687 §12 config-path rebase).
  - _resolve_home_dir (lr-e84ae1): the HOME-empty-vs-HOME-unset chicken-and-
    egg fix -- `Path.home()` alone degrades correctly when HOME is UNSET but
    NOT when HOME is present-and-empty, which silently sent every user-level
    config-root resolution (this module's own DEFAULT_CONFIG_ROOT, and
    transport.provider_config.DEFAULT_USER_CONFIG_ROOT derived from it) to a
    root-relative path instead of the real home directory.
"""

from __future__ import annotations

import os

import pytest

from clagentic_loadout.release.secrets_config import (
    DEFAULT_CONFIG_ROOT,
    SecretEnvError,
    _resolve_home_dir,
    read_role_env_file,
)


def _write_env_file(path, content: str, *, mode: int = 0o600) -> None:
    path.write_text(content)
    os.chmod(path, mode)


class TestDefaultConfigRoot:
    def test_default_root_is_loadout_standard_path(self):
        """No `crew` config-dir literal anywhere in the resolved path
        (tome #687 §12)."""
        parts = DEFAULT_CONFIG_ROOT.parts
        assert "clagentic" in parts
        assert "loadout" in parts
        assert "roles" in parts
        assert "crew" not in parts


class TestResolveHomeDir:
    """lr-e84ae1: HOME='' (present but empty) must resolve the SAME as HOME
    being entirely unset -- both fall through to the passwd-database lookup,
    never to Path.home()'s own empty-string short-circuit (PosixPath('/'))."""

    def test_normal_home_env_used_directly(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert _resolve_home_dir() == tmp_path

    def test_empty_home_env_falls_back_to_passwd_lookup(self, monkeypatch):
        import os
        import pwd

        monkeypatch.setenv("HOME", "")
        expected = pwd.getpwuid(os.getuid()).pw_dir
        resolved = _resolve_home_dir()
        assert str(resolved) == expected
        # The specific regression this closes: HOME='' must never resolve to
        # a root-relative path (Path.home()'s own empty-string behavior).
        assert str(resolved) != "/"

    def test_unset_home_env_falls_back_to_passwd_lookup(self, monkeypatch):
        import os
        import pwd

        monkeypatch.delenv("HOME", raising=False)
        expected = pwd.getpwuid(os.getuid()).pw_dir
        assert str(_resolve_home_dir()) == expected

    def test_passwd_lookup_failure_falls_back_to_path_home(self, monkeypatch):
        import pwd

        monkeypatch.setenv("HOME", "")

        def _raise_keyerror(_uid):
            raise KeyError("no passwd entry")

        monkeypatch.setattr(pwd, "getpwuid", _raise_keyerror)
        # Must not raise -- degrades to Path.home()'s own behavior rather
        # than propagating the passwd lookup failure.
        _resolve_home_dir()


class TestReadRoleEnvFile:
    def test_happy_path_returns_parsed_kvs(self, tmp_path):
        env_file = tmp_path / "release-dispatcher.env"
        _write_env_file(env_file, "STATUS_HOOK_SECRET=hunter2\n")

        kvs = read_role_env_file(
            "release-dispatcher", ("STATUS_HOOK_SECRET",), config_root=tmp_path
        )
        assert kvs == {"STATUS_HOOK_SECRET": "hunter2"}

    def test_export_prefix_is_stripped(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "export STATUS_HOOK_SECRET=hunter2\n")

        kvs = read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)
        assert kvs["STATUS_HOOK_SECRET"] == "hunter2"

    def test_quoted_value_is_unquoted(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, 'STATUS_HOOK_SECRET="hunter2 with spaces"\n')

        kvs = read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)
        assert kvs["STATUS_HOOK_SECRET"] == "hunter2 with spaces"

    def test_comments_and_blank_lines_skipped(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(
            env_file,
            "# a comment\n\nSTATUS_HOOK_SECRET=hunter2\n",
        )

        kvs = read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)
        assert kvs["STATUS_HOOK_SECRET"] == "hunter2"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(SecretEnvError, match="not found"):
            read_role_env_file("nope", ("STATUS_HOOK_SECRET",), config_root=tmp_path)

    def test_group_readable_file_refused(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "STATUS_HOOK_SECRET=hunter2\n", mode=0o640)

        with pytest.raises(SecretEnvError, match="insecure permissions"):
            read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)

    def test_world_readable_file_refused(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "STATUS_HOOK_SECRET=hunter2\n", mode=0o604)

        with pytest.raises(SecretEnvError, match="insecure permissions"):
            read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)

    def test_missing_required_key_raises(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "OTHER_KEY=x\n")

        with pytest.raises(SecretEnvError, match="missing required key"):
            read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)

    def test_malformed_line_raises(self, tmp_path):
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "not-a-valid-line\n")

        with pytest.raises(SecretEnvError, match="malformed line"):
            read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)

    def test_path_traversal_role_name_rejected(self, tmp_path):
        with pytest.raises(SecretEnvError, match="invalid characters"):
            read_role_env_file(
                "../../etc/passwd", ("STATUS_HOOK_SECRET",), config_root=tmp_path
            )

    def test_path_separator_role_name_rejected(self, tmp_path):
        with pytest.raises(SecretEnvError, match="invalid characters"):
            read_role_env_file(
                "some/role", ("STATUS_HOOK_SECRET",), config_root=tmp_path
            )

    def test_secret_value_never_in_exception_message(self, tmp_path):
        """A missing-key failure must not leak any OTHER key's value present
        in the file (defense in depth: exception text names only the missing
        key, never file contents)."""
        env_file = tmp_path / "role.env"
        _write_env_file(env_file, "UNRELATED_SECRET=super-sekret-value\n")

        with pytest.raises(SecretEnvError) as exc_info:
            read_role_env_file("role", ("STATUS_HOOK_SECRET",), config_root=tmp_path)
        assert "super-sekret-value" not in str(exc_info.value)

    def test_trailing_newline_role_name_rejected(self, tmp_path):
        """lr-3e3318: _SAFE_NAME_RE re-anchored with \\A...\\Z (was ^...$,
        which in Python without re.MULTILINE also matches just before a
        trailing newline) -- 'role\\n' must be rejected, not silently
        accepted as 'role'."""
        with pytest.raises(SecretEnvError, match="invalid characters"):
            read_role_env_file(
                "role\n", ("STATUS_HOOK_SECRET",), config_root=tmp_path
            )

    def test_leading_newline_role_name_rejected(self, tmp_path):
        with pytest.raises(SecretEnvError, match="invalid characters"):
            read_role_env_file(
                "\nrole", ("STATUS_HOOK_SECRET",), config_root=tmp_path
            )
