"""doctor.identity_check -- which identity provider the deployment configured.

Advisory only: the check never fails the run and never resolves an identity, so
it reads no token and no delivery directory. It reports the configured provider
and whether that provider's answer is self-declared.
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.doctor.checks import CheckResult
from clagentic_loadout.identity.config import PROVIDER_STATIC, load_identity_config
from clagentic_loadout.identity.errors import IdentityError
from clagentic_loadout.identity.seam import has_legacy_attestation_config

CHECK_NAME = "identity_provider"


def check_identity_provider(*, config_root: str | Path | None = None) -> CheckResult:
    try:
        cfg = load_identity_config(config_root=config_root)
    except IdentityError as exc:
        return CheckResult(
            name=CHECK_NAME,
            ok=True,
            summary=f"WARN: identity config is invalid ({exc.detail})",
            resolved={"provider": None, "valid": False},
        )
    if cfg.provider is None:
        legacy = has_legacy_attestation_config(config_root=config_root)
        return CheckResult(
            name=CHECK_NAME,
            ok=True,
            summary=(
                "identity.provider is not set; the legacy attestation chain applies"
                if legacy
                else "WARN: identity.provider is not set and no legacy attestation keys are "
                "configured; the identity seam refuses until a provider is chosen"
            ),
            resolved={"provider": None, "legacy_attestation": legacy, "self_declared": None},
        )
    self_declared = cfg.provider == PROVIDER_STATIC
    return CheckResult(
        name=CHECK_NAME,
        ok=True,
        summary=f"identity.provider is {cfg.provider} (self_declared: {str(self_declared).lower()})",
        resolved={"provider": cfg.provider, "self_declared": self_declared},
    )
