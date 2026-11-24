"""new_file_out -- the file form of a verb's output: write a new file, never replace one.

Several verbs print a document that can be too large for a caller to read from
standard output. Each takes ``--out FILE`` for it: the document goes to a new
file and standard output carries one JSON line naming the path. An existing
file is never replaced, so a second run cannot overwrite what an earlier one
wrote. This module is that one path; a verb maps ``NewFileError`` to its own
exit code.
"""

from __future__ import annotations

import json


class NewFileError(Exception):
    """The file could not be created: it exists, or it cannot be written."""


def write_new_file(path: str, data: bytes | str) -> None:
    """Create *path* holding *data* (text is written as UTF-8). Raises
    NewFileError when *path* already exists or cannot be written."""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    try:
        with open(path, "xb") as handle:
            handle.write(payload)
    except FileExistsError as exc:
        raise NewFileError(f"{path!r} already exists; this verb never replaces a file") from exc
    except OSError as exc:
        raise NewFileError(f"cannot write {path!r}: {exc}") from exc


def emit_new_file(path: str, data: bytes | str) -> None:
    """Write *data* to the new file *path* and print the one-line result that
    names it."""
    write_new_file(path, data)
    print(json.dumps({"result": "written", "path": path}))


__all__ = ["NewFileError", "emit_new_file", "write_new_file"]
