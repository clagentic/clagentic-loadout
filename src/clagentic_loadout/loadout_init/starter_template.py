"""loadout_init.starter_template — locate + copy the packaged starter
config.yaml.

Two pure, testable operations:

  1. `starter_template_path` -- resolve the packaged template's own on-disk
     path (works both from a source checkout and an installed wheel, via
     `Path(__file__)`-relative lookup -- same package-data access pattern
     `schemas/*.json` already uses, see pyproject.toml's
     `[tool.setuptools.package-data]`).
  2. `copy_starter_template` -- copy that template to a target repo's
     resolved `.clagentic/loadout/config.yaml` path (via
     `target_config_path`'s own canonical-path resolution, NOT
     `repo_config.resolve_repo_config_path`'s legacy-fallback READ
     resolution -- /loadout-init always writes to the canonical new path,
     never the legacy one, even if a legacy file happens to already exist
     there).

No interactive elicitation, no prompting, no YAML mutation of the copied
file's own content -- this module gets a clean template onto disk and
refuses to overwrite an existing config without `force=True`. The
guided-conversation half of `/loadout-init` (asking the operator for
post_merge deployment values, the ci_pass toggle, reviewer/authorized roles)
lives entirely in `.claude/skills/loadout-init/SKILL.md`'s own prose, which
edits the copied file directly with ordinary file edits after this module
places it.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from clagentic_loadout.repo_config import DEFAULT_CONFIG_RELATIVE_PATH

#: The packaged starter template, relative to this module's own directory --
#: resolved via `Path(__file__).parent` rather than a hardcoded absolute
#: path, so it works identically from a source checkout or an installed
#: package (mirrors `schemas/*.json`'s own package-data shape).
_STARTER_TEMPLATE_FILENAME = "starter_config.yaml"


class StarterTemplateError(ValueError):
    """Raised when the packaged starter template is missing, or a copy
    target already exists and `force` was not requested. Always names the
    RESOLVED path involved (conformance rule 4), never a stale guess."""


def starter_template_path() -> Path:
    """Return the packaged starter template's own on-disk path.

    Raises:
        StarterTemplateError: the packaged file is missing -- a broken
            install/build, never silently swallowed.
    """
    path = Path(__file__).parent / _STARTER_TEMPLATE_FILENAME
    if not path.is_file():
        raise StarterTemplateError(
            f"packaged starter template not found at {path} -- broken "
            f"clagentic-loadout install/build."
        )
    return path


def target_config_path(
    repo_root: str | Path, *, config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH
) -> Path:
    """Resolve the CANONICAL write target for a repo's /loadout-init copy:
    `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`).

    Deliberately NOT `repo_config.resolve_repo_config_path` -- that function
    resolves a READ path with legacy-fallback and wrapper-hop discovery for
    an EXISTING config; /loadout-init always targets the canonical NEW path
    directly, so a not-yet-migrated repo (only a legacy `.loadout/config.yaml`
    present) is initialized onto the new home rather than having its copy
    redirected onto the legacy one.
    """
    return Path(repo_root) / config_relative_path


def copy_starter_template(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    force: bool = False,
) -> Path:
    """Copy the packaged starter template to *repo_root*'s resolved
    `.clagentic/loadout/config.yaml` path, creating parent directories as
    needed.

    Returns the path the template was copied to.

    Raises:
        StarterTemplateError: the packaged template is missing, or the
            target path already exists and `force` is False (never silently
            clobbers an existing repo config).
    """
    source = starter_template_path()
    target = target_config_path(repo_root, config_relative_path=config_relative_path)

    if target.exists() and not force:
        raise StarterTemplateError(
            f"{target} already exists -- pass force=True to overwrite "
            f"(/loadout-init never clobbers an existing repo config by default)."
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    return target


__all__ = [
    "StarterTemplateError",
    "copy_starter_template",
    "starter_template_path",
    "target_config_path",
]
