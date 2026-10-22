"""repo_test.spec — the ``test`` block of a repo profile.

Schema (strict: an unknown key makes the whole profile invalid)::

    test:
      full: [python3, -m, pytest, "--junitxml={junit}"]
      impacted: [./run-tests, --junit, "{junit}", --since, "{base}"]
      files: [python3, -m, pytest, "--junitxml={junit}"]   # file paths are appended
      base: origin/main                                    # default for --base
      tiers:                                               # named runs, one argv each
        integration: [python3, -m, pytest, "-m", integration, "--junitxml={junit}"]

Each of ``full``, ``impacted`` and ``files`` is an argv list, run without a
shell, with the repository root as the working directory. Every command must
carry the ``{junit}`` placeholder: loadout substitutes the path of the JUnit XML
file the command has to write, and reads the pass / fail / skip counts and the
failing test ids from it. ``{base}`` is available to ``impacted`` only and is
replaced by the full commit SHA the base revision resolves to. Every key is
optional; a run mode whose key is absent is refused by the verb, naming the key.

``tiers`` maps a tier name to an argv list under the same rules as ``full`` (it
carries ``{junit}`` and may not use ``{base}``). ``clagentic-loadout test run tier
<name>`` runs one. Names are the repository's own; nothing here knows any.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

JUNIT_PLACEHOLDER = "{junit}"
BASE_PLACEHOLDER = "{base}"

MODE_FULL = "full"
MODE_IMPACTED = "impacted"
MODE_FILES = "files"
MODES = (MODE_FULL, MODE_IMPACTED, MODE_FILES)
MODE_TIER = "tier"
RUN_MODES = (*MODES, MODE_TIER)

KEY_BASE = "base"
KEY_TIERS = "tiers"
_ALLOWED_KEYS = (*MODES, KEY_BASE, KEY_TIERS)

TIER_NAME_RE = re.compile(r"\A[a-z][a-z0-9_-]{0,31}\Z")
_MAX_TIERS = 50

_MAX_ARGV = 100
_MAX_ARG_CHARS = 500


class TestBlockError(ValueError):
    """The ``test`` block is not in the documented shape. The message names the
    offending key path."""

    # Not a test class; the prefix is the domain word.
    __test__ = False


@dataclass(frozen=True)
class DeclaredTests:
    """The commands a repository declares for running its tests."""

    # Not a test class; the prefix is the domain word.
    __test__ = False

    full: tuple[str, ...] | None = None
    impacted: tuple[str, ...] | None = None
    files: tuple[str, ...] | None = None
    base: str | None = None
    tiers: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def argv_for(self, mode: str) -> tuple[str, ...] | None:
        """The declared argv template for *mode*, or None when not declared."""
        return getattr(self, mode)

    def tier_argv(self, name: str) -> tuple[str, ...] | None:
        """The declared argv template for tier *name*, or None when not declared."""
        return dict(self.tiers).get(name)

    def declared_keys(self) -> list[str]:
        """The keys this block sets, for ``loadout-profile check``."""
        return [key for key in _ALLOWED_KEYS if getattr(self, key)]


def _argv(value: Any, where: str, *, allow_base: bool) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise TestBlockError(f"{where} must be a non-empty list of strings (an argv)")
    if len(value) > _MAX_ARGV:
        raise TestBlockError(f"{where} may hold at most {_MAX_ARGV} entries")
    out: list[str] = []
    for position, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise TestBlockError(f"{where}[{position}] must be a non-empty string")
        if len(item) > _MAX_ARG_CHARS or "\n" in item or "\r" in item or "\0" in item:
            raise TestBlockError(
                f"{where}[{position}] must be one line of at most {_MAX_ARG_CHARS} characters"
            )
        out.append(item)
    if not any(JUNIT_PLACEHOLDER in item for item in out):
        raise TestBlockError(
            f"{where} must carry the {JUNIT_PLACEHOLDER} placeholder, the path the command "
            "writes its JUnit XML report to"
        )
    if not allow_base and any(BASE_PLACEHOLDER in item for item in out):
        raise TestBlockError(f"{where} may not use {BASE_PLACEHOLDER}; only impacted can")
    return tuple(out)


def _tiers(value: Any, where: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
    if value is None:
        return ()
    if not isinstance(value, dict):
        raise TestBlockError(f"{where} must be a mapping of tier name to argv list")
    if len(value) > _MAX_TIERS:
        raise TestBlockError(f"{where} may hold at most {_MAX_TIERS} tiers")
    out: list[tuple[str, tuple[str, ...]]] = []
    for name, argv in value.items():
        if not isinstance(name, str) or TIER_NAME_RE.match(name) is None:
            raise TestBlockError(
                f"{where}.{name} is not a valid tier name: use a lowercase letter, then lowercase "
                "letters, digits, '-' or '_', at most 32 characters"
            )
        out.append((name, _argv(argv, f"{where}.{name}", allow_base=False)))
    return tuple(out)


def parse_test_block(block: Any, where: str = "test") -> DeclaredTests:
    """The ``test`` block of a parsed profile. ``None`` is no block."""
    if block is None:
        return DeclaredTests()
    if not isinstance(block, dict):
        raise TestBlockError(f"{where} must be a mapping")
    unknown = sorted(str(k) for k in block if k not in _ALLOWED_KEYS)
    if unknown:
        raise TestBlockError(
            "unknown or misplaced key: " + ", ".join(f"{where}.{key}" for key in unknown)
        )
    commands: dict[str, tuple[str, ...] | None] = {}
    for mode in MODES:
        value = block.get(mode)
        commands[mode] = (
            _argv(value, f"{where}.{mode}", allow_base=mode == MODE_IMPACTED)
            if value is not None
            else None
        )
    base = block.get(KEY_BASE)
    if base is not None:
        if not isinstance(base, str) or not base.strip() or base.startswith("-"):
            raise TestBlockError(f"{where}.{KEY_BASE} must be a revision name, not an option")
        base = base.strip()
    return DeclaredTests(base=base, tiers=_tiers(block.get(KEY_TIERS), f"{where}.{KEY_TIERS}"), **commands)


__all__ = [
    "BASE_PLACEHOLDER",
    "JUNIT_PLACEHOLDER",
    "KEY_BASE",
    "KEY_TIERS",
    "MODES",
    "MODE_FILES",
    "MODE_FULL",
    "MODE_IMPACTED",
    "MODE_TIER",
    "RUN_MODES",
    "TIER_NAME_RE",
    "DeclaredTests",
    "TestBlockError",
    "parse_test_block",
]
