"""loadout-identity: whoami, verify and the conformance contract."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout._version import get_version
from clagentic_loadout.cli import main as umbrella_main
from clagentic_loadout.identity import errors
from clagentic_loadout.identity import verb
from tests._support.identity_support import ISSUER, Issuer, clean_env, make_delivery, write_config

DIR_VAR = "CLAGENTIC_IDENTITY_DIR"


def _setup(tmp_path: Path):
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    cfg = write_config(
        tmp_path / "cfg", {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
    )
    directory = str(tmp_path / "delivery")
    token = issuer.sign(issuer.claims(directory))
    return issuer, cfg, token, make_delivery(tmp_path, token)


def test_help_and_version_exit_ok(capsys) -> None:
    assert verb.main(["--help"]) == 0
    assert "whoami" in capsys.readouterr().out
    assert verb.main(["--version"]) == 0
    assert get_version() in capsys.readouterr().out


def test_bad_usage_exits_one() -> None:
    assert verb.main(["no-such-command"]) == verb.EXIT_USAGE
    assert verb.main([]) == verb.EXIT_USAGE


def test_whoami_prints_the_actor_with_provider_and_self_declared(tmp_path: Path, capsys) -> None:
    _, cfg, _, delivered = _setup(tmp_path)
    rc = verb.main(["whoami"], env=clean_env(**{DIR_VAR: delivered}), config_root=cfg)
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["provider"] == "gatekeeper" and out["self_declared"] is False
    assert out["actor"] == "agent:example-builder" and out["roles"] == ["builder"]


def test_whoami_refusal_exit_code_is_the_class_code(tmp_path: Path, capsys) -> None:
    _, cfg, _, _ = _setup(tmp_path)
    rc = verb.main(["whoami"], env=clean_env(), config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.IDENTITY_DIR_MISSING]
    assert DIR_VAR in capsys.readouterr().err


def test_whoami_with_no_provider_exits_with_provider_unset(tmp_path: Path, capsys) -> None:
    rc = verb.main(["whoami"], env={}, config_root=tmp_path / "none")
    assert rc == errors.EXIT_CODES[errors.PROVIDER_UNSET]
    assert "identity.provider" in capsys.readouterr().err


def test_verify_prints_claims_and_uses_the_directory_binding(tmp_path: Path, capsys) -> None:
    _, cfg, token, delivered = _setup(tmp_path)
    assert verb.main(["verify", token, "--dir", delivered], env={}, config_root=cfg) == 0
    assert json.loads(capsys.readouterr().out)["sub"] == "agent:example-builder"
    rc = verb.main(["verify", token, "--dir", str(tmp_path / "elsewhere")], env={}, config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.DIR_MISMATCH]
    rc = verb.main(["verify", token], env={}, config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.IDENTITY_DIR_MISSING]


def test_verify_peer_mode_and_now(tmp_path: Path) -> None:
    _, cfg, token, delivered = _setup(tmp_path)
    rc = verb.main(["verify", token, "--peer-id", "peer.example"], env={}, config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.AUDIENCE_MISMATCH]
    rc = verb.main(["verify", token, "--dir", delivered, "--now", "5"], env={}, config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.NOT_YET_VALID]


def test_verify_without_an_assertion_provider_is_a_config_error(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "cfg", {"provider": "static", "static": {"actor": "agent:x"}})
    rc = verb.main(["verify", "a.b.c", "--dir", str(tmp_path / "d")], env={}, config_root=cfg)
    assert rc == errors.EXIT_CODES[errors.CONFIG_INVALID]


def test_exit_codes_are_distinct_and_inside_the_reserved_range() -> None:
    codes = list(errors.EXIT_CODES.values())
    assert len(set(codes)) == len(codes)
    assert all(90 <= c <= 111 for c in codes)


def test_umbrella_dispatches_identity(capsys) -> None:
    assert umbrella_main(["identity", "--help"]) == 0
    assert "whoami" in capsys.readouterr().out
    assert umbrella_main(["--help"]) == 0
    assert "identity" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_console_script_module_runs(flag: str) -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-m", "clagentic_loadout.identity.verb", flag],
        capture_output=True,
        text=True,
        env={**clean_env(), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    assert proc.returncode == 0 and proc.stdout.strip()
