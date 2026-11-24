"""review.drop_check — was each dropped candidate read at the reviewed head?

Two report-only uses of one question, never a refusal:

``check_drops`` reads a stage manifest (review.stage_manifest) and a reviewer's
dropped file, and says for each drop whether it is proven: it names the head the
manifest pinned as its ``read_at`` and the file is one the stage holds a copy of
or one the pull request deleted. An unproven drop carries the one reason it is
unproven. ``gaps`` lists the files the stage could not give the reviewer.

``annotate_dropped`` is what ``loadout-review post`` does with the same input
when there is no manifest: it records ``read_at`` and ``proven`` on each entry
(proven only when ``read_at`` is the reviewed head) so the verdict fence carries
them, and returns the entries it marked unproven for the caller to name.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from clagentic_loadout.review.stage_manifest import Manifest

REASON_NO_READ_AT = "no read_at"
REASON_NO_COPY = "no head copy"
REASON_OTHER_REVISION = "read at another revision"
REASON_NOT_STAGED = "no head copy: the stage could not provide it"


class DropInputError(Exception):
    """A dropped or findings file cannot be read as the shape this check takes."""


def _load(path: str, what: str) -> Any:
    resolved = Path(path).expanduser().resolve()
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError) as exc:
        raise DropInputError(f"cannot read the {what} {str(resolved)!r}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise DropInputError(f"the {what} {str(resolved)!r} is not valid JSON: {exc}") from exc


def _entries(data: Any, key: str, path: str, what: str) -> list[dict[str, Any]]:
    resolved = str(Path(path).expanduser().resolve())
    if isinstance(data, dict):
        data = data.get(key)
    if not isinstance(data, list):
        raise DropInputError(
            f"the {what} {resolved!r} must be a JSON array (or an object with a {key!r} array)"
        )
    for position, entry in enumerate(data, start=1):
        if not isinstance(entry, dict) or not isinstance(entry.get("file"), str) or not entry["file"]:
            raise DropInputError(
                f"{what} {resolved!r}: entry {position} must be an object with a string 'file'"
            )
    return data


def load_dropped(path: str) -> list[dict[str, Any]]:
    entries = _entries(_load(path, "dropped file"), "dropped", path, "dropped file")
    resolved = str(Path(path).expanduser().resolve())
    for position, entry in enumerate(entries, start=1):
        line, rule, read_at = entry.get("line"), entry.get("rule_id"), entry.get("read_at")
        if isinstance(line, bool) or not isinstance(line, int):
            raise DropInputError(f"dropped file {resolved!r}: entry {position} needs an integer 'line'")
        if not isinstance(rule, str) or not rule:
            raise DropInputError(f"dropped file {resolved!r}: entry {position} needs a string 'rule_id'")
        if read_at is not None and not isinstance(read_at, str):
            raise DropInputError(
                f"dropped file {resolved!r}: entry {position} 'read_at' must be a string or null"
            )
    return entries


def load_findings_files(path: str) -> list[str]:
    """The file of each finding in a findings file: a bare array, or an object
    holding the array under 'findings' (a `run` output)."""
    return [entry["file"] for entry in _entries(_load(path, "findings file"), "findings", path, "findings file")]


def drop_id(entry: Mapping[str, Any]) -> str:
    return f"{entry.get('file')}:{entry.get('line')}:{entry.get('rule_id')}"


def check_drops(
    manifest: Manifest, dropped: Sequence[Mapping[str, Any]], finding_files: Sequence[str] = ()
) -> dict[str, Any]:
    """The report for *dropped* against *manifest*. Always a result; no outcome of
    it is an error."""
    results = []
    for position, entry in enumerate(dropped, start=1):
        path, read_at = entry["file"], entry.get("read_at")
        has_copy = path in manifest.files
        deleted = path in manifest.deleted
        if not read_at:
            reason = REASON_NO_READ_AT
        elif read_at != manifest.head_sha:
            reason = f"{REASON_OTHER_REVISION} ({read_at})"
        elif not (has_copy or deleted):
            reason = REASON_NO_COPY
        else:
            reason = None
        item: dict[str, Any] = {
            "position": position,
            "id": drop_id(entry),
            "read_at": read_at or None,
            "proven": reason is None,
        }
        if reason is not None:
            item["reason"] = reason
        results.append(item)

    gaps = [{"path": path, "reason": why} for path, why in manifest.missing.items()]
    named = set(manifest.missing)
    for path in [*(e["file"] for e in dropped), *finding_files]:
        if path in named or path in manifest.files or path in manifest.deleted:
            continue
        named.add(path)
        gaps.append({"path": path, "reason": REASON_NOT_STAGED})
    return {
        "result": "check-drops",
        "head_sha": manifest.head_sha,
        "drops": results,
        "unproven_count": sum(1 for item in results if not item["proven"]),
        "gaps": gaps,
    }


def _read_at_value(value: Any) -> str | None:
    """The revision as the reviewer gave it, collapsed to one line; None when it
    gave none. A value that is not a 40-hex SHA is kept as given, never refused."""
    if value is None:
        return None
    text = " ".join(value.split()) if isinstance(value, str) else str(value)
    return text or None


def annotate_dropped(
    dropped: Sequence[Any], head_sha: str
) -> tuple[list[Any], list[tuple[str, str | None]]]:
    """*dropped* with ``read_at`` and ``proven`` set on every entry that is an
    object (anything else is left for the fence validator to refuse), and the
    ``(id, read_at)`` of each entry marked unproven. A ``proven`` the caller typed
    is replaced: it is computed here, from the reviewed head."""
    annotated: list[Any] = []
    unproven: list[tuple[str, str | None]] = []
    for entry in dropped:
        if not isinstance(entry, dict):
            annotated.append(entry)
            continue
        read_at = _read_at_value(entry.get("read_at"))
        proven = read_at == head_sha
        annotated.append({**entry, "read_at": read_at, "proven": proven})
        if not proven:
            unproven.append((drop_id(entry), read_at))
    return annotated, unproven


__all__ = [
    "DropInputError",
    "annotate_dropped",
    "check_drops",
    "drop_id",
    "load_dropped",
    "load_findings_files",
]
