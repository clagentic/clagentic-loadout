"""identity.actor -- the one value the provider seam returns."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class Actor:
    """Who is acting, as resolved by a provider.

    `sub` is who the delegation chain acts for; `actor` is the current actor
    (the outermost `act.sub`, else `sub`). `chain` runs from the current actor
    outward and ends with `sub`. `roles` is a ceiling, never a grant.
    `provider` names what produced the value (`gatekeeper`, `jwks:<iss>`,
    `declared`, `module:<name>`, `legacy:<source>`). `self_declared` is true when
    nothing outside this process vouched for the value.
    """

    sub: str
    actor: str
    chain: tuple[str, ...]
    roles: tuple[str, ...]
    grade: str
    origin: str
    provider: str
    self_declared: bool
    dir: str | None = None
    sid: str | None = None
    spawn: str | None = None
    contract_version: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["chain"] = list(self.chain)
        out["roles"] = list(self.roles)
        return out


def actor_from_claims(claims: Mapping[str, Any], *, provider: str) -> Actor:
    """Build an `Actor` from claims that already passed verification."""
    sub = claims["sub"]
    levels: list[str] = []
    act = claims.get("act")
    while isinstance(act, dict):
        levels.append(act["sub"])
        act = act.get("act")
    chain = tuple(levels) + (sub,)
    return Actor(
        sub=sub,
        actor=chain[0],
        chain=chain,
        roles=tuple(claims["roles"]),
        grade=claims["grade"],
        origin=claims["origin"],
        provider=provider,
        self_declared=False,
        dir=claims.get("dir"),
        sid=claims["sid"],
        spawn=claims.get("spawn"),
        contract_version=claims["cia"],
    )
