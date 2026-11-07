"""Caller binding under the identity provider seam: one verb-by-provider binding
table, one refusal table, and the unset-provider guarantee.

With `identity.provider` set, the bound caller is the seam's Actor, a minted role
must be one the Actor holds, and every result carries `provider` and
`self_declared`. With it unset, every verb binds through the legacy chain exactly
as before.
"""

from __future__ import annotations

import importlib
import json
import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.identity import Actor
from clagentic_loadout.transport import attestation, provider_config
from clagentic_loadout.transport.attestation import AttestationError
from clagentic_loadout.transport.caller_binding import (
    CallerBindingError,
    bind_caller,
    bind_configured_provider,
    resolve_for_binding,
)
from clagentic_loadout.transport.credential_provider import DEFAULT_ROLE
from clagentic_loadout.transport.seam_binding import (
    SeamIdentity,
    identity_for_actor,
    provider_fields,
    staging_env,
)
from tests._support.identity_support import ISSUER, Issuer, clean_env, make_delivery, write_config
from tests.test_caller_binding_conformance import (
    _MINIMAL_ARGV_BY_SCRIPT,
    _NO_MINT_NO_AUTHORITY_ENTRY_POINTS,
    _NON_CREDENTIAL_ENTRY_POINTS,
    _argv_with_git_tree,
    _benign_success_opener,
    _build_arg_parser_for,
    _caller_role_flag,
    _load_verb_entry_points,
)

DIR_VAR = "CLAGENTIC_IDENTITY_DIR"


def _static(roles: list[str], actor: str = "builder-bot") -> dict:
    return {"provider": "static", "static": {"actor": actor, "roles": roles}}


def _use_config(monkeypatch, tmp_path: Path, identity: dict | None, **sections: dict) -> Path:
    cfg = write_config(tmp_path / "cfg", identity, **sections)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", cfg)
    # attestation binds its own copy of the default root at import time.
    monkeypatch.setattr(attestation, "DEFAULT_USER_CONFIG_ROOT", cfg)
    return cfg


def _declared_actor(roles: tuple[str, ...], origin: str = "declared") -> Actor:
    return Actor(
        sub="builder-bot", actor="builder-bot", chain=("builder-bot",), roles=roles,
        grade="declared", origin=origin, provider="declared", self_declared=True,
    )


@pytest.fixture
def bound_verbs() -> list[tuple[str, object, str]]:
    out = []
    for script, module_path in _load_verb_entry_points():
        if script in _NON_CREDENTIAL_ENTRY_POINTS or script in _NO_MINT_NO_AUTHORITY_ENTRY_POINTS:
            continue
        flag = _caller_role_flag(_build_arg_parser_for(module_path))
        if flag is not None:
            out.append((script, importlib.import_module(module_path), flag))
    return out


@pytest.fixture
def git_tree(tmp_path: Path) -> Path:
    tree = tmp_path / "tree"
    tree.mkdir()
    subprocess.run(["git", "init", "-q", str(tree)], check=True, capture_output=True)
    return tree


class _PermissiveTokens:
    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        return "token"


class _RefusingTokens:
    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        raise AssertionError(f"a token was requested for {role!r} past a refused binding")


def _run_verb(module, argv: list[str], tokens, monkeypatch) -> int:
    monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
    kwargs = {"token_provider": tokens, "opener": _benign_success_opener}
    if "authority_provider" in module.main.__code__.co_varnames:
        kwargs["authority_provider"] = type(
            "A", (), {"authority_allows": lambda self, *a, **k: True}
        )()
    return module.main(argv, **kwargs)


# --- the verb-by-provider binding table ------------------------------------------------


@pytest.mark.parametrize(
    "identity,caller,refused,fragments",
    [
        # AC2: the actor holds reviewer only, builder is requested.
        (_static(["reviewer"]), "builder", True, ["requested role 'builder'", "roles present [reviewer]"]),
        # AC9: no role at all.
        (_static([]), "builder", True, ["requested role 'builder'", "roles present [none]"]),
        # The actor's own name is not a role it holds.
        (_static(["builder"]), "builder-bot", True, ["requested role 'builder-bot'"]),
        # AC3: the held role binds; an omitted caller mints the single role.
        (_static(["builder"]), "builder", False, []),
        (_static(["builder"]), None, False, []),
        # An omitted caller with several roles is never guessed.
        (_static(["builder", "reviewer"]), None, True, ["roles [builder, reviewer]", "name the role"]),
        (_static([]), None, True, ["roles [none]"]),
        # AC1: an assertion provider with no delivery directory.
        ({"provider": "gatekeeper", "gatekeeper": {"jwks": "file:///nonexistent-keys.json"}}, "builder", True, [DIR_VAR]),
        ({"provider": "jwks", "issuers": [{"iss": "file:///nonexistent-keys.json"}]}, None, True, [DIR_VAR]),
    ],
)
def test_every_minting_verb_binds_to_the_actor(
    bound_verbs, git_tree, monkeypatch, capsys, tmp_path, identity, caller, refused, fragments
) -> None:
    _use_config(monkeypatch, tmp_path, identity)
    monkeypatch.delenv(DIR_VAR, raising=False)
    assert {name for name, _m, _f in bound_verbs} == set(_MINIMAL_ARGV_BY_SCRIPT)
    for script, module, flag in bound_verbs:
        argv = _argv_with_git_tree(_MINIMAL_ARGV_BY_SCRIPT[script], git_tree)
        if caller is not None:
            argv = [flag, caller, *argv]
        capsys.readouterr()
        rc = _run_verb(module, argv, _RefusingTokens() if refused else _PermissiveTokens(), monkeypatch)
        err = capsys.readouterr().err
        if refused:
            assert rc == module.EXIT_CALLER_INVOKER_MISMATCH, (script, rc, err)
            for fragment in fragments:
                assert fragment in err, (script, fragment, err)
        else:
            assert rc != module.EXIT_CALLER_INVOKER_MISMATCH, (script, rc, err)


def test_the_two_verbs_without_a_prior_binding_refuse_too(monkeypatch, tmp_path, capsys) -> None:
    from clagentic_loadout.release import dispatch
    from clagentic_loadout.transport import stage_body_verb

    _use_config(monkeypatch, tmp_path, _static(["reviewer"]))
    monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    rc = stage_body_verb.main(["--caller", "builder", "--target-pr", "1"])
    assert rc == stage_body_verb.EXIT_CALLER_INVOKER_MISMATCH
    assert "roles present [reviewer]" in capsys.readouterr().err
    assert not list(tmp_path.glob("clagentic-loadout/*"))

    with pytest.raises(SystemExit) as exc:
        dispatch.resolve_hook_secret(secret_env_caller="builder", secret_env_var=None)
    assert exc.value.code == dispatch.EXIT_SECRET_FAILED
    assert "roles present [reviewer]" in capsys.readouterr().err


def test_release_dispatch_reads_the_secret_of_the_actors_single_role(monkeypatch, tmp_path) -> None:
    from clagentic_loadout.release import dispatch

    _use_config(monkeypatch, tmp_path, _static(["releaser"]))
    asked: list[str] = []

    def fake_read(role, keys):
        asked.append(role)
        return {"STATUS_HOOK_SECRET": "s"}

    monkeypatch.setattr(dispatch, "read_role_env_file", fake_read)
    assert dispatch.resolve_hook_secret(secret_env_caller=None, secret_env_var=None) == "s"
    assert asked == ["releaser"]


# --- the mapping and the shared primitives ------------------------------------------------


def test_actor_to_role_mapping_is_explicit() -> None:
    explicit = identity_for_actor(_declared_actor(("builder", "reviewer")), caller_explicit=True)
    assert explicit.subject == "builder-bot" and explicit.roles == ("builder", "reviewer")
    omitted = identity_for_actor(_declared_actor(("builder",)), caller_explicit=False)
    assert omitted.subject == "builder" and omitted.actor == "builder-bot"
    with pytest.raises(AttestationError, match="name the role"):
        identity_for_actor(_declared_actor(("a", "b")), caller_explicit=False)
    assert provider_fields(explicit) == {"provider": "declared", "self_declared": True}


def test_a_legacy_origin_actor_binds_by_name_equality() -> None:
    actor = Actor(
        sub="agent:spawned", actor="agent:spawned", chain=("agent:spawned",), roles=(),
        grade="unverified", origin="legacy", provider="legacy:sidecar-subagent", self_declared=True,
    )
    identity = identity_for_actor(actor, caller_explicit=False)
    assert identity.roles is None and identity.subject == "agent:spawned"
    bind_caller("agent:spawned", caller_explicit=True, identity=identity)
    with pytest.raises(CallerBindingError):
        bind_caller("builder", caller_explicit=True, identity=identity)


def test_a_legacy_identity_carries_the_additive_fields() -> None:
    identity = attestation.Identity("builder", attestation.SOURCE_CONFIGURED)
    assert provider_fields(identity) == {"provider": "legacy", "self_declared": False}
    assert attestation.Identity.__slots__ == ("subject", "source")


def test_bind_configured_provider_is_inert_when_unset() -> None:
    assert bind_configured_provider("builder", caller_explicit=True, seam_actor_fn=lambda: None) is None


def test_the_role_ceiling_is_never_a_grant() -> None:
    identity = identity_for_actor(_declared_actor(("reviewer",)), caller_explicit=True)
    with pytest.raises(CallerBindingError) as exc:
        bind_caller("builder", caller_explicit=True, identity=identity)
    assert "requested role 'builder'" in str(exc.value) and "roles present [reviewer]" in str(exc.value)


# --- identity.provider unset: nothing changes (AC8) -------------------------------------


@pytest.mark.parametrize("legacy_keys", [None, {"identity_env": "WHO"}, {"sidecars": []}])
def test_unset_provider_binds_through_the_legacy_chain(monkeypatch, tmp_path, legacy_keys) -> None:
    sections = {"attestation": legacy_keys} if legacy_keys else {}
    _use_config(monkeypatch, tmp_path, None, **sections)
    monkeypatch.setenv("WHO", "agent:old")
    calls: list[str] = []

    def legacy():
        calls.append("legacy")
        return attestation.Identity("agent:old", attestation.SOURCE_CONFIGURED)

    explicit = resolve_for_binding(caller_explicit=True, caller="agent:old", resolve_identity_fn=legacy)
    assert explicit == attestation.Identity("agent:old", attestation.SOURCE_CONFIGURED)
    assert not isinstance(explicit, SeamIdentity)
    assert provider_fields(explicit) == {"provider": "legacy", "self_declared": False}
    omitted = resolve_for_binding(caller_explicit=False, caller="builder", resolve_identity_fn=legacy)
    assert omitted.subject == DEFAULT_ROLE  # builtin-fallback policy: never resolved
    assert calls == ["legacy"]


@pytest.mark.parametrize("legacy_keys", [None, {"identity_env": "WHO", "sidecars": []}])
def test_unset_provider_every_verb_binds_as_before(
    bound_verbs, git_tree, monkeypatch, capsys, tmp_path, legacy_keys
) -> None:
    """With identity.provider unset, with or without legacy attestation keys, an
    injected mismatching identity is refused and an omitted caller is not, for
    every verb: the legacy binding, untouched."""
    sections = {"attestation": legacy_keys} if legacy_keys else {}
    _use_config(monkeypatch, tmp_path, None, **sections)
    mismatching = attestation.Identity("someone-else", attestation.SOURCE_CONFIGURED)
    for script, module, flag in bound_verbs:
        argv = _argv_with_git_tree(_MINIMAL_ARGV_BY_SCRIPT[script], git_tree)
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
        kwargs = {"token_provider": _RefusingTokens(), "identity_provider": lambda: mismatching}
        if "authority_provider" in module.main.__code__.co_varnames:
            kwargs["authority_provider"] = type("A", (), {"authority_allows": lambda self, *a, **k: True})()
        assert module.main([flag, "builder", *argv], **kwargs) == module.EXIT_CALLER_INVOKER_MISMATCH, script
        capsys.readouterr()


def test_unset_provider_staging_stays_on_tmpdir() -> None:
    assert staging_env(attestation.Identity("builder", "configured")) is None


# --- assertion providers: results, scratch and staged bodies -----------------------------


def _assertion_env(tmp_path: Path, issuer: Issuer, name: str, **claims) -> dict:
    directory = str(tmp_path / name)
    token = issuer.sign(issuer.claims(directory, **claims))
    return clean_env(**{DIR_VAR: make_delivery(tmp_path, token, name=name)})


def _acquire(monkeypatch, tmp_path, env: dict, extra: list[str]):
    from clagentic_loadout.acquire import verb
    from tests.test_acquire_verb import _RecordingTokenProvider, _forgejo_opener

    for key in list(env):
        monkeypatch.setenv(key, env[key])
    monkeypatch.setenv("TMPDIR", str(tmp_path / "ambient-tmp"))
    tokens = _RecordingTokenProvider()
    rc = verb.main(
        [
            *extra, "--platform", "forgejo", "--git-host-base-url", "http://git-host.example.com",
            "--stage-scratch", "some-owner/some-repo", "42",
        ],
        token_provider=tokens,
        opener=_forgejo_opener(),
    )
    return rc, tokens


def test_static_provider_result_says_declared_and_self_declared(monkeypatch, tmp_path, capsys) -> None:
    _use_config(monkeypatch, tmp_path, _static(["builder"]))
    rc, tokens = _acquire(monkeypatch, tmp_path, {}, [])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and tokens.resolved_for == ["builder"]
    assert out["provider"] == "declared" and out["self_declared"] is True
    # Static identities have no verified directory: scratch stays on TMPDIR.
    assert out["scratch_root"].startswith(str(tmp_path / "ambient-tmp"))


def test_jwks_provider_mints_without_a_gatekeeper_and_roots_scratch_in_the_directory(
    monkeypatch, tmp_path, capsys
) -> None:
    launcher = Issuer(iss="file://" + str(tmp_path / "launcher-jwks.json"), alg="ES256", kid="es")
    launcher.write_jwks(tmp_path / "launcher-jwks.json")
    _use_config(monkeypatch, tmp_path, {"provider": "jwks", "issuers": [{"iss": launcher.iss}]})
    env = _assertion_env(tmp_path, launcher, "delivery")
    rc, tokens = _acquire(monkeypatch, tmp_path, env, ["--caller", "builder"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and tokens.resolved_for == ["builder"]
    assert out["provider"] == f"jwks:{launcher.iss}" and out["self_declared"] is False
    assert out["scratch_root"].startswith(str(tmp_path / "delivery" / "tmp"))


def test_gatekeeper_scratch_is_in_the_verified_directory(monkeypatch, tmp_path, capsys) -> None:
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    _use_config(monkeypatch, tmp_path, {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}})
    rc, _tokens = _acquire(monkeypatch, tmp_path, _assertion_env(tmp_path, issuer, "delivery"), [])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["provider"] == "gatekeeper"
    assert out["scratch_root"].startswith(str(tmp_path / "delivery" / "tmp" / "clagentic-loadout-acquire"))


def _stage_and_read(monkeypatch, tmp_path, issuer, cfg_identity, stager: str, reader: str) -> str:
    """Stage as one actor, then read as another; returns the read outcome."""
    from clagentic_loadout.transport import body_env, stage_body_verb

    _use_config(monkeypatch, tmp_path, cfg_identity)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "ambient-tmp"))
    stage_env = _assertion_env(tmp_path, issuer, stager, sub=f"agent:{stager}", jti=f"jti-{stager}")
    for key, value in stage_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "hello"}')
    assert stage_body_verb.main(["--caller", "builder", "--target-pr", "7"]) == 0
    staged = list((tmp_path / stager / "tmp" / "clagentic-loadout").glob("body.builder.*"))
    assert len(staged) == 2
    assert not (tmp_path / "ambient-tmp" / "clagentic-loadout").exists()
    stamp = json.loads((tmp_path / stager / "tmp" / "clagentic-loadout" / "body.builder.stamp.json").read_text())
    assert stamp["identity"] == {"actor": f"agent:{stager}", "jti": f"jti-{stager}"}

    read_env = (
        stage_env
        if reader == stager
        else _assertion_env(tmp_path, issuer, reader, sub=f"agent:{reader}", jti=f"jti-{reader}")
    )
    from clagentic_loadout.transport.seam_binding import configured_actor, staging_env

    actor = configured_actor(env=read_env)
    identity = identity_for_actor(actor, caller_explicit=True)
    try:
        return body_env.read_body_bytes(
            caller="builder", expect_target_pr=7,
            env=staging_env(identity, read_env), expect_actor=identity.actor,
        ).decode()
    except body_env.BodyEnvError as exc:
        return f"refused: {exc}"


def test_a_staged_body_is_read_by_its_own_actor_and_by_no_other(monkeypatch, tmp_path) -> None:
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    cfg = {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
    own = _stage_and_read(monkeypatch, tmp_path, issuer, cfg, "alpha", "alpha")
    assert json.loads(own) == {"body": "hello"}
    other = _stage_and_read(monkeypatch, tmp_path, issuer, cfg, "beta", "gamma")
    assert other.startswith("refused: --body-env: no identity stamp staged") and "gamma" in other


def test_a_body_staged_by_another_actor_in_the_same_directory_is_refused(tmp_path) -> None:
    from clagentic_loadout.transport import body_env

    env = {"TMPDIR": str(tmp_path)}
    body_env.stage_caller_body(
        caller="builder", body_bytes=b'{"body": "x"}', target_pr=3, env=env,
        stager_identity={"actor": "agent:alpha", "jti": "j1"},
    )
    with pytest.raises(body_env.BodyEnvError, match="staged by 'agent:alpha', not by"):
        body_env.read_caller_body_bytes(
            caller="builder", expect_target_pr=3, env=env, expect_actor="agent:beta", consume=False
        )
    assert body_env.read_caller_body_bytes(
        caller="builder", expect_target_pr=3, env=env, expect_actor="agent:alpha"
    ) == b'{"body": "x"}'


def test_an_unstamped_body_is_refused_when_an_actor_is_expected(tmp_path) -> None:
    from clagentic_loadout.transport import body_env

    env = {"TMPDIR": str(tmp_path)}
    body_env.stage_caller_body(caller="builder", body_bytes=b'{"body": "x"}', target_pr=3, env=env)
    with pytest.raises(body_env.BodyEnvError, match="staged by None"):
        body_env.claim_caller_body(caller="builder", expect_target_pr=3, env=env, expect_actor="agent:alpha")
    # The refusal released the pair: it is still readable by a caller that expects no actor.
    assert body_env.read_caller_body_bytes(caller="builder", expect_target_pr=3, env=env)


def _prefer_assertion_cfg(tmp_path: Path, issuer: Issuer, sidecars: list[dict]) -> dict:
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    return {
        "provider": "prefer-assertion",
        "assertion_provider": "gatekeeper",
        "gatekeeper": {"jwks": jwks, "issuer": ISSUER},
    }


def _spawn_adapter(tmp_path: Path, scope: str) -> dict:
    return {
        "dir": str(tmp_path / "sc"), "file_prefix": "spawn-", "session_id_env": "SPAWN_ID", "scope": scope,
    }


@pytest.mark.parametrize("scope,accepted", [("per-spawn", True), ("session", False)])
def test_prefer_assertion_through_a_verb_accepts_only_a_per_spawn_sidecar(
    monkeypatch, tmp_path, capsys, scope, accepted
) -> None:
    issuer = Issuer()
    (tmp_path / "sc").mkdir()
    (tmp_path / "sc" / "spawn-s1").write_text("builder\n")
    events = tmp_path / "events"
    _use_config(
        monkeypatch, tmp_path, _prefer_assertion_cfg(tmp_path, issuer, []),
        attestation={"sidecars": [_spawn_adapter(tmp_path, scope)]},
    )
    monkeypatch.delenv(DIR_VAR, raising=False)
    monkeypatch.setenv("SPAWN_ID", "s1")
    monkeypatch.setenv("CLAGENTIC_LOADOUT_TELEMETRY_SINK", "filesystem")
    monkeypatch.setenv("CLAGENTIC_LOADOUT_TELEMETRY_DIR", str(events))
    from clagentic_loadout.acquire import verb
    from tests.test_acquire_verb import _RecordingTokenProvider, _forgejo_opener

    tokens = _RecordingTokenProvider()
    rc = verb.main(
        ["--caller", "builder", "--platform", "forgejo", "--git-host-base-url", "http://git-host.example.com",
         "some-owner/some-repo", "42"],
        token_provider=tokens, opener=_forgejo_opener(),
    )
    captured = capsys.readouterr()
    if accepted:
        assert rc == 0 and tokens.resolved_for == ["builder"], captured.err
        assert json.loads(captured.out)["provider"] == "legacy:sidecar-subagent"
        assert json.loads(captured.out)["self_declared"] is True
        written = list(events.glob("identity-fallback*"))
        assert len(written) == 1 and len(written[0].read_text().splitlines()) == 1
    else:
        assert rc == verb.EXIT_CALLER_INVOKER_MISMATCH and tokens.resolved_for == []
        assert DIR_VAR in captured.err
    assert captured.err.count("prefer-assertion is using the legacy") == 1
