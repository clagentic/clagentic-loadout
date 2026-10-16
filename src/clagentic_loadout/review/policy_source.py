"""review.policy_source — read evaluator policy from the trusted base revision.

Dispositions and stakes can lower a verdict, so they are read at the revision
the pull request merges into (through a review.policy_reader.PolicyReader: the
host API, or a local repository when one is named), never from a working tree
and never from the PR head. An entry first introduced in the diff being gated
therefore cannot apply to that diff: the base does not contain it. That is
structural, not a check that could be skipped.

Also read from the base tree: exposure markers (see stakes.EXPOSURE_MARKER_GLOBS),
CODEOWNERS, and the repo profile (``profile.yaml``, see review.repo_profile). A
missing or invalid profile is carried as state, never as a degraded reason: it is
reported as a finding and must not hold a merge. The one host-derived input, repository visibility, is passed in
by the caller, which already fetched the repository record.

Failure posture: a file that is absent is simply no policy. A file that cannot
be read, decoded or parsed resolves to no policy AND a reason, which the
evaluator reports as a DEGRADED verdict; the only effect of "no policy" is that
nothing is demoted and every stake stays at its worst case.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import stakes as stk
from clagentic_loadout.review import repo_profile as rp
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReader, PolicyReadError
from clagentic_loadout.sha import FULL_SHA_RE

DISPOSITIONS_PATH = ".clagentic/loadout/dispositions.yaml"
STAKES_PATH = ".clagentic/loadout/stakes.yaml"
PROFILE_PATH = ".clagentic/loadout/profile.yaml"

#: What reading the repo profile came to. ``unread`` means the base revision's
#: tree could not be read at all, which is already reported as a degraded input;
#: a profile problem is never reported through ``degraded``, because a missing or
#: invalid profile is a finding and must not hold a merge.
PROFILE_OK = "ok"
PROFILE_MISSING = "missing"
PROFILE_INVALID = "invalid"
PROFILE_UNREAD = "unread"


@dataclass(frozen=True)
class PolicySource:
    """Policy as it stands at the base revision, plus why any of it is missing."""

    dispositions: tuple[disp.Disposition, ...]
    stakes: stk.Stakes
    degraded: tuple[str, ...]
    repo_profile: rp.RepoProfile | None = None
    profile_state: str = PROFILE_UNREAD
    profile_problem: str | None = None


def _yaml(text: str, path: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyReadError(f"{path} is not valid YAML: {exc}") from exc


def read_policy(
    repo_path: str | Path,
    base: str,
    *,
    profile: str | None = None,
    visibility: str | None = None,
) -> PolicySource:
    """Policy at the *base* commit of the local repository at *repo_path*."""
    return read_policy_from(
        GitPolicyReader(repo_path), base, profile=profile, visibility=visibility
    )


def read_policy_from(
    reader: PolicyReader,
    base: str,
    *,
    profile: str | None = None,
    visibility: str | None = None,
) -> PolicySource:
    """Policy at the *base* commit, read through *reader*.

    *profile* selects a stakes profile by name (default: the file's own
    ``profile`` key). *visibility* is the repository's host-reported visibility.
    Never raises for a policy problem; see the module docstring."""
    reasons: list[str] = []
    if not FULL_SHA_RE.match(base):
        reasons.append(f"base revision {base!r} is not a full 40-character commit SHA")
        return PolicySource((), stk.Stakes(visibility=visibility), tuple(reasons))

    try:
        paths = reader.list_paths(base)
    except PolicyReadError as exc:
        reasons.append(str(exc))
        return PolicySource((), stk.Stakes(visibility=visibility), tuple(reasons))
    present = set(paths)

    dispositions: tuple[disp.Disposition, ...] = ()
    if DISPOSITIONS_PATH in present:
        try:
            dispositions = disp.parse_dispositions(
                _yaml(reader.read_text(base, DISPOSITIONS_PATH), DISPOSITIONS_PATH)
            )
        except (PolicyReadError, disp.DispositionsFormatError) as exc:
            reasons.append(f"dispositions ignored: {exc}")

    selected: stk.StakesProfile | None = None
    profiles: dict[str, stk.StakesProfile] = {}
    default_name: str | None = None
    if STAKES_PATH in present:
        try:
            profiles, default_name = stk.parse_stakes_document(
                _yaml(reader.read_text(base, STAKES_PATH), STAKES_PATH)
            )
        except (PolicyReadError, stk.StakesFormatError) as exc:
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
            codeowners = stk.parse_codeowners(reader.read_text(base, location))
        except PolicyReadError as exc:
            reasons.append(f"CODEOWNERS ignored: {exc}")
        break

    tree = stk.TreeStakes(
        exposed_dirs=stk.exposed_dirs_from_paths(paths), codeowners=codeowners
    )
    repo_profile, profile_state, profile_problem = read_repo_profile(reader, base, present)
    return PolicySource(
        dispositions,
        stk.Stakes(profile=selected, tree=tree, visibility=visibility),
        tuple(reasons),
        repo_profile,
        profile_state,
        profile_problem,
    )


def read_repo_profile(
    reader: PolicyReader, base: str, present: set[str]
) -> tuple[rp.RepoProfile | None, str, str | None]:
    """``(profile, state, problem)`` for the repo profile at *base*, where
    *present* is the set of paths in that tree. Never raises: a file that cannot
    be read or parsed is state ``invalid`` with the reason, and no class rule
    applies from it. Shared with ``loadout-profile check`` so the verb and the
    review agree on what a valid profile is."""
    if PROFILE_PATH not in present:
        return None, PROFILE_MISSING, None
    try:
        profile = rp.parse_profile_document(_yaml(reader.read_text(base, PROFILE_PATH), PROFILE_PATH))
    except (PolicyReadError, rp.ProfileFormatError) as exc:
        return None, PROFILE_INVALID, str(exc)
    return profile, PROFILE_OK, None


__all__ = [
    "DISPOSITIONS_PATH",
    "PROFILE_INVALID",
    "PROFILE_MISSING",
    "PROFILE_OK",
    "PROFILE_PATH",
    "PROFILE_UNREAD",
    "STAKES_PATH",
    "PolicySource",
    "read_policy",
    "read_policy_from",
    "read_repo_profile",
]
