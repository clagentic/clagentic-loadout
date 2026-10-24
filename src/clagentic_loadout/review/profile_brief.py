"""review.profile_brief — what a repo profile says that a builder is held to.

One function, ``profile_brief``, turns a parsed profile into the plain-data
brief: the class, the rule statements each class in effect carries, the per-path
class overrides, and the commands the profile declares for running tests. It is
the single place that decides what that brief holds, so a verb that exposes it
and a document that embeds it (the standard spec) cannot disagree.

``read_profile`` reads the profile the way ``loadout-profile check`` does, from
the working tree or a revision, through the same loader the review uses.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from clagentic_loadout.repo_test.spec import DeclaredTests
from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReader, PolicyReadError
from clagentic_loadout.review.policy_source import (
    PROFILE_PATH,
    STAKES_PATH,
    profile_completeness,
    read_repo_profile,
)
from clagentic_loadout.review.profile_posture import CompletenessItem
from clagentic_loadout.review.profile_rules import classes_in_effect, rule_statements
from clagentic_loadout.review.profile_verb import WorkingTreeReader
from clagentic_loadout.review.repo_profile import RepoProfile


class ProfileReadError(Exception):
    """The repository could not be listed at all, so nothing is known about its
    profile. A missing or invalid profile is not this: ``read_profile`` reports
    those as a state."""


def declared_tests(tests: DeclaredTests | None) -> dict[str, Any] | None:
    """The commands a profile's ``test`` block declares, as plain data, or None
    when it declares none."""
    if tests is None:
        return None
    out: dict[str, Any] = {
        key: list(getattr(tests, key))
        for key in ("full", "impacted", "files")
        if getattr(tests, key)
    }
    if tests.base:
        out["base"] = tests.base
    if tests.tiers:
        out["tiers"] = {name: list(argv) for name, argv in tests.tiers}
    return out or None


def profile_brief(profile: RepoProfile) -> dict[str, Any]:
    """The brief for *profile*: ``class``, ``class_rules`` (the statements of
    every class in effect, keyed by class), ``paths`` (the per-path class
    overrides) and ``tests`` (the declared test commands, or None)."""
    return {
        "class": profile.repo_class,
        "class_rules": {
            name: rule_statements(profile, name) for name in classes_in_effect(profile)
        },
        "paths": [{"glob": glob, "class": value} for glob, value in profile.paths],
        "tests": declared_tests(profile.test),
    }


def read_profile(
    repo_path: str | Path, ref: str | None = None
) -> tuple[RepoProfile | None, str, str | None]:
    """``(profile, state, problem)`` for the repository at *repo_path*: its
    working tree, or revision *ref* when given. *state* is one of the
    review.policy_source PROFILE_* values. Raises ProfileReadError when the
    repository cannot be listed."""
    reader, base, present = _open(repo_path, ref)
    return read_repo_profile(reader, base, present)


def _open(repo_path: str | Path, ref: str | None) -> tuple[PolicyReader, str, set[str]]:
    root = Path(repo_path)
    reader: PolicyReader
    if ref is not None:
        reader = GitPolicyReader(root)
        base = ref
    else:
        reader = WorkingTreeReader(root)
        base = "working tree"
    try:
        present = set(reader.list_paths(base))
    except PolicyReadError as exc:
        raise ProfileReadError(f"cannot list {str(repo_path)!r} at {base}: {exc}") from exc
    return reader, base, present


def read_completeness(
    repo_path: str | Path,
    ref: str | None = None,
    *,
    host_visibility: str | None = None,
    now: dt.datetime | None = None,
) -> tuple[CompletenessItem, ...]:
    """What the repo profile of the repository at *repo_path* is missing, read
    the way ``read_profile`` reads it. For reporting only: neither the push
    verification report nor the spec is a gate input, and the gates read the
    merge base through ``read_policy``. Raises ProfileReadError when the
    repository cannot be listed."""
    reader, base, present = _open(repo_path, ref)
    profile, state, problem = read_repo_profile(reader, base, present)
    _, items = profile_completeness(
        profile,
        state,
        problem,
        stakes_present=STAKES_PATH in present,
        host_visibility=host_visibility,
        now=now,
    )
    return items


__all__ = [
    "PROFILE_PATH",
    "ProfileReadError",
    "declared_tests",
    "profile_brief",
    "read_completeness",
    "read_profile",
]
