"""merge.fence_state — findings STATE carried inside the verdict fence.

A blocking finding raised at one head has no representation at a later head
unless the fence says so. Without that, a correctly scoped clean verdict at a
later head reads as resolution of an earlier blocker nobody re-examined. This
module defines the optional state fields a tool-constructed fence may carry and
the reader-side logic that turns a reviewer's fence history into a refusal.

Fields (all optional; fence_schema_version 2 whenever any is present):

  findings_open   [{id, rule_id, head}]  findings the reviewer holds open
  supersedes      <comment id>           the earlier comment this one replaces
  cleared_claims  [{id, head, evidence}] findings the reviewer resolved, at
                                         the head it reviewed, with one line
                                         of evidence
  scanners_run    [{scanner, status, reason}]
                                         per scanner: ran, not_applicable,
                                         not_invoked or failed

EVIDENCE fields ride in the same tool-built fence but are not state: they hold
no finding open and clear none, so they never change the fence schema version
and the merge gate does not read them. They are machine-readable copies of what
the verdict prose shows:

  failure_sequences [{file, line, rule_id, failure_sequence}]
                                         the trigger steps and damage of each
                                         blocking finding that stated them
  dropped           [{file, line, rule_id, message, reason}]
                                         candidates the reviewer examined and
                                         dropped, each with the reason
  range             {basis, base|since, head}
                                         the commit range the review covered
  engines           [{engine, model, reason, chunks}]
                                         which engine answered the run's chunks

COMPATIBILITY. A version-1 fence (no state fields) stays valid and readable.
It reads as "no findings asserted open at that fence", never as an error: an
absent field is silence, not a malformed verdict.

ONLY THE VERB EMITS THESE. They are accepted as structured input by the
review-post and loadout-review post verbs and rendered into the fence by
merge.verdict; a hand-authored fence carrying them is refused by the same
pre-embedded-fence check that refuses any hand-authored fence.

SUPERSEDES IS PROVENANCE, NOT RESOLUTION. Replacing a comment does not clear
the findings it held open; only a cleared_claims entry at the head under
review does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.sha import FULL_SHA_RE

#: Fence schema version stamped on a fence that carries any state field. A
#: fence without the stamp is version 1.
FENCE_SCHEMA_VERSION = 2

KEY_FENCE_SCHEMA_VERSION = "fence_schema_version"
KEY_FINDINGS_OPEN = "findings_open"
KEY_SUPERSEDES = "supersedes"
KEY_CLEARED_CLAIMS = "cleared_claims"
KEY_SCANNERS_RUN = "scanners_run"

#: The caller-suppliable state fields, in the order they render.
STATE_KEYS = (KEY_FINDINGS_OPEN, KEY_SUPERSEDES, KEY_CLEARED_CLAIMS, KEY_SCANNERS_RUN)

KEY_FAILURE_SEQUENCES = "failure_sequences"
KEY_DROPPED = "dropped"
KEY_RANGE = "range"
KEY_ENGINES = "engines"

#: The caller-suppliable evidence fields, in the order they render. Kept apart
#: from STATE_KEYS on purpose: the gate's "does this fence assert state" logic
#: reads STATE_KEYS only.
EVIDENCE_KEYS = (KEY_FAILURE_SEQUENCES, KEY_DROPPED, KEY_RANGE, KEY_ENGINES)

RANGE_BASIS_BASE_HEAD = "base..head"
RANGE_BASIS_SINCE = "since"
ENGINE_CARRIER = "carrier"
ENGINE_FALLBACK = "fallback"

SCANNER_RAN = "ran"
SCANNER_NOT_APPLICABLE = "not_applicable"
SCANNER_NOT_INVOKED = "not_invoked"
SCANNER_FAILED = "failed"
SCANNER_STATUSES = (SCANNER_RAN, SCANNER_NOT_APPLICABLE, SCANNER_NOT_INVOKED, SCANNER_FAILED)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$")


@dataclass(frozen=True)
class FindingsState:
    """The state fields parsed from one fence. Empty for a version-1 fence."""

    fence_schema_version: int = 1
    findings_open: tuple[dict[str, Any], ...] = ()
    supersedes: int | None = None
    cleared_claims: tuple[dict[str, Any], ...] = ()
    scanners_run: tuple[dict[str, Any], ...] = ()


def _text(value: Any, where: str, *, single_line: bool = True) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{where} must be a non-empty string, got {value!r}")
    if single_line and ("\n" in value or "\r" in value):
        raise ValueError(f"{where} must be a single line")
    offending = find_fence_syntax(value)
    if offending is not None:
        raise ValueError(
            f"{where} contains the fence-delimiter sequence {offending!r}; a "
            f"tool-constructed fence never carries fence-shaped caller text"
        )
    return value


def _finding_id(value: Any, where: str) -> str:
    text = _text(value, where)
    if not _ID_RE.match(text):
        raise ValueError(
            f"{where} must be 1-64 characters of letters, digits, '.', '_', ':' or '-', "
            f"starting with a letter or digit, got {value!r}"
        )
    return text


def _head(value: Any, where: str) -> str:
    if not isinstance(value, str) or not FULL_SHA_RE.match(value):
        raise ValueError(f"{where} must be a full 40-character lowercase hex SHA, got {value!r}")
    return value


def _entries(value: Any, key: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a JSON array, got {type(value).__name__}")
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ValueError(f"{key}[{index}] must be a JSON object, got {type(entry).__name__}")
    return value


def _exact_keys(entry: Mapping[str, Any], allowed: Iterable[str], where: str) -> None:
    unknown = sorted(set(entry) - set(allowed))
    if unknown:
        raise ValueError(f"{where} has unknown field(s) {unknown}; allowed: {sorted(allowed)}")


def index_scanners(entries: Iterable[Mapping[str, Any]]) -> dict[str, Mapping[str, Any]]:
    """Map scanner name to its outcome entry, refusing a repeated name.

    The one place that decides what a repeated scanner means: it is an error,
    never last-write-wins, because a later `ran` must not be able to hide an
    earlier `failed` for the same scanner. The JSON schema cannot express
    uniqueness on a property, so the writer (normalize_findings_state) and every
    reader (parse_verdict_block, the scanner gate) go through here.
    """
    indexed: dict[str, Mapping[str, Any]] = {}
    for entry in entries:
        name = entry["scanner"]
        if name in indexed:
            raise ValueError(f"scanners_run lists scanner {name!r} more than once")
        indexed[name] = entry
    return indexed


def _line_number(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{where} must be an integer of 1 or greater, got {value!r}")
    return value


def _normalize_failure_sequences(value: Any) -> list[dict[str, Any]]:
    rendered = []
    for index, entry in enumerate(_entries(value, KEY_FAILURE_SEQUENCES)):
        where = f"{KEY_FAILURE_SEQUENCES}[{index}]"
        _exact_keys(entry, ("file", "line", "rule_id", "failure_sequence"), where)
        rendered.append(
            {
                "file": _text(entry.get("file"), f"{where}.file"),
                "line": _line_number(entry.get("line"), f"{where}.line"),
                "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
                "failure_sequence": _text(
                    entry.get("failure_sequence"), f"{where}.failure_sequence", single_line=False
                ),
            }
        )
    return rendered


def _normalize_dropped(value: Any) -> list[dict[str, Any]]:
    rendered = []
    for index, entry in enumerate(_entries(value, KEY_DROPPED)):
        where = f"{KEY_DROPPED}[{index}]"
        _exact_keys(entry, ("file", "line", "rule_id", "message", "reason"), where)
        rendered.append(
            {
                "file": _text(entry.get("file"), f"{where}.file"),
                "line": _line_number(entry.get("line"), f"{where}.line"),
                "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
                "message": _text(entry.get("message"), f"{where}.message", single_line=False),
                "reason": _text(entry.get("reason"), f"{where}.reason", single_line=False),
            }
        )
    return rendered


def _normalize_range(value: Any) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{KEY_RANGE} must be a JSON object, got {type(value).__name__}")
    basis = value.get("basis")
    if basis == RANGE_BASIS_BASE_HEAD:
        _exact_keys(value, ("basis", "base", "head"), KEY_RANGE)
        return {
            "basis": basis,
            "base": _head(value.get("base"), f"{KEY_RANGE}.base"),
            "head": _head(value.get("head"), f"{KEY_RANGE}.head"),
        }
    if basis == RANGE_BASIS_SINCE:
        _exact_keys(value, ("basis", "since", "head"), KEY_RANGE)
        return {
            "basis": basis,
            "since": _head(value.get("since"), f"{KEY_RANGE}.since"),
            "head": _head(value.get("head"), f"{KEY_RANGE}.head"),
        }
    raise ValueError(
        f"{KEY_RANGE}.basis must be {RANGE_BASIS_BASE_HEAD!r} or {RANGE_BASIS_SINCE!r}, "
        f"got {basis!r}"
    )


def _normalize_engines(value: Any) -> list[dict[str, Any]]:
    rendered = []
    seen: set[tuple[Any, ...]] = set()
    for index, entry in enumerate(_entries(value, KEY_ENGINES)):
        where = f"{KEY_ENGINES}[{index}]"
        _exact_keys(entry, ("engine", "model", "reason", "chunks"), where)
        engine = entry.get("engine")
        if engine not in (ENGINE_CARRIER, ENGINE_FALLBACK):
            raise ValueError(
                f"{where}.engine must be {ENGINE_CARRIER!r} or {ENGINE_FALLBACK!r}, got {engine!r}"
            )
        item: dict[str, Any] = {"engine": engine}
        for key in ("model", "reason"):
            if key in entry:
                item[key] = _text(entry[key], f"{where}.{key}")
        if "chunks" in entry:
            item["chunks"] = _line_number(entry["chunks"], f"{where}.chunks")
        identity = (engine, item.get("model"), item.get("reason"))
        if identity in seen:
            raise ValueError(f"{where} repeats engine {engine!r} with the same model and reason")
        seen.add(identity)
        rendered.append(item)
    return rendered


def normalize_findings_state(
    raw: Mapping[str, Any] | None, *, head_sha: str, review_status: str
) -> dict[str, Any]:
    """Validate caller-supplied state and return the fence fields to render.

    Evidence fields (EVIDENCE_KEYS) are validated and returned beside the
    state fields; only a state field raises the fence schema version.

    Returns an empty dict for None or an empty mapping, the two spellings of
    "no state supplied", so a caller that supplies none produces exactly the
    fence it always did. Any other value that is not a mapping, falsy ones
    included (an empty string, 0, False, an empty list), is malformed. Raises
    ValueError on any malformed or contradictory input; nothing is repaired.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError(f"findings state must be a JSON object, got {type(raw).__name__}")
    if not raw:
        return {}
    allowed = (*STATE_KEYS, *EVIDENCE_KEYS)
    unknown = sorted(set(raw) - set(allowed))
    if unknown:
        raise ValueError(f"findings state has unknown field(s) {unknown}; allowed: {list(allowed)}")

    # A key that is present is validated, whatever its value: an explicit null
    # is malformed input, never a synonym for "not supplied".
    out: dict[str, Any] = {}
    open_ids: set[str] = set()
    if KEY_FINDINGS_OPEN in raw:
        rendered = []
        for index, entry in enumerate(_entries(raw[KEY_FINDINGS_OPEN], KEY_FINDINGS_OPEN)):
            where = f"{KEY_FINDINGS_OPEN}[{index}]"
            _exact_keys(entry, ("id", "rule_id", "head"), where)
            finding_id = _finding_id(entry.get("id"), f"{where}.id")
            if finding_id in open_ids:
                raise ValueError(f"{where}.id {finding_id!r} is listed twice")
            open_ids.add(finding_id)
            rendered.append(
                {
                    "id": finding_id,
                    "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
                    "head": _head(entry.get("head"), f"{where}.head"),
                }
            )
        if rendered and review_status == "clean":
            raise ValueError(
                f"{KEY_FINDINGS_OPEN} lists {len(rendered)} finding(s) on a clean verdict; "
                f"a verdict that holds findings open is blocking"
            )
        out[KEY_FINDINGS_OPEN] = rendered

    if KEY_SUPERSEDES in raw:
        value = raw[KEY_SUPERSEDES]
        if isinstance(value, str) and value.isdigit():
            value = int(value)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{KEY_SUPERSEDES} must be a positive comment id, got {raw[KEY_SUPERSEDES]!r}")
        out[KEY_SUPERSEDES] = value

    if KEY_CLEARED_CLAIMS in raw:
        rendered = []
        seen: set[str] = set()
        for index, entry in enumerate(_entries(raw[KEY_CLEARED_CLAIMS], KEY_CLEARED_CLAIMS)):
            where = f"{KEY_CLEARED_CLAIMS}[{index}]"
            _exact_keys(entry, ("id", "head", "evidence"), where)
            finding_id = _finding_id(entry.get("id"), f"{where}.id")
            claim_head = _head(entry.get("head"), f"{where}.head")
            if claim_head != head_sha:
                raise ValueError(
                    f"{where}.head {claim_head} is not the head this verdict is for "
                    f"({head_sha}); a finding is cleared at the head the reviewer examined"
                )
            if finding_id in seen:
                raise ValueError(f"{where}.id {finding_id!r} is cleared twice")
            if finding_id in open_ids:
                raise ValueError(f"{where}.id {finding_id!r} is both cleared and held open")
            seen.add(finding_id)
            rendered.append(
                {
                    "id": finding_id,
                    "head": claim_head,
                    "evidence": _text(entry.get("evidence"), f"{where}.evidence"),
                }
            )
        out[KEY_CLEARED_CLAIMS] = rendered

    if KEY_SCANNERS_RUN in raw:
        rendered = []
        for index, entry in enumerate(_entries(raw[KEY_SCANNERS_RUN], KEY_SCANNERS_RUN)):
            where = f"{KEY_SCANNERS_RUN}[{index}]"
            _exact_keys(entry, ("scanner", "status", "reason"), where)
            name = _text(entry.get("scanner"), f"{where}.scanner")
            status = entry.get("status")
            if status not in SCANNER_STATUSES:
                raise ValueError(f"{where}.status must be one of {list(SCANNER_STATUSES)}, got {status!r}")
            item: dict[str, Any] = {"scanner": name, "status": status}
            # A reason is optional only for a scanner that ran; every other
            # status has to say why.
            if status != SCANNER_RAN or entry.get("reason") is not None:
                item["reason"] = _text(entry.get("reason"), f"{where}.reason")
            rendered.append(item)
        index_scanners(rendered)
        out[KEY_SCANNERS_RUN] = rendered

    if out:
        out[KEY_FENCE_SCHEMA_VERSION] = FENCE_SCHEMA_VERSION

    if KEY_FAILURE_SEQUENCES in raw:
        out[KEY_FAILURE_SEQUENCES] = _normalize_failure_sequences(raw[KEY_FAILURE_SEQUENCES])
    if KEY_DROPPED in raw:
        out[KEY_DROPPED] = _normalize_dropped(raw[KEY_DROPPED])
    if KEY_RANGE in raw:
        out[KEY_RANGE] = _normalize_range(raw[KEY_RANGE])
    if KEY_ENGINES in raw:
        out[KEY_ENGINES] = _normalize_engines(raw[KEY_ENGINES])
    return out


def has_findings_state(data: Mapping[str, Any]) -> bool:
    """True when a fence payload carries findings state: any state key present
    (an explicit null counts, it is a malformed assertion, not silence) or the
    version 2 stamp. The one definition of "stateful" the reader uses."""
    return any(key in data for key in STATE_KEYS) or (
        data.get(KEY_FENCE_SCHEMA_VERSION) == FENCE_SCHEMA_VERSION
    )


_RAW_VERSION_2_RE = re.compile(rf"{KEY_FENCE_SCHEMA_VERSION}\W+{FENCE_SCHEMA_VERSION}\b")


def raw_mentions_findings_state(raw: str) -> bool:
    """has_findings_state for fence text that is not a readable JSON object:
    true when the raw text names a state key or the version 2 stamp. A plain
    substring scan on purpose, so it errs toward "stateful" and a damaged fence
    that may have held a finding open is never mistaken for silence."""
    return any(key in raw for key in STATE_KEYS) or _RAW_VERSION_2_RE.search(raw) is not None


def state_from_fence(data: Mapping[str, Any]) -> FindingsState:
    """Read the state fields out of an already schema-validated fence payload."""
    supersedes = data.get(KEY_SUPERSEDES)
    return FindingsState(
        fence_schema_version=int(data.get(KEY_FENCE_SCHEMA_VERSION, 1)),
        findings_open=tuple(data.get(KEY_FINDINGS_OPEN) or ()),
        supersedes=supersedes if isinstance(supersedes, int) else None,
        cleared_claims=tuple(data.get(KEY_CLEARED_CLAIMS) or ()),
        scanners_run=tuple(data.get(KEY_SCANNERS_RUN) or ()),
    )


def unresolved_prior_findings(
    earlier: Iterable[tuple[str, FindingsState]],
    current: FindingsState,
    current_head: str,
) -> list[dict[str, Any]]:
    """Findings held open by an earlier fence that nothing has resolved.

    *earlier* is the reviewer's earlier fences in chronological order, each as
    (head the fence was for, its state). A finding is open from the fence that
    lists it until a LATER-OR-SAME fence clears it at that fence's own head. It
    is resolved at the current fence when the current fence clears it at the
    current head, or re-raises it (the verdict status then decides, not this
    function). What is returned is exactly what the current fence leaves
    unaccounted for.
    """
    still_open: dict[str, dict[str, Any]] = {}
    for fence_head, state in earlier:
        for finding in state.findings_open:
            still_open[finding["id"]] = finding
        for claim in state.cleared_claims:
            if claim.get("head") == fence_head:
                still_open.pop(claim["id"], None)
    cleared_now = {c["id"] for c in current.cleared_claims if c.get("head") == current_head}
    raised_now = {f["id"] for f in current.findings_open}
    return [f for fid, f in still_open.items() if fid not in cleared_now and fid not in raised_now]


_SHORT_SHA = 12


def render_range(value: Mapping[str, Any]) -> str:
    """The verdict line for a normalized range: "range: base..head" with short
    SHAs for a full review, "range: since <sha>" for a delta review."""
    if value["basis"] == RANGE_BASIS_SINCE:
        return f"range: since {value['since'][:_SHORT_SHA]}"
    return f"range: {value['base'][:_SHORT_SHA]}..{value['head'][:_SHORT_SHA]}"


def render_engines(entries: Iterable[Mapping[str, Any]]) -> str:
    """The verdict line for normalized engine entries: "engine: carrier", or
    "engine: fallback: <model>, reason <reason>" for a fallback run (the model
    and the reason each only when known); several engines in one run are
    joined with "; "."""
    parts = []
    for entry in entries:
        if entry["engine"] == ENGINE_CARRIER:
            parts.append(ENGINE_CARRIER)
            continue
        label = f"{ENGINE_FALLBACK}: {entry['model']}" if entry.get("model") else ENGINE_FALLBACK
        parts.append(f"{label}, reason {entry['reason']}" if entry.get("reason") else label)
    return "engine: " + "; ".join(parts)


def failure_sequences_of(findings: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The fence's machine-readable copy of each finding's failure_sequence:
    one entry per finding that carries a non-empty one, in finding order. The
    one place that decides which findings contribute, so the prose under a
    bullet and the fence cannot disagree."""
    return [
        {
            "file": finding.get("file"),
            "line": finding.get("line"),
            "rule_id": finding.get("rule_id"),
            "failure_sequence": finding["failure_sequence"],
        }
        for finding in findings
        if isinstance(finding.get("failure_sequence"), str) and finding["failure_sequence"].strip()
    ]


__all__ = [
    "ENGINE_CARRIER",
    "ENGINE_FALLBACK",
    "EVIDENCE_KEYS",
    "KEY_DROPPED",
    "KEY_ENGINES",
    "KEY_FAILURE_SEQUENCES",
    "KEY_RANGE",
    "RANGE_BASIS_BASE_HEAD",
    "RANGE_BASIS_SINCE",
    "failure_sequences_of",
    "render_engines",
    "render_range",
    "FENCE_SCHEMA_VERSION",
    "KEY_CLEARED_CLAIMS",
    "KEY_FENCE_SCHEMA_VERSION",
    "KEY_FINDINGS_OPEN",
    "KEY_SCANNERS_RUN",
    "KEY_SUPERSEDES",
    "SCANNER_FAILED",
    "SCANNER_NOT_APPLICABLE",
    "SCANNER_NOT_INVOKED",
    "SCANNER_RAN",
    "SCANNER_STATUSES",
    "STATE_KEYS",
    "FindingsState",
    "has_findings_state",
    "index_scanners",
    "normalize_findings_state",
    "raw_mentions_findings_state",
    "state_from_fence",
    "unresolved_prior_findings",
]
