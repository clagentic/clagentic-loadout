"""clagentic_loadout.wait — sanctioned wait primitives for autonomous agents.

Two importable primitives (Wave A slice 4, tome #688):

  poll_wait     — poll a file on disk until it meets a size/content
                  condition, or a timeout expires. Avoids Bash command
                  substitution (`$(...)`) in the caller's tool call by
                  doing the poll loop in pure Python.

  scoped_test_wait — run one scoped build/test/lint command to completion
                  in the foreground, streaming output to a file, instead of
                  backgrounding it with shell `&`. The allowed command
                  shapes are config-driven (see `config`), not a hardcoded
                  module import.

Both are also exposed as standalone console scripts via pyproject.toml so a
caller who cannot use `$(...)` substitution or shell backgrounding in its
tool-call surface has a single sanctioned command for each operation.
"""

from __future__ import annotations

from clagentic_loadout.wait.config import (
    DEFAULT_SCOPED_TEST_PATTERNS,
    ScopedTestWaitConfig,
    is_scoped_test_command,
    load_scoped_test_patterns,
)
from clagentic_loadout.wait.poll import PollWaitResult, poll_wait
from clagentic_loadout.wait.scoped_test import ScopedTestWaitResult, scoped_test_wait

__all__ = [
    "DEFAULT_SCOPED_TEST_PATTERNS",
    "PollWaitResult",
    "ScopedTestWaitConfig",
    "ScopedTestWaitResult",
    "is_scoped_test_command",
    "load_scoped_test_patterns",
    "poll_wait",
    "scoped_test_wait",
]
