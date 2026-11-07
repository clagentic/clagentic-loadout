"""The identity provider seam: one config table, one refusal table."""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from clagentic_loadout.identity import Actor, IdentityError, resolve
from clagentic_loadout.identity import errors
from tests._support.identity_support import ISSUER, Issuer, clean_env, make_delivery, write_config

DIR_VAR = "CLAGENTIC_IDENTITY_DIR"


@pytest.fixture
def issuer() -> Issuer:
    return Issuer()


def _gatekeeper_cfg(tmp_path: Path, issuer: Issuer) -> dict:
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    return {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}


def _token_env(tmp_path: Path, issuer: Issuer, **overrides) -> dict:
    directory = str(tmp_path / "delivery")
    token = issuer.sign(issuer.claims(directory, **overrides))
    return clean_env(**{DIR_VAR: make_delivery(tmp_path, token)})


def test_gatekeeper_returns_the_signed_claims(tmp_path: Path, issuer: Issuer) -> None:
    cfg = write_config(tmp_path / "cfg", _gatekeeper_cfg(tmp_path, issuer))
    env = _token_env(
        tmp_path,
        issuer,
        sub="human:example-user",
        act={"sub": "agent:example-builder", "act": {"sub": "runtime:example-runtime"}},
        roles=["builder", "reviewer"],
        grade="harness",
        spawn="sp-1",
    )
    actor = resolve(env=env, config_root=cfg)
    assert isinstance(actor, Actor)
    assert actor.actor == "agent:example-builder"
    assert actor.sub == "human:example-user"
    assert actor.chain == ("agent:example-builder", "runtime:example-runtime", "human:example-user")
    assert actor.roles == ("builder", "reviewer")
    assert actor.grade == "harness"
    assert actor.provider == "gatekeeper"
    assert actor.self_declared is False
    assert actor.contract_version == 1 and actor.sid == "sess-1" and actor.spawn == "sp-1"


def test_jwks_provider_names_the_issuer_and_needs_no_gatekeeper(tmp_path: Path) -> None:
    launcher = Issuer(iss="file://" + str(tmp_path / "launcher-jwks.json"), alg="ES256", kid="es")
    locator = launcher.write_jwks(tmp_path / "launcher-jwks.json")
    assert locator == launcher.iss
    cfg = write_config(tmp_path / "cfg", {"provider": "jwks", "issuers": [{"iss": launcher.iss}]})
    env = _token_env(tmp_path, launcher)
    actor = resolve(env=env, config_root=cfg)
    assert actor.provider == f"jwks:{launcher.iss}"
    assert actor.self_declared is False


def test_driven_origin_is_workload_grade_never_human(tmp_path: Path, issuer: Issuer) -> None:
    cfg = write_config(tmp_path / "cfg", _gatekeeper_cfg(tmp_path, issuer))
    env = _token_env(tmp_path, issuer, cia=2, origin="driven", sub="runtime:example-runtime")
    actor = resolve(env=env, config_root=cfg)
    assert actor.grade == "workload" and actor.origin == "driven"
    assert actor.contract_version == 2


@pytest.mark.parametrize("provider", ["gatekeeper", "jwks"])
@pytest.mark.parametrize("failure", ["unset", "relative", "absent", "unsafe_mode", "invalid_token"])
def test_assertion_providers_refuse_without_any_fallback(
    tmp_path: Path, issuer: Issuer, provider: str, failure: str
) -> None:
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    identity = (
        {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
        if provider == "gatekeeper"
        else {"provider": "jwks", "issuers": [{"iss": ISSUER, "jwks": jwks}]}
    )
    # A legacy chain that would happily answer must never be consulted.
    cfg = write_config(
        tmp_path / "cfg",
        identity,
        attestation={"identity_env": "LEGACY_WHO", "bound_identity": "builtin-fallback"},
    )
    env = clean_env(LEGACY_WHO="parent-session", USER="root", LOGNAME="root")
    if failure == "relative":
        env[DIR_VAR] = "relative/dir"
    elif failure == "absent":
        env[DIR_VAR] = str(tmp_path / "nowhere")
    elif failure == "unsafe_mode":
        directory = make_delivery(tmp_path, issuer.sign(issuer.claims(str(tmp_path / "delivery"))))
        Path(directory).chmod(0o755)
        env[DIR_VAR] = directory
    elif failure == "invalid_token":
        env[DIR_VAR] = make_delivery(tmp_path, "not.a.token")
    with pytest.raises(IdentityError) as caught:
        resolve(env=env, config_root=cfg)
    message = str(caught.value)
    if failure != "invalid_token":
        assert DIR_VAR in message
    assert "parent-session" not in message
    assert caught.value.code in errors.EXIT_CODES and caught.value.code != errors.ATTESTATION_REFUSED


@pytest.mark.parametrize(
    "case,expected",
    [
        ("unknown_key", errors.KEY_NOT_FOUND),
        ("expired", errors.EXPIRED),
        ("wrong_major", errors.CONTRACT_VERSION_UNSUPPORTED),
        ("untrusted_issuer", errors.ISSUER_UNTRUSTED),
        ("wrong_dir", errors.DIR_MISMATCH),
        ("symlinked_file", errors.IDENTITY_FILE_SYMLINK),
        ("missing_file", errors.IDENTITY_FILE_MISSING),
    ],
)
def test_each_refusal_names_its_class(tmp_path: Path, issuer: Issuer, case: str, expected: str) -> None:
    cfg = write_config(tmp_path / "cfg", _gatekeeper_cfg(tmp_path, issuer))
    directory = str(tmp_path / "delivery")
    claims = issuer.claims(directory)
    signer = issuer
    if case == "unknown_key":
        signer = Issuer(kid="other")
    elif case == "expired":
        claims = issuer.claims(directory, iat=int(time.time()) - 100, exp=int(time.time()) - 10)
    elif case == "wrong_major":
        claims = issuer.claims(directory, cia=3)
    elif case == "untrusted_issuer":
        claims = issuer.claims(directory, iss="https://other.example")
    elif case == "wrong_dir":
        claims = issuer.claims(str(tmp_path / "somewhere-else"))
    token = signer.sign(claims)
    delivered = make_delivery(tmp_path, token)
    if case == "symlinked_file":
        real = tmp_path / "real.jwt"
        real.write_text(token)
        (Path(delivered) / "identity.jwt").unlink()
        (Path(delivered) / "identity.jwt").symlink_to(real)
    elif case == "missing_file":
        (Path(delivered) / "identity.jwt").unlink()
    with pytest.raises(IdentityError) as caught:
        resolve(env=clean_env(**{DIR_VAR: delivered}), config_root=cfg)
    assert caught.value.code == expected
    assert expected in str(caught.value)


def test_static_returns_the_declaration_and_reads_no_environment(tmp_path: Path) -> None:
    cfg = write_config(
        tmp_path / "cfg", {"provider": "static", "static": {"actor": "agent:solo", "roles": ["builder"]}}
    )
    env = {DIR_VAR: str(tmp_path / "unused"), "USER": "someone", "CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV": "x"}
    actor = resolve(env=env, config_root=cfg)
    assert (actor.actor, actor.roles, actor.provider, actor.self_declared) == (
        "agent:solo",
        ("builder",),
        "declared",
        True,
    )
    assert resolve(env={}, config_root=cfg) == actor


def test_provider_module_actor_is_used_and_its_name_recorded(tmp_path: Path, monkeypatch) -> None:
    module_dir = tmp_path / "mods"
    module_dir.mkdir()
    (module_dir / "example_identity_module.py").write_text(
        "from clagentic_loadout.identity import Actor\n"
        "def resolve(*, env, config):\n"
        "    return Actor(sub='agent:mod', actor='agent:mod', chain=('agent:mod',), roles=('reader',),\n"
        "                 grade='workload', origin='agent', provider='gatekeeper', self_declared=False)\n"
    )
    monkeypatch.syspath_prepend(str(module_dir))
    cfg = write_config(tmp_path / "cfg", {"provider_module": "example_identity_module"})
    actor = resolve(env={}, config_root=cfg)
    assert actor.actor == "agent:mod"
    assert actor.provider == "module:example_identity_module"


@pytest.mark.parametrize(
    "body,expected",
    [
        ("", errors.PROVIDER_MODULE_INVALID),
        ("def resolve(*, env, config):\n    return 'agent:x'\n", errors.PROVIDER_MODULE_INVALID),
        ("def resolve(*, env, config):\n    raise RuntimeError('boom')\n", errors.PROVIDER_MODULE_INVALID),
    ],
)
def test_provider_module_failures_are_refusals(tmp_path: Path, monkeypatch, body: str, expected: str) -> None:
    module_dir = tmp_path / "mods"
    module_dir.mkdir()
    (module_dir / "example_bad_module.py").write_text(body)
    monkeypatch.syspath_prepend(str(module_dir))
    cfg = write_config(tmp_path / "cfg", {"provider_module": "example_bad_module"})
    with pytest.raises(IdentityError) as caught:
        resolve(env={}, config_root=cfg)
    assert caught.value.code == expected
    sys.modules.pop("example_bad_module", None)


def test_unimportable_provider_module_is_a_refusal(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "cfg", {"provider_module": "no_such_identity_module"})
    with pytest.raises(IdentityError) as caught:
        resolve(env={}, config_root=cfg)
    assert caught.value.code == errors.PROVIDER_MODULE_INVALID


@pytest.mark.parametrize(
    "config,expected,names",
    [
        ({}, errors.PROVIDER_UNSET, "identity.provider"),
        (None, errors.PROVIDER_UNSET, "identity.provider"),
        ({"provider": "os-user"}, errors.CONFIG_INVALID, "identity.provider"),
        ({"provider": "gatekeeper"}, errors.CONFIG_INVALID, "identity.gatekeeper.jwks"),
        ({"provider": "jwks"}, errors.CONFIG_INVALID, "identity.issuers"),
        (
            {"provider": "jwks", "issuers": [{"iss": "urn:opaque"}]},
            errors.CONFIG_INVALID,
            "identity.issuers[1].jwks",
        ),
        ({"provider": "static"}, errors.CONFIG_INVALID, "identity.static.actor"),
        (
            {"provider": "gatekeeper", "provider_module": "m"},
            errors.CONFIG_INVALID,
            "identity.provider_module",
        ),
        (
            {"provider": "prefer-assertion"},
            errors.CONFIG_INVALID,
            "identity.assertion_provider",
        ),
    ],
)
def test_config_table(tmp_path: Path, config, expected: str, names: str) -> None:
    cfg = write_config(tmp_path / "cfg", config)
    with pytest.raises(IdentityError) as caught:
        resolve(env={}, config_root=cfg)
    assert caught.value.code == expected
    assert names in str(caught.value)


def test_no_config_file_at_all_refuses_naming_the_key(tmp_path: Path) -> None:
    with pytest.raises(IdentityError) as caught:
        resolve(env={}, config_root=tmp_path / "empty")
    assert "identity.provider" in str(caught.value)


def test_legacy_attestation_config_without_a_provider_keeps_the_legacy_chain(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "cfg", None, attestation={"identity_env": "LEGACY_WHO"})
    actor = resolve(env={"LEGACY_WHO": "agent:legacy"}, config_root=cfg)
    assert actor.actor == "agent:legacy"
    assert actor.provider == "legacy:configured"
    assert actor.self_declared is True


# --- prefer-assertion (the transition value) -------------------------------------------


def _prefer_cfg(tmp_path: Path, issuer: Issuer, **attestation) -> Path:
    identity = {
        "provider": "prefer-assertion",
        "assertion_provider": "gatekeeper",
        "gatekeeper": {"jwks": issuer.write_jwks(tmp_path / "jwks.json"), "issuer": ISSUER},
    }
    return write_config(tmp_path / "cfg", identity, attestation=attestation)


def test_prefer_assertion_answers_from_the_assertion_when_the_directory_is_set(
    tmp_path: Path, issuer: Issuer, capsys
) -> None:
    cfg = _prefer_cfg(tmp_path, issuer, identity_env="LEGACY_WHO")
    env = _token_env(tmp_path, issuer)
    env["LEGACY_WHO"] = "agent:legacy"
    actor = resolve(env=env, config_root=cfg)
    assert actor.provider == "gatekeeper"
    assert capsys.readouterr().err == ""


def test_prefer_assertion_directory_set_but_invalid_refuses_without_fallback(
    tmp_path: Path, issuer: Issuer
) -> None:
    cfg = _prefer_cfg(tmp_path, issuer, identity_env="LEGACY_WHO")
    env = clean_env(LEGACY_WHO="agent:legacy", **{DIR_VAR: make_delivery(tmp_path, "bad.token.here")})
    with pytest.raises(IdentityError):
        resolve(env=env, config_root=cfg)


def _per_spawn_adapter(tmp_path: Path, **extra) -> dict:
    return {
        "dir": str(tmp_path / "sc"),
        "file_prefix": "spawn-",
        "session_id_env": "SPAWN_ID",
        "scope": "per-spawn",
        **extra,
    }


def test_prefer_assertion_accepts_a_per_spawn_sidecar_with_one_notice_and_event(
    tmp_path: Path, issuer: Issuer, capsys
) -> None:
    (tmp_path / "sc").mkdir()
    (tmp_path / "sc" / "spawn-s1").write_text("agent:spawned\n")
    cfg = _prefer_cfg(tmp_path, issuer, sidecars=[_per_spawn_adapter(tmp_path)])
    events = tmp_path / "events"
    env = clean_env(
        SPAWN_ID="s1",
        CLAGENTIC_LOADOUT_TELEMETRY_SINK="filesystem",
        CLAGENTIC_LOADOUT_TELEMETRY_DIR=str(events),
    )
    actor = resolve(env=env, config_root=cfg)
    assert actor.provider == "legacy:sidecar-subagent" and actor.actor == "agent:spawned"
    err = capsys.readouterr().err
    assert err.count("prefer-assertion is using the legacy") == 1
    written = list(events.glob("identity-fallback*"))
    assert len(written) == 1 and len(written[0].read_text().splitlines()) == 1


@pytest.mark.parametrize(
    "source",
    ["layer1_configured_env", "unscoped_adapter", "session_scope", "os_user", "per_spawn_miss"],
)
def test_prefer_assertion_refuses_every_other_legacy_source(
    tmp_path: Path, issuer: Issuer, capsys, source: str
) -> None:
    (tmp_path / "sc").mkdir()
    (tmp_path / "sc" / "sess-s1").write_text("agent:parent\n")
    (tmp_path / "sc" / "plain-s1").write_text("agent:unscoped\n")
    attest: dict = {"bound_identity": "builtin-fallback"}
    env = clean_env(USER="root", LOGNAME="root")
    if source == "layer1_configured_env":
        attest["identity_env"] = "LEGACY_WHO"
        env["LEGACY_WHO"] = "agent:configured"
    elif source == "unscoped_adapter":
        attest["sidecars"] = [
            {"dir": str(tmp_path / "sc"), "file_prefix": "plain-", "session_id_env": "SESS_ID"}
        ]
        env["SESS_ID"] = "s1"
    elif source == "session_scope":
        attest["sidecars"] = [
            {
                "dir": str(tmp_path / "sc"),
                "file_prefix": "sess-",
                "session_id_env": "SESS_ID",
                "scope": "session",
            }
        ]
        env["SESS_ID"] = "s1"
    elif source == "per_spawn_miss":
        attest["sidecars"] = [_per_spawn_adapter(tmp_path)]
        env["SPAWN_ID"] = "missing-spawn"
    cfg = _prefer_cfg(tmp_path, issuer, **attest)
    with pytest.raises(IdentityError) as caught:
        resolve(env=env, config_root=cfg)
    assert DIR_VAR in str(caught.value)
    for leaked in ("agent:parent", "agent:configured", "agent:unscoped", "root"):
        assert f"{leaked}\n" not in str(caught.value)
    assert capsys.readouterr().err.count("prefer-assertion is using the legacy") == 1


@pytest.mark.parametrize(
    "attestation_section,present",
    [
        ({"sidecars": []}, True),
        ({"identity_env": ""}, True),
        ({"bound_identity": "required"}, True),
        ({"sidecars": None}, False),
        ({}, False),
    ],
)
def test_legacy_attestation_config_is_key_presence_not_truthiness(
    tmp_path: Path, attestation_section: dict, present: bool
) -> None:
    from clagentic_loadout.identity import has_legacy_attestation_config

    cfg = write_config(tmp_path / "cfg", None, attestation=attestation_section)
    assert has_legacy_attestation_config(config_root=cfg) is present
