"""identity.delivery -- local delivery of an assertion (contract section 6).

The assertion sits in `identity.jwt` inside the directory named by
`CLAGENTIC_IDENTITY_DIR`. The directory must be absolute, owned by the reading
user, mode exactly 0700, and not a symlink; the file is opened without following
a symlink and every check runs on the open handle.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import Mapping

from clagentic_loadout.identity import errors

IDENTITY_DIR_ENV = "CLAGENTIC_IDENTITY_DIR"
IDENTITY_FILE = "identity.jwt"
MAX_FILE_BYTES = 16384


def _missing(message: str) -> errors.IdentityError:
    return errors.IdentityError(errors.IDENTITY_DIR_MISSING, f"{IDENTITY_DIR_ENV}: {message}")


def read_delivery(env: Mapping[str, str]) -> tuple[str, str]:
    """Return `(token, directory)` read from the delivery directory, or raise."""
    directory = (env.get(IDENTITY_DIR_ENV) or "").strip()
    if not directory:
        raise _missing("is unset or empty")
    if not os.path.isabs(directory):
        raise _missing(f"{directory!r} is not an absolute path")
    # Open the directory once and do every later step relative to that
    # descriptor, so a swap of the path after the check cannot redirect the read.
    try:
        dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise errors.IdentityError(
                errors.IDENTITY_DIR_UNSAFE, f"{IDENTITY_DIR_ENV} {directory!r} is a symlink"
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise _missing(f"{directory!r} is not a directory") from exc
        raise _missing(f"{directory!r} cannot be read: {exc.strerror}") from exc
    try:
        info = os.fstat(dir_fd)
        if info.st_uid != os.geteuid():
            raise errors.IdentityError(
                errors.IDENTITY_DIR_UNSAFE,
                f"{IDENTITY_DIR_ENV} {directory!r} is not owned by this user",
            )
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise errors.IdentityError(
                errors.IDENTITY_DIR_UNSAFE,
                f"{IDENTITY_DIR_ENV} {directory!r} has mode {stat.S_IMODE(info.st_mode):04o}, need 0700",
            )
        try:
            fd = os.open(IDENTITY_FILE, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dir_fd)
        except FileNotFoundError as exc:
            raise errors.IdentityError(
                errors.IDENTITY_FILE_MISSING, f"{IDENTITY_FILE} is absent from {IDENTITY_DIR_ENV}"
            ) from exc
        except OSError as exc:
            code = errors.IDENTITY_FILE_SYMLINK if exc.errno == errno.ELOOP else errors.IDENTITY_FILE_MISSING
            raise errors.IdentityError(code, f"{IDENTITY_FILE} cannot be opened: {exc.strerror}") from exc
    finally:
        os.close(dir_fd)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise errors.IdentityError(
                errors.IDENTITY_FILE_SYMLINK, f"{IDENTITY_FILE} is not a regular file"
            )
        raw = os.read(fd, MAX_FILE_BYTES + 1)
    except OSError as exc:
        raise errors.IdentityError(
            errors.IDENTITY_FILE_MISSING, f"{IDENTITY_FILE} cannot be read: {exc.strerror}"
        ) from exc
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE_BYTES:
        raise errors.IdentityError(errors.MALFORMED, f"{IDENTITY_FILE} is over {MAX_FILE_BYTES} bytes")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise errors.IdentityError(errors.MALFORMED, f"{IDENTITY_FILE} is not UTF-8") from exc
    if text.endswith("\n"):
        text = text[:-1]
    return text, directory
