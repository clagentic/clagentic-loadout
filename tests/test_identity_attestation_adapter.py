"""The thin adapter to the legacy Identity, the narrow per-spawn entry, the
no-default isolation guarantee and the doctor check."""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.doctor.identity_check import check_identity_provider
from clagentic_loadout.identity import Actor
from clagentic_loadout.transport import attestation
from tests._support.identity_support import clean_env, write_config


def _actor(name: str) -> Actor:
    return Actor(
        sub=name, actor=name, chain=(name,), roles=(), grade="workload", origin="agent",
        provider="gatekeeper", self_declared=False,
    )


def test_identity_from_actor_keeps_the_existing_identity_shape() -> None:
    identity = attestation.identity_from_actor(_actor("agent:x"))
    assert identity == attestation.Identity("agent:x", attestation.SOURCE_ASSERTION)


def test_resolve_bound_identity_default_path_is_unchanged(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "cfg", None, attestation={"bound_identity": "builtin-fallback"})
    env = clean_env(USER="someone", LOGNAME="someone")
    assert attestation.resolve_bound_identity(env=env, config_root=cfg).source == attestation.SOURCE_BUILTIN


def test_loading_config_with_no_identity_provider_does_not_touch_the_seam(tmp_path: Path) -> None:
    # The no-default refusal lives in the seam only: the legacy chain resolves on a
    # config with no identity section, exactly as before.
    cfg = write_config(tmp_path / "cfg", None, attestation={"identity_env": "WHO"})
    identity = attestation.resolve_bound_identity(env={"WHO": "agent:old"}, config_root=cfg)
    assert identity == attestation.Identity("agent:old", attestation.SOURCE_CONFIGURED)


@pytest.mark.parametrize(
    "identity,attest,fragment,self_declared",
    [
        ({"provider": "static", "static": {"actor": "agent:x"}}, None, "static (self_declared: true)", True),
        ({"provider": "gatekeeper"}, None, "gatekeeper (self_declared: false)", False),
        (None, {"identity_env": "X"}, "legacy attestation chain applies", None),
        (None, None, "WARN: identity.provider is not set", None),
        ({"provider": "bogus"}, None, "WARN: identity config is invalid", None),
    ],
)
def test_doctor_reports_the_provider_and_never_fails(
    tmp_path: Path, identity, attest, fragment: str, self_declared
) -> None:
    sections = {"attestation": attest} if attest else {}
    cfg = write_config(tmp_path / "cfg", identity, **sections)
    result = check_identity_provider(config_root=cfg)
    assert result.ok is True
    assert fragment in result.summary
    if "valid" not in result.resolved:
        assert result.resolved["self_declared"] is self_declared
