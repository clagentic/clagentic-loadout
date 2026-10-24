"""review.findings_contract — the machine contract between a review chunk's
model reply and the merged findings a reviewer judges.

The pipeline cannot judge prose. Each chunk's carrier is told (via
OUTPUT_CONTRACT, appended LAST to every chunk prompt) to answer with one JSON
array of finding objects and nothing else; ``parse_chunk_reply`` accepts
exactly that, so a reply that is chat, an apology, or a refusal is an invalid
reply — never an empty, clean review.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.merge.fence_state import KEY_SPEC_TAG, SPEC_TAGS
from clagentic_loadout.review import facts as vocab
from clagentic_loadout.review.facts import KEY_FACTS
from clagentic_loadout.review.finding_identity import KEY_FINGERPRINT, is_fingerprint

SEVERITIES = ("blocking", "nit", "praise")

#: Mirrors the "at most 200 characters" line in OUTPUT_CONTRACT.
MAX_MESSAGE_CHARS = 200

#: Mirrors the "at most 600 characters" line in OUTPUT_CONTRACT.
MAX_FAILURE_SEQUENCE_CHARS = 600

#: Optional finding key carrying a blocking finding's trigger steps and damage.
KEY_FAILURE_SEQUENCE = "failure_sequence"

# Higher is more severe; used only to pick which duplicate survives a merge.
_SEVERITY_RANK = {"praise": 0, "nit": 1, "blocking": 2}

def _choices(values: tuple[str, ...]) -> str:
    return " | ".join(values)


_IMPACT_KEYS = ", ".join('"' + dimension + '"' for dimension in vocab.IMPACT_DIMENSIONS)


# Built from the facts vocabulary itself, so the prompt can never offer a value
# the evaluator would not recognise.
OUTPUT_CONTRACT = f"""\
## Output contract

Reply with exactly one JSON array and nothing else: no prose before or after,
no Markdown. Each element is an object with these keys:
  "file": path as it appears in the diff header (string)
  "line": line number inside a diff hunk (integer, 1 or greater)
  "rule_id": a rule id from the rulebook above (string)
  "message": what is wrong and why, at most {MAX_MESSAGE_CHARS} characters (string)
  "facts": an object stating facts about the defect, described below
  "failure_sequence": OPTIONAL, for a defect you believe is blocking: the
    concrete steps that trigger the defect and the damage that results, at most
    {MAX_FAILURE_SEQUENCE_CHARS} characters (string)
Do not state a severity or a verdict. Severity and blocking are computed from
your facts, and a "severity" key in a reply is ignored. Report defects only.

The "facts" object has these keys, all required unless marked OPTIONAL:
  "category": {_choices(vocab.CATEGORIES)}
  "cwe": OPTIONAL, like "CWE-79"
  "reachable": {_choices(vocab.REACHABLE_VALUES)}
  "attacker_precondition": {_choices(vocab.PRECONDITIONS)}
  "impact": an object with the keys {_IMPACT_KEYS},
    each one of {_choices(vocab.IMPACT_LEVELS)}
  "durability": {_choices(vocab.DURABILITY_VALUES)}
  "evidence": OPTIONAL, at most {vocab.MAX_EVIDENCE_CHARS} characters (string)
State "unknown" when you cannot establish a fact. A fact that is missing or
outside its list is treated as the worst case, which can only make the finding
block.
Reply [] when the chunk has no findings.
"""

#: Precedes OUTPUT_CONTRACT (which stays last) in an incremental review, the one
#: place a reply can name prior findings as resolved.
RESOLVED_REPLY_ADDENDUM = """\
## Incremental reply form

The output contract below asks for a JSON array. When the incremental review
section lists open findings, you may instead reply with exactly one JSON object
and nothing else:
  {"findings": [ ...the elements the contract describes... ], "resolved": ["<id>", ...]}
"resolved" names, by the id shown for it, each listed finding that no longer
applies. A listed finding you neither report again nor name in "resolved" stays
open. A bare array is also accepted and resolves nothing.

"""

#: Appended to the original prompt when a reply was not a findings array.
FORMAT_REPROMPT = (
    "\n\nYour previous reply was not a JSON findings array. Reply again with "
    "exactly one JSON array and nothing else, following the output contract "
    "above. Reply [] when there are no findings.\n"
)

_FENCE_RE = re.compile(r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


class InvalidReplyError(ValueError):
    """A chunk reply is not a valid findings array."""


def validate_finding(
    item: Any,
    position: int,
    *,
    lenient_severity: bool = False,
    truncate_message: bool = True,
    carry_fingerprint: bool = False,
    carry_facts: bool = False,
    carry_spec_tag: bool = False,
    reply_contract: bool = False,
    problems: list[str] | None = None,
) -> dict[str, Any]:
    """Validate one finding object and return it in canonical shape.

    The default is the strict form a findings file must satisfy: a severity
    from SEVERITIES, exactly as written, and a message cut to
    MAX_MESSAGE_CHARS. ``lenient_severity`` is for findings a person has
    edited: the severity may be absent (returned as None), and a present one
    is matched ignoring case and surrounding whitespace, then returned
    normalized, so "Blocking" can never slip past a clean-vs-blocking check.
    ``truncate_message`` keeps an edited message whole (and an edited
    failure_sequence).

    ``reply_contract`` is the form a model reply must satisfy: the reviewer
    states facts and never a severity, so a ``severity`` key is accepted and
    discarded whatever it holds (the returned finding has no ``severity``), the
    ``facts`` object is carried exactly as written for the evaluator to judge,
    and an optional ``failure_sequence`` is kept for any finding.

    ``failure_sequence`` is optional: it is carried through when the severity
    is blocking and it is a non-empty string, and silently left out otherwise
    (OUTPUT_CONTRACT asks for it on blocking findings only), so a finding without it
    is exactly as valid as before.

    ``fingerprint`` (see review.finding_identity) is optional and is carried
    through only with ``carry_fingerprint``, for findings read back from a
    findings file; a model reply never sets it, so a fingerprint in a reply is
    dropped like any unknown key. A malformed one is dropped, not refused: the
    finding keeps its position-only identity.

    ``spec_tag`` is read only with ``carry_spec_tag``, for a review that runs
    against a sealed spec: every finding must then carry exactly one tag from
    SPEC_TAGS, and one that carries none, a value outside the vocabulary, or
    several is refused. Without the flag the key is dropped like any unknown
    key, so a review that is given no spec is unchanged.

    ``facts`` (see review.facts) is optional and additive: it is carried through
    only with ``carry_facts``, exactly as the reader found it, because judging it
    is the evaluator's job (a malformed value there resolves to the worst case
    and is reported, rather than refusing the whole file). Without the flag it is
    dropped like any unknown key, so every existing caller sees the shape it
    always did.

    Under ``reply_contract`` a ``failure_sequence`` the reply placed inside
    ``facts`` (where the contract does not put it) is moved to the top level when
    the finding has none there, under the same checks; when the finding has one,
    the top-level value stands and the nested one is dropped. Either way a line
    is appended to ``problems`` so the caller can say so. A nested value that
    fails the checks stays in ``facts``, which the evaluator then rejects like
    any other unknown key. A move is preferred to a re-prompt: the content is
    unambiguous and a re-prompt spends a whole chunk call on a slip that loses
    nothing.
    """
    if not isinstance(item, dict):
        raise InvalidReplyError(f"finding {position} is not an object")
    file_value = item.get("file")
    rule_id = item.get("rule_id")
    message = item.get("message")
    line = item.get("line")
    severity = item.get("severity")
    if not isinstance(file_value, str) or not file_value:
        raise InvalidReplyError(f"finding {position} has no file")
    if not isinstance(rule_id, str) or not rule_id:
        raise InvalidReplyError(f"finding {position} has no rule_id")
    if not isinstance(message, str) or not message:
        raise InvalidReplyError(f"finding {position} has no message")
    if isinstance(line, bool) or not isinstance(line, int):
        raise InvalidReplyError(f"finding {position} line is not an integer")
    if line < 1:
        raise InvalidReplyError(f"finding {position} line must be >= 1, got {line}")
    if lenient_severity and severity is not None:
        severity = severity.strip().lower() if isinstance(severity, str) else None
    if (
        not reply_contract
        and not (lenient_severity and item.get("severity") is None)
        and severity not in SEVERITIES
    ):
        raise InvalidReplyError(
            f"finding {position} severity must be one of {', '.join(SEVERITIES)}, "
            f"got {item.get('severity')!r}"
        )
    if truncate_message and len(message) > MAX_MESSAGE_CHARS:
        # A good finding with a long message is kept, not discarded; the
        # contract's bound is enforced by truncation.
        message = message[: MAX_MESSAGE_CHARS - 3] + "..."
    validated: dict[str, Any] = {
        "file": file_value,
        "line": line,
        "rule_id": rule_id,
        "message": message,
    }
    if not reply_contract:
        validated["severity"] = severity
    carry_facts = carry_facts or reply_contract
    sequence = _clean_sequence(item.get(KEY_FAILURE_SEQUENCE), truncate_message)
    if sequence is not None and (reply_contract or severity == "blocking"):
        validated[KEY_FAILURE_SEQUENCE] = sequence
    if carry_spec_tag:
        tag = item.get(KEY_SPEC_TAG)
        if not isinstance(tag, str) or tag not in SPEC_TAGS:
            raise InvalidReplyError(
                f"finding {position} must carry exactly one {KEY_SPEC_TAG} from "
                f"{', '.join(SPEC_TAGS)}, got {tag!r}"
            )
        validated[KEY_SPEC_TAG] = tag
    if carry_fingerprint and is_fingerprint(item.get(KEY_FINGERPRINT)):
        validated[KEY_FINGERPRINT] = item[KEY_FINGERPRINT]
    if carry_facts and item.get(KEY_FACTS) is not None:
        validated[KEY_FACTS] = item[KEY_FACTS]
        if reply_contract:
            _relocate_nested_sequence(validated, truncate_message, position, problems)
    return validated


def _clean_sequence(value: Any, truncate: bool) -> str | None:
    """A usable failure_sequence (non-blank string, bounded), or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if truncate and len(value) > MAX_FAILURE_SEQUENCE_CHARS:
        value = value[: MAX_FAILURE_SEQUENCE_CHARS - 3] + "..."
    return value


def _relocate_nested_sequence(
    validated: dict[str, Any], truncate: bool, position: int, problems: list[str] | None
) -> None:
    """Move a failure_sequence a reply wrote inside ``facts`` to the top level."""
    facts = validated[KEY_FACTS]
    if not isinstance(facts, dict) or KEY_FAILURE_SEQUENCE not in facts:
        return
    rest = {k: v for k, v in facts.items() if k != KEY_FAILURE_SEQUENCE}
    if KEY_FAILURE_SEQUENCE in validated:
        outcome = "kept the top-level value and dropped the one inside facts"
    else:
        nested = _clean_sequence(facts[KEY_FAILURE_SEQUENCE], truncate)
        if nested is None:
            return
        validated[KEY_FAILURE_SEQUENCE] = nested
        outcome = "moved the one inside facts to the top level"
    validated[KEY_FACTS] = rest
    if problems is not None:
        problems.append(f"finding {position} put failure_sequence inside facts: {outcome}")


@dataclass(frozen=True)
class ChunkReply:
    """A parsed chunk reply: its findings, and the ids of listed prior findings
    the reviewer explicitly named as resolved (empty when it named none, or
    when what it wrote was not a list of id strings)."""

    findings: list[dict[str, Any]]
    resolved: list[str]
    #: How many findings carried a ``severity`` key. The value is discarded; the
    #: count lets the caller say so.
    ignored_severity: int = 0
    #: One line per reply slip repaired without a re-prompt (see validate_finding).
    problems: tuple[str, ...] = ()


def _resolved_ids(value: Any) -> list[str]:
    """The reply's ``resolved`` value as ids. Anything but a list of non-empty
    strings resolves nothing: a malformed list must never close a finding."""
    if not isinstance(value, list) or not all(isinstance(i, str) and i.strip() for i in value):
        return []
    return [i.strip() for i in value]


def parse_chunk_output(text: str) -> ChunkReply:
    """Parse one chunk's reply into validated findings and resolved ids.

    Accepts a bare JSON array (findings only), or a JSON object
    ``{"findings": [...], "resolved": [...]}``, either optionally wrapped in one
    Markdown code fence (models add it unprompted). Anything else raises
    InvalidReplyError.

    A finding states facts, not a severity (see validate_finding's
    ``reply_contract``). A reply written for an older rulebook that still
    carries a ``severity`` is accepted; the value is discarded and counted in
    ``ignored_severity``. A finding without facts, or with facts outside the
    vocabulary, is accepted too and resolved to the worst case by the
    evaluator: nothing here relaxes that.
    """
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if not stripped.startswith(("[", "{")):
        raise InvalidReplyError("reply is not a JSON array")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidReplyError(f"reply is not valid JSON: {exc.msg}") from exc
    resolved: list[str] = []
    if isinstance(data, dict):
        resolved = _resolved_ids(data.get("resolved"))
        data = data.get("findings")
    if not isinstance(data, list):
        raise InvalidReplyError("reply is not a JSON array")
    problems: list[str] = []
    findings = [
        validate_finding(item, i + 1, reply_contract=True, problems=problems)
        for i, item in enumerate(data)
    ]
    ignored = sum(1 for item in data if isinstance(item, dict) and "severity" in item)
    return ChunkReply(findings, resolved, ignored, tuple(problems))


def parse_chunk_reply(text: str) -> list[dict[str, Any]]:
    """The findings of one chunk's reply (see parse_chunk_output)."""
    return parse_chunk_output(text).findings


def merge_findings(
    per_chunk: list[tuple[int, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Concatenate per-chunk findings in chunk order, tagging each with its
    chunk index and dropping duplicates (a hunk split across chunks can
    surface the same finding twice).

    A duplicate that states the same facts adds nothing and is dropped; when
    both copies carry a severity the more severe one is kept, in the position of
    the first occurrence. A duplicate that states DIFFERENT facts is kept as a
    further copy: which of the two is stronger is decided by the evaluator, from
    the computed severity, so a milder copy can never hide a blocking one here.
    """
    merged: list[dict[str, Any]] = []
    copies: dict[tuple[Any, ...], list[int]] = {}
    for index, findings in sorted(per_chunk, key=lambda pair: pair[0]):
        for finding in findings:
            key = (finding["file"], finding["line"], finding["rule_id"], finding["message"])
            positions = copies.setdefault(key, [])
            same_facts = next(
                (p for p in positions if merged[p].get(KEY_FACTS) == finding.get(KEY_FACTS)), None
            )
            if same_facts is None:
                positions.append(len(merged))
                merged.append({**finding, "chunk": index})
                continue
            kept = merged[same_facts]
            if _SEVERITY_RANK.get(finding.get("severity"), 0) > _SEVERITY_RANK.get(
                kept.get("severity"), 0
            ):
                merged[same_facts] = {**finding, "chunk": index}
    return merged
