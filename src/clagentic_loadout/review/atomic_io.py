"""review.atomic_io — the one writer for every state or record file under review/.

A file is written to a uniquely named temp file beside its target and moved
into place with os.replace, so an interrupted write leaves the previous file
intact instead of truncated content that a later reader would misparse.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def write_bytes_atomic(path: Path, data: bytes) -> None:
    """Replace *path* with *data* atomically. Raises OSError on failure, after
    removing the temp file so no half-written file stays in the directory."""
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, data: Any) -> None:
    """Atomically replace *path* with *data* serialised as JSON."""
    write_bytes_atomic(path, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))
