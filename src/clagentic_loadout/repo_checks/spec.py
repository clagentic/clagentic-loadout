"""repo_checks.spec — the ``lint`` and ``build`` blocks of a repo profile.

Each block is an ordered mapping of a check name to the entry shape of
``push.verify`` (``argv``, ``timeout_seconds``, ``env_passthrough``)::

    lint:
      ruff: {argv: [ruff, check, .]}
    build:
      install: {argv: [npm, ci], timeout_seconds: 900}
      compile: {argv: [npm, run, build], env_passthrough: [CI]}

The entries run in the order written. The entry fields are parsed by
``push.verify_config.parse_command_fields``, the same code ``push.verify`` uses.
A block that is absent or ``null`` declares nothing; an empty mapping declares
nothing either. Anything else outside the shape is refused naming the key path.
"""

from __future__ import annotations

from typing import Any

from clagentic_loadout.push.verify_config import InvalidVerifyConfigError, VerifyEntry, parse_command_fields

KEY_LINT = "lint"
KEY_BUILD = "build"
CHECK_BLOCKS = (KEY_LINT, KEY_BUILD)

_ENTRY_KEYS = ("argv", "timeout_seconds", "env_passthrough")


class ChecksBlockError(ValueError):
    """A ``lint`` or ``build`` block is not in the documented shape. The message
    names the offending key path."""


def parse_checks_block(block: Any, key: str) -> tuple[VerifyEntry, ...]:
    """The entries of the block named *key* (``lint`` or ``build``) in a parsed
    profile, in declared order. ``None`` is no block."""
    if block is None:
        return ()
    if not isinstance(block, dict):
        raise ChecksBlockError(
            f"{key} must be a mapping of a check name to {{argv, timeout_seconds, env_passthrough}}"
        )
    entries: list[VerifyEntry] = []
    for name, raw in block.items():
        if not isinstance(name, str) or not name.strip():
            raise ChecksBlockError(f"{key}: a check name must be a non-empty string, got {name!r}")
        where = f"{key}.{name}"
        if not isinstance(raw, dict):
            raise ChecksBlockError(f"{where} must be a mapping with an argv, got {raw!r}")
        unknown = sorted(str(k) for k in raw if k not in _ENTRY_KEYS)
        if unknown:
            raise ChecksBlockError(
                "unknown or misplaced key: " + ", ".join(f"{where}.{k}" for k in unknown)
            )
        try:
            entries.append(parse_command_fields(raw, name.strip(), where))
        except InvalidVerifyConfigError as exc:
            raise ChecksBlockError(str(exc)) from exc
    names = [entry.name for entry in entries]
    duplicated = sorted({n for n in names if names.count(n) > 1})
    if duplicated:
        raise ChecksBlockError(f"{key}: check names must be unique once trimmed, duplicated: {duplicated!r}")
    return tuple(entries)


__all__ = ["CHECK_BLOCKS", "KEY_BUILD", "KEY_LINT", "ChecksBlockError", "parse_checks_block"]
