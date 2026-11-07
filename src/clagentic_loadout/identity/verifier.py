"""identity.verifier -- the identity assertion verifier.

Implements the verification order of the published contract (sections 4 and 7,
failure codes in `identity.errors`) over a standard JWS library for the
signature step. Header and payload parsing, the algorithm allow-list, issuer
trust, key selection, time, claim shape, audience and the directory binding are
this module's own; the library only checks the signature once the key has been
chosen from the trusted issuer's key set, never from the token.

Accepted contract majors: 1 and 2 (`claims.SUPPORTED_MAJORS`).
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Callable, Collection, Mapping

import jwt
from jwt.exceptions import PyJWTError

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.claims import SUPPORTED_MAJORS, check_shape, check_times

MODE_LOCAL = "local"
MODE_PEER = "peer"

_ALGS = {"EdDSA": ("OKP", "Ed25519"), "ES256": ("EC", "P-256")}
_B64URL = re.compile(r"[A-Za-z0-9_-]*")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"non-finite number {name}")


_DECODER = json.JSONDecoder(parse_constant=_reject_constant)

JwksLoader = Callable[[str], Mapping[str, Any]]


def _malformed(message: str) -> errors.IdentityError:
    return errors.IdentityError(errors.MALFORMED, message)


def _segment_object(segment: str, what: str) -> dict[str, Any]:
    if not _B64URL.fullmatch(segment):
        raise _malformed(f"{what} is not base64url")
    try:
        text = base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)).decode("utf-8")
        value, end = _DECODER.raw_decode(text.lstrip())
    except (ValueError, UnicodeDecodeError) as exc:
        raise _malformed(f"{what} is not a JSON object") from exc
    rest = text.lstrip()[end:]
    if rest.strip():
        raise _malformed(f"{what} has content after the JSON object")
    if not isinstance(value, dict):
        raise _malformed(f"{what} is not a JSON object")
    return value


def _select_key(jwks: Mapping[str, Any], kid: Any) -> Mapping[str, Any]:
    keys = jwks.get("keys")
    if isinstance(kid, str) and isinstance(keys, list):
        for key in keys:
            if isinstance(key, dict) and key.get("kid") == kid:
                return key
    raise errors.IdentityError(errors.KEY_NOT_FOUND, f"no key {kid!r} in the issuer key set")


def _check_signature(token: str, alg: str, key: Mapping[str, Any]) -> None:
    kty, crv = _ALGS[alg]
    if key.get("kty") != kty or key.get("crv") != crv:
        raise errors.IdentityError(errors.SIGNATURE_INVALID, "key type does not match alg")
    try:
        public_key = jwt.PyJWK.from_dict(dict(key), algorithm=alg).key
        jwt.api_jws.PyJWS().decode_complete(token, public_key, algorithms=[alg])
    except (PyJWTError, ValueError, TypeError) as exc:
        raise errors.IdentityError(errors.SIGNATURE_INVALID, "signature does not verify") from exc


def verify(
    token: str,
    *,
    trusted_issuers: Collection[str],
    jwks_loader: JwksLoader,
    mode: str = MODE_LOCAL,
    observed_dir: str | None = None,
    peer_id: str | None = None,
    now: int | None = None,
) -> dict[str, Any]:
    """Verify *token* and return its claims, or raise `IdentityError`.

    `jwks_loader(iss)` returns the key set of an issuer already found in
    *trusted_issuers*. Local mode needs *observed_dir* (the directory the token
    was read from); peer mode needs *peer_id*.
    """
    if mode not in (MODE_LOCAL, MODE_PEER):
        raise ValueError(f"unknown verification mode {mode!r}")
    if mode == MODE_LOCAL and not observed_dir:
        raise errors.IdentityError(
            errors.IDENTITY_DIR_MISSING, "local verification needs the directory the token was read from"
        )
    if mode == MODE_PEER and not peer_id:
        raise ValueError("peer verification needs peer_id")

    # 1. Structure.
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise _malformed("not a three-segment compact JWS")
    header = _segment_object(parts[0], "header")
    payload = _segment_object(parts[1], "payload")
    if "crit" in header:
        raise _malformed("header carries crit")

    # 2. Algorithm allow-list.
    alg = header.get("alg")
    if not isinstance(alg, str) or alg not in _ALGS:
        raise errors.IdentityError(errors.ALG_NOT_ALLOWED, f"alg {alg!r} is not allowed")

    # 3. Issuer trust, by exact string equality.
    iss = payload.get("iss")
    if not isinstance(iss, str) or iss not in trusted_issuers:
        raise errors.IdentityError(errors.ISSUER_UNTRUSTED, f"issuer {iss!r} is not trusted")

    # 4-5. Key from the trusted issuer's key set, never from the token.
    key = _select_key(jwks_loader(iss), header.get("kid"))
    _check_signature(token, alg, key)

    # 6. Contract major.
    cia = payload.get("cia")
    if type(cia) is not int or cia not in SUPPORTED_MAJORS:
        raise errors.IdentityError(
            errors.CONTRACT_VERSION_UNSUPPORTED,
            f"cia {cia!r} is not a supported major ({', '.join(map(str, SUPPORTED_MAJORS))})",
        )

    # 7-8. Time, then shape.
    check_times(payload, now=int(time.time()) if now is None else now)
    check_shape(payload)

    # 9. Audience.
    if mode == MODE_LOCAL:
        if "aud" in payload:
            raise errors.IdentityError(
                errors.AUDIENCE_UNEXPECTED, "a token minted for a peer is not an identity for this host"
            )
        # 10. Directory binding, lexical only.
        if os.path.normpath(payload["dir"]) != os.path.normpath(observed_dir):
            raise errors.IdentityError(
                errors.DIR_MISMATCH, "dir claim differs from the directory the token was read from"
            )
    elif payload.get("aud") != peer_id:
        raise errors.IdentityError(errors.AUDIENCE_MISMATCH, "aud is absent or not this peer")
    return payload
