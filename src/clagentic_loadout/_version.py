"""_version.py — single source of truth for the package version string.

lr-0b45: every verb's --version output, and the umbrella CLI's own
--version, resolve the version through this one module rather than each
carrying its own duplicated literal. The version itself lives in exactly
one place: pyproject.toml's [project].version — this module reads it back
via importlib.metadata at runtime (installed package) and falls back to
parsing pyproject.toml directly for the uninstalled/editable-checkout dev
loop (pip install -e . populates metadata; a bare `pytest` run from a
fresh clone without any install does not).
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version as _metadata_version
from pathlib import Path

#: The distribution name as declared in pyproject.toml's [project].name —
#: NOT the console-script or import-package name (those may legitimately
#: differ; here they happen to match by convention).
DISTRIBUTION_NAME = "clagentic_loadout"

#: Fallback used only when neither installed package metadata nor
#: pyproject.toml can be read (should not happen in a normal checkout or
#: install, but keeps this module total rather than raising).
_UNKNOWN_VERSION = "0.0.0+unknown"


def _read_pyproject_version() -> str | None:
    """Best-effort fallback: parse `version = "..."` out of pyproject.toml
    directly, for the case where the package is not installed (e.g. running
    tests from a fresh checkout with no `pip install -e .` yet)."""
    pyproject_path = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if not pyproject_path.is_file():
        return None
    in_project_table = False
    for line in pyproject_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped == "[project]":
            in_project_table = True
            continue
        if stripped.startswith("[") and stripped != "[project]":
            if in_project_table:
                break
            continue
        if in_project_table and stripped.startswith("version"):
            _, _, rhs = stripped.partition("=")
            return rhs.strip().strip('"').strip("'")
    return None


def get_version() -> str:
    """Resolve the installed package version.

    Precedence: installed distribution metadata (the normal case for any
    pip/pipx/uv install) -> a direct pyproject.toml parse (editable/
    uninstalled dev checkout) -> a total, never-raising fallback constant.
    """
    try:
        return _metadata_version(DISTRIBUTION_NAME)
    except PackageNotFoundError:
        pass
    fallback = _read_pyproject_version()
    return fallback if fallback else _UNKNOWN_VERSION
