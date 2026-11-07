"""identity.claims -- claim shape rules of the identity assertion contract.

Pure functions over an already-decoded payload; no I/O. The rules are the
required-claims table, the name patterns and the per-major origin vocabulary of
the published contract.
"""

from __future__ import annotations

import re
from typing import Any, Mapping

from clagentic_loadout.identity import errors

SUPPORTED_MAJORS = (1, 2)
MAX_LIFETIME_SECONDS = 43200

_NAME = r"[A-Za-z0-9._-]{1,128}"
_ACTOR_STRING = re.compile(rf"(human|agent|runtime):{_NAME}")
_ACTOR_SUBJECT = re.compile(rf"(agent|runtime):{_NAME}")
_ROLE = re.compile(r"[a-z][a-z0-9_-]{0,63}")

GRADES = ("workload", "harness")
_ORIGINS_V1 = ("human", "scheduled", "loop", "trigger", "agent")
_ORIGINS_BY_MAJOR = {1: _ORIGINS_V1, 2: _ORIGINS_V1 + ("driven",)}
_ALWAYS_REQUIRED = ("iss", "sub", "roles", "grade", "rt", "sid", "origin", "iat", "exp", "jti", "cia")
_MAX_ACT_DEPTH = 32


def _bad(message: str) -> errors.IdentityError:
    return errors.IdentityError(errors.CLAIMS_INVALID, message)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _check_act(act: Any) -> None:
    depth = 0
    while act is not None:
        depth += 1
        if depth > _MAX_ACT_DEPTH:
            raise _bad("act chain is too deep")
        if not isinstance(act, dict):
            raise _bad("act must be an object")
        sub = act.get("sub")
        if not isinstance(sub, str) or not _ACTOR_SUBJECT.fullmatch(sub):
            raise _bad("act.sub must be an agent or runtime actor")
        act = act.get("act")


def check_times(claims: Mapping[str, Any], *, now: int) -> None:
    """Step 7: time claims. Types first, then the comparisons, zero leeway."""
    for name in ("iat", "exp"):
        if name not in claims:
            raise _bad(f"{name} is required")
    for name in ("iat", "exp", "nbf"):
        if name in claims and (type(claims[name]) is not int or claims[name] < 0):
            raise _bad(f"{name} must be an integer NumericDate")
    iat, exp = claims["iat"], claims["exp"]
    if exp <= now:
        raise errors.IdentityError(errors.EXPIRED, f"exp {exp} is not after now {now}")
    nbf = claims.get("nbf")
    if (nbf is not None and now < nbf) or iat > now:
        raise errors.IdentityError(errors.NOT_YET_VALID, "assertion is not valid yet")
    if exp - iat > MAX_LIFETIME_SECONDS:
        raise errors.IdentityError(
            errors.TTL_EXCEEDED, f"lifetime exceeds {MAX_LIFETIME_SECONDS} seconds"
        )


def check_shape(claims: Mapping[str, Any]) -> None:
    """Step 8: claim types, name patterns and the required-claims table."""
    for name in _ALWAYS_REQUIRED:
        if name not in claims:
            raise _bad(f"{name} is required")
    cia = claims["cia"]
    if not _nonempty_str(claims["iss"]):
        raise _bad("iss must be a non-empty string")
    sub = claims["sub"]
    if not isinstance(sub, str) or not _ACTOR_STRING.fullmatch(sub):
        raise _bad("sub must be a human, agent or runtime actor")
    roles = claims["roles"]
    if (
        not isinstance(roles, list)
        or not all(isinstance(r, str) and _ROLE.fullmatch(r) for r in roles)
        or len(set(roles)) != len(roles)
    ):
        raise _bad("roles must be a list of unique role names")
    grade = claims["grade"]
    if grade not in GRADES or not isinstance(grade, str):
        raise _bad("grade must be workload or harness")
    rt = claims["rt"]
    if not (isinstance(rt, dict) and _nonempty_str(rt.get("id")) and _nonempty_str(rt.get("version"))):
        raise _bad("rt must carry id and version")
    for name in ("sid", "jti"):
        if not _nonempty_str(claims[name]):
            raise _bad(f"{name} must be a non-empty string")
    origin = claims["origin"]
    if not isinstance(origin, str) or origin not in _ORIGINS_BY_MAJOR[cia]:
        raise _bad(f"origin {origin!r} is not valid for contract major {cia}")
    for name in ("spawn", "aud"):
        if name in claims and not _nonempty_str(claims[name]):
            raise _bad(f"{name} must be a non-empty string")
    if "dir" in claims and not (isinstance(claims["dir"], str) and claims["dir"].startswith("/")):
        raise _bad("dir must be an absolute path")
    if "act" in claims:
        _check_act(claims["act"])

    if "aud" in claims:
        if "dir" in claims:
            raise _bad("an exchanged token must not carry dir")
    elif "dir" not in claims:
        raise _bad("a local assertion requires dir")
    if grade == "harness":
        for name in ("act", "spawn"):
            if name not in claims:
                raise _bad(f"a harness assertion requires {name}")
    if origin == "driven":
        if grade != "workload":
            raise _bad("origin driven requires grade workload")
        if sub.startswith("human:"):
            raise _bad("origin driven never names a human sub")
