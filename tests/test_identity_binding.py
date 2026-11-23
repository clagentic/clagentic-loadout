"""Caller binding under the identity provider seam: one verb-by-provider binding
table, one refusal table, and the unset-provider guarantee.

With `identity.provider` set, the bound caller is the seam's Actor, a minted role
must be one the Actor holds, and every result carries `provider` and
`self_declared`. With it unset, every verb binds through the legacy chain exactly
as before.
"""

from __future__ import annotations

import getpass
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


def _named(held: list[str], *, actor: str = "svc:builder-one", **caller_names) -> dict:
    identity = _static(held, actor)
    if caller_names:
        identity["caller_names"] = caller_names
    return identity


class _PermissiveTokens:
    def __init__(self) -> None:
        self.roles: list[str] = []

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.roles.append(role)
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
        # The actor's own name binds to its single role.
        (_static(["builder"]), "builder-bot", False, []),
        # A name binds through the optional prefix, or as the exact actor string.
        (_named(["builder"], actor_prefix="svc:"), "builder-one", False, []),
        (_named(["builder"], actor_prefix="svc:"), "svc:builder-one", False, []),
        (_named(["builder"]), "svc:builder-one", False, []),
        # Another actor's name stays refused; so does the bare name when no prefix is configured.
        (_named(["builder"], actor_prefix="svc:"), "reviewer-two", True,
         ["'reviewer-two'", "svc:builder-one", "roles present [builder]"]),
        (_named(["builder"]), "builder-one", True, ["'builder-one'", "roles present [builder]"]),
        # Several roles: the name binds only through caller_names.roles, to a role the actor holds.
        (_named(["builder", "reviewer"], roles={"builder-one": "builder"}, actor_prefix="svc:"),
         "builder-one", False, []),
        (_named(["builder", "reviewer"], actor_prefix="svc:"), "builder-one", True,
         ["roles present [builder, reviewer]", "caller_names.roles"]),
        (_named(["builder", "reviewer"], roles={"builder-one": "releaser"}, actor_prefix="svc:"),
         "builder-one", True, ["roles present [builder, reviewer]", "'releaser'"]),
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
        tokens = _RefusingTokens() if refused else _PermissiveTokens()
        rc = _run_verb(module, argv, tokens, monkeypatch)
        err = capsys.readouterr().err
        if refused:
            assert rc == module.EXIT_CALLER_INVOKER_MISMATCH, (script, rc, err)
            for fragment in fragments:
                assert fragment in err, (script, fragment, err)
        else:
            assert rc != module.EXIT_CALLER_INVOKER_MISMATCH, (script, rc, err)
            # The value downstream is the flag value as given, as on the legacy path.
            if caller is not None:
                assert set(tokens.roles) <= {caller}, (script, tokens.roles)


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


def test_stage_body_and_release_dispatch_bind_a_name_and_keep_the_flag_value(
    monkeypatch, tmp_path, capsys
) -> None:
    from clagentic_loadout.release import dispatch
    from clagentic_loadout.transport import stage_body_verb

    _use_config(monkeypatch, tmp_path, _named(["releaser"], actor_prefix="svc:"))
    monkeypatch.setattr("sys.stdin.buffer.read", lambda: b'{"body": "x"}')
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    rc = stage_body_verb.main(["--caller", "builder-one", "--target-pr", "1"])
    assert rc == 0, capsys.readouterr().err
    assert list(tmp_path.glob("clagentic-loadout/body.builder-one.*"))

    asked: list[str] = []

    def fake_read(role, keys):
        asked.append(role)
        return {"STATUS_HOOK_SECRET": "s"}

    monkeypatch.setattr(dispatch, "read_role_env_file", fake_read)
    assert dispatch.resolve_hook_secret(secret_env_caller="builder-one", secret_env_var=None) == "s"
    assert asked == ["builder-one"]


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


@pytest.mark.parametrize(
    "identity,caller,role",
    [
        (_static(["builder", "reviewer"]), "reviewer", "reviewer"),
        (_static(["builder"]), "builder-bot", "builder"),
        (_named(["builder"], actor_prefix="svc:"), "builder-one", "builder"),
        (_named(["builder", "reviewer"], roles={"builder-one": "builder"}, actor_prefix="svc:"),
         "builder-one", "builder"),
        (_named(["builder", "reviewer"], roles={"builder-one": "reviewer"}, actor_prefix="svc:"),
         "builder-one", "reviewer"),
    ],
)
def test_a_bound_caller_resolves_to_the_expected_held_role(monkeypatch, tmp_path, identity, caller, role) -> None:
    config = identity["static"]
    _use_config(monkeypatch, tmp_path, identity)
    actor = Actor(
        sub=config["actor"], actor=config["actor"], chain=(config["actor"],),
        roles=tuple(config["roles"]), grade="declared", origin="declared",
        provider="declared", self_declared=True,
    )
    bound = identity_for_actor(actor, caller_explicit=True)
    bind_caller(caller, caller_explicit=True, identity=bound)
    assert bound.resolved_role == role


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


def test_a_legacy_origin_actor_never_reads_caller_names(monkeypatch, tmp_path) -> None:
    _use_config(monkeypatch, tmp_path, _named(["builder"], actor_prefix="agent:"))
    monkeypatch.setattr(
        "clagentic_loadout.identity.config.load_user_config_section",
        lambda *a, **k: pytest.fail("caller_names was read on the legacy path"),
    )
    actor = Actor(
        sub="agent:spawned", actor="agent:spawned", chain=("agent:spawned",), roles=(),
        grade="unverified", origin="legacy", provider="legacy:sidecar-subagent", self_declared=True,
    )
    identity = identity_for_actor(actor, caller_explicit=True)
    bind_caller("agent:spawned", caller_explicit=True, identity=identity)
    with pytest.raises(CallerBindingError):
        bind_caller("spawned", caller_explicit=True, identity=identity)


def test_unset_provider_with_caller_names_binds_by_name_equality(monkeypatch, tmp_path) -> None:
    _use_config(monkeypatch, tmp_path, {"caller_names": {"actor_prefix": "svc:"}})
    legacy_identity = attestation.Identity("svc:builder-one", attestation.SOURCE_CONFIGURED)
    identity = resolve_for_binding(
        caller_explicit=True, caller="builder-one", resolve_identity_fn=lambda: legacy_identity
    )
    assert not isinstance(identity, SeamIdentity)
    with pytest.raises(CallerBindingError):
        bind_caller("builder-one", caller_explicit=True, identity=identity)


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
    assert other.startswith("refused: --body-env: no staged pair under") and "gamma" in other


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
        body_env.peek_caller_body(caller="builder", expect_target_pr=3, env=env, expect_actor="agent:alpha")
    # The refusal touched nothing: the pair is still readable by a caller that expects no actor.
    assert body_env.read_caller_body_bytes(caller="builder", expect_target_pr=3, env=env)


# --- assertion-or-legacy: assertion with the directory, the legacy chain without it ----


def _bind_outcome(cfg: Path, env: dict, *, caller: str = "builder", explicit: bool = True):
    """Bind through the real `configured_actor` and the real legacy resolver."""
    from clagentic_loadout.transport.caller_binding import resolve_for_binding
    from clagentic_loadout.transport.seam_binding import configured_actor

    try:
        identity = resolve_for_binding(
            caller_explicit=explicit,
            caller=caller,
            resolve_identity_fn=lambda: attestation.resolve_bound_identity(env=env, config_root=cfg),
            seam_actor_fn=lambda: configured_actor(env=env, config_root=cfg),
        )
        bind_caller(caller, caller_explicit=explicit, identity=identity)
    except (AttestationError, CallerBindingError) as exc:
        return ("refused", type(exc).__name__, str(exc))
    return (
        "ok", identity.subject, identity.source, provider_fields(identity),
        getattr(identity, "roles", None), getattr(identity, "actor", None),
        getattr(identity, "dir", None), getattr(identity, "jti", None),
    )


def _legacy_sources(tmp_path: Path) -> dict[str, tuple[dict | None, dict, str]]:
    """source -> (attestation section, env, expected source or 'refused')."""
    (tmp_path / "sc").mkdir(exist_ok=True)
    for name in ("plain-s1", "spawn-s1", "sess-s1"):
        (tmp_path / "sc" / name).write_text("builder\n")

    def adapter(prefix: str, env_var: str, scope: str | None = None) -> dict:
        out = {"dir": str(tmp_path / "sc"), "file_prefix": prefix, "session_id_env": env_var}
        if scope:
            out["scope"] = scope
        return out

    return {
        "layer1_configured_env": ({"identity_env": "WHO"}, {"WHO": "builder"}, "configured"),
        "bound_identity_required_miss": ({"bound_identity": "required"}, {}, "refused"),
        "bound_identity_builtin_fallback": ({"bound_identity": "builtin-fallback"}, {}, "builtin"),
        "unscoped_sidecar_adapter": (
            {"sidecars": [adapter("plain-", "SESS_ID")]}, {"SESS_ID": "s1"}, "sidecar",
        ),
        "per_spawn_sidecar_adapter": (
            {"sidecars": [adapter("spawn-", "SPAWN_ID", "per-spawn")]}, {"SPAWN_ID": "s1"}, "sidecar-subagent",
        ),
        "session_scope_sidecar": (
            {"sidecars": [adapter("sess-", "SESS_ID", "session")]}, {"SESS_ID": "s1"}, "sidecar-session",
        ),
        "os_user": (None, {}, "builtin"),
        "per_spawn_miss": (
            {"sidecars": [adapter("spawn-", "SPAWN_ID", "per-spawn")]}, {"SPAWN_ID": "missing"}, "refused",
        ),
    }


@pytest.mark.parametrize("dir_value", [None, ""], ids=["dir-unset", "dir-blank"])
@pytest.mark.parametrize(
    "source",
    [
        "layer1_configured_env", "bound_identity_required_miss", "bound_identity_builtin_fallback",
        "unscoped_sidecar_adapter", "per_spawn_sidecar_adapter", "session_scope_sidecar", "os_user",
        "per_spawn_miss",
    ],
)
def test_assertion_or_legacy_without_the_directory_binds_exactly_as_provider_unset(
    tmp_path: Path, source: str, dir_value
) -> None:
    section, extra_env, expected = _legacy_sources(tmp_path)[source]
    sections = {"attestation": section} if section else {}
    unset_cfg = write_config(tmp_path / "cfg-unset", None, **sections)
    combined_cfg = write_config(
        tmp_path / "cfg-combined",
        {"provider": "assertion-or-legacy", "assertion_provider": "gatekeeper"},
        **sections,
    )
    env = clean_env(**extra_env)
    if dir_value is not None:
        env[DIR_VAR] = dir_value
    # The OS user is whoever runs the suite, so the caller names it for those rows.
    caller = getpass.getuser() if expected == "builtin" else "builder"
    baseline = _bind_outcome(unset_cfg, env, caller=caller)
    assert baseline[0] == ("refused" if expected == "refused" else "ok")
    if expected != "refused":
        assert baseline[2] == expected and baseline[3] == {"provider": "legacy", "self_declared": False}
    assert _bind_outcome(combined_cfg, env, caller=caller) == baseline


@pytest.mark.parametrize("kind", ["gatekeeper", "jwks"])
@pytest.mark.parametrize("case", ["valid", "invalid"])
def test_assertion_or_legacy_with_the_directory_binds_as_the_assertion_provider(
    tmp_path: Path, kind: str, case: str
) -> None:
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    base = (
        {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
        if kind == "gatekeeper"
        else {"provider": "jwks", "issuers": [{"iss": ISSUER, "jwks": jwks}]}
    )
    plain = write_config(tmp_path / "cfg-plain", base)
    combined = write_config(
        tmp_path / "cfg-combined",
        {**base, "provider": "assertion-or-legacy", "assertion_provider": kind},
        attestation={"identity_env": "WHO"},
    )
    token = "bad.token.here" if case == "invalid" else issuer.sign(issuer.claims(str(tmp_path / "d")))
    env = clean_env(WHO="legacy-parent", **{DIR_VAR: make_delivery(tmp_path, token, name="d")})
    expected = _bind_outcome(plain, env)
    assert expected[0] == ("refused" if case == "invalid" else "ok")
    if case == "valid":
        assert expected[3]["self_declared"] is False and expected[6] == str(tmp_path / "d")
    assert _bind_outcome(combined, env) == expected


@pytest.mark.parametrize("dir_value", [None, ""], ids=["dir-unset", "dir-blank"])
def test_assertion_or_legacy_through_a_verb_reports_legacy_and_not_self_declared(
    monkeypatch, tmp_path, capsys, dir_value
) -> None:
    from clagentic_loadout.acquire import verb
    from tests.test_acquire_verb import _RecordingTokenProvider, _forgejo_opener

    results = []
    for name, identity in (
        ("unset", None),
        ("combined", {"provider": "assertion-or-legacy", "assertion_provider": "gatekeeper"}),
    ):
        sub = tmp_path / name
        sub.mkdir()
        _use_config(monkeypatch, sub, identity, attestation={"identity_env": "WHO"})
        monkeypatch.setenv("WHO", "builder")
        if dir_value is None:
            monkeypatch.delenv(DIR_VAR, raising=False)
        else:
            monkeypatch.setenv(DIR_VAR, dir_value)
        tokens = _RecordingTokenProvider()
        rc = verb.main(
            ["--caller", "builder", "--platform", "forgejo", "--git-host-base-url",
             "http://git-host.example.com", "some-owner/some-repo", "42"],
            token_provider=tokens, opener=_forgejo_opener(),
        )
        captured = capsys.readouterr()
        out = json.loads(captured.out)
        assert "identity" not in captured.err
        results.append((rc, tokens.resolved_for, out["provider"], out["self_declared"], captured.err))
    assert results[0] == results[1]
    assert results[0][:4] == (0, ["builder"], "legacy", False)


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


# --- identity.staging_root: the staging root as a template over the verified claims ------


def _staging_identity(monkeypatch, tmp_path, template, **claims) -> Path:
    """Gatekeeper config with *template* (`ROOT` stands for a per-test scratch root;
    None leaves the key unset) and the verified identity in the process env. Returns
    the scratch root."""
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    scratch = tmp_path / "scratch"
    cfg = {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
    if template is not None:
        cfg["staging_root"] = template.replace("ROOT", str(scratch))
    _use_config(monkeypatch, tmp_path, cfg)
    monkeypatch.setenv("TMPDIR", str(tmp_path / "ambient-tmp"))
    claims.setdefault("sub", "agent:x")
    claims.setdefault("sid", "s1")
    for key, value in _assertion_env(tmp_path, issuer, "delivery", **claims).items():
        monkeypatch.setenv(key, value)
    return scratch


def _draft_path(capsys) -> Path:
    from clagentic_loadout.transport import stage_body_verb

    assert stage_body_verb.main(["--caller", "builder", "--show-draft-path"]) == 0
    return Path(capsys.readouterr().out.strip())


def _stage_draft(path: Path) -> int:
    from clagentic_loadout.transport import stage_body_verb

    path.write_text('{"body": "hello"}')
    return stage_body_verb.main(["--caller", "builder", "--target-pr", "7", "--body-draft"])


@pytest.mark.parametrize(
    "template,claims,rel",
    [
        ("ROOT/{actor_name}/{sid}", {}, "x/s1"),
        ("ROOT/{actor_name}/{spawn}", {"spawn": "sp9"}, "x/sp9"),
        ("ROOT/{actor_name}-{sid}-{spawn}", {"spawn": "sp9", "sub": "runtime:y"}, "y-s1-sp9"),
    ],
)
def test_staging_root_template_roots_the_draft_and_the_stage(
    monkeypatch, tmp_path, capsys, template, claims, rel
) -> None:
    scratch = _staging_identity(monkeypatch, tmp_path, template, **claims)
    path = _draft_path(capsys)
    assert path == scratch / rel / "clagentic-loadout" / "draft.builder.json"
    assert _stage_draft(path) == 0
    assert (scratch / rel / "clagentic-loadout" / "body.builder.json").is_file()
    assert not (tmp_path / "delivery" / "tmp").exists()


def test_staging_root_unset_keeps_the_delivery_tmp_path(monkeypatch, tmp_path, capsys) -> None:
    _staging_identity(monkeypatch, tmp_path, None)
    path = _draft_path(capsys)
    assert str(path) == str(tmp_path / "delivery" / "tmp" / "clagentic-loadout" / "draft.builder.json")
    # Unset keeps today's behavior: the delivery tmp dir is the writer's to create.
    path.parent.mkdir(parents=True)
    assert _stage_draft(path) == 0


def test_staging_root_separates_actors_and_spawns(monkeypatch, tmp_path, capsys) -> None:
    roots = set()
    for index, (sub, sid) in enumerate([("agent:x", "s1"), ("agent:y", "s1"), ("agent:x", "s2")]):
        sub_path = tmp_path / f"case{index}"
        sub_path.mkdir()
        _staging_identity(monkeypatch, sub_path, "ROOT/{actor_name}/{sid}", sub=sub, sid=sid)
        roots.add(_draft_path(capsys).parent)
    assert len(roots) == 3


@pytest.mark.parametrize(
    "template,claims,needles",
    [
        ("ROOT/{actor_name}", {}, ["identity.staging_root", "ROOT/{actor_name}", "no per-spawn placeholder"]),
        ("ROOT/{sid}/{bogus}", {}, ["identity.staging_root", "ROOT/{sid}/{bogus}", "{bogus}"]),
        ("ROOT/{sid!r}", {}, ["identity.staging_root", "{sid}"]),
        ("{sid}/rel", {}, ["identity.staging_root", "{sid}/rel", "not an absolute path"]),
        ("ROOT/{sid}", {"sid": "a/b"}, ["identity.staging_root", "ROOT/{sid}", "{sid}", "'a/b'"]),
        ("ROOT/{sid}", {"sid": ".."}, ["identity.staging_root", "{sid}", "'..'"]),
        ("ROOT/{spawn}", {}, ["identity.staging_root", "ROOT/{spawn}", "{spawn}", "no such claim"]),
        ("ROOT/{sid}/..", {}, ["identity.staging_root", "ROOT/{sid}/..", "'..' path segment"]),
    ],
)
def test_a_staging_root_that_cannot_render_is_refused_with_no_fallback(
    monkeypatch, tmp_path, capsys, template, claims, needles
) -> None:
    from clagentic_loadout.transport import stage_body_verb

    scratch = _staging_identity(monkeypatch, tmp_path, template, **claims)
    rc = stage_body_verb.main(["--caller", "builder", "--show-draft-path"])
    captured = capsys.readouterr()
    assert rc == stage_body_verb.EXIT_CALLER_INVOKER_MISMATCH and captured.out == ""
    for needle in needles:
        assert needle.replace("ROOT", str(scratch)) in captured.err, captured.err
    assert not scratch.exists() and not (tmp_path / "delivery" / "tmp").exists()


def test_a_staging_root_template_is_refused_at_load_when_not_a_string(monkeypatch, tmp_path) -> None:
    from clagentic_loadout.identity.config import load_identity_config
    from clagentic_loadout.identity.errors import CONFIG_INVALID, IdentityError

    cfg = write_config(tmp_path / "cfg", {"staging_root": ["a"]})
    with pytest.raises(IdentityError, match="identity.staging_root") as info:
        load_identity_config(config_root=cfg)
    assert info.value.code == CONFIG_INVALID


@pytest.mark.parametrize("kind", ["static", "legacy", "module"])
def test_staging_root_has_no_effect_without_a_verified_assertion(kind) -> None:
    identity = identity_for_actor(
        Actor(
            sub="builder-bot", actor="builder-bot", chain=("builder-bot",), roles=("builder",),
            grade="workload", origin=kind, provider="declared" if kind == "static" else f"{kind}:x",
            self_declared=True, dir="<unused-dir>", sid="s1",
        ),
        caller_explicit=True,
    )
    assert staging_env(identity) is None


def test_the_staging_root_is_created_0700_for_stage_and_acquire_scratch(monkeypatch, tmp_path, capsys) -> None:
    scratch = _staging_identity(monkeypatch, tmp_path, "ROOT/{actor_name}/{sid}")
    assert not scratch.exists()
    assert _stage_draft(_draft_path(capsys)) == 0
    root = scratch / "x" / "s1"
    assert (root.stat().st_mode & 0o777, (root / "clagentic-loadout").stat().st_mode & 0o777) == (0o700, 0o700)

    from clagentic_loadout.acquire import verb
    from tests.test_acquire_verb import _RecordingTokenProvider, _forgejo_opener

    rc = verb.main(
        ["--caller", "builder", "--platform", "forgejo", "--git-host-base-url", "http://git-host.example.com",
         "--stage-scratch", "some-owner/some-repo", "42"],
        token_provider=_RecordingTokenProvider(), opener=_forgejo_opener(),
    )
    assert rc == 0
    assert json.loads(capsys.readouterr().out)["scratch_root"].startswith(str(root / "clagentic-loadout-acquire"))
