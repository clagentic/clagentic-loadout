"""review.policy_source — read evaluator policy from the trusted base revision.

Dispositions and stakes can lower a verdict, so they are read with
``git show <base>:<path>`` against the revision the pull request merges into,
never from the working tree and never from the PR head. An entry first
introduced in the diff being gated therefore cannot apply to that diff: the
base does not contain it. That is structural, not a check that could be skipped.

Also read from the base tree: exposure markers (see stakes.EXPOSURE_MARKER_GLOBS)
and CODEOWNERS. The one host-derived input, repository visibility, is passed in
by the caller, which already fetched the repository record.

Failure posture: a file that is absent is simply no policy. A file that cannot
be read, decoded or parsed resolves to no policy AND a reason, which the
evaluator reports as a DEGRADED verdict; the only effect of "no policy" is that
nothing is demoted and every stake stays at its worst case.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clagentic_loadout.merge.pre_check_env import without_git_location
from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.sha import FULL_SHA_RE

DISPOSITIONS_PATH = ".clagentic/loadout/dispositions.yaml"
STAKES_PATH = ".clagentic/loadout/stakes.yaml"

_GIT_TIMEOUT_SECONDS = 30
_MAX_POLICY_BYTES = 512 * 1024


@dataclass(frozen=True)
class PolicySource:
    """Policy as it stands at the base revision, plus why any of it is missing."""

    dispositions: tuple[disp.Disposition, ...]
    stakes: stk.Stakes
    degraded: tuple[str, ...]


class _GitFailure(Exception):
    """A git read failed; the message names what was being read."""


def _git(repo_path: Path, args: list[str], what: str) -> bytes:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo_path), *args],
            shell=False,
            capture_output=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            env=without_git_location(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _GitFailure(f"cannot read {what}: {exc}") from exc
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", "replace").strip().splitlines()
        raise _GitFailure(f"cannot read {what}: {detail[-1] if detail else 'git failed'}")
    return completed.stdout


def _tree_paths(repo_path: Path, base: str) -> list[str]:
    out = _git(
        repo_path, ["ls-tree", "-r", "-z", "--name-only", base], f"the file list at {base[:12]}"
    )
    return [p for p in out.decode("utf-8", "replace").split("\0") if p]


def _show(repo_path: Path, base: str, path: str) -> str:
    raw = _git(repo_path, ["show", f"{base}:{path}"], f"{path} at {base[:12]}")
    if len(raw) > _MAX_POLICY_BYTES:
        raise _GitFailure(f"{path} at {base[:12]} is larger than {_MAX_POLICY_BYTES} bytes")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _GitFailure(f"{path} at {base[:12]} is not valid UTF-8: {exc}") from exc


def _yaml(text: str, path: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise _GitFailure(f"{path} is not valid YAML: {exc}") from exc


def read_policy(
    repo_path: str | Path,
    base: str,
    *,
    profile: str | None = None,
    visibility: str | None = None,
) -> PolicySource:
    """Policy at the *base* commit of the repository at *repo_path*.

    *profile* selects a stakes profile by name (default: the file's own
    ``profile`` key). *visibility* is the repository's host-reported visibility.
    Never raises for a policy problem; see the module docstring."""
    root = Path(repo_path)
    reasons: list[str] = []
    if not FULL_SHA_RE.match(base):
        reasons.append(f"base revision {base!r} is not a full 40-character commit SHA")
        return PolicySource((), stk.Stakes(visibility=visibility), tuple(reasons))

    try:
        paths = _tree_paths(root, base)
    except _GitFailure as exc:
        reasons.append(str(exc))
        return PolicySource((), stk.Stakes(visibility=visibility), tuple(reasons))
    present = set(paths)

    dispositions: tuple[disp.Disposition, ...] = ()
    if DISPOSITIONS_PATH in present:
        try:
            dispositions = disp.parse_dispositions(
                _yaml(_show(root, base, DISPOSITIONS_PATH), DISPOSITIONS_PATH)
            )
        except (_GitFailure, disp.DispositionsFormatError) as exc:
            reasons.append(f"dispositions ignored: {exc}")

    selected: stk.StakesProfile | None = None
    profiles: dict[str, stk.StakesProfile] = {}
    default_name: str | None = None
    if STAKES_PATH in present:
        try:
            profiles, default_name = stk.parse_stakes_document(
                _yaml(_show(root, base, STAKES_PATH), STAKES_PATH)
            )
        except (_GitFailure, stk.StakesFormatError) as exc:
            reasons.append(f"stakes profile ignored: {exc}")
            profiles, default_name = {}, None
    wanted = profile or default_name
    if wanted is not None:
        selected = profiles.get(wanted)
        if selected is None and not any("stakes profile ignored" in r for r in reasons):
            reasons.append(f"stakes profile {wanted!r} is not defined in {STAKES_PATH} at the base")

    codeowners: tuple[stk.CodeownersRule, ...] | None = None
    for location in stk.CODEOWNERS_LOCATIONS:
        if location not in present:
            continue
        try:
            codeowners = stk.parse_codeowners(_show(root, base, location))
        except _GitFailure as exc:
            reasons.append(f"CODEOWNERS ignored: {exc}")
        break

    tree = stk.TreeStakes(
        exposed_dirs=stk.exposed_dirs_from_paths(paths), codeowners=codeowners
    )
    return PolicySource(
        dispositions, stk.Stakes(profile=selected, tree=tree, visibility=visibility), tuple(reasons)
    )


__all__ = ["DISPOSITIONS_PATH", "STAKES_PATH", "PolicySource", "read_policy"]
