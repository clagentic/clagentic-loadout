"""session.launch -- write a built layer to the loadout runtime dir, and resolve
the runtime command `launch` execs.

The only path this module writes is ``<runtime dir>/session/<role>.json``
(runtime dir ``~/.clagentic/loadout/``, mode 0700; file mode 0600, replaced
atomically). It never writes a user-level settings file or a file in a repository.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from clagentic_loadout.session.config import DEFAULT_RUNTIME_COMMAND

SESSION_SUBDIR = "session"
_RUNTIME_PARTS = (".clagentic", "loadout")
_ROLE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

SOURCE_FLAG = "the --runtime flag"
SOURCE_CONFIG = "session_profile.runtime_command"
SOURCE_DEFAULT = "the default"


class RuntimeNotFoundError(LookupError):
    """The runtime command is not on PATH."""


def valid_role(role: str) -> bool:
    """Roles are opaque keys, but one becomes a file name, so it is a plain token."""
    return bool(_ROLE_PATTERN.match(role)) and ".." not in role


def runtime_dir(base: str | Path | None = None) -> Path:
    return Path(base) if base is not None else Path(os.path.expanduser("~")).joinpath(*_RUNTIME_PARTS)


def _ensure_private_dirs(base: Path) -> Path:
    """Create the runtime dir and its session subdir with mode 0700."""
    target = base / SESSION_SUBDIR
    for directory in (base.parent, base, target):
        if not directory.exists():
            directory.mkdir(mode=0o700)
            directory.chmod(0o700)
    return target


def write_layer(layer: dict[str, Any], role: str, *, base: str | Path | None = None) -> Path:
    """Write *layer* to ``<runtime dir>/session/<role>.json`` and return its
    absolute path. Raises OSError when the file cannot be written."""
    root = runtime_dir(base).absolute()
    root.parent.parent.mkdir(parents=True, exist_ok=True)
    target_dir = _ensure_private_dirs(root)
    final = target_dir / f"{role}.json"
    fd, tmp_name = tempfile.mkstemp(dir=target_dir, prefix=f".{role}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(layer, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, final)
    except BaseException:
        Path(tmp_name).unlink(missing_ok=True)
        raise
    return final


def resolve_runtime(
    flag: str | None,
    config_value: str | None,
    *,
    which: Callable[[str], str | None] = shutil.which,
) -> tuple[str, str]:
    """(absolute path, command) of the runtime. The command is the flag, else the
    config value, else ``claude``; a command not on PATH raises
    RuntimeNotFoundError naming the command and where it came from."""
    if flag:
        command, source = flag, SOURCE_FLAG
    elif config_value:
        command, source = config_value, SOURCE_CONFIG
    else:
        command, source = DEFAULT_RUNTIME_COMMAND, SOURCE_DEFAULT
    found = which(command)
    if not found:
        raise RuntimeNotFoundError(
            f"runtime command {command!r} (from {source}) is not on PATH"
        )
    return found, command
