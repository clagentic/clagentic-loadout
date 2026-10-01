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
from typing import Any

SEVERITIES = ("blocking", "nit", "praise")

# Higher is more severe; used only to pick which duplicate survives a merge.
_SEVERITY_RANK = {"praise": 0, "nit": 1, "blocking": 2}

OUTPUT_CONTRACT = """\
## Output contract

Reply with exactly one JSON array and nothing else: no prose before or after,
no Markdown. Each element is an object with these keys:
  "file": path as it appears in the diff header (string)
  "line": line number inside a diff hunk (integer)
  "rule_id": a rule id from the rulebook above (string)
  "severity": "blocking", "nit", or "praise"
  "message": what is wrong and why, at most 200 characters (string)
Reply [] when the chunk has no findings.
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


def _validate_finding(item: Any, position: int) -> dict[str, Any]:
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
    if severity not in SEVERITIES:
        raise InvalidReplyError(f"finding {position} severity is not one of {SEVERITIES}")
    return {
        "file": file_value,
        "line": line,
        "rule_id": rule_id,
        "severity": severity,
        "message": message,
    }


def parse_chunk_reply(text: str) -> list[dict[str, Any]]:
    """Parse one chunk's reply into validated findings.

    Accepts a bare JSON array, optionally wrapped in one Markdown code fence
    (models add it unprompted). Anything else raises InvalidReplyError.
    """
    stripped = text.strip()
    fenced = _FENCE_RE.match(stripped)
    if fenced:
        stripped = fenced.group(1).strip()
    if not stripped.startswith("["):
        raise InvalidReplyError("reply is not a JSON array")
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise InvalidReplyError(f"reply is not valid JSON: {exc.msg}") from exc
    if not isinstance(data, list):
        raise InvalidReplyError("reply is not a JSON array")
    return [_validate_finding(item, i + 1) for i, item in enumerate(data)]


def merge_findings(
    per_chunk: list[tuple[int, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Concatenate per-chunk findings in chunk order, tagging each with its
    chunk index and dropping duplicates (a hunk split across chunks can
    surface the same finding twice). Duplicates may differ in severity; the
    most severe copy is kept, in the position of the first occurrence, so a
    dedupe never hides a blocking finding behind a nit.
    """
    merged: list[dict[str, Any]] = []
    position: dict[tuple[Any, ...], int] = {}
    for index, findings in sorted(per_chunk, key=lambda pair: pair[0]):
        for finding in findings:
            key = (finding["file"], finding["line"], finding["rule_id"], finding["message"])
            if key not in position:
                position[key] = len(merged)
                merged.append({**finding, "chunk": index})
                continue
            kept = merged[position[key]]
            if _SEVERITY_RANK[finding["severity"]] > _SEVERITY_RANK[kept["severity"]]:
                merged[position[key]] = {**finding, "chunk": index}
    return merged
