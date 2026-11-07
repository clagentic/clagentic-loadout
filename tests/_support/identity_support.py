"""Shared helpers for the identity tests: a throwaway issuer that signs
assertions, a delivery directory, and a user-level config writer."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import jwt
import yaml
from cryptography.hazmat.primitives.asymmetric import ec, ed25519
from jwt.algorithms import ECAlgorithm, OKPAlgorithm

ISSUER = "https://issuer.example"
CONTRACT_DIR = Path(__file__).resolve().parents[1] / "identity_contract"


class Issuer:
    """A throwaway signing key plus the key set that verifies it."""

    def __init__(self, iss: str = ISSUER, alg: str = "EdDSA", kid: str = "k1") -> None:
        self.iss, self.alg, self.kid = iss, alg, kid
        if alg == "EdDSA":
            self._key = ed25519.Ed25519PrivateKey.generate()
            public = OKPAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            public = ECAlgorithm.to_jwk(self._key.public_key(), as_dict=True)
        self.jwks = {"keys": [{**public, "kid": kid, "alg": alg, "use": "sig"}]}

    def claims(self, directory: str, **overrides) -> dict:
        now = int(time.time())
        claims = {
            "iss": self.iss,
            "sub": "agent:example-builder",
            "roles": ["builder"],
            "grade": "workload",
            "rt": {"id": "example-runtime", "version": "1.0.0"},
            "sid": "sess-1",
            "origin": "agent",
            "dir": directory,
            "iat": now - 5,
            "exp": now + 600,
            "jti": "jti-1",
            "cia": 1,
        }
        claims.update(overrides)
        return {k: v for k, v in claims.items() if v is not None}

    def sign(self, claims: dict) -> str:
        return jwt.encode(claims, self._key, algorithm=self.alg, headers={"kid": self.kid})

    def write_jwks(self, path: Path) -> str:
        path.write_text(json.dumps(self.jwks))
        path.chmod(0o600)
        return "file://" + str(path)


def make_delivery(root: Path, token: str, name: str = "delivery") -> str:
    directory = root / name
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    (directory / "identity.jwt").write_text(token + "\n")
    return str(directory)


def write_config(config_root: Path, identity: dict | None = None, **sections: dict) -> Path:
    config_root.mkdir(parents=True, exist_ok=True)
    body = dict(sections)
    if identity is not None:
        body["identity"] = identity
    (config_root / "config.yaml").write_text(yaml.safe_dump(body))
    return config_root


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CLAGENTIC", "LOGNAME", "USER"))}
    env.update(extra)
    return env
