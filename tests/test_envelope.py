"""test_envelope.py — unit tests for clagentic_loadout.envelope and the
packaged schema contract set (ported lr-8edc from an internal dispatch
platform's schemas/envelope-in.json, envelope-out.json, common.json, and
its own envelope_validator, Wave A slice 3, tome #688).

Boundary note (tome #688): the source project's envelope_validator tests
are entirely agent-payload-schema tests (validate_payload_only against a
specific agent's input-schema.json) — that surface is the source
project's domain (per-agent contracts) and stays there. This module covers ONLY the
loadout-owned surface: the envelope SHAPE (envelope-in.json / envelope-
out.json) and the shared common.json fragments, including the identity-
strip proof required by this slice (agent_name is an open validated string,
not an enum of internal cast names).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clagentic_loadout.envelope import (
    MODE_ENFORCE,
    MODE_WARN,
    SCHEMAS_DIR,
    EnvelopeValidationError,
    validate_against_schema,
    validate_envelope,
)


def _base_participants() -> list[dict]:
    return [
        {"kind": "human", "name": "operator", "role": "invoker", "position": 0},
        {"kind": "agent", "name": "some-custom-builder", "role": "invokee", "position": 1},
    ]


def _valid_envelope_in(**overrides) -> dict:
    envelope = {
        "task_id": "lr-8edc",
        "project": "clagentic-loadout",
        "agent_name": "some-custom-builder",
        "invoker": {"kind": "human", "name": "operator"},
        "authorized_by": "operator",
        "conversation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ab",
        "invocation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ac",
        "attempt": 1,
        "participants": _base_participants(),
        "invocation_ts": "2026-07-07T22:00:00Z",
    }
    envelope.update(overrides)
    return envelope


def _valid_envelope_out(**overrides) -> dict:
    envelope = {
        "task_id": "lr-8edc",
        "agent_name": "some-custom-builder",
        "invoker": {"kind": "human", "name": "operator"},
        "status": "ok",
        "conversation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ab",
        "invocation_id": "018f8a3e-7b2c-7f4a-8b3d-0123456789ac",
        "attempt": 1,
        "participants": _base_participants(),
        "invocation_ts": "2026-07-07T22:00:00Z",
        "duration_ms": 4200,
        "tool_call_count": 3,
        "error_codes": [],
        "agent_result_path": "results/lr-8edc/some-custom-builder-1720389600.json",
    }
    envelope.update(overrides)
    return envelope


class TestSchemaArtifacts:
    """The packaged schema files are the published contract — sanity-check
    identity (versioned $id, no legacy host) as a belt-and-suspenders check
    alongside the repo-wide anonymization grep-guard."""

    def test_schemas_dir_contains_the_envelope_contract_files(self):
        """The envelope contract set (slice 3) shares SCHEMAS_DIR with other
        published schemas added by later slices (e.g. the telemetry event
        schemas, slice 5) — this checks the envelope files are present as a
        subset, not that they are the only files in the directory."""
        names = {p.name for p in SCHEMAS_DIR.glob("*.json")}
        assert {"envelope-in.json", "envelope-out.json", "common.json"} <= names

    @pytest.mark.parametrize(
        "name", ["envelope-in.json", "envelope-out.json", "common.json"]
    )
    def test_id_is_versioned_public_namespace(self, name):
        """$id is the version-pinned GitHub raw URI for the eventual public
        clagentic/clagentic-loadout repo (operator decision, lr-8edc amendment)
        -- a stable identifier that resolves once that repo is published.
        The validator never fetches it over the network; it resolves $ref by
        local file scan (see TestNoInternalCoupling / _build_registry).

        The $id path segment (`v1`) stays fixed across additive contract
        migrations (lr-7a6e) because sibling schemas in this directory
        resolve `$ref: "common.json#/..."` relative to their own base URI
        (same directory) -- bumping one file's $id alone breaks that
        relative resolution for every other schema here. Per-schema
        content revisions are tracked via the `contract_revision` note in
        each file's own `description`, not via this path segment. See
        TestFollowupsContractRevision for the revision-tracking proof."""
        import json

        doc = json.loads((SCHEMAS_DIR / name).read_text(encoding="utf-8"))
        assert doc["$id"].startswith(
            "https://raw.githubusercontent.com/clagentic/clagentic-loadout/v1/schemas/"
        )
        assert doc["$id"].endswith(name)


class TestValidateEnvelopeIn:
    def test_representative_envelope_validates_clean(self):
        assert validate_envelope(_valid_envelope_in()) == []

    def test_missing_required_field_errors(self):
        envelope = _valid_envelope_in()
        del envelope["invocation_ts"]
        errors = validate_envelope(envelope, mode="warn")
        assert errors
        assert any("invocation_ts" in e for e in errors)

    def test_unknown_key_rejected(self):
        envelope = _valid_envelope_in(unexpected_field="nope")
        errors = validate_envelope(envelope, mode="warn")
        assert errors

    def test_task_id_is_optional_at_envelope_layer(self):
        envelope = _valid_envelope_in()
        del envelope["task_id"]
        assert validate_envelope(envelope) == []

    def test_malformed_agent_name_rejected(self):
        """agent_name enforces the shape pattern (lowercase, leading letter)
        even though it is no longer an enum of specific names."""
        envelope = _valid_envelope_in(agent_name="Not Valid!")
        errors = validate_envelope(envelope, mode="warn")
        assert errors
        assert any("agent_name" in e or "invoker" not in e for e in errors)


class TestValidateEnvelopeOut:
    def test_representative_envelope_validates_clean(self):
        assert validate_envelope(_valid_envelope_out(), schema_name="envelope-out.json") == []

    def test_missing_required_field_errors(self):
        envelope = _valid_envelope_out()
        del envelope["agent_result_path"]
        errors = validate_envelope(envelope, schema_name="envelope-out.json", mode="warn")
        assert errors
        assert any("agent_result_path" in e for e in errors)

    def test_status_enum_enforced(self):
        envelope = _valid_envelope_out(status="not-a-real-status")
        errors = validate_envelope(envelope, schema_name="envelope-out.json", mode="warn")
        assert errors


class TestAgentNameEnumRemoved:
    """Proves the boundary-rule fix for this slice: agent_name is an OPEN
    validated string (shape only), not an enumeration of internal cast
    names. Which concrete names are valid for a deployment is that
    deployment's config, never a hardcode in the published schema."""

    def test_arbitrary_valid_shaped_agent_name_accepted(self):
        """A name that is not any part of any internal roster must still
        validate — proving the enum was actually removed, not just renamed."""
        envelope = _valid_envelope_in(agent_name="totally-invented-role-42")
        assert validate_envelope(envelope) == []

    def test_participant_agent_name_also_open(self):
        envelope = _valid_envelope_in(
            participants=[
                {"kind": "human", "name": "operator", "role": "invoker", "position": 0},
                {
                    "kind": "agent",
                    "name": "another-invented-role",
                    "role": "invokee",
                    "position": 1,
                },
            ]
        )
        assert validate_envelope(envelope) == []

    def test_malformed_agent_name_shape_still_rejected(self):
        """The field stays validated — just not against a name enum. An
        uppercase or empty value is still a shape violation."""
        envelope = _valid_envelope_in(agent_name="")
        assert validate_envelope(envelope, mode="warn") != []

        envelope = _valid_envelope_in(agent_name="UPPERCASE-NOT-ALLOWED")
        assert validate_envelope(envelope, mode="warn") != []


class TestFollowupsContractRevision:
    """lr-7a6e: followups[] on envelope-out.json (contract_revision
    envelope-out@2 / common@2). Additive-only: present, empty, and missing
    all validate. The followup descriptor is generic -- no task-tracker
    coupling, no tracker ID field -- per CLAUDE.md hard rule 6a."""

    def test_followups_absent_still_validates(self):
        """A pre-migration caller that never sets followups keeps working
        unchanged -- proves the migration is additive, not required."""
        envelope = _valid_envelope_out()
        assert "followups" not in envelope
        assert validate_envelope(envelope, schema_name="envelope-out.json") == []

    def test_followups_empty_list_validates(self):
        envelope = _valid_envelope_out(followups=[])
        assert validate_envelope(envelope, schema_name="envelope-out.json") == []

    def test_followups_present_validates(self):
        envelope = _valid_envelope_out(
            followups=[
                {"summary": "extract the retry helper into its own module"},
                {
                    "summary": "tighten the timeout error message",
                    "kind": "refactor",
                    "related_path": "src/clagentic_loadout/wait/poll.py",
                    "priority": "low",
                },
            ]
        )
        assert validate_envelope(envelope, schema_name="envelope-out.json") == []

    def test_followup_missing_summary_rejected(self):
        envelope = _valid_envelope_out(followups=[{"kind": "refactor"}])
        errors = validate_envelope(
            envelope, schema_name="envelope-out.json", mode="warn"
        )
        assert errors

    def test_followup_unknown_key_rejected(self):
        """The descriptor is additionalProperties:false -- no tracker ID,
        assignee, or status field can slip in through this schema."""
        envelope = _valid_envelope_out(
            followups=[{"summary": "ok", "task_id": "lr-0000"}]
        )
        errors = validate_envelope(
            envelope, schema_name="envelope-out.json", mode="warn"
        )
        assert errors

    def test_followup_invalid_priority_rejected(self):
        envelope = _valid_envelope_out(
            followups=[{"summary": "ok", "priority": "urgent"}]
        )
        errors = validate_envelope(
            envelope, schema_name="envelope-out.json", mode="warn"
        )
        assert errors


class TestEnforceMode:
    """lr-273d: validate_envelope's mode parameter. enforce (the default)
    raises on an invalid envelope; warn preserves the historical
    list[str]-return contract unchanged. This module never imports a hook,
    a dispatch transport, or any deployment-specific env var — mode is a
    pure parameter on the existing entry point (CLAUDE.md hard rules 2,
    6a)."""

    def test_enforce_is_the_default_mode(self):
        """No mode kwarg supplied at all still enforces — proves the
        DEFAULT is enforce, not warn."""
        envelope = _valid_envelope_in()
        del envelope["invocation_ts"]
        with pytest.raises(EnvelopeValidationError):
            validate_envelope(envelope)

    def test_enforce_mode_raises_on_invalid_envelope(self):
        envelope = _valid_envelope_in()
        del envelope["invocation_ts"]
        with pytest.raises(EnvelopeValidationError) as excinfo:
            validate_envelope(envelope, mode=MODE_ENFORCE)
        assert any("invocation_ts" in e for e in excinfo.value.errors)

    def test_enforce_mode_returns_empty_list_on_valid_envelope(self):
        assert validate_envelope(_valid_envelope_in(), mode=MODE_ENFORCE) == []

    def test_warn_mode_returns_list_unchanged_never_raises(self):
        envelope = _valid_envelope_in()
        del envelope["invocation_ts"]
        errors = validate_envelope(envelope, mode=MODE_WARN)
        assert isinstance(errors, list)
        assert any("invocation_ts" in e for e in errors)

    def test_warn_mode_returns_empty_list_on_valid_envelope(self):
        assert validate_envelope(_valid_envelope_in(), mode=MODE_WARN) == []

    def test_unrecognized_mode_reports_resolved_value(self):
        """CLAUDE.md rule 4 (CLI hygiene / error clarity): the error message
        names the actual bad value passed, not a generic complaint."""
        with pytest.raises(ValueError) as excinfo:
            validate_envelope(_valid_envelope_in(), mode="not-a-real-mode")
        assert "not-a-real-mode" in str(excinfo.value)

    def test_enforce_error_carries_same_messages_as_warn(self):
        """A caller migrating from warn to enforce does not lose diagnostic
        detail — the exception's .errors is the same list warn returns."""
        envelope = _valid_envelope_in()
        del envelope["invocation_ts"]
        warn_errors = validate_envelope(envelope, mode=MODE_WARN)
        with pytest.raises(EnvelopeValidationError) as excinfo:
            validate_envelope(envelope, mode=MODE_ENFORCE)
        assert excinfo.value.errors == warn_errors


class TestValidateAgainstSchema:
    def test_validates_instance_against_arbitrary_schema_file(self, tmp_path: Path):
        import json

        schema_path = tmp_path / "widget.json"
        schema_path.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://example.invalid/widget.json",
                    "type": "object",
                    "required": ["name"],
                    "properties": {"name": {"type": "string"}},
                }
            ),
            encoding="utf-8",
        )
        assert validate_against_schema({"name": "ok"}, schema_path) == []
        assert validate_against_schema({}, schema_path) != []

    def test_missing_schema_file_reports_not_found(self, tmp_path: Path):
        errors = validate_against_schema({}, tmp_path / "does-not-exist.json")
        assert errors
        assert "not found" in errors[0]

    def test_extra_schema_dirs_resolve_refs_to_common_fragments(self, tmp_path: Path):
        """A deployment-supplied schema can $ref the published common.json
        fragments by passing SCHEMAS_DIR (already default-included) plus its
        own dir for additional local fragments. Relative $ref resolution is
        base-URI relative, so the deployment schema's $id must share the
        loadout schema namespace (or use an absolute $ref) to resolve
        "common.json#/..." the same way envelope-in.json/envelope-out.json
        do."""
        import json

        schema_path = tmp_path / "custom-agent-payload.json"
        schema_path.write_text(
            json.dumps(
                {
                    "$schema": "https://json-schema.org/draft/2020-12/schema",
                    "$id": "https://raw.githubusercontent.com/clagentic/clagentic-loadout/v1/schemas/custom-agent-payload.json",
                    "type": "object",
                    "required": ["status"],
                    "properties": {
                        "status": {"$ref": "common.json#/$defs/status"}
                    },
                }
            ),
            encoding="utf-8",
        )
        assert validate_against_schema({"status": "ok"}, schema_path) == []
        assert validate_against_schema({"status": "bogus"}, schema_path) != []


class TestNoInternalCoupling:
    """Transport-agnostic + no-lore invariants (CLAUDE.md hard rules 2, 6a)."""

    def test_module_has_no_lore_import(self):
        import clagentic_loadout.envelope as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "lore" not in source.lower().replace("explore", "")

    def test_module_does_not_reference_agents_dir_concept(self):
        """The ported validator's agents_dir parameter (per-agent
        input-schema.json lookup) is a source-platform concept per the tome
        #688 boundary rule and must not appear here."""
        import clagentic_loadout.envelope as mod

        source = Path(mod.__file__).read_text(encoding="utf-8")
        assert "agents_dir" not in source
