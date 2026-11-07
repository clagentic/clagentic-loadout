"""clagentic_loadout.identity -- the pluggable identity provider seam.

`resolve()` returns an `Actor` from the provider named by `identity.provider`, or
raises `IdentityError`. See docs/identity.md.
"""

from __future__ import annotations

from clagentic_loadout.identity.actor import Actor
from clagentic_loadout.identity.config import PROVIDER_STATIC, load_identity_config
from clagentic_loadout.identity.errors import IdentityError
from clagentic_loadout.identity.seam import has_legacy_attestation_config, resolve
from clagentic_loadout.identity.verifier import verify

__all__ = [
    "Actor",
    "IdentityError",
    "PROVIDER_STATIC",
    "has_legacy_attestation_config",
    "load_identity_config",
    "resolve",
    "verify",
]
