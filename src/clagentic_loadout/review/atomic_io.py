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

#: Mode for files and directories that hold engine output: owner only.
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700


def write_bytes_atomic(path: Path, data: bytes, *, mode: int = 0o666) -> None:
    """Replace *path* with *data* atomically. The temp file is created AT
    *mode* (the umask can only narrow it), so there is no window in which the
    content is readable more widely than asked. Raises OSError on failure,
    after removing the temp file so no half-written file stays in the
    directory."""
    tmp = path.parent / f".{path.name}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def write_json_atomic(path: Path, data: Any) -> None:
    """Atomically replace *path* with *data* serialised as JSON."""
    write_bytes_atomic(path, json.dumps(data, indent=2, sort_keys=True).encode("utf-8"))


def ensure_private_dir(path: Path) -> None:
    """Create *path* (and any missing parents) so that *path* itself is owner
    only, created at that mode. A directory that already exists wider, from an
    earlier version, is narrowed too. Raises OSError on failure."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=PRIVATE_DIR_MODE)
    except FileExistsError:
        if not path.is_dir():
            raise
        if path.stat().st_mode & 0o077:
            os.chmod(path, PRIVATE_DIR_MODE)
