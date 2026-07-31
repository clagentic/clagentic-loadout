"""settings_path.py — parameterized harness settings-file location (lr-4e04).

The allowlist generator's `--write` path needs to know WHERE a consuming
harness's settings file lives before it can merge a fragment into it. That
location is never hardcoded: resolution precedence, highest first —

    1. --settings-file PATH (explicit CLI flag)
    2. CLAGENTIC_LOADOUT_SETTINGS_FILE (env var override)
    3. HOME-derived default: ~/.config/clagentic/loadout/settings.json —
       the same ~/.config/clagentic/loadout/ root every other loadout
       user-level config already uses (provider_config.DEFAULT_USER_CONFIG_ROOT,
       secrets_config.DEFAULT_CONFIG_ROOT).

Empty-HOME fail-fast (mirrors scripts/install.sh's lr-e8cc discipline,
ported to the Python side of the provisioning contract): an agent-spawn
environment that never sets HOME is a real, expected caller, not a
misconfiguration — but silently resolving `${HOME:-}/.config/...` down to a
root-relative path succeeds while writing nowhere useful, which is worse
than refusing outright. This module refuses with a resolved-values error
(conformance rule 4) whenever HOME is empty/unset AND neither
--settings-file nor CLAGENTIC_LOADOUT_SETTINGS_FILE compensates.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Env var override for the settings-file path (precedence tier 2). Same
#: CLAGENTIC_LOADOUT_* namespace every other override in this package uses.
SETTINGS_FILE_ENV_VAR = "CLAGENTIC_LOADOUT_SETTINGS_FILE"

#: HOME-derived default settings-file path, relative to $HOME (precedence
#: tier 3) — under the same ~/.config/clagentic/loadout/ root
#: provider_config.DEFAULT_USER_CONFIG_ROOT and secrets_config.
#: DEFAULT_CONFIG_ROOT already use, so this is not a fourth loadout config
#: root invented for one verb.
DEFAULT_SETTINGS_FILE_RELATIVE = Path(".config") / "clagentic" / "loadout" / "settings.json"


class SettingsPathError(ValueError):
    """Raised when no settings-file path can be resolved — HOME is empty/
    unset and no override compensates. Reports every resolved input
    (explicit flag, env var, HOME) rather than a stale/generic guess."""


def resolve_settings_path(
    explicit: str | None = None,
    *,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the harness settings-file path.

    Args:
        explicit: --settings-file flag value, highest precedence.
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.

    Raises:
        SettingsPathError: no explicit path, no env override, and HOME is
            empty/unset — refuses rather than silently resolving a
            root-relative path (the lr-e8cc discipline).
    """
    active_env = env if env is not None else dict(os.environ)

    if explicit:
        return Path(explicit)

    env_override = active_env.get(SETTINGS_FILE_ENV_VAR)
    if env_override:
        return Path(env_override)

    home = active_env.get("HOME", "")
    if not home:
        raise SettingsPathError(
            f"HOME is empty or unset, and neither --settings-file nor "
            f"{SETTINGS_FILE_ENV_VAR} was given to compensate. resolved: "
            f"explicit={explicit!r} {SETTINGS_FILE_ENV_VAR}="
            f"{env_override!r} HOME={home!r}. Refusing to fall back to a "
            f"root-relative path (e.g. /.config/clagentic/loadout/"
            f"settings.json) — that would succeed silently while writing "
            f"nowhere useful. Fix one of: set HOME to a real, writable "
            f"directory; pass --settings-file PATH; set "
            f"{SETTINGS_FILE_ENV_VAR}."
        )

    return Path(home) / DEFAULT_SETTINGS_FILE_RELATIVE


__all__ = [
    "DEFAULT_SETTINGS_FILE_RELATIVE",
    "SETTINGS_FILE_ENV_VAR",
    "SettingsPathError",
    "resolve_settings_path",
]
