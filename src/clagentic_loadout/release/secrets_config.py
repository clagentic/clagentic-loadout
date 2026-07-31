"""secrets_config.py — loadout-standard role-scoped .env reader.

Reads a role-scoped .env secret file under the loadout config root
(``~/.config/clagentic/loadout/roles/<role>.env``), per the loadout
config-path standard (repo CLAUDE.md hard rule 3 / CLI-NAMING-STANDARD.md).
This is a rebase of a prior per-agent secret-env convention (tome #687 §12
— "default caller becomes a role, not a name") onto that standard.

This module is deliberately narrow: it is release.dispatch's ONE secret-file
reader, not a general credentials client. It carries only the security
properties dispatch.py's caller actually needs (mode-600 refusal,
path-traversal-safe role name, never logs/echoes values) — it does not port
any broker login flow or credential-minting machinery, which belong to the
credentials seam (tome #687 §3), not the release-event seam.

File format: ``KEY=VALUE`` / ``export KEY=VALUE`` lines, '#' comments,
optional quoting. One key is read here: STATUS_HOOK_SECRET.
"""

from __future__ import annotations

import os
import pwd
import re
import stat
from pathlib import Path


def _resolve_home_dir() -> Path:
    """Resolve the invoking user's home directory WITHOUT the `HOME`-unset-
    equals-`HOME`-empty chicken-and-egg (lr-e84ae1).

    `Path.home()` degrades correctly when `HOME` is entirely UNSET (falls
    through to a passwd-database lookup, `pwd.getpwuid(os.getuid()).pw_dir`)
    but NOT when `HOME` is present and set to the EMPTY STRING: CPython's
    `Path.home()` treats `os.environ.get("HOME")` as authoritative whenever
    the key exists at all, so `HOME=''` (falsy, but present) short-circuits
    straight to `PosixPath('/')` instead of falling through to the same
    passwd lookup an unset `HOME` would trigger. An isolated-HOME spawn
    harness that clears `HOME` to `''` (rather than deleting the var
    outright) hits exactly this gap -- confirmed by direct reproduction
    (`HOME=''` -> `Path.home() == PosixPath('/')`, verified against this same
    interpreter/host that reports `pwd.getpwuid(os.getuid()).pw_dir ==
    '/root'` correctly).

    This module's own `DEFAULT_CONFIG_ROOT` (and every user-level config
    root derived from it -- see `transport.provider_config.
    DEFAULT_USER_CONFIG_ROOT`) is evaluated ONCE at import time, so a wrong
    value here silently sends every user-level config-file read (the
    lr-52d7 post_merge_env deployment-override tier included) to the wrong
    directory for the lifetime of the process -- never a loud failure, just
    a config file that "doesn't exist" at the resolved (wrong) path. Falling
    through to the passwd-database lookup whenever `HOME` is falsy (empty OR
    unset) closes that gap without requiring every caller to pre-populate
    `HOME` before this package is ever imported -- the same class of
    forward-reference problem the lr-52d7 seam itself exists to avoid (a
    deployment cannot always guarantee `HOME` is populated before its own
    provisioning of that very value is read).

    Never raises: if even the passwd lookup fails (no matching passwd entry
    for the current uid -- an unusual, container-without-passwd-entry
    scenario), falls through to `Path.home()`'s own behavior one last time
    so this helper degrades no worse than the stdlib would have.
    """
    if os.environ.get("HOME"):
        return Path.home()
    try:
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (KeyError, OSError):
        return Path.home()


#: Loadout per-repo/per-role secret-env root (CLI-NAMING-STANDARD.md config
#: convention, repo CLAUDE.md hard rule 3). No `crew` config-dir literal.
#: Resolved via `_resolve_home_dir` (lr-e84ae1), not a bare `Path.home()`
#: call -- see that helper's docstring for the HOME-empty-vs-unset gap this
#: closes.
DEFAULT_CONFIG_ROOT = _resolve_home_dir() / ".config" / "clagentic" / "loadout" / "roles"

#: Default role whose .env is read when the caller supplies no override.
#: A role, not an agent name (tome #687 §12) — the generic
#: release-dispatching role, not any specific caller's identity.
DEFAULT_ROLE = "release-dispatcher"

#: Bare role/name token only: alphanumeric, hyphen, underscore, 1-64 chars.
#: Rejects any value containing path separators that could traverse outside
#: the configured roles directory.
#:
#: Anchored with \A...\Z, not ^...$ (lr-3e3318, sibling fix alongside
#: transport.credential_provider._SAFE_ROLE_RE/_SAFE_REPO_RE and
#: transport.git_host_api._SAFE_CALLER_RE): '$' without re.MULTILINE also
#: matches just before a trailing newline in Python, so 'role\n' would
#: otherwise pass and be joined into a config-root path.
_SAFE_NAME_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


class SecretEnvError(ValueError):
    """Raised on any failure reading/parsing a role's secret-env file —
    missing file, insecure permissions, malformed line, or missing required
    key. Callers translate this to their own exit-code convention; this
    module never calls sys.exit so it stays exit-code-agnostic and testable
    in isolation."""


def read_role_env_file(
    role_or_name: str,
    required_keys: tuple[str, ...],
    *,
    config_root: Path | None = None,
) -> dict[str, str]:
    """Parse ``<config_root>/<role_or_name>.env`` and return a key->value dict.

    Security properties preserved from the source reader: *role_or_name* is
    validated against a safe-token regex before the path is constructed
    (path-traversal guard); the file must be mode 600 (no group/world
    read/write/exec bits) or the read is refused; values are returned as a
    local dict, never written to any process environment; file contents are
    never included in raised exception messages.

    Args:
        role_or_name: bare role or agent name — must match _SAFE_NAME_RE.
        required_keys: keys that must be present and non-empty.
        config_root: override the roles directory (mainly for tests).
            Defaults to DEFAULT_CONFIG_ROOT.

    Raises:
        SecretEnvError: invalid name, file not found, insecure permissions,
            malformed line, or a required key missing/empty.
    """
    if not _SAFE_NAME_RE.match(role_or_name):
        raise SecretEnvError(
            f"role/name {role_or_name!r} contains invalid characters (only "
            f"alphanumeric, hyphen, underscore; no path separators or "
            f"traversal). This is a security boundary: the value must be a "
            f"bare role or agent name."
        )

    root = config_root if config_root is not None else DEFAULT_CONFIG_ROOT
    path = root / f"{role_or_name}.env"

    if not path.exists():
        raise SecretEnvError(
            f"secret-env file not found: {path}. Ensure "
            f"{path} exists with chmod 600."
        )

    try:
        file_stat = path.stat()
    except OSError as exc:
        raise SecretEnvError(f"cannot stat secret-env file {path}: {exc}") from exc

    mode = file_stat.st_mode
    if mode & (
        stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP
        | stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH
    ):
        raise SecretEnvError(
            f"secret-env file {path} has insecure permissions "
            f"(mode {oct(stat.S_IMODE(mode))}; expected 0o600 / owner-only). "
            f"Fix with: chmod 600 {path}"
        )

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise SecretEnvError(f"cannot read secret-env file {path}: {exc}") from exc

    kvs: dict[str, str] = {}
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].strip()
        eq = line.find("=")
        if eq < 0:
            raise SecretEnvError(
                f"malformed line in secret-env file {path} (no '=' separator)."
            )
        key = line[:eq].strip()
        val = line[eq + 1:].strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ('"', "'"):
            val = val[1:-1]
        if not key:
            raise SecretEnvError(f"empty key in secret-env file {path}.")
        kvs[key] = val

    missing = [k for k in required_keys if not kvs.get(k)]
    if missing:
        raise SecretEnvError(
            f"secret-env file {path} is missing required key(s): "
            f"{', '.join(missing)}."
        )

    return kvs


__all__ = [
    "DEFAULT_CONFIG_ROOT",
    "DEFAULT_ROLE",
    "SecretEnvError",
    "read_role_env_file",
]
