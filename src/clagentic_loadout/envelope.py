"""envelope.py — dispatch envelope schema validator.

Published, versioned contract for the loadout dispatch envelope (Wave A
slice 3, tome #688). Validates the outer envelope shape (envelope-in.json /
envelope-out.json) against the schemas packaged in
clagentic_loadout/schemas/.

Boundary (tome #688): this module owns the envelope SHAPE only — it does
not know about, and never imports, any specific agent's payload schema.
Per-agent payload validation is a deployment concern layered on top of
`payload` (an opaque object at this layer); a caller that wants to also
validate an agent-specific payload schema does so with its own schema file
via validate_against_schema(), passing the loadout schemas dir alongside its
own for $ref resolution.

Transport-agnostic (CLAUDE.md hard rule 2): no orchestration coupling, no
task-tracker client, no environment-variable coupling to any specific
deployment. Pure schema validation over plain dicts and paths.

Public surface:
    validate_envelope(envelope, schema_name="envelope-in.json", extra_schema_dirs=(),
                       mode="enforce")
        -> list[str]  # mode="warn": always returns the error list (empty = valid),
                      # never raises.
                      # mode="enforce" (DEFAULT): raises EnvelopeValidationError
                      # when the error list is non-empty; returns [] on success.
                      # mode is a single parameter on the existing entry point
                      # (rather than a second wrapper function) so there is
                      # exactly one call site to learn, and warn-mode keeps the
                      # historical list[str] contract byte-for-byte for any
                      # caller that already branches on it.

    validate_against_schema(instance, schema_path, extra_schema_dirs=())
        -> list[str]  # empty = valid — generic single-schema validator, reusable
                      # for any deployment-supplied schema that needs to $ref
                      # the published common.json fragments. Shape-only helper;
                      # no enforce/warn mode of its own — call sites that want
                      # enforce semantics on a custom schema wrap the list[str]
                      # result in EnvelopeValidationError themselves.

EnvelopeValidationError(ValueError): raised by validate_envelope(mode="enforce")
    when validation fails. Carries the same list[str] error messages
    (.errors attribute) that mode="warn" returns, so a caller migrating
    between modes does not lose diagnostic detail.

A deployment wires enforce-mode validation into its own gate (e.g. a
PreToolUse hook) by calling validate_envelope(envelope) and catching
EnvelopeValidationError — see docs/integration.md's "Envelope enforce
mode" section for a worked example. loadout does not own or import any
hook framework; the primitive is the validation call, nothing more.

Internal helpers (_load_json, _build_registry) are private.

All schemas are JSON Schema draft 2020-12. Validation uses
jsonschema.Draft202012Validator + referencing.Registry for $ref resolution.

Dependencies: jsonschema, referencing.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

#: Directory holding the packaged, published loadout schemas
#: (envelope-in.json, envelope-out.json, common.json).
SCHEMAS_DIR = Path(__file__).resolve().parent / "schemas"

#: Valid values for validate_envelope()'s `mode` parameter.
MODE_ENFORCE = "enforce"
MODE_WARN = "warn"
VALID_MODES = frozenset({MODE_ENFORCE, MODE_WARN})


class EnvelopeValidationError(ValueError):
    """Raised by validate_envelope(mode="enforce") when the envelope fails
    schema validation. `.errors` carries the same list[str] messages that
    mode="warn" returns, so a caller catching this exception has the full
    diagnostic detail available without a second validation pass."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        summary = "; ".join(errors)
        super().__init__(f"envelope validation failed ({len(errors)} error(s)): {summary}")


# Lazy import so import-time failures surface as ImportError at call time,
# not when the module is loaded — keeps a caller's fail-open path intact if
# jsonschema is absent in an unusual install.
def _get_validator_classes():  # type: ignore[return]
    try:
        from jsonschema import Draft202012Validator
        from referencing import Registry, Resource
        from referencing.jsonschema import DRAFT202012
        return Draft202012Validator, Registry, Resource, DRAFT202012
    except ImportError as exc:
        raise ImportError(
            f"clagentic_loadout.envelope requires jsonschema + referencing: {exc}"
        ) from exc


def _load_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _build_registry(*scan_dirs: Path) -> Any:
    """
    Build a referencing.Registry keyed by $id for all JSON schemas found under
    scan_dirs. Enables $ref resolution across envelope-in.json, envelope-
    out.json, and common.json (and any deployment-supplied schema dirs)
    without an HTTP fetch.
    """
    Draft202012Validator, Registry, Resource, DRAFT202012 = _get_validator_classes()
    resources: list[tuple[str, Any]] = []
    for base_dir in scan_dirs:
        if not base_dir.is_dir():
            continue
        for json_path in base_dir.rglob("*.json"):
            try:
                doc = _load_json(json_path)
            except (json.JSONDecodeError, OSError):
                continue
            if isinstance(doc, dict) and "$id" in doc:
                resources.append(
                    (doc["$id"], Resource.from_contents(doc, default_specification=DRAFT202012))
                )
    return Registry().with_resources(resources)


def validate_envelope(
    envelope: dict[str, Any],
    *,
    schema_name: str = "envelope-in.json",
    extra_schema_dirs: tuple[Path, ...] = (),
    mode: str = MODE_ENFORCE,
) -> list[str]:
    """
    Validate a dispatch envelope against one of the published envelope
    schemas (envelope-in.json or envelope-out.json), resolving $ref against
    common.json.

    Parameters
    ----------
    envelope:          The parsed dispatch envelope dict.
    schema_name:       Which published schema to validate against —
                        "envelope-in.json" (default) or "envelope-out.json".
    extra_schema_dirs: Additional directories to scan for $ref resolution
                        (e.g. a deployment's own schema fragments). The
                        packaged loadout schemas dir is always included.
    mode:               "enforce" (default) or "warn". "enforce" raises
                        EnvelopeValidationError when the envelope is
                        invalid; "warn" never raises and returns the error
                        list as before. An unrecognized mode raises
                        ValueError reporting the resolved (invalid) value
                        and the valid set, not a stale/guessed message.

    Returns
    -------
    list[str]
        Empty list means valid. Each entry is a human-readable error
        string. In mode="enforce" this return only ever happens on
        success (empty list) — a non-empty result raises instead.

    Raises
    ------
    EnvelopeValidationError
        mode="enforce" and the envelope failed validation.
    ValueError
        mode is not one of VALID_MODES.
    """
    if mode not in VALID_MODES:
        raise ValueError(f"mode must be one of {sorted(VALID_MODES)}, got {mode!r}.")

    Draft202012Validator, Registry, Resource, DRAFT202012 = _get_validator_classes()

    schema_path = SCHEMAS_DIR / schema_name
    if not schema_path.is_file():
        errors = [f"envelope schema not found: {schema_path}"]
        if mode == MODE_ENFORCE:
            raise EnvelopeValidationError(errors)
        return errors

    schema = _load_json(schema_path)
    registry = _build_registry(SCHEMAS_DIR, *extra_schema_dirs)

    validator = Draft202012Validator(schema, registry=registry)
    errors = []
    for err in validator.iter_errors(envelope):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"envelope: {path}: {err.message}")

    if errors and mode == MODE_ENFORCE:
        raise EnvelopeValidationError(errors)

    return errors


def validate_against_schema(
    instance: dict[str, Any],
    schema_path: Path,
    *,
    extra_schema_dirs: tuple[Path, ...] = (),
) -> list[str]:
    """
    Validate an arbitrary JSON-compatible dict against a single schema file,
    using the same Draft202012Validator + referencing.Registry machinery as
    validate_envelope(). Useful for a deployment-supplied schema (e.g. an
    agent's own payload schema) that needs to $ref the published common.json
    fragments.

    Parameters
    ----------
    instance:           The parsed JSON object to validate.
    schema_path:         Path to the schema file to validate against (must
                        declare $id if it needs to $ref or be $ref'd by
                        siblings).
    extra_schema_dirs:  Additional directories to scan for $ref resolution.
                        The packaged loadout schemas dir is always included.

    Returns
    -------
    list[str]
        Empty list means valid. Each entry is a human-readable error string.
    """
    Draft202012Validator, Registry, Resource, DRAFT202012 = _get_validator_classes()

    if not schema_path.is_file():
        return [f"schema not found: {schema_path}"]

    schema = _load_json(schema_path)
    registry = _build_registry(SCHEMAS_DIR, *extra_schema_dirs)

    validator = Draft202012Validator(schema, registry=registry)
    errors: list[str] = []
    for err in validator.iter_errors(instance):
        path = "/".join(str(p) for p in err.absolute_path) or "<root>"
        errors.append(f"{path}: {err.message}")

    return errors
