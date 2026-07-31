"""guard.credential_paths — role-agnostic credential-path denial checks
(lr-fd279d, port of the reference deployment's guard-credentials.py; lr-5a8d
epic, slice 1).

PORT PATTERN — see `guard.write_scope` module docstring for the full
role-keying rationale shared by both modules in this slice; the short
version for this module:

1. NO ROLE GATING AT ALL. Unlike guard-scope.py, the reference
   guard-credentials.py applies the SAME check to every one of its six
   managed agent names (`_CREW_AGENTS` there) — it has no per-agent branch.
   This module therefore takes no `WriteRole`-style parameter: credential-
   path denial is a role-independent property of a Read/Glob/Bash call. A
   caller wires this into whichever of its roles can invoke Read/Glob/Bash
   at all (its own harness's capability model decides that, not this
   module).

2. NO OPERATOR-HOME HARDCODE. The reference hardcodes `/root/` as "operator
   home territory" and a narrow `/root/.claude/settings.json(.bak-*)`
   exception. `/root/` is a machine-specific absolute-path assumption
   (CLAUDE.md rule 1: no operator org/host/path hardcodes in product code).
   This module instead takes an explicit, caller-supplied
   `protected_home_prefixes` set (defaulting to empty — a caller that wants
   home-directory protection declares its own prefixes) plus an explicit
   `allowed_exact_paths` / `allowed_bak_prefix` pair for narrow config-file
   exceptions. The DENY-LIST SUBSTRING CHECK (`.netrc`, `.git-credentials`,
   `inject_credentials`, etc.) is universal and ships as a default constant
   since it names credential FILE SHAPES, not machine paths.

3. BAK-PATH VALIDATION IS PORTED AS A REUSABLE HELPER, not duplicated
   inline. `is_valid_bak_path` mirrors the reference's `_bak_check.py`
   (lr-9c39: reject directory-as-prefix and traversal suffixes) but is
   parameterized on the base path instead of a single hardcoded
   `/root/.claude/settings.json.bak-` literal.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Credential-file-SHAPE substrings this module denies regardless of which
#: role or absolute path they appear under. These name file KINDS, never
#: machine-specific paths, so they ship as an unconditional default rather
#: than caller-supplied config — a caller that genuinely needs a narrower
#: or wider list can still pass its own via `deny_list=`.
DEFAULT_CREDENTIAL_DENY_LIST: tuple[str, ...] = (
    ".netrc",
    "netrc",
    ".git-credentials",
    "git-credentials",
    "inject_credentials",
    "inject_credentials.py",
)

#: For Bash commands, "netrc" and "git-credentials" must appear as file-path
#: components (preceded by `/`, whitespace, or `.`), not as CLI flag names
#: like `--netrc` — a generic substring match on the Bash channel would
#: false-positive on a legitimate `curl --netrc` invocation. `inject_credentials`
#: has no legitimate flag form so a bare substring is fine there.
DEFAULT_BASH_CREDENTIAL_PATTERNS: tuple[re.Pattern, ...] = (
    re.compile(r"[/\s.]\.?netrc(?:\s|$|'|\"|;)"),
    re.compile(r"[/\s.]\.?git-credentials(?:\s|$|'|\"|;)"),
    re.compile(r"inject_credentials"),
)

#: Suffix grammar for a `.bak-<suffix>` backup path: alphanumeric-or-dash
#: only, no path separators or `.` — rejects directory-as-prefix
#: (`...bak-evil/anything`) and traversal (`...bak-../../etc/shadow`)
#: attacks (lr-9c39).
_BAK_SUFFIX_RE: re.Pattern = re.compile(r"^[A-Za-z0-9-]+$")


def is_valid_bak_path(path_str: str, base_path: str) -> bool:
    """Return True iff *path_str* resolves to a valid `<base_path>-<suffix>`
    backup path.

    Validation (lr-9c39):
      1. resolved path starts with the exact `base_path` prefix.
      2. suffix after the prefix is non-empty and matches `_BAK_SUFFIX_RE`
         (alphanumeric + dash only — no `/` or `.`).
      3. resolved path must not be an existing directory (defense in depth
         against a directory-as-prefix bypass). A non-existent path is
         allowed, since a caller may be about to create the backup.

    Any exception (unresolvable path, filesystem error) returns False.
    """
    try:
        resolved = str(Path(path_str).resolve())
    except Exception:
        return False

    if not resolved.startswith(base_path):
        return False

    suffix = resolved[len(base_path):]
    if not suffix or not _BAK_SUFFIX_RE.match(suffix):
        return False

    try:
        return not Path(resolved).is_dir()
    except Exception:
        return False


def _matches_deny_list(s: str, deny_list: tuple[str, ...]) -> str | None:
    """Return the matched deny-list entry if *s* contains one, else None."""
    s_lower = s.lower()
    for entry in deny_list:
        if entry in s_lower:
            return entry
    return None


def _resolves_under_prefix(path_str: str, prefixes: tuple[str, ...]) -> str | None:
    """Return the matching prefix if *path_str* resolves to or under one of
    *prefixes*, else None. Each prefix is treated as a directory path
    (trailing "/" optional in the input)."""
    try:
        resolved = str(Path(path_str).resolve())
    except Exception:
        resolved = path_str
    for prefix in prefixes:
        pfx_dir = prefix.rstrip("/")
        if resolved == pfx_dir or resolved.startswith(pfx_dir + "/"):
            return prefix
    return None


def _is_explicitly_allowed(
    path_str: str,
    allowed_exact_paths: tuple[str, ...],
    allowed_bak_prefixes: tuple[str, ...],
) -> bool:
    try:
        resolved = str(Path(path_str).resolve())
    except Exception:
        resolved = path_str
    if resolved in allowed_exact_paths:
        return True
    for bak_prefix in allowed_bak_prefixes:
        if is_valid_bak_path(path_str, bak_prefix):
            return True
    return False


def check_read_path(
    file_path: str,
    *,
    deny_list: tuple[str, ...] = DEFAULT_CREDENTIAL_DENY_LIST,
    protected_home_prefixes: tuple[str, ...] = (),
    allowed_exact_paths: tuple[str, ...] = (),
    allowed_bak_prefixes: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Guard for a Read-style call: inspect *file_path*.

    Denies when *file_path* contains a *deny_list* substring, or resolves
    under one of *protected_home_prefixes* and is not covered by
    *allowed_exact_paths* / *allowed_bak_prefixes*. Returns (True, "") when
    *file_path* is empty (nothing to enforce) or passes both checks.
    """
    file_path = (file_path or "").strip()
    if not file_path:
        return True, ""

    matched = _matches_deny_list(file_path, deny_list)
    if matched:
        return False, (
            f"Read denied: path {file_path!r} matches credential deny-list "
            f"entry {matched!r}. Credential files must not be read."
        )

    matched_prefix = _resolves_under_prefix(file_path, protected_home_prefixes)
    if matched_prefix and not _is_explicitly_allowed(
        file_path, allowed_exact_paths, allowed_bak_prefixes
    ):
        return False, (
            f"Read denied: path {file_path!r} resolves under protected prefix "
            f"{matched_prefix!r}. Enumerate an explicit exception if access is "
            f"genuinely required."
        )

    return True, ""


def check_glob_call(
    pattern: str,
    path: str = "",
    *,
    deny_list: tuple[str, ...] = DEFAULT_CREDENTIAL_DENY_LIST,
    protected_home_prefixes: tuple[str, ...] = (),
    allowed_exact_paths: tuple[str, ...] = (),
    allowed_bak_prefixes: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Guard for a Glob-style call: inspect *pattern* and *path*.

    Denies when either field contains a *deny_list* substring, or *path*
    resolves under a protected prefix without an explicit exception.
    """
    pattern = (pattern or "").strip()
    path = (path or "").strip()

    for field_name, value in (("pattern", pattern), ("path", path)):
        if not value:
            continue
        matched = _matches_deny_list(value, deny_list)
        if matched:
            return False, (
                f"Glob denied: {field_name}={value!r} matches credential "
                f"deny-list entry {matched!r}. Path disclosure for credential "
                f"files is an info-leak even when content is not returned."
            )

    if path:
        matched_prefix = _resolves_under_prefix(path, protected_home_prefixes)
        if matched_prefix and not _is_explicitly_allowed(
            path, allowed_exact_paths, allowed_bak_prefixes
        ):
            return False, (
                f"Glob denied: path={path!r} resolves under protected prefix "
                f"{matched_prefix!r}. Enumerate an explicit exception if access "
                f"is genuinely required."
            )

    return True, ""


def check_bash_command(
    command: str,
    *,
    patterns: tuple[re.Pattern, ...] = DEFAULT_BASH_CREDENTIAL_PATTERNS,
) -> tuple[bool, str]:
    """Guard for a Bash-style call: path-aware check on the raw command
    string.

    Uses *patterns* (path-anchored, defaulting to `DEFAULT_BASH_CREDENTIAL_PATTERNS`)
    instead of a generic deny-list substring check, to avoid false positives
    on CLI flags like `--netrc` (a curl authentication flag, not a
    credential-file-path reference).
    """
    command = (command or "").strip()
    if not command:
        return True, ""

    for pat in patterns:
        if pat.search(command):
            return False, (
                f"Bash denied: command contains a credential-path indicator "
                f"matching {pat.pattern!r}. Commands must not read or inspect "
                f"credential files."
            )

    return True, ""


__all__ = [
    "DEFAULT_BASH_CREDENTIAL_PATTERNS",
    "DEFAULT_CREDENTIAL_DENY_LIST",
    "check_bash_command",
    "check_glob_call",
    "check_read_path",
    "is_valid_bak_path",
]
