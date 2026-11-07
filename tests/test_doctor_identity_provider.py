"""The doctor identity check reports the provider, its key-set sources, the
self_declared flag and the recorded fallback count, and never fails the run."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.doctor.identity_check import check_identity_provider
from tests._support.identity_support import ISSUER, write_config

SINK = "CLAGENTIC_LOADOUT_TELEMETRY_SINK"
SINK_DIR = "CLAGENTIC_LOADOUT_TELEMETRY_DIR"


def _gatekeeper(tmp_path: Path) -> dict:
    return {"provider": "gatekeeper", "gatekeeper": {"jwks": "file:///keys/jwks.json", "issuer": ISSUER}}


def test_gatekeeper_reports_its_key_set_source_and_that_it_is_not_self_declared(tmp_path: Path) -> None:
    result = check_identity_provider(config_root=write_config(tmp_path / "cfg", _gatekeeper(tmp_path)), env={})
    assert result.ok is True
    assert result.resolved["provider"] == "gatekeeper"
    assert result.resolved["self_declared"] is False
    assert result.resolved["jwks_sources"] == [{"iss": ISSUER, "jwks": "file:///keys/jwks.json"}]
    assert result.resolved["fallback_count"] is None


def test_static_has_no_key_set_and_is_self_declared(tmp_path: Path) -> None:
    cfg = write_config(tmp_path / "cfg", {"provider": "static", "static": {"actor": "bot", "roles": ["builder"]}})
    result = check_identity_provider(config_root=cfg, env={})
    assert result.resolved["self_declared"] is True and result.resolved["jwks_sources"] is None


@pytest.mark.parametrize("recorded,expected", [(0, 0), (3, 3)])
def test_fallback_count_reads_the_filesystem_sink(tmp_path: Path, recorded: int, expected: int) -> None:
    events = tmp_path / "events"
    events.mkdir()
    lines = [json.dumps({"schema": "identity-fallback/v1", "reason": "x"})] * recorded
    lines.append(json.dumps({"schema": "something-else/v1"}))
    (events / "identity-fallback_v1-stream.jsonl").write_text("\n".join(lines) + "\n")
    env = {SINK: "filesystem", SINK_DIR: str(events)}
    result = check_identity_provider(config_root=write_config(tmp_path / "cfg", _gatekeeper(tmp_path)), env=env)
    assert result.resolved["fallback_count"] == expected
    assert result.ok is True


def test_an_unusable_sink_is_reported_as_unknown_not_as_a_failure(tmp_path: Path) -> None:
    env = {SINK: "filesystem"}
    result = check_identity_provider(config_root=write_config(tmp_path / "cfg", _gatekeeper(tmp_path)), env=env)
    assert result.resolved["fallback_count"] is None and result.ok is True
