"""merge.required_reviewers — which reviewer roles a pull request's merge requires.

ONE RESOLUTION, TWO CALLERS. ``loadout-merge`` enforces a set of reviewer roles
and ``loadout-ruling`` attributes a posted fence to one of them. Both ask
``resolve_required_reviewers`` rather than keep a roster of their own, so a role
the merge gate enforces on a pull request is a role whose fences the ruling verb
can attribute and rule on for that pull request, and the reverse.

The answer is the union of two sources:

  * the FLOOR: the roles the repository's tracked gate declares at the pull
    request's BASE commit (``merge.repo_gate_runtime``), content the pull request
    under review cannot change. A role the deployment cannot resolve to a login
    is dropped with a warning, never a refusal;
  * the caller's own ``--required-reviewer`` values, which add roles and never
    remove any. A flag names a role already in the floor under the flag's login.

With no tracked gate file the floor is empty and the flags alone decide.

TRUST. A ruling relaxes a gate, so the ruling verb's flag widens only
ATTRIBUTION (which comment counts as a required reviewer's verdict) and never
authority: who may write a ruling is decided elsewhere (``review.ruling_authority``)
and no caller input reaches it. The ruling verb takes BARE role names only
(``bare_only``): each login is derived by the tool, never typed. A ruling is
matched by fingerprint, rule and path, not by the login that posted the fence, so
a pinned login would let any account's comment supply the fingerprint.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass

from clagentic_loadout.merge.errors import MergeUsageError
from clagentic_loadout.merge.repo_gate_runtime import RepoGate, with_resolvable_reviewer_roles
from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)


@dataclass(frozen=True)
class RequiredReviewers:
    """The roles a merge requires, each with the login that speaks for it."""

    #: ``{role name: expected login}``: the floor and the flags together.
    reviewers: dict[str, str]
    #: The gate after unresolvable declared roles were dropped; its warnings and
    #: notices are for the caller to print.
    gate: RepoGate
    #: The roles only the floor requires (no flag named them).
    floor_only: dict[str, str]


def parse_required_reviewers(
    raw: Iterable[str] | None,
    platform: str,
    *,
    bare_only: bool = False,
    resolve_login: Callable[[str, str], str] = resolve_reviewer_login,
) -> dict[str, str]:
    """Parse repeated ``--required-reviewer`` values into a ``{name: login}`` dict.

    Each entry is either:
      - an explicit ``reviewer_name:git_host_login`` pair, whose login is used
        verbatim (refused when *bare_only*); or
      - a BARE reviewer name, whose expected login is DERIVED platform-aware via
        ``merge.reviewer_login.resolve_reviewer_login``: the bare name itself on
        Forgejo, or the deployment's configured GitHub App slug plus ``[bot]`` on
        GitHub. The derived login stays tool-authoritative (resolved from config),
        never trusted from a PR comment's claimed identity.

    *resolve_login* is the role-to-login derivation; callers override it only to
    substitute a different derivation under test.

    Raises MergeUsageError on a malformed entry, on a pair when *bare_only*, or
    when a bare name has no derivable login on the platform, all before any
    network call.
    """
    if not raw:
        return {}
    result: dict[str, str] = {}
    for entry in raw:
        if ":" in entry:
            if bare_only:
                raise MergeUsageError(
                    f"--required-reviewer {entry!r} must be a bare role name: the login is "
                    f"derived from the role, and a login typed here is never accepted"
                )
            name, login = entry.split(":", 1)
            name, login = name.strip(), login.strip()
            if not name or not login:
                raise MergeUsageError(
                    f"--required-reviewer {entry!r} must be 'reviewer_name:git_host_login' "
                    f"with both parts non-empty."
                )
            result[name] = login
            continue
        name = entry.strip()
        if not name:
            raise MergeUsageError(
                f"--required-reviewer {entry!r} must be a non-empty reviewer "
                f"name, or 'reviewer_name:git_host_login'."
            )
        try:
            result[name] = resolve_login(name, platform)
        except ReviewerLoginNotConfiguredError as exc:
            raise MergeUsageError(str(exc)) from exc
    return result


def resolve_required_reviewers(
    repo_gate: RepoGate,
    platform: str,
    flagged: Mapping[str, str],
    *,
    ignore_repo_gate: bool = False,
    overridable: bool = True,
) -> RequiredReviewers:
    """The roles required for a merge: *repo_gate*'s declared roles (the floor)
    plus *flagged* (already parsed by ``parse_required_reviewers``).

    With *ignore_repo_gate* only *flagged* applies. *overridable* names whether
    the caller offers that flag, so a refusal does not point at one it lacks.

    Raises MergeUsageError when the gate declares scanners for a role that is not
    required, since such a declaration could never gate anything.
    """
    gate = with_resolvable_reviewer_roles(repo_gate, platform, flagged_roles=flagged)
    if ignore_repo_gate:
        return RequiredReviewers(dict(flagged), gate, {})
    undeclared = [role for role in gate.reviewer_roles if role not in flagged]
    # Every declared role resolves here: an unresolvable one was already dropped,
    # with its scanners, in with_resolvable_reviewer_roles.
    floor_only = parse_required_reviewers(undeclared, platform)
    reviewers = {**flagged, **floor_only}
    unreachable = gate.unreachable_scanner_roles(reviewers)
    if unreachable:
        remedy = "; --ignore-repo-gate overrides it (logged and attested)." if overridable else "."
        raise MergeUsageError(
            f"merge.required_scanners declares scanners for role(s) {unreachable!r}, "
            f"which are not required reviewers (required: {sorted(reviewers)!r}). "
            f"Scanners are checked on a required reviewer's verdict, so this "
            f"declaration could never gate anything. Add the role to "
            f"merge.required_reviewer_roles or --required-reviewer, or remove it from "
            f"merge.required_scanners{remedy}"
        )
    return RequiredReviewers(reviewers, gate, floor_only)


__all__ = ["RequiredReviewers", "parse_required_reviewers", "resolve_required_reviewers"]
