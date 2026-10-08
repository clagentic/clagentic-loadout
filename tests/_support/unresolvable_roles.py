"""Make chosen declared reviewer roles unresolvable on a Forgejo-defaulting verb.

On Forgejo a declared role always resolves (to its mapped caller, else to the
bare role), so a test of the gate's per-role drop/rename behaviour cannot reach
it through the real Forgejo branch. This stands in for the platform whose
direct resolution fails (a GitHub deployment with no slug for the role): for the
chosen roles the Forgejo branch behaves as `resolve_declared_role` does when the
role's own slug is missing, i.e. it falls to the optional role mapping and
raises when there is none. Every other role resolves through the real branch.
The seam is the one resolver both the gate and the doctor call."""

from __future__ import annotations

from typing import Callable

from clagentic_loadout.merge import reviewer_login


def make_roles_unresolvable(monkeypatch, is_unresolvable: Callable[[str], bool]) -> None:
    real = reviewer_login.resolve_forgejo_declared_role

    def resolve(role: str):
        if is_unresolvable(role):
            unresolved = reviewer_login.ReviewerLoginNotConfiguredError(
                f"no GitHub App slug configured for reviewer {role!r}"
            )
            return reviewer_login.resolve_role_via_mapping(role, unresolved)
        return real(role)

    monkeypatch.setattr(reviewer_login, "resolve_forgejo_declared_role", resolve)
