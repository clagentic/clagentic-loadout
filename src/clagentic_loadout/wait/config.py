"""config.py — per-repo configuration for the scoped-test-wait allowlist.

Ported from the reference implementation's scoped_test_verbs.py (Wave A
slice 4, tome #688), re-parametrized as per-repo config rather than a fixed
module import. The source copy stays primary until that project's separate
CUT OVER + RETIRE + VERIFY-GONE task for this slice.

BACKGROUND: the source module hardcoded a single verb allowlist
(BUILD_TEST_LINT) shared between a Bash-call guard and the
scoped-test-wait primitive, so the two callers could never drift apart. That
coupling is right for a single deployment but wrong for a published package —
different consumers of clagentic_loadout will want different scoped-command
policies. This module keeps the "one definition, both admission points"
property, but makes the pattern SET itself a per-repo config value with a
generic default rather than a fixed name.

Config surface: a caller-supplied list of regex strings overrides
DEFAULT_SCOPED_TEST_PATTERNS. load_scoped_test_patterns() reads them from the
repo's single sectioned config file (default
`.clagentic/loadout/config.yaml` — see `repo_config.py`, lr-446c35, for the
shared path constant/legacy-fallback loader every section owner in this
package now resolves through), under a `wait:` top-level section, key
`scoped_test_patterns`, falling back to the default set when the file,
section, or key is absent. This module does not itself locate the repo root
or enforce any particular config file location beyond the documented
default — a caller with its own config-discovery convention can pass
patterns straight to is_scoped_test_command() instead.

`.clagentic/loadout/config.yaml` is ONE file shared across every loadout
verb, each verb owning its own top-level section (this module owns `wait:`
only). A future slice adding its own config reads a different top-level key
from the same file without touching this loader — see CONFIG_SECTION_WAIT
below for the convention. This was the FIRST per-repo sectioned-config
reader in the package, so it established that shape rather than each verb
inventing its own per-verb config file.

These are PRECISION patterns — fixed subcommands only, no arbitrary shell, no
package installs (go get / pip install / apt), no network. That safety
property is enforced by the caller executing matched commands via an
argv-list subprocess.run() (never shell=True, see scoped_test.py), never by
this module alone.

Dependencies: PyYAML (parses the sectioned repo-local config file).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)

#: Top-level section key this module owns within the repo-local config file.
#: A future verb adds its own top-level section (e.g. `push:`, `review:`)
#: alongside this one, without needing changes here.
CONFIG_SECTION_WAIT = "wait"

#: Config key (within the `wait:` section) holding the list of scoped-test
#: regex pattern strings.
CONFIG_KEY_SCOPED_TEST_PATTERNS = "scoped_test_patterns"

# Default scoped build/test/lint command shapes — generic toolchain
# invocations, not tied to any one project or agent. A caller may override
# this set entirely via config; this is the baked-in fallback.
DEFAULT_SCOPED_TEST_PATTERNS: list[str] = [
    # Go toolchain.
    r"^go\s+(build|test|vet|fmt)(\s|$)",
    # Python toolchain: `-m pytest`, `-m py_compile`, `-m ruff`, `-m flake8`,
    # `-m mypy`, `-m unittest`, `-m build`, `-m venv` only — not arbitrary `-m`.
    r"^python3\s+-m\s+(pytest|py_compile|ruff|flake8|mypy|unittest|build|venv)(\s|$)",
    # Standalone linters when invoked directly rather than via `python3 -m`.
    r"^(ruff|flake8|mypy)(\s|$)",
    # Bare pytest — alias for `python3 -m pytest`.
    r"^pytest(\s|$)",
    # make: only the conventional build/test/lint subtargets, not arbitrary make.
    r"^make\s+(build|test|lint|vet|fmt|check)(\s|$)",
    # Node/JS toolchain: npm test|run|ci only. These run package.json scripts
    # or install strictly from the committed lockfile — they do not fetch and
    # execute an arbitrary, unpinned remote package the way `npx <pkg>` does,
    # so they are consistent with the no-install/no-network contract above.
    #
    # `npx <pkg>` is deliberately EXCLUDED from this default set: npx's whole
    # purpose is to fetch-and-execute a package that may not be pinned or
    # even installed locally, which is exactly the network + arbitrary-code-
    # execution shape this default set exists to keep out (pre-merge security
    # review finding). A repo that has a genuine, reviewed need for npx can
    # opt in explicitly by adding its own pattern to its repo-local config's
    # wait.scoped_test_patterns — that is the repo's own trust decision, not
    # something the shipped default should grant for free.
    r"^npm\s+(test|run|ci)(\s|$)",
    # Shell test harness: the canonical relative-path smoke-suite entrypoint
    # for an enrolled repo. Allows optional trailing arguments (e.g. --quick).
    r"^sh scripts/smoke\.sh(\s|$)",
]


class InvalidScopedTestConfigError(ValueError):
    """Raised when a config file's scoped_test_patterns value is malformed
    (not a list of strings, or a string that is not a valid regex)."""


@dataclass(frozen=True)
class ScopedTestWaitConfig:
    """Resolved scoped-test-wait configuration: the compiled pattern set plus
    where it came from, for diagnostics."""

    patterns: tuple[re.Pattern[str], ...]
    source: str  # "default" or the config file path it was read from


def _compile_patterns(raw_patterns: list[str], *, source: str) -> tuple[re.Pattern[str], ...]:
    compiled = []
    for raw in raw_patterns:
        if not isinstance(raw, str):
            raise InvalidScopedTestConfigError(
                f"{source}: {CONFIG_KEY_SCOPED_TEST_PATTERNS} entries must be strings, "
                f"got {raw!r}."
            )
        try:
            compiled.append(re.compile(raw))
        except re.error as exc:
            raise InvalidScopedTestConfigError(
                f"{source}: pattern {raw!r} is not a valid regex: {exc}."
            ) from exc
    return tuple(compiled)


def _default_config() -> ScopedTestWaitConfig:
    return ScopedTestWaitConfig(
        patterns=_compile_patterns(DEFAULT_SCOPED_TEST_PATTERNS, source="default"),
        source="default",
    )


def load_scoped_test_patterns(
    repo_root: str | Path | None = None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> ScopedTestWaitConfig:
    """Resolve the scoped-test-wait pattern set for a repo.

    Reads `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`), expecting a YAML mapping with a
    `wait:` top-level section holding a `scoped_test_patterns` key (a list of
    regex strings). Falls back to DEFAULT_SCOPED_TEST_PATTERNS when the file,
    the `wait:` section, or the `scoped_test_patterns` key is absent.

    Args:
        repo_root: repo root to resolve the config path against. When None,
            the default pattern set is returned directly (no file lookup) —
            a caller with no repo context still gets a safe, importable
            default.
        config_relative_path: override the config file's relative path
            (mainly for tests).

    Returns:
        ScopedTestWaitConfig with compiled patterns and a `source` label.

    Raises:
        InvalidScopedTestConfigError: the config file exists but
            scoped_test_patterns is malformed.
    """
    if repo_root is None:
        return _default_config()

    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path
    )
    if not config_path.exists():
        return _default_config()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise InvalidScopedTestConfigError(
            f"{config_path}: could not be read as YAML: {exc}."
        ) from exc

    if not isinstance(raw, dict):
        return _default_config()

    wait_section = raw.get(CONFIG_SECTION_WAIT)
    if not isinstance(wait_section, dict) or CONFIG_KEY_SCOPED_TEST_PATTERNS not in wait_section:
        return _default_config()

    raw_patterns = wait_section[CONFIG_KEY_SCOPED_TEST_PATTERNS]
    if not isinstance(raw_patterns, list):
        raise InvalidScopedTestConfigError(
            f"{config_path}: {CONFIG_SECTION_WAIT}.{CONFIG_KEY_SCOPED_TEST_PATTERNS} must be "
            f"a list of strings, got {type(raw_patterns).__name__}."
        )

    return ScopedTestWaitConfig(
        patterns=_compile_patterns(raw_patterns, source=str(config_path)),
        source=str(config_path),
    )


def is_scoped_test_command(
    cmd: str,
    patterns: tuple[re.Pattern[str], ...] | list[re.Pattern[str]] | list[str] | None = None,
) -> bool:
    """Return True iff `cmd` matches one of the scoped-test-wait pattern
    shapes.

    Args:
        cmd: the full command string, e.g. "python3 -m pytest tests/ -v".
        patterns: compiled regex patterns (or raw strings) to match against.
            When None (default), uses DEFAULT_SCOPED_TEST_PATTERNS.

    Does not itself apply any compound-shell or forbidden-substring checks —
    the executing caller (scoped_test.py) relies on argv-list subprocess
    execution (never shell=True) to make those checks structurally
    unnecessary for its own admission decision.
    """
    if patterns is None:
        compiled: tuple[re.Pattern[str], ...] = _compile_patterns(
            DEFAULT_SCOPED_TEST_PATTERNS, source="default"
        )
    else:
        compiled = tuple(
            p if isinstance(p, re.Pattern) else re.compile(p) for p in patterns
        )
    return any(pat.match(cmd) for pat in compiled)
