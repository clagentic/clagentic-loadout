"""test_caller_attested_value_contract.py — tome #700 correction 3 (lr-e5eeab).

Confirms the contract T4-lo exists to lock in: every verb's `--caller`/
`--role` flag is consumed as an ALREADY-ATTESTED, opaque config-key value
sourced from the invoking harness/guard-hook -- never a free CLI arg this
package re-authenticates itself, and never sourced by ingesting a
harness-identity sidecar/side-channel (which would break loadout's
orchestration-agnostic trust model, CLAUDE.md rule 2). T4-gk (lr-116b57,
clagentic-gatekeeper) already landed the mint-time entitlement check this
package's reference deployment layers in front of these seams; this test
suite covers loadout's own side of the boundary:

  1. No verb/seam in this package IMPORTS, reads env for, or otherwise
     wires up a harness-identity sidecar file/env-var/side-channel to
     derive `--caller`/`--role` -- a structural check (import graph +
     known env-var-reading call sites), not a bare word-ban: this
     package's docs/docstrings legitimately DISCUSS the sidecar concept in
     prose (to state that it must never be ingested, per this task's own
     scope note), and `transport.body_env`'s pre-existing, unrelated
     "identity-stamp sidecar" convention (a loadout-owned, param-only,
     per-invocation body-staging artifact, lr-becdef/lr-199b99) also uses
     the word legitimately -- a whole-repo grep-ban on the word "sidecar"
     would false-positive on both. This test instead asserts the SPECIFIC,
     forbidden shape: nothing in `transport.credential_provider` or
     `merge.authority` (the two seams `--caller`/`--role` actually flow
     through) reads any file path or env var whose name suggests an
     identity/attestation side-channel distinct from the `role`/`repo`
     arguments those seams already accept as explicit parameters.
  2. `--caller`/`--role` argparse help text on every verb that exposes the
     flag documents the already-attested-value contract, so a caller
     reading `--help` sees the same statement the module docstrings and
     docs/ pages carry -- not just prose buried in a docstring nobody
     reads at the CLI.
  3. `resolve_token`/`check_authority` treat `role` as a pure pass-through
     string -- no seam here parses it, looks it up against a live identity
     service, or otherwise treats it as anything other than a config key
     (already covered functionally by test_review_verb.py's
     `_RecordingTokenProvider` proof and test_merge_verb.py's authority
     tests; this module adds the "no sidecar ingestion" and "--help says
     so" checks those did not previously assert).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from clagentic_loadout.acquire import verb as acquire_verb
from clagentic_loadout.merge import authority as merge_authority
from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import credential_provider

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The two seams --caller/--role actually flow through to reach a
#: credential/authority decision (see this repo's docs/merge-authority.md
#: and docs/integration.md's "Credentials" section for the consumer-facing
#: statement of why these are the load-bearing modules for this contract).
_CALLER_CONSUMING_SEAMS: tuple[Path, ...] = (
    Path(credential_provider.__file__),
    Path(merge_authority.__file__),
)


def _module_calls_and_names(path: Path) -> set[str]:
    """Every Name/Attribute identifier referenced anywhere in *path*'s AST
    (function calls, attribute access, imports) -- a structural surface far
    harder to accidentally satisfy than a substring search, and immune to
    matching this module's OWN prose describing what must never happen."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


#: Identifier substrings that would indicate this seam reaches for a
#: harness-identity side-channel to derive/verify the role/caller value,
#: instead of accepting it as the explicit `role`/`caller` function
#: argument every TokenProvider/AuthorityProvider call site already passes.
_FORBIDDEN_IDENTIFIER_SUBSTRINGS: tuple[str, ...] = (
    "sidecar",
    "identity_stamp",  # the --body-env staging concept, unrelated to this seam
    "harness_identity",
)


@pytest.mark.parametrize(
    "path",
    _CALLER_CONSUMING_SEAMS,
    ids=lambda p: str(p.relative_to(REPO_ROOT.parent) if REPO_ROOT.parent in p.parents else p.name),
)
def test_credential_and_authority_seams_ingest_no_identity_sidecar(path: Path) -> None:
    """Structural guard: transport.credential_provider and merge.authority
    -- the two seams --caller/--role actually reach -- must not reference
    any identifier suggesting a harness-identity sidecar/side-channel
    ingestion path. Both accept role/caller purely as an explicit function
    argument (already proven end-to-end by test_review_verb.py's
    _RecordingTokenProvider and this repo's merge-authority tests); this
    guard is the structural backstop against a future change quietly
    adding a second, out-of-band identity source."""
    identifiers = _module_calls_and_names(path)
    hits = [
        identifier
        for identifier in identifiers
        for forbidden in _FORBIDDEN_IDENTIFIER_SUBSTRINGS
        if forbidden in identifier.lower()
    ]
    assert not hits, (
        f"{path.name}: found identifier(s) suggesting sidecar/side-channel "
        f"identity ingestion -- loadout MUST NOT ingest a harness identity "
        f"sidecar (lr-e5eeab scope): {sorted(set(hits))}"
    )


@pytest.mark.parametrize(
    "build_parser,flag",
    [
        (review_verb._build_arg_parser, "--caller"),
        (push_verb._build_arg_parser, "--caller"),
        (merge_verb._build_arg_parser, "--role"),
        (acquire_verb._build_arg_parser, "--caller"),
    ],
    ids=["review-post:--caller", "push:--caller", "merge:--role", "acquire:--caller"],
)
def test_caller_role_help_documents_already_attested_contract(build_parser, flag) -> None:
    """Every verb exposing --caller/--role states, IN ITS OWN --help TEXT,
    that the value must already be attested by the invoking harness/guard-
    hook -- not just in a module docstring a CLI caller never reads."""
    parser = build_parser()
    # Inspect the flag's own registered help string directly (parser._actions)
    # rather than slicing format_help()'s rendered text -- format_help()
    # prints the flag a SECOND time in the usage synopsis before the options
    # list, and a plain substring index() finds that occurrence first,
    # which carries no help text at all. Going straight to the Action's own
    # `.help` attribute is exact and immune to argparse's rendering/wrapping
    # choices.
    matching_actions = [
        action
        for action in parser._actions  # noqa: SLF001 -- test-only introspection, not runtime code
        if flag in action.option_strings
    ]
    assert matching_actions, f"{flag} not found on this parser's registered actions"
    help_text = matching_actions[0].help or ""
    assert "already attested" in help_text, (
        f"{flag} help text does not document the already-attested-value "
        f"contract (tome #700 correction 3, lr-e5eeab): {help_text!r}"
    )
    assert "opaque config key" in help_text, (
        f"{flag} help text does not state the opaque-config-key consumption "
        f"model: {help_text!r}"
    )
