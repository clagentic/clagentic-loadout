"""review.policy_source — read evaluator policy from the trusted base revision.

Dispositions and stakes can lower a verdict, so they are read at the revision
the pull request merges into (through a review.policy_reader.PolicyReader: the
host API, or a local repository when one is named), never from a working tree
and never from the PR head. An entry first introduced in the diff being gated
therefore cannot apply to that diff: the base does not contain it. That is
structural, not a check that could be skipped.

Also read from the base tree: exposure markers (see stakes.EXPOSURE_MARKER_GLOBS),
CODEOWNERS, and the repo profile (``profile.yaml``, see review.repo_profile),
whose posture keys (see review.profile_posture) are the stakes a repository
claims. A missing or invalid profile is carried as state, never as a degraded
reason: it is reported as a finding and must not hold a merge. The one
host-derived input, repository visibility, is passed in by the caller, which
already fetched the repository record. ``stakes.yaml`` is retired: it is never
read, and its presence at the base is only a completeness item.

Failure posture: a file that is absent is simply no policy. A file that cannot
be read, decoded or parsed resolves to no policy AND a reason, which the
evaluator reports as a DEGRADED verdict; the only effect of "no policy" is that
nothing is demoted and every stake stays at its worst case. Posture that is
missing, unrecognised or unconfirmed is likewise worst case, but is reported as
completeness items rather than a degraded reason.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from clagentic_loadout.review import dispositions as disp
from clagentic_loadout.review import profile_posture as pp
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
    #: What the repo profile is missing or has wrong, for reporting. Never a
    #: reason to hold a merge.
    completeness: tuple[pp.CompletenessItem, ...] = ()


def _yaml(text: str, path: str) -> Any:
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PolicyReadError(f"{path} is not valid YAML: {exc}") from exc


def read_policy(
    repo_path: str | Path,
    base: str,
    *,
    visibility: str | None = None,
    now: dt.datetime | None = None,
) -> PolicySource:
    """Policy at the *base* commit of the local repository at *repo_path*."""
    return read_policy_from(GitPolicyReader(repo_path), base, visibility=visibility, now=now)


def read_policy_from(
    reader: PolicyReader,
    base: str,
    *,
    visibility: str | None = None,
    now: dt.datetime | None = None,
) -> PolicySource:
    """Policy at the *base* commit, read through *reader*.

    *visibility* is the repository's host-reported visibility. *now* is the
    clock the posture's ``confirmed_at`` age is measured against (default: the
    current time). Never raises for a policy problem; see the module docstring."""
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
    claims, items = profile_completeness(
        repo_profile,
        profile_state,
        profile_problem,
        stakes_present=STAKES_PATH in present,
        host_visibility=visibility,
        now=now,
    )
    claimed = stk.StakesProfile(
        name="profile",
        exposure=claims.exposure,
        data_class=claims.data_class,
        merge_control=claims.merge_control,
    )
    return PolicySource(
        dispositions,
        stk.Stakes(
            profile=claimed,
            tree=tree,
            visibility=pp.raised_visibility(claims.declared_visibility, visibility),
        ),
        tuple(reasons),
        repo_profile,
        profile_state,
        profile_problem,
        items,
    )


def profile_completeness(
    profile: rp.RepoProfile | None,
    state: str,
    problem: str | None,
    *,
    stakes_present: bool,
    host_visibility: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[pp.PostureClaims, tuple[pp.CompletenessItem, ...]]:
    """``(claims, items)`` for a profile read by ``read_repo_profile``: the
    posture it resolves to and what is missing from it, in a fixed order (the
    profile file, each posture key, the visibility check, the retired file).
    The single computation behind ``loadout-profile check``, the push
    verification report, the standard spec and the evaluator's stakes."""
    items: list[pp.CompletenessItem] = []
    file_item = pp.profile_file_item(state, problem, PROFILE_PATH)
    if file_item is not None:
        items.append(file_item)
    claims = pp.PostureClaims()
    if state in (PROFILE_OK, PROFILE_MISSING):
        claims = pp.resolve_posture(
            profile.posture_values() if profile is not None else None,
            now=now,
            profile_path=PROFILE_PATH,
        )
        items.extend(claims.items)
        mismatch = pp.visibility_item(claims.declared_visibility, host_visibility)
        if mismatch is not None:
            items.append(mismatch)
    if stakes_present:
        items.append(pp.retired_stakes_item(STAKES_PATH, PROFILE_PATH))
    return claims, tuple(items)


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
    "profile_completeness",
    "read_policy",
    "read_policy_from",
    "read_repo_profile",
]
