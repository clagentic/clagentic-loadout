"""Every vendored contract vector (major 1 and major 2) against the verifier,
plus the integrity of the vendored copies themselves."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import jsonschema
import pytest

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.actor import actor_from_claims
from clagentic_loadout.identity.delivery import IDENTITY_DIR_ENV, read_delivery
from clagentic_loadout.identity.verifier import MODE_LOCAL, MODE_PEER, verify

CONTRACT = Path(__file__).resolve().parent / "identity_contract"
PIN_FILE = CONTRACT / "PINNED_FROM.md"

#: One v1 vector carries cia 2 and expects the major-1 refusal. The contract says a
#: major-2 verifier reads it as valid; the v2 set holds the cia 3 equivalent.
V1_READ_AS_VALID_BY_MAJOR_2 = {"contract-version-unsupported"}


def _manifest(major: str) -> dict:
    return json.loads((CONTRACT / "vectors" / major / "manifest.json").read_text())


def _cases():
    for major in ("v1", "v2"):
        manifest = _manifest(major)
        for vec in manifest["vectors"]:
            yield pytest.param(major, vec, id=f"{major}/{vec['id']}")


def _jwks_loader(major: str, manifest: dict):
    def load(iss: str):
        return json.loads((CONTRACT / "vectors" / major / manifest["jwks_files"][iss]).read_text())

    return load


def _run_token(major: str, vec: dict) -> dict:
    manifest = _manifest(major)
    return verify(
        vec["token"],
        trusted_issuers=[t["iss"] for t in manifest["trust"]["issuers"]],
        jwks_loader=_jwks_loader(major, manifest),
        mode=MODE_PEER if vec["mode"] == "peer" else MODE_LOCAL,
        observed_dir=vec.get("observed_dir"),
        peer_id=vec.get("peer_id"),
        now=vec["now"],
    )


def _run_delivery(vec: dict, tmp_path: Path) -> str:
    layout = vec["layout"]
    env: dict[str, str] = {}
    if layout["dir"] == "absent":
        env[IDENTITY_DIR_ENV] = str(tmp_path / "absent")
    elif layout["dir"] == "present":
        directory = tmp_path / "delivery"
        directory.mkdir()
        directory.chmod(int(layout["dir_mode"], 8))
        env[IDENTITY_DIR_ENV] = str(directory)
        if layout["file"] == "regular":
            (directory / "identity.jwt").write_text(layout["content"])
        elif layout["file"] == "symlink":
            target = tmp_path / "elsewhere.jwt"
            target.write_text(layout["content"])
            (directory / "identity.jwt").symlink_to(target)
    token, _ = read_delivery(env)
    return token


@pytest.mark.parametrize("major,vec", list(_cases()))
def test_vector(major: str, vec: dict, tmp_path: Path) -> None:
    expect = vec["expect"]
    if vec["id"] in V1_READ_AS_VALID_BY_MAJOR_2 and major == "v1":
        expect = {"ok": True, "actor": None, "grade": None}
    if vec["kind"] == "delivery":
        if expect["ok"]:
            assert _run_delivery(vec, tmp_path) == vec["layout"]["content"]
        else:
            with pytest.raises(errors.IdentityError) as caught:
                _run_delivery(vec, tmp_path)
            assert caught.value.code == expect["error"]
        return
    if expect["ok"]:
        claims = _run_token(major, vec)
        actor = actor_from_claims(claims, provider="test")
        if expect["actor"] is not None:
            assert actor.actor == expect["actor"]
            assert actor.grade == expect["grade"]
    else:
        with pytest.raises(errors.IdentityError) as caught:
            _run_token(major, vec)
        assert caught.value.code == expect["error"]


def test_every_failure_code_of_the_contract_has_a_vector() -> None:
    covered = {
        vec["expect"]["error"]
        for major in ("v1", "v2")
        for vec in _manifest(major)["vectors"]
        if not vec["expect"]["ok"]
    }
    contract_codes = set(errors.EXIT_CODES) - {
        errors.PROVIDER_UNSET,
        errors.CONFIG_INVALID,
        errors.JWKS_UNAVAILABLE,
        errors.PROVIDER_MODULE_INVALID,
        errors.ATTESTATION_REFUSED,
    }
    assert contract_codes <= covered


def test_v2_driven_vectors_yield_workload_grade_never_human() -> None:
    for vec in _manifest("v2")["vectors"]:
        if vec["id"].startswith("v2-valid-driven"):
            claims = _run_token("v2", vec)
            actor = actor_from_claims(claims, provider="test")
            assert claims["origin"] == "driven"
            assert actor.grade == "workload"
            assert not actor.sub.startswith("human:")


@pytest.mark.parametrize("major", ["v1", "v2"])
def test_passing_vector_payloads_validate_against_the_schema_of_their_major(major: str) -> None:
    import base64

    schemas = {
        1: json.loads((CONTRACT / "schemas" / "identity-assertion.json").read_text()),
        2: json.loads((CONTRACT / "schemas" / "identity-assertion-2.json").read_text()),
    }
    for vec in _manifest(major)["vectors"]:
        if vec["kind"] != "token" or not vec["expect"]["ok"]:
            continue
        seg = vec["token"].split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4)))
        jsonschema.validate(payload, schemas[payload["cia"]])


def test_vendored_files_match_the_pin_table() -> None:
    text = PIN_FILE.read_text()
    assert re.search(r"Pinned commit: 8fe103264be8156172da794fd15f2126fbc43cbf", text)
    rows = re.findall(r"^\| (\S+) \| \S+ \| ([0-9a-f]{40}) \|$", text, re.M)
    assert len(rows) == 7
    for rel, blob in rows:
        data = (CONTRACT / rel).read_bytes()
        git_blob = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        assert git_blob == blob, rel
