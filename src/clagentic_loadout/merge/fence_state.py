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
  resolved          [{file, line, rule_id, message, by, reason, fingerprint}]
                                         prior findings a delta review closed:
                                         by "reviewer" (re-judged against the
                                         new head) or "caller" (ruled resolved
                                         by the caller, with the reason);
                                         file/line is the prior location
  range             {basis, base|since, head}
                                         the commit range the review covered
  engines           [{engine, model, reason, chunks}]
                                         which engine answered the run's chunks

EVALUATION fields record the verdict the evaluator computed, and are what a
fence_schema_version 3 fence adds:

  verdict_outcome   CLEAN | BLOCKED | DEGRADED | STALE
  verdict_reasons   [str]                one line per reason the outcome is not
                                         CLEAN (empty for CLEAN)
  evaluator_version str                  the rules the verdict was computed under
  policy_base_sha   <sha>                the base revision policy was read from;
                                         omitted when it was not known
  evaluated_findings [{file, line, rule_id, message, facts?, fingerprint?}]
                                         the findings the outcome was computed
                                         from, so the merge gate can compute it
                                         again under the policy it reads itself;
                                         the fingerprint is present only when a
                                         ruling registry is configured

They are present together or not at all (policy_base_sha excepted). A fence at
version 1 or 2 carries none of them and reads as NOT EVALUATED: its
review_status was asserted by the poster, not computed.

SPEC fields record a review that ran against a sealed spec, and are what a
fence_schema_version 4 fence adds. They are evidence, like the fields above them:
they hold no finding open, clear none, and the merge gate does not read them.

  spec_hash       <sha256>               the sealed spec the review ran against
  spec_tags       [{file, line, rule_id, message, spec_tag}]
                                         exactly one tag for each finding, from
                                         in-spec, out-of-spec or fix-caused
  spec_coverage   [{criterion, status}]  each acceptance criterion of that spec,
                                         once, as met or unmet

They are present together or not at all. A version 4 fence is a superset of
version 3: it says nothing about evaluation by itself, so a fence that carries
both reads as evaluated exactly as a version 3 fence does.

DOCS-ONLY field records a review that took the lighter docs-only path, or that
rejected the justification for taking it, and is what a fence_schema_version 5
fence adds. It is evidence, like the fields above it: it holds no finding open,
clears none, and the merge gate does not read it to decide anything; the merge
verb only prints it as a waiver line.

  docs_only       {classification, allowlist_source, justification, spec_hash,
                   accepted, rejection_reason}
                                         what the changeset classified as, the
                                         base revision and profile path the docs
                                         allowlist was read from, the requesting
                                         agent's justification verbatim, the
                                         sealed spec it came from, whether the
                                         reviewer accepted it, and why not

A version 5 fence is a superset of version 4, for the same reason.

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

#: Version stamped on a fence that carries the evaluation fields. It is a
#: superset of version 2: it says nothing about findings state by itself.
EVALUATED_FENCE_SCHEMA_VERSION = 3

#: Version stamped on a fence that carries the spec fields. A superset of
#: version 3, for the same reason version 3 is a superset of version 2.
SPEC_FENCE_SCHEMA_VERSION = 4

#: Version stamped on a fence that carries the docs-only field. A superset of
#: version 4.
DOCS_ONLY_FENCE_SCHEMA_VERSION = 5

#: Every fence_schema_version a reader accepts.
FENCE_SCHEMA_VERSIONS = (1, 2, 3, 4, 5)

KEY_FENCE_SCHEMA_VERSION = "fence_schema_version"
KEY_FINDINGS_OPEN = "findings_open"
KEY_SUPERSEDES = "supersedes"
KEY_CLEARED_CLAIMS = "cleared_claims"
KEY_SCANNERS_RUN = "scanners_run"

#: The caller-suppliable state fields, in the order they render.
STATE_KEYS = (KEY_FINDINGS_OPEN, KEY_SUPERSEDES, KEY_CLEARED_CLAIMS, KEY_SCANNERS_RUN)

KEY_FAILURE_SEQUENCES = "failure_sequences"
KEY_DROPPED = "dropped"
KEY_RESOLVED = "resolved"
KEY_RANGE = "range"
KEY_ENGINES = "engines"

#: The caller-suppliable evidence fields, in the order they render. Kept apart
#: from STATE_KEYS on purpose: the gate's "does this fence assert state" logic
#: reads STATE_KEYS only.
KEY_VERDICT_OUTCOME = "verdict_outcome"
KEY_VERDICT_REASONS = "verdict_reasons"
KEY_EVALUATOR_VERSION = "evaluator_version"
KEY_POLICY_BASE_SHA = "policy_base_sha"
KEY_EVALUATED_FINDINGS = "evaluated_findings"

#: The tool-supplied evaluation fields. Part of EVIDENCE_KEYS because they ride
#: in the same tool-built fence, are derived rather than caller-typed, and hold
#: no finding open; unlike the other evidence they raise the schema version.
EVALUATION_KEYS = (
    KEY_VERDICT_OUTCOME,
    KEY_VERDICT_REASONS,
    KEY_EVALUATOR_VERSION,
    KEY_POLICY_BASE_SHA,
    KEY_EVALUATED_FINDINGS,
)

VERDICT_OUTCOMES = ("CLEAN", "BLOCKED", "DEGRADED", "STALE")

KEY_SPEC_HASH = "spec_hash"
KEY_SPEC_TAGS = "spec_tags"
KEY_SPEC_COVERAGE = "spec_coverage"

#: The tool-supplied spec fields; all or nothing, and they raise the schema
#: version to SPEC_FENCE_SCHEMA_VERSION.
SPEC_KEYS = (KEY_SPEC_HASH, KEY_SPEC_TAGS, KEY_SPEC_COVERAGE)

#: A finding's key for its tag in a findings file (not a fence key).
KEY_SPEC_TAG = "spec_tag"
SPEC_TAG_IN_SPEC = "in-spec"
SPEC_TAG_OUT_OF_SPEC = "out-of-spec"
SPEC_TAG_FIX_CAUSED = "fix-caused"
#: The vocabulary a review tags every finding with when it runs against a spec.
SPEC_TAGS = (SPEC_TAG_IN_SPEC, SPEC_TAG_OUT_OF_SPEC, SPEC_TAG_FIX_CAUSED)

COVERAGE_MET = "met"
COVERAGE_UNMET = "unmet"
COVERAGE_STATUSES = (COVERAGE_MET, COVERAGE_UNMET)

KEY_DOCS_ONLY = "docs_only"

#: The classifications a docs-only object may state.
CLASSIFICATION_DOCS_ONLY = "docs-only"
CLASSIFICATION_CODE = "code"
CLASSIFICATIONS = (CLASSIFICATION_DOCS_ONLY, CLASSIFICATION_CODE)

EVIDENCE_KEYS = (
    KEY_FAILURE_SEQUENCES,
    KEY_DROPPED,
    KEY_RESOLVED,
    KEY_RANGE,
    KEY_ENGINES,
    *EVALUATION_KEYS,
    *SPEC_KEYS,
    KEY_DOCS_ONLY,
)

RANGE_BASIS_BASE_HEAD = "base..head"
RANGE_BASIS_SINCE = "since"
ENGINE_CARRIER = "carrier"
ENGINE_FALLBACK = "fallback"
RESOLVED_BY_REVIEWER = "reviewer"
RESOLVED_BY_CALLER = "caller"
RESOLVED_BY = (RESOLVED_BY_REVIEWER, RESOLVED_BY_CALLER)

SCANNER_RAN = "ran"
SCANNER_NOT_APPLICABLE = "not_applicable"
SCANNER_NOT_INVOKED = "not_invoked"
SCANNER_FAILED = "failed"
SCANNER_STATUSES = (SCANNER_RAN, SCANNER_NOT_APPLICABLE, SCANNER_NOT_INVOKED, SCANNER_FAILED)

_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")

#: Restated from the spec contract (spec.model) because the merge layer reads a
#: fence without importing the spec layer.
_CRITERION_ID_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SPEC_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class FindingsState:
    """The state fields parsed from one fence. Empty for a version-1 fence."""

    fence_schema_version: int = 1
    findings_open: tuple[dict[str, Any], ...] = ()
    supersedes: int | None = None
    cleared_claims: tuple[dict[str, Any], ...] = ()
    scanners_run: tuple[dict[str, Any], ...] = ()
    #: The computed verdict the fence records; None for a fence that was never
    #: evaluated (version 1 or 2, or any fence without verdict_outcome).
    verdict_outcome: str | None = None
    verdict_reasons: tuple[str, ...] = ()
    evaluator_version: str | None = None
    policy_base_sha: str | None = None
    #: The findings the outcome was computed from; None when the fence does not
    #: carry them, which is different from a fence that carries none.
    evaluated_findings: tuple[dict[str, Any], ...] | None = None
    #: The docs-only object the fence carries; None when it carries none.
    docs_only: dict[str, Any] | None = None

    @property
    def evaluated(self) -> bool:
        """True only for a version 3 fence that states a recognised outcome. A
        fence without these fields reads as not evaluated, never as clean."""
        return (
            self.fence_schema_version >= EVALUATED_FENCE_SCHEMA_VERSION
            and self.verdict_outcome in VERDICT_OUTCOMES
        )


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


def _normalize_resolved(value: Any) -> list[dict[str, Any]]:
    rendered = []
    for index, entry in enumerate(_entries(value, KEY_RESOLVED)):
        where = f"{KEY_RESOLVED}[{index}]"
        _exact_keys(
            entry, ("file", "line", "rule_id", "message", "by", "reason", "fingerprint"), where
        )
        by = entry.get("by")
        if by not in RESOLVED_BY:
            raise ValueError(f"{where}.by must be one of {list(RESOLVED_BY)}, got {by!r}")
        item: dict[str, Any] = {
            "file": _text(entry.get("file"), f"{where}.file"),
            "line": _line_number(entry.get("line"), f"{where}.line"),
            "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
            "message": _text(entry.get("message"), f"{where}.message", single_line=False),
            "by": by,
        }
        if "reason" in entry:
            item["reason"] = _text(entry["reason"], f"{where}.reason")
        elif by == RESOLVED_BY_CALLER:
            raise ValueError(f"{where}.reason is required when by is {RESOLVED_BY_CALLER!r}")
        if "fingerprint" in entry:
            item["fingerprint"] = _text(entry["fingerprint"], f"{where}.fingerprint")
        rendered.append(item)
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
    if KEY_RESOLVED in raw:
        out[KEY_RESOLVED] = _normalize_resolved(raw[KEY_RESOLVED])
    if KEY_RANGE in raw:
        out[KEY_RANGE] = _normalize_range(raw[KEY_RANGE])
    if KEY_ENGINES in raw:
        out[KEY_ENGINES] = _normalize_engines(raw[KEY_ENGINES])
    if any(key in raw for key in EVALUATION_KEYS):
        out.update(_normalize_evaluation(raw))
        out[KEY_FENCE_SCHEMA_VERSION] = EVALUATED_FENCE_SCHEMA_VERSION
    if any(key in raw for key in SPEC_KEYS):
        out.update(_normalize_spec(raw))
        out[KEY_FENCE_SCHEMA_VERSION] = SPEC_FENCE_SCHEMA_VERSION
    if KEY_DOCS_ONLY in raw:
        out[KEY_DOCS_ONLY] = normalize_docs_only(raw[KEY_DOCS_ONLY])
        out[KEY_FENCE_SCHEMA_VERSION] = DOCS_ONLY_FENCE_SCHEMA_VERSION
    return out


_DOCS_ONLY_KEYS = (
    "classification",
    "allowlist_source",
    "justification",
    "spec_hash",
    "accepted",
    "rejection_reason",
)


def normalize_docs_only(value: Any) -> dict[str, Any]:
    """Validate a docs-only object and return it as the fence renders it.

    The justification is the requesting agent's own text and may run to several
    lines; every string still goes through the fence-syntax check, like all
    caller text. A rejected justification states why; an accepted one states no
    reason. Raises ValueError on anything else."""
    where = KEY_DOCS_ONLY
    if not isinstance(value, Mapping):
        raise ValueError(f"{where} must be a JSON object, got {type(value).__name__}")
    _exact_keys(value, _DOCS_ONLY_KEYS, where)
    missing = [key for key in _DOCS_ONLY_KEYS if key not in value]
    if missing:
        raise ValueError(f"{where} is missing {missing}")
    classification = value["classification"]
    if classification not in CLASSIFICATIONS:
        raise ValueError(
            f"{where}.classification must be one of {list(CLASSIFICATIONS)}, got {classification!r}"
        )
    source = value["allowlist_source"]
    if not isinstance(source, Mapping):
        raise ValueError(f"{where}.allowlist_source must be a JSON object")
    _exact_keys(source, ("base_sha", "path"), f"{where}.allowlist_source")
    spec_hash = value["spec_hash"]
    if not (isinstance(spec_hash, str) and _SPEC_HASH_RE.match(spec_hash)):
        raise ValueError(f"{where}.spec_hash must be 64 lowercase hex characters, got {spec_hash!r}")
    accepted = value["accepted"]
    if not isinstance(accepted, bool):
        raise ValueError(f"{where}.accepted must be true or false, got {accepted!r}")
    if accepted and classification != CLASSIFICATION_DOCS_ONLY:
        raise ValueError(
            f"{where}.accepted may be true only when the classification is "
            f"{CLASSIFICATION_DOCS_ONLY!r}, got {classification!r}"
        )
    reason = value["rejection_reason"]
    if accepted and reason is not None:
        raise ValueError(f"{where}.rejection_reason must be null when the justification is accepted")
    if not accepted and reason is None:
        raise ValueError(f"{where}.rejection_reason is required when the justification is rejected")
    return {
        "classification": classification,
        "allowlist_source": {
            "base_sha": _head(source.get("base_sha"), f"{where}.allowlist_source.base_sha"),
            "path": _text(source.get("path"), f"{where}.allowlist_source.path"),
        },
        "justification": _text(
            value["justification"], f"{where}.justification", single_line=False
        ),
        "spec_hash": spec_hash,
        "accepted": accepted,
        "rejection_reason": None if reason is None else _text(reason, f"{where}.rejection_reason"),
    }


_MAX_WAIVER_JUSTIFICATION_CHARS = 300


def docs_only_waiver_line(reviewer: str, docs_only: Mapping[str, Any]) -> str | None:
    """The one-line audit statement for a docs-only waiver a reviewer accepted,
    or None when the object records no accepted waiver. The justification is
    shown on one line, cut to a bounded length; the fence holds it verbatim."""
    if docs_only.get("accepted") is not True:
        return None
    # A fence read from a comment is not re-normalized, so the invariant that
    # normalize_docs_only enforces is checked again here: anything short of it
    # earns no waiver line (the review stands as a full one).
    if docs_only.get("classification") != CLASSIFICATION_DOCS_ONLY:
        return None
    for key in ("justification", "spec_hash"):
        field = docs_only.get(key)
        if not isinstance(field, str) or not field.strip():
            return None
    source = docs_only["allowlist_source"]
    justification = " ".join(str(docs_only["justification"]).split())
    if len(justification) > _MAX_WAIVER_JUSTIFICATION_CHARS:
        justification = justification[: _MAX_WAIVER_JUSTIFICATION_CHARS - 3] + "..."
    return (
        f"{reviewer}: docs-only review waiver accepted: classified {docs_only['classification']} "
        f"against the allowlist in {source['path']} at base {source['base_sha'][:_SHORT_SHA]}; "
        f"justification (spec {docs_only['spec_hash'][:_SHORT_SHA]}): {justification}"
    )


def render_docs_only(docs_only: Mapping[str, Any]) -> str:
    """The verdict line for a normalized docs-only object."""
    if docs_only["accepted"]:
        return "docs-only: lighter review, justification accepted; findings are advisory"
    return f"docs-only: justification rejected, full review: {docs_only['rejection_reason']}"


def _normalize_spec(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the spec fields. They are all or nothing: a hash without the tags
    and the coverage, or the reverse, would be a review nobody can tell the
    specification of."""
    missing = [key for key in SPEC_KEYS if key not in raw]
    if missing:
        raise ValueError(f"spec fields must be given together, missing {missing}")
    spec_hash = raw[KEY_SPEC_HASH]
    if not (isinstance(spec_hash, str) and _SPEC_HASH_RE.match(spec_hash)):
        raise ValueError(
            f"{KEY_SPEC_HASH} must be 64 lowercase hex characters (a sha256), got {spec_hash!r}"
        )
    tags = []
    for index, entry in enumerate(_entries(raw[KEY_SPEC_TAGS], KEY_SPEC_TAGS)):
        where = f"{KEY_SPEC_TAGS}[{index}]"
        _exact_keys(entry, ("file", "line", "rule_id", "message", KEY_SPEC_TAG), where)
        tag = entry.get(KEY_SPEC_TAG)
        if tag not in SPEC_TAGS:
            raise ValueError(f"{where}.{KEY_SPEC_TAG} must be exactly one of {list(SPEC_TAGS)}, got {tag!r}")
        tags.append(
            {
                "file": _text(entry.get("file"), f"{where}.file"),
                "line": _line_number(entry.get("line"), f"{where}.line"),
                "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
                "message": _text(entry.get("message"), f"{where}.message", single_line=False),
                KEY_SPEC_TAG: tag,
            }
        )
    coverage = []
    seen: set[str] = set()
    for index, entry in enumerate(_entries(raw[KEY_SPEC_COVERAGE], KEY_SPEC_COVERAGE)):
        where = f"{KEY_SPEC_COVERAGE}[{index}]"
        _exact_keys(entry, ("criterion", "status"), where)
        criterion = entry.get("criterion")
        if not (isinstance(criterion, str) and _CRITERION_ID_RE.match(criterion)):
            raise ValueError(f"{where}.criterion must be an acceptance criterion id, got {criterion!r}")
        if criterion in seen:
            raise ValueError(f"{where}.criterion {criterion!r} is listed twice")
        seen.add(criterion)
        status = entry.get("status")
        if status not in COVERAGE_STATUSES:
            raise ValueError(f"{where}.status must be one of {list(COVERAGE_STATUSES)}, got {status!r}")
        coverage.append({"criterion": criterion, "status": status})
    if not coverage:
        raise ValueError(f"{KEY_SPEC_COVERAGE} must list at least one acceptance criterion")
    return {KEY_SPEC_HASH: spec_hash, KEY_SPEC_TAGS: tags, KEY_SPEC_COVERAGE: coverage}


def _normalize_evaluation(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Validate the evaluation fields. They are all or nothing (the base
    revision aside): an outcome without its reasons or the evaluator version
    would be a verdict nobody can tell the provenance of."""
    missing = [
        key
        for key in (KEY_VERDICT_OUTCOME, KEY_VERDICT_REASONS, KEY_EVALUATOR_VERSION)
        if key not in raw
    ]
    if missing:
        raise ValueError(f"evaluation fields must be given together, missing {missing}")
    outcome = raw[KEY_VERDICT_OUTCOME]
    if outcome not in VERDICT_OUTCOMES:
        raise ValueError(f"{KEY_VERDICT_OUTCOME} must be one of {list(VERDICT_OUTCOMES)}, got {outcome!r}")
    reasons = raw[KEY_VERDICT_REASONS]
    if not isinstance(reasons, list):
        raise ValueError(f"{KEY_VERDICT_REASONS} must be a JSON array, got {type(reasons).__name__}")
    rendered: dict[str, Any] = {
        KEY_VERDICT_OUTCOME: outcome,
        KEY_VERDICT_REASONS: [
            _text(reason, f"{KEY_VERDICT_REASONS}[{index}]") for index, reason in enumerate(reasons)
        ],
        KEY_EVALUATOR_VERSION: _text(raw[KEY_EVALUATOR_VERSION], KEY_EVALUATOR_VERSION),
    }
    if raw.get(KEY_POLICY_BASE_SHA) is not None:
        rendered[KEY_POLICY_BASE_SHA] = _head(raw[KEY_POLICY_BASE_SHA], KEY_POLICY_BASE_SHA)
    if KEY_EVALUATED_FINDINGS in raw:
        rendered[KEY_EVALUATED_FINDINGS] = _normalize_evaluated_findings(raw[KEY_EVALUATED_FINDINGS])
    return rendered


#: The only severity a carried finding may state: a finding the evaluator does
#: not score. Anything else a reviewer wrote is not an input to the verdict.
_PRAISE = "praise"

#: The shape of the finding link hint (review.finding_identity), restated here
#: because the merge layer reads a fence without importing the review layer.
_FINGERPRINT_RE = re.compile(r"\Afp2:[0-9a-f]{16}\Z")


def _plain_json(value: Any, where: str) -> Any:
    """*value* if it is plain JSON data none of whose strings is fence-shaped.
    Facts are reviewer-typed and nested, so each string in them is checked the
    way every other caller text is."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        offending = find_fence_syntax(value)
        if offending is not None:
            raise ValueError(
                f"{where} contains the fence-delimiter sequence {offending!r}; a "
                f"tool-constructed fence never carries fence-shaped caller text"
            )
        return value
    if isinstance(value, list):
        return [_plain_json(item, f"{where}[{i}]") for i, item in enumerate(value)]
    if isinstance(value, Mapping):
        return {
            _text(key, f"{where} key"): _plain_json(item, f"{where}.{key}")
            for key, item in value.items()
        }
    raise ValueError(f"{where} must be plain JSON data, got {type(value).__name__}")


def _normalize_evaluated_findings(value: Any) -> list[dict[str, Any]]:
    rendered = []
    for index, entry in enumerate(_entries(value, KEY_EVALUATED_FINDINGS)):
        where = f"{KEY_EVALUATED_FINDINGS}[{index}]"
        _exact_keys(
            entry, ("file", "line", "rule_id", "message", "facts", "severity", "fingerprint"), where
        )
        item: dict[str, Any] = {
            "file": _text(entry.get("file"), f"{where}.file"),
            "line": _line_number(entry.get("line"), f"{where}.line"),
            "rule_id": _text(entry.get("rule_id"), f"{where}.rule_id"),
            "message": _text(entry.get("message"), f"{where}.message", single_line=False),
        }
        if entry.get("fingerprint") is not None:
            fingerprint = entry["fingerprint"]
            if not (isinstance(fingerprint, str) and _FINGERPRINT_RE.match(fingerprint)):
                raise ValueError(
                    f"{where}.fingerprint must be a fingerprint like fp2:0123456789abcdef, "
                    f"got {fingerprint!r}"
                )
            item["fingerprint"] = fingerprint
        if entry.get("facts") is not None:
            facts = entry["facts"]
            if not isinstance(facts, Mapping):
                raise ValueError(f"{where}.facts must be a JSON object, got {type(facts).__name__}")
            item["facts"] = _plain_json(facts, f"{where}.facts")
        if entry.get("severity") is not None:
            if entry["severity"] != _PRAISE:
                raise ValueError(
                    f"{where}.severity may only be {_PRAISE!r}, got {entry['severity']!r}"
                )
            item["severity"] = _PRAISE
        rendered.append(item)
    return rendered


def has_findings_state(data: Mapping[str, Any]) -> bool:
    """True when a fence payload carries findings state: any state key present
    (an explicit null counts, it is a malformed assertion, not silence) or the
    version 2 stamp. The one definition of "stateful" the reader uses."""
    return any(key in data for key in STATE_KEYS) or (
        data.get(KEY_FENCE_SCHEMA_VERSION) == FENCE_SCHEMA_VERSION
    )


_RAW_VERSION_2_RE = re.compile(
    rf"{KEY_FENCE_SCHEMA_VERSION}\W+"
    rf"(?:{FENCE_SCHEMA_VERSION}|{EVALUATED_FENCE_SCHEMA_VERSION}|{SPEC_FENCE_SCHEMA_VERSION}"
    rf"|{DOCS_ONLY_FENCE_SCHEMA_VERSION})\b"
)


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
        verdict_outcome=data.get(KEY_VERDICT_OUTCOME),
        verdict_reasons=tuple(data.get(KEY_VERDICT_REASONS) or ()),
        evaluator_version=data.get(KEY_EVALUATOR_VERSION),
        policy_base_sha=data.get(KEY_POLICY_BASE_SHA),
        evaluated_findings=(
            tuple(data[KEY_EVALUATED_FINDINGS])
            if isinstance(data.get(KEY_EVALUATED_FINDINGS), list)
            else None
        ),
        docs_only=data[KEY_DOCS_ONLY] if isinstance(data.get(KEY_DOCS_ONLY), dict) else None,
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
    "engine: carrier: <model>" when the carrier's model is known, or
    "engine: fallback: <model>, reason <reason>" for a fallback run (the model
    and the reason each only when known); several engines in one run are
    joined with "; "."""
    parts = []
    for entry in entries:
        if entry["engine"] == ENGINE_CARRIER:
            parts.append(f"{ENGINE_CARRIER}: {entry['model']}" if entry.get("model") else ENGINE_CARRIER)
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


def with_derived_failure_sequences(
    state: Mapping[str, Any] | None, findings: Iterable[Mapping[str, Any]]
) -> dict[str, Any]:
    """*state* as a new dict whose failure_sequences are those derived from
    *findings*. A caller-staged copy is replaced (or removed when no finding
    states a sequence), so the fence can never contradict the bullets."""
    merged = {k: v for k, v in (state or {}).items() if k != KEY_FAILURE_SEQUENCES}
    sequences = failure_sequences_of(findings)
    if sequences:
        merged[KEY_FAILURE_SEQUENCES] = sequences
    return merged


__all__ = [
    "CLASSIFICATIONS",
    "CLASSIFICATION_CODE",
    "CLASSIFICATION_DOCS_ONLY",
    "DOCS_ONLY_FENCE_SCHEMA_VERSION",
    "FENCE_SCHEMA_VERSIONS",
    "KEY_DOCS_ONLY",
    "docs_only_waiver_line",
    "normalize_docs_only",
    "render_docs_only",
    "with_derived_failure_sequences",
    "COVERAGE_MET",
    "COVERAGE_STATUSES",
    "COVERAGE_UNMET",
    "KEY_SPEC_COVERAGE",
    "KEY_SPEC_HASH",
    "KEY_SPEC_TAG",
    "KEY_SPEC_TAGS",
    "SPEC_FENCE_SCHEMA_VERSION",
    "SPEC_KEYS",
    "SPEC_TAGS",
    "SPEC_TAG_FIX_CAUSED",
    "SPEC_TAG_IN_SPEC",
    "SPEC_TAG_OUT_OF_SPEC",
    "ENGINE_CARRIER",
    "ENGINE_FALLBACK",
    "EVALUATED_FENCE_SCHEMA_VERSION",
    "EVALUATION_KEYS",
    "EVIDENCE_KEYS",
    "KEY_EVALUATED_FINDINGS",
    "KEY_EVALUATOR_VERSION",
    "KEY_POLICY_BASE_SHA",
    "KEY_VERDICT_OUTCOME",
    "KEY_VERDICT_REASONS",
    "VERDICT_OUTCOMES",
    "KEY_DROPPED",
    "KEY_ENGINES",
    "KEY_FAILURE_SEQUENCES",
    "KEY_RESOLVED",
    "KEY_RANGE",
    "RESOLVED_BY_CALLER",
    "RESOLVED_BY_REVIEWER",
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
