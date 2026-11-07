"""doctor.identity_check -- which identity provider the deployment configured.

Advisory only: the check never fails the run and never resolves an identity, so
it reads no token and no delivery directory. It reports the configured provider,
the key-set sources it trusts, whether the provider's answer is self-declared and
how many `prefer-assertion` fallbacks the telemetry sink has recorded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping

from clagentic_loadout.doctor.checks import CheckResult
from clagentic_loadout.identity import (
    PROVIDER_STATIC,
    IdentityError,
    has_legacy_attestation_config,
    load_identity_config,
)
from clagentic_loadout.identity.seam import FALLBACK_EVENT_SCHEMA
from clagentic_loadout.telemetry import FilesystemSink, SinkConfigError, resolve_sink

CHECK_NAME = "identity_provider"


def _fallback_count(env: Mapping[str, str]) -> int | None:
    """Recorded `prefer-assertion` fallbacks, or None when the sink keeps no
    readable record (no sink, a webhook, or one that cannot be resolved)."""
    try:
        sink = resolve_sink(dict(env))
    except SinkConfigError:
        return None
    if not isinstance(sink, FilesystemSink):
        return None
    prefix = FALLBACK_EVENT_SCHEMA.partition("/")[0]
    count = 0
    for path in sorted(sink.directory.glob(f"{prefix}*.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(record, dict) and record.get("schema") == FALLBACK_EVENT_SCHEMA:
                count += 1
    return count


def _key_set_sources(cfg) -> list[dict[str, str]] | None:
    """The `{iss, jwks}` pairs the provider trusts; None for a provider that
    verifies no assertion, or whose issuer config is invalid."""
    try:
        _kind, issuers = cfg.assertion_issuers()
    except IdentityError:
        return None
    return [{"iss": issuer.iss, "jwks": issuer.locator} for issuer in issuers]


def check_identity_provider(
    *, config_root: str | Path | None = None, env: Mapping[str, str] | None = None
) -> CheckResult:
    active_env = os.environ if env is None else env
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
        resolved={
            "provider": cfg.provider,
            "self_declared": self_declared,
            "jwks_sources": _key_set_sources(cfg),
            "fallback_count": _fallback_count(active_env),
        },
    )
