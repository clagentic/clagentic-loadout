"""transport.note_compose — general-purpose, tool-owned body composition.

lr-10a996 (BODY-TRANSPORT half; pairs with the emit-and-verify readback,
lr-482c20, a separate task). WHY THIS MODULE EXISTS: before this, a caller
posting a comment/PR body that needed BOTH prose AND a caller-side tracking
reference had no way to do so without authoring guard-hostile shell --
either hand-typing a fenced markdown block containing literal backticks
inside a --body-stdin JSON producer, or staging state notes via a
`cat >> $HOME/... << EOF` heredoc with a $VAR-substituted redirect target.
guard-bash.py's static argv scanner cannot tell a markdown fence apart from
shell command substitution, and cannot analyze a heredoc's redirect target
when it is variable-substituted -- it fails closed on both, producing false
blocks and manual operator Allow/Deny prompts (lr-d9dedc, observed against a
Forgejo deployment).

THE FIX: move body composition INTO the verb. The caller passes STRUCTURED
data -- prose text and an optional opaque caller_tracking_id -- over
--body-stdin's ordinary JSON channel (already the sole body path per
transport.git_host_api's own module docstring; no new disk-staging flag,
no second content source). This module does ALL markdown/fence
construction in-process and returns a plain string; the caller's shell
command never contains a backtick, a heredoc, or a variable redirect
target of any kind.

CALLER-ID SLOT (parameterized, not baked in): caller_tracking_id is an
opaque string this module never interprets -- a lore task id for a
LORE-integrated deployment, or any other deployment's own tracking-id
shape (CLAUDE.md rule 6a: no lore/LORE_* dependency in product code). When
omitted, build_composed_body returns the prose unchanged -- this module
adds zero overhead/fence noise to a plain body post that carries no
tracking id.

Distinct from merge.verdict's ```review-result``` fence: that fence is a
merge-gate ENFORCEMENT contract (parsed and acted on by loadout-merge) and
stays scoped to the reviewer-verdict shape. The ```loadout-note``` fence
this module builds carries no enforcement semantics -- it is inert
metadata a deployment may read back for its own bookkeeping, never
something loadout's own gates parse or act on. The two fences intentionally
use different fence-language tags (`review-result` vs `loadout-note`) so a
reader can never confuse a caller-tracking note for a merge-gate verdict.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from clagentic_loadout.envelope import validate_against_schema

#: The fence language token that marks a loadout-note metadata block.
#: Distinct from merge.verdict.VERDICT_FENCE ("review-result") -- this
#: fence carries no enforcement semantics and is never parsed by a gate.
NOTE_FENCE = "loadout-note"

_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
_NOTE_SCHEMA_PATH = _SCHEMAS_DIR / "loadout-note.schema.json"

#: Extracts the JSON payload from a fenced loadout-note block. The fence
#: LANGUAGE TAG must be on the SAME LINE as the opening triple-backticks,
#: mirroring merge.verdict._FENCE_RE's same requirement.
_NOTE_FENCE_RE = re.compile(r"```loadout-note\s*\n(.*?)\n```", re.DOTALL)


def build_caller_note_block(caller_tracking_id: str) -> str:
    """Build the fenced ```loadout-note``` block carrying *caller_tracking_id*.

    Returns a multi-line string starting with a blank line (visual
    separation from human prose above), then the fenced block -- the same
    shape convention as merge.verdict.build_verdict_block.

    Raises ValueError if caller_tracking_id is empty/whitespace-only.
    """
    if not caller_tracking_id or not caller_tracking_id.strip():
        raise ValueError("caller_tracking_id must be a non-empty string")
    payload = json.dumps({"caller_tracking_id": caller_tracking_id}, separators=(", ", ": "))
    return f"\n```{NOTE_FENCE}\n{payload}\n```\n"


def build_composed_body(prose: str, *, caller_tracking_id: str | None) -> str:
    """Compose the final POST body from caller-supplied structured fields.

    *prose* is the caller's ordinary comment/PR text (from --body-stdin's
    'body' field -- no backtick, fence, or shell metacharacter required in
    it). *caller_tracking_id*, when supplied (non-None, non-empty), is
    appended as a tool-constructed ```loadout-note``` fence via
    build_caller_note_block -- the caller never authors the fence itself.

    When caller_tracking_id is None or empty, returns *prose* unchanged: a
    plain body post that carries no tracking id pays no fence-composition
    overhead.
    """
    if caller_tracking_id is None or not caller_tracking_id.strip():
        return prose
    return f"{prose}\n{build_caller_note_block(caller_tracking_id)}"


def parse_caller_note_block(body: str) -> dict[str, str] | None:
    """Parse the fenced ```loadout-note``` block from a comment/PR body, if
    present. Returns the parsed {"caller_tracking_id": ...} dict, or None if
    no block is found. Only the LAST match is returned (most-recent
    invocation wins on retries), mirroring merge.verdict's own convention.

    Raises ValueError on malformed JSON, a non-object payload, or a schema
    violation (missing/extra field, wrong type) -- a caller that wants a
    tolerant "best effort" read should catch ValueError itself; this
    function never silently returns a partial/guessed result.
    """
    matches = _NOTE_FENCE_RE.findall(body)
    if not matches:
        return None
    raw = matches[-1].strip()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"loadout-note block contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"loadout-note block JSON is not an object: {type(data).__name__}")
    errors = validate_against_schema(data, _NOTE_SCHEMA_PATH)
    if errors:
        raise ValueError(f"loadout-note block failed schema validation: {'; '.join(errors)}")
    return data


__all__ = [
    "NOTE_FENCE",
    "build_caller_note_block",
    "build_composed_body",
    "parse_caller_note_block",
]
