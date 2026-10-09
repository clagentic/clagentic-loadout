"""review.pr_record — the PR title/body record written beside findings.json.

A diff-chunk reviewer never sees the PR description, yet some rules are facts
about it. `loadout-review run` therefore records the title and body it fetched
at the reviewed head in a sibling file, so a later step that holds no host
credential can still read them. The file is separate from findings.json so
that contract is untouched.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.review.atomic_io import write_json_atomic

PR_RECORD_SCHEMA = "loadout.review-pr/1"
PR_RECORD_FILENAME = "pr.json"

#: Upper bound on the recorded body, in UTF-8 bytes.
BODY_MAX_BYTES = 64 * 1024


def cap_body(body: str, limit: int = BODY_MAX_BYTES) -> tuple[str, bool]:
    """*body* cut to at most *limit* UTF-8 bytes on a character boundary, and
    whether it was cut."""
    raw = body.encode("utf-8", errors="surrogatepass")
    if len(raw) <= limit:
        return body, False
    # A cut inside a multi-byte sequence leaves a partial trailing character;
    # ignoring undecodable bytes drops exactly that fragment.
    return raw[:limit].decode("utf-8", errors="ignore"), True


def build_pr_record(acquired: AcquiredPr, platform: str) -> dict[str, Any]:
    body, truncated = cap_body(acquired.body)
    return {
        "schema": PR_RECORD_SCHEMA,
        "platform": platform,
        "repo": f"{acquired.owner}/{acquired.repo}",
        "pr_number": acquired.pr_number,
        "head_sha": acquired.head_sha,
        "title": acquired.title,
        "body": body,
        "body_truncated": truncated,
    }


def write_pr_record(run_dir: Path, acquired: AcquiredPr, platform: str) -> Path:
    """Atomically (re)write run_dir/pr.json for *acquired*. Raises OSError
    when the file cannot be written."""
    path = run_dir / PR_RECORD_FILENAME
    write_json_atomic(path, build_pr_record(acquired, platform))
    return path
