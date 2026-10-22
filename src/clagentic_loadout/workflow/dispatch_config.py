"""workflow.dispatch_config — the repo-config allow-list for workflow dispatch.

The one fail-closed check of `loadout-workflow-dispatch`: a workflow file and
a ref may be dispatched only if the repo's own config lists both. The key is a
top-level list in the sectioned repo config file (see `repo_config.py`):

    workflow_dispatch:
      - workflow: release.yml
        refs: [main]

Matching is exact on both fields: no globs, no prefixes. A missing config
file, a missing key, or any malformed entry is an error, never "nothing is
restricted" -- an unreadable allow-list must not read as an open one. Errors
carry the config path and the key path of the offending value.

This module only reads and matches; it makes no network call and mints nothing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level key this module owns in the repo-local config file.
CONFIG_KEY_WORKFLOW_DISPATCH = "workflow_dispatch"

#: A workflow is addressed by its file name, never by a path: a slash would
#: let the value escape the `/actions/workflows/{file}` URL segment.
_WORKFLOW_FILE_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\.ya?ml\Z")

#: A ref is a branch or tag name. Whitespace, control characters and `?#%`
#: are refused so the value cannot be misread as URL structure by anything
#: downstream of the JSON body.
_REF_RE = re.compile(r"\A[^\s\x00-\x1f\x7f?#%\\]+\Z")


class DispatchConfigError(ValueError):
    """The allow-list is absent or malformed. Names the path and key path."""


class DispatchNotListedError(ValueError):
    """The workflow or ref is not listed in the repo's allow-list."""


@dataclass(frozen=True)
class DispatchEntry:
    workflow: str
    refs: tuple[str, ...]


def is_valid_workflow_file(value: object) -> bool:
    return isinstance(value, str) and _WORKFLOW_FILE_RE.match(value) is not None


def is_valid_ref(value: object) -> bool:
    return isinstance(value, str) and _REF_RE.match(value) is not None


def _parse_entry(raw: object, *, index: int, config_path: Path) -> DispatchEntry:
    where = f"{config_path}: {CONFIG_KEY_WORKFLOW_DISPATCH}[{index}]"
    if not isinstance(raw, dict):
        raise DispatchConfigError(
            f"{where} must be a mapping with 'workflow' and 'refs', "
            f"got {type(raw).__name__}."
        )
    unknown = sorted(set(raw) - {"workflow", "refs"})
    if unknown:
        raise DispatchConfigError(
            f"{where} has unknown key(s) {unknown}; expected only 'workflow' and 'refs'."
        )
    workflow = raw.get("workflow")
    if not is_valid_workflow_file(workflow):
        raise DispatchConfigError(
            f"{where}.workflow must be a workflow file name such as 'release.yml' "
            f"(no path separators), got {workflow!r}."
        )
    refs = raw.get("refs")
    if not isinstance(refs, list) or not refs:
        raise DispatchConfigError(
            f"{where}.refs must be a non-empty list of ref names, got {refs!r}."
        )
    for ref_index, ref in enumerate(refs):
        if not is_valid_ref(ref):
            raise DispatchConfigError(
                f"{where}.refs[{ref_index}] must be a ref name with no whitespace "
                f"or URL-structure characters, got {ref!r}."
            )
    return DispatchEntry(workflow=workflow, refs=tuple(refs))


def load_dispatch_allowlist(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> tuple[DispatchEntry, ...]:
    """Read and validate the `workflow_dispatch` list for *repo_root*.

    Raises DispatchConfigError when the config file is missing or unreadable,
    the key is absent, or any entry is malformed.
    """
    config_path = resolve_repo_config_path(repo_root, config_relative_path=config_relative_path)
    if not config_path.is_file():
        raise DispatchConfigError(
            f"no repo config file at {config_path}; add a "
            f"'{CONFIG_KEY_WORKFLOW_DISPATCH}' list there to allow a dispatch."
        )
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise DispatchConfigError(f"{config_path}: could not be read as YAML: {exc}.") from exc

    if not isinstance(raw, dict) or CONFIG_KEY_WORKFLOW_DISPATCH not in raw:
        raise DispatchConfigError(
            f"{config_path}: no top-level '{CONFIG_KEY_WORKFLOW_DISPATCH}' key; "
            f"a dispatch is refused unless the workflow and ref are listed there."
        )
    entries = raw[CONFIG_KEY_WORKFLOW_DISPATCH]
    if not isinstance(entries, list):
        raise DispatchConfigError(
            f"{config_path}: {CONFIG_KEY_WORKFLOW_DISPATCH} must be a list, "
            f"got {type(entries).__name__}."
        )
    return tuple(
        _parse_entry(entry, index=index, config_path=config_path)
        for index, entry in enumerate(entries)
    )


def check_dispatch_allowed(
    entries: tuple[DispatchEntry, ...], *, workflow: str, ref: str, source: str
) -> None:
    """Raise DispatchNotListedError unless *workflow* lists *ref* in *entries*."""
    listed_workflows = sorted({entry.workflow for entry in entries})
    matching = [entry for entry in entries if entry.workflow == workflow]
    if not matching:
        raise DispatchNotListedError(
            f"workflow {workflow!r} is not listed in {source} under "
            f"'{CONFIG_KEY_WORKFLOW_DISPATCH}' (listed: {listed_workflows or 'none'})."
        )
    allowed_refs = sorted({r for entry in matching for r in entry.refs})
    if ref not in allowed_refs:
        raise DispatchNotListedError(
            f"ref {ref!r} is not listed for workflow {workflow!r} in {source} "
            f"(listed refs: {allowed_refs})."
        )
