"""review.run_resolved — the resolved list a review run recorded.

`loadout-review run` writes ``findings.json`` into its run directory, with the
prior findings the run resolved under ``resolved``. A reviewer that posts a bare
findings array hands `post` no such list, so `post` reads it from the run
directory instead. The directory is loadout-written state for one run at one
head: it is used only when its binding and its findings document both name the
repository, the PR and the head being posted.

A source that is missing, unreadable or lists nothing yields no entries and a
note naming which source it was. A source that names a different head is a
conflict: nothing from any reviewer-side source may then be used.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clagentic_loadout.merge.fence_state import KEY_RESOLVED, normalize_findings_state
from clagentic_loadout.review.run_pipeline import BINDING_FILENAME, FINDINGS_FILENAME, default_run_dir


@dataclass(frozen=True)
class RunResolved:
    entries: tuple[dict[str, Any], ...] = ()
    #: True when a run directory disagrees with the head being posted.
    conflict: bool = False
    notes: tuple[str, ...] = ()


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None


def _check_dir(
    run_dir: Path, *, owner: str, repo: str, pr_number: int, head_sha: str
) -> tuple[str, str]:
    """('ok' | 'missing' | 'conflict', why) for one candidate directory."""
    binding = _load_json(run_dir / BINDING_FILENAME)
    document = _load_json(run_dir / FINDINGS_FILENAME)
    if not isinstance(binding, dict):
        return "missing", f"{str(run_dir)!r} has no readable {BINDING_FILENAME}"
    if binding.get("repo") != f"{owner}/{repo}".lower() or binding.get("pr_number") != pr_number:
        return "missing", f"{str(run_dir)!r} is bound to another repository or PR"
    if binding.get("head_sha") != head_sha:
        return "conflict", (
            f"the run directory {str(run_dir)!r} is bound to head {binding.get('head_sha')}, "
            f"not the head {head_sha} being posted"
        )
    if not isinstance(document, dict):
        return "missing", f"{str(run_dir / FINDINGS_FILENAME)!r} is absent or unreadable"
    if document.get("head_sha") != head_sha:
        return "conflict", (
            f"{str(run_dir / FINDINGS_FILENAME)!r} is for head {document.get('head_sha')}, "
            f"not the head {head_sha} being posted"
        )
    return "ok", ""


def candidate_run_dirs(
    explicit: str | None, findings_path: str, run_root: Path, *, owner: str, repo: str,
    pr_number: int, head_sha: str,
) -> list[Path]:
    """Where the run directory may be, most specific first: --run-dir, the
    directory holding the findings file, then every run at this head under the
    run root."""
    if explicit:
        return [Path(explicit)]
    beside = Path(findings_path).resolve().parent
    if (beside / BINDING_FILENAME).is_file():
        return [beside]
    keyed = default_run_dir(run_root, owner, repo, pr_number, head_sha).parent
    prefix = head_sha[:12]
    try:
        found = sorted(p for p in keyed.iterdir() if p.is_dir() and p.name.startswith(prefix))
    except OSError:
        return []
    return found


def read_run_resolved(
    candidates: Sequence[Path], *, owner: str, repo: str, pr_number: int, head_sha: str
) -> RunResolved:
    """The validated ``resolved`` entries of the one run directory among
    *candidates* that is bound to this repository, PR and head."""
    if not candidates:
        return RunResolved(notes=("no run directory was found, so the run's resolved list is missing",))
    usable: list[Path] = []
    notes: list[str] = []
    for run_dir in candidates:
        status, why = _check_dir(
            run_dir, owner=owner, repo=repo, pr_number=pr_number, head_sha=head_sha
        )
        if status == "conflict":
            return RunResolved(conflict=True, notes=(why,))
        if status == "ok":
            usable.append(run_dir)
        else:
            notes.append(why)
    if not usable:
        return RunResolved(notes=tuple(f"run resolved list missing: {n}" for n in notes))
    if len(usable) > 1:
        listed = ", ".join(repr(str(p)) for p in usable)
        return RunResolved(
            notes=(f"run resolved list ambiguous: several runs at this head ({listed}); pass --run-dir",)
        )
    document = _load_json(usable[0] / FINDINGS_FILENAME)
    raw = document.get(KEY_RESOLVED) if isinstance(document, dict) else None
    if not raw:
        return RunResolved(
            notes=(f"{str(usable[0] / FINDINGS_FILENAME)!r} carries no resolved list",)
        )
    try:
        entries = normalize_findings_state(
            {KEY_RESOLVED: raw}, head_sha=head_sha, review_status="blocking"
        )[KEY_RESOLVED]
    except ValueError as exc:
        return RunResolved(
            notes=(f"{str(usable[0] / FINDINGS_FILENAME)!r} resolved list is malformed: {exc}",)
        )
    return RunResolved(entries=tuple(entries))
