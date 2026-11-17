"""repo_test.spec — the ``test`` block of a repo profile.

Schema (strict: an unknown key makes the whole profile invalid)::

    test:
      full: [python3, -m, pytest, "--junitxml={junit}"]
      impacted: [./run-tests, --junit, "{junit}", --since, "{base}"]
      files: [python3, -m, pytest, "--junitxml={junit}"]   # file paths are appended
      prepare: [./bin/prepare]                             # runs before any of the above
      report_format: junit                                 # junit (default) | go-test-json
      base: origin/main                                    # default for --base
      paths: ["spec/**"]                                   # extra test-file globs
      ratio_warn: 2                                        # changeset ratio budget
      ratio_floor_lines: 100                               # changeset ratio floor
      timeout_seconds: 7200                                # run timeout, default 14400
      tiers:                                              # named runs, one argv each
        integration: [python3, -m, pytest, "-m", integration, "--junitxml={junit}"]

``prepare`` is an optional argv, run once in the repository root before the
command of any run mode or tier. It is an argv like the others but carries no
``{junit}`` placeholder: it writes no report. A prepare that fails ends the run
as unrunnable without running the tests.

``paths`` lists globs of files that count as tests in addition to the built-in
rule (review.test_files); it extends that rule and never replaces it.

``ratio_warn`` and ``ratio_floor_lines`` tune the advisory test:code ratio of a
change (review.changeset_ratio). Both are kept as written and judged where they
are used: a value that is not a positive integer falls back to its default with
a warning naming the key, so a typo never invalidates the rest of the profile.

``timeout_seconds`` is the longest a run's command may take before it is ended and
the run reads ``timed_out``. It is handled like ``ratio_warn``: kept as written and
judged where it is used (``resolve_timeout``), so a value that is not a positive
number falls back to the built-in default of 14400 seconds with a warning naming the
key. The ``--timeout`` flag of a run overrides it.

``report_format`` names how the commands of the block report their results, for
every run mode and tier. ``junit`` (the default, and what a block without the key
means) is the report described next. ``go-test-json`` is an event stream, one JSON
object per line, written to the command's standard output (the stream ``go test
-json`` writes); its standard error stays in ``output.log``. Under it no command
carries ``{junit}``: there is no report file to name.

Each of ``full``, ``impacted`` and ``files`` is an argv list, run without a
shell, with the repository root as the working directory. Under ``junit`` every
command must carry the ``{junit}`` placeholder: loadout substitutes the path of
the JUnit XML file the command has to write, and reads the pass / fail / skip
counts and the failing test ids from it. ``{base}`` is available to ``impacted`` only and is
replaced by the full commit SHA the base revision resolves to. Every key is
optional; a run mode whose key is absent is refused by the verb, naming the key.

``tiers`` maps a tier name to an argv list under the same rules as ``full`` (it
carries ``{junit}`` under ``junit`` and may not use ``{base}``). ``clagentic-loadout test run tier
<name>`` runs one. Names are the repository's own; nothing here knows any.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

JUNIT_PLACEHOLDER = "{junit}"
BASE_PLACEHOLDER = "{base}"

REPORT_JUNIT = "junit"
REPORT_GO_TEST_JSON = "go-test-json"
REPORT_FORMATS = (REPORT_JUNIT, REPORT_GO_TEST_JSON)

MODE_FULL = "full"
MODE_IMPACTED = "impacted"
MODE_FILES = "files"
MODES = (MODE_FULL, MODE_IMPACTED, MODE_FILES)
MODE_TIER = "tier"
RUN_MODES = (*MODES, MODE_TIER)

KEY_BASE = "base"
KEY_TIERS = "tiers"
KEY_PATHS = "paths"
KEY_RATIO_WARN = "ratio_warn"
KEY_RATIO_FLOOR_LINES = "ratio_floor_lines"
KEY_PREPARE = "prepare"
KEY_REPORT_FORMAT = "report_format"
KEY_TIMEOUT_SECONDS = "timeout_seconds"
_ALLOWED_KEYS = (
    *MODES,
    KEY_PREPARE,
    KEY_REPORT_FORMAT,
    KEY_BASE,
    KEY_TIERS,
    KEY_PATHS,
    KEY_RATIO_WARN,
    KEY_RATIO_FLOOR_LINES,
    KEY_TIMEOUT_SECONDS,
)

# Above every observed full suite, so a healthy run never meets it.
DEFAULT_TIMEOUT_SECONDS = 14400
TIMEOUT_FROM_FLAG = "flag"
TIMEOUT_FROM_PROFILE = "profile"
TIMEOUT_FROM_DEFAULT = "default"
_TIMEOUT_SOURCE_TEXT = {
    TIMEOUT_FROM_FLAG: "the --timeout flag",
    TIMEOUT_FROM_PROFILE: f"the profile's test.{KEY_TIMEOUT_SECONDS}",
    TIMEOUT_FROM_DEFAULT: "the built-in default",
}
_MAX_PATH_GLOBS = 100

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
    prepare: tuple[str, ...] | None = None
    base: str | None = None
    tiers: tuple[tuple[str, tuple[str, ...]], ...] = ()
    paths: tuple[str, ...] = ()
    ratio_warn: Any = None
    ratio_floor_lines: Any = None
    # Kept as written; resolve_timeout judges it where a run uses it.
    timeout_seconds: Any = None
    # None is the default format, junit; a value is one of REPORT_FORMATS.
    report_format: str | None = None

    @property
    def format(self) -> str:
        """The report format every command of the block uses."""
        return self.report_format or REPORT_JUNIT

    def argv_for(self, mode: str) -> tuple[str, ...] | None:
        """The declared argv template for *mode*, or None when not declared."""
        return getattr(self, mode)

    def tier_argv(self, name: str) -> tuple[str, ...] | None:
        """The declared argv template for tier *name*, or None when not declared."""
        return dict(self.tiers).get(name)

    def declared_keys(self) -> list[str]:
        """The keys this block sets, for ``loadout-profile check``."""
        return [key for key in _ALLOWED_KEYS if getattr(self, key) not in (None, (), "")]


def _argv_items(value: Any, where: str) -> tuple[str, ...]:
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
    return tuple(out)


def _report_format(value: Any, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in REPORT_FORMATS:
        raise TestBlockError(f"{where} must be one of: {', '.join(REPORT_FORMATS)}")
    return value


def _argv(value: Any, where: str, *, allow_base: bool, report_format: str = REPORT_JUNIT) -> tuple[str, ...]:
    out = _argv_items(value, where)
    if report_format == REPORT_JUNIT and not any(JUNIT_PLACEHOLDER in item for item in out):
        raise TestBlockError(
            f"{where} must carry the {JUNIT_PLACEHOLDER} placeholder, the path the command "
            "writes its JUnit XML report to"
        )
    if not allow_base and any(BASE_PLACEHOLDER in item for item in out):
        raise TestBlockError(f"{where} may not use {BASE_PLACEHOLDER}; only impacted can")
    return out


def _tiers(value: Any, where: str, report_format: str) -> tuple[tuple[str, tuple[str, ...]], ...]:
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
        out.append((name, _argv(argv, f"{where}.{name}", allow_base=False, report_format=report_format)))
    return tuple(out)


def _paths(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or len(value) > _MAX_PATH_GLOBS:
        raise TestBlockError(f"{where} must be a list of at most {_MAX_PATH_GLOBS} path globs")
    out: list[str] = []
    for position, item in enumerate(value):
        if (
            not isinstance(item, str)
            or not item.strip()
            or len(item) > _MAX_ARG_CHARS
            or "\n" in item
            or "\r" in item
        ):
            raise TestBlockError(f"{where}[{position}] must be a non-empty one-line glob")
        out.append(item.strip())
    return tuple(dict.fromkeys(out))


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
    report_format = _report_format(block.get(KEY_REPORT_FORMAT), f"{where}.{KEY_REPORT_FORMAT}")
    commands: dict[str, tuple[str, ...] | None] = {}
    for mode in MODES:
        value = block.get(mode)
        commands[mode] = (
            _argv(
                value,
                f"{where}.{mode}",
                allow_base=mode == MODE_IMPACTED,
                report_format=report_format or REPORT_JUNIT,
            )
            if value is not None
            else None
        )
    base = block.get(KEY_BASE)
    if base is not None:
        if not isinstance(base, str) or not base.strip() or base.startswith("-"):
            raise TestBlockError(f"{where}.{KEY_BASE} must be a revision name, not an option")
        base = base.strip()
    prepare = block.get(KEY_PREPARE)
    return DeclaredTests(
        prepare=_argv_items(prepare, f"{where}.{KEY_PREPARE}") if prepare is not None else None,
        base=base,
        tiers=_tiers(block.get(KEY_TIERS), f"{where}.{KEY_TIERS}", report_format or REPORT_JUNIT),
        report_format=report_format,
        paths=_paths(block.get(KEY_PATHS), f"{where}.{KEY_PATHS}"),
        ratio_warn=block.get(KEY_RATIO_WARN),
        ratio_floor_lines=block.get(KEY_RATIO_FLOOR_LINES),
        timeout_seconds=block.get(KEY_TIMEOUT_SECONDS),
        **commands,
    )


@dataclass(frozen=True)
class EffectiveTimeout:
    """The timeout a run is held to, where it came from, and a warning when the
    profile's value was unusable and the default applied instead."""

    seconds: float
    source: str
    warning: str | None = None

    @property
    def source_text(self) -> str:
        return _TIMEOUT_SOURCE_TEXT[self.source]

    def as_record(self) -> dict[str, Any]:
        record: dict[str, Any] = {"seconds": self.seconds, "source": self.source}
        if self.warning is not None:
            record["warning"] = self.warning
        return record


def is_positive_number(value: Any) -> bool:
    # bool is an int subclass; ``true`` is not a number of seconds.
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        # isfinite converts to float, which a huge int cannot do.
        return math.isfinite(value) and value > 0
    except OverflowError:
        return False


def resolve_timeout(flag: float | None, declared: Any) -> EffectiveTimeout:
    """The effective timeout: the flag, else the profile's ``timeout_seconds``, else
    the default. A profile value that is not a positive number warns and falls back
    to the default; it never makes a run refuse."""
    if flag is not None:
        return EffectiveTimeout(flag, TIMEOUT_FROM_FLAG)
    if declared is None:
        return EffectiveTimeout(DEFAULT_TIMEOUT_SECONDS, TIMEOUT_FROM_DEFAULT)
    if is_positive_number(declared):
        return EffectiveTimeout(declared, TIMEOUT_FROM_PROFILE)
    warning = (
        f"test.{KEY_TIMEOUT_SECONDS} must be a positive number of seconds, got {declared!r}; "
        f"using {DEFAULT_TIMEOUT_SECONDS}"
    )
    return EffectiveTimeout(DEFAULT_TIMEOUT_SECONDS, TIMEOUT_FROM_DEFAULT, warning)


__all__ = [
    "BASE_PLACEHOLDER",
    "DEFAULT_TIMEOUT_SECONDS",
    "EffectiveTimeout",
    "JUNIT_PLACEHOLDER",
    "KEY_BASE",
    "KEY_TIMEOUT_SECONDS",
    "KEY_PATHS",
    "KEY_PREPARE",
    "KEY_RATIO_FLOOR_LINES",
    "KEY_RATIO_WARN",
    "KEY_REPORT_FORMAT",
    "KEY_TIERS",
    "MODES",
    "MODE_FILES",
    "MODE_FULL",
    "MODE_IMPACTED",
    "MODE_TIER",
    "REPORT_FORMATS",
    "REPORT_GO_TEST_JSON",
    "REPORT_JUNIT",
    "RUN_MODES",
    "TIER_NAME_RE",
    "DeclaredTests",
    "TestBlockError",
    "is_positive_number",
    "parse_test_block",
    "resolve_timeout",
]
