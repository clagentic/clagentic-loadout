"""repo_test.permission — the one optional permission rule for ``test report``.

``clagentic-loadout test report`` is read-only and takes run ids, never paths, so
an agent session can be allowed to run it without a prompt. This module adds
exactly one rule, ``REPORT_RULE``, to the ``permissions.allow`` list of a Claude
Code settings file, and nothing else. It is opt-in: the installer calls it only
when asked (``--register-test-report-permission``), and a person can run it by
hand with ``clagentic-loadout test register-permission``.

The settings file is parsed as JSON and written back whole, so its other
contents are preserved but its formatting is normalised to two-space indent.
A file that does not parse, or whose ``permissions`` or ``allow`` has an
unexpected type, is refused and left untouched, as is a symlink.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

from clagentic_loadout.review.atomic_io import write_bytes_atomic

REPORT_RULE = "Bash(clagentic-loadout test report *)"
SETTINGS_ENV_VAR = "CLAGENTIC_LOADOUT_CLAUDE_SETTINGS"
CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"

RESULT_ADDED = "added"
RESULT_PRESENT = "already-present"
RESULT_WOULD_ADD = "would-add"


class SettingsError(Exception):
    """The settings file cannot be updated safely. The message names the file."""


def default_settings_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    explicit = environ.get(SETTINGS_ENV_VAR, "").strip()
    if explicit:
        return Path(explicit).expanduser()
    config_dir = environ.get(CONFIG_DIR_ENV_VAR, "").strip()
    base = Path(config_dir).expanduser() if config_dir else Path.home() / ".claude"
    return base / "settings.json"


def register_report_permission(settings_path: Path, *, dry_run: bool = False) -> str:
    """Make sure ``REPORT_RULE`` is in the allow list of *settings_path*. Returns
    ``added``, ``already-present``, or ``would-add`` (dry run)."""
    if settings_path.is_symlink():
        raise SettingsError(f"{str(settings_path)!r} is a symlink; refusing to write through it")
    mode = 0o600
    if settings_path.exists():
        if not settings_path.is_file():
            raise SettingsError(f"{str(settings_path)!r} exists and is not a regular file")
        try:
            document = json.loads(settings_path.read_text(encoding="utf-8"))
            mode = settings_path.stat().st_mode & 0o777
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SettingsError(f"cannot read {str(settings_path)!r} as JSON: {exc}") from exc
    else:
        document = {}
    if not isinstance(document, dict):
        raise SettingsError(f"{str(settings_path)!r} is not a JSON object")
    permissions = document.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise SettingsError(f"`permissions` in {str(settings_path)!r} is not an object")
    allow = permissions.setdefault("allow", [])
    if not isinstance(allow, list):
        raise SettingsError(f"`permissions.allow` in {str(settings_path)!r} is not a list")
    if REPORT_RULE in allow:
        return RESULT_PRESENT
    if dry_run:
        return RESULT_WOULD_ADD
    allow.append(REPORT_RULE)
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        write_bytes_atomic(settings_path, (json.dumps(document, indent=2) + "\n").encode("utf-8"), mode=mode)
    except OSError as exc:
        raise SettingsError(f"cannot write {str(settings_path)!r}: {exc}") from exc
    return RESULT_ADDED


__all__ = [
    "CONFIG_DIR_ENV_VAR",
    "REPORT_RULE",
    "RESULT_ADDED",
    "RESULT_PRESENT",
    "RESULT_WOULD_ADD",
    "SETTINGS_ENV_VAR",
    "SettingsError",
    "default_settings_path",
    "register_report_permission",
]
