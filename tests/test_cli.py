"""Smoke test: pytest wiring can import and exercise the package (lr-5bf2 Slice 0).

Verb dispatch, --help/--version, and exit-code contracts are deferred to the
release-readiness phase (lr-183e) — this only proves src/ layout + pytest
configuration resolve correctly end to end.
"""

from __future__ import annotations

from clagentic_loadout.cli import main


def test_main_returns_zero_with_no_args() -> None:
    assert main([]) == 0
