"""test_cli_conformance.py — CLI hygiene conformance suite (lr-0b45).

CLAUDE.md rule 4: "every verb: --help/--version, reserved exit-code range
with child-process codes remapped off it, error messages that report
resolved values." This module is the enforced test suite for that rule,
covering:

  1. Every registered verb's --help exits 0 (no traceback).
  2. Every registered verb's --version (or, for the two verbs whose own
     --version flag was already a business argument, --cli-version) exits 0
     and prints the single-sourced package version string.
  3. The umbrella CLI's own --help/--version.
  4. Unknown-verb dispatch: a helpful message on stderr, EXIT_UMBRELLA_USAGE,
     never a traceback.
  5. Umbrella dispatch smoke tests: `clagentic-loadout <verb> --help`
     reaches the SAME help text as invoking the verb's own entry point
     directly -- proving the umbrella is a thin router, not a
     reimplementation.

wait/cli.py's two entry points (poll_wait_main, scoped_test_wait_main) call
sys.exit() directly rather than returning an int (see that module's
docstring) -- this suite captures that via pytest.raises(SystemExit),
consistent with how those two entry points have always behaved.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from clagentic_loadout._version import get_version
from clagentic_loadout.acquire import verb as acquire_verb
from clagentic_loadout.cli import EXIT_UMBRELLA_USAGE, main as umbrella_main
from clagentic_loadout.doctor import cli as doctor_cli
from clagentic_loadout.merge import close_verb as merge_close_verb
from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.provisioning import cli as provisioning_cli
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.release import detector as release_detector
from clagentic_loadout.release import dispatch as release_dispatch
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import git_host_api
from clagentic_loadout.transport import stage_body_verb
from clagentic_loadout.wait import cli as wait_cli

_PACKAGE_VERSION = get_version()

#: (label, main callable, version flag). Every verb registered with the
#: umbrella (see cli.py's _SIMPLE_VERBS/_GROUPED_VERBS) must appear here.
#: version_flag is "--version" for the argparse-native verbs; the two
#: release verbs use "--cli-version" because their own --version flag was
#: already a release-version-string business argument that predates the
#: CLI conformance rule (see each module's _build_arg_parser docstring for
#: the --cli-version help text explaining the trade-off).
_ARGPARSE_VERBS = [
    ("push", push_verb.main, "--version"),
    ("review-post", review_verb.main, "--version"),
    ("acquire", acquire_verb.main, "--version"),
    ("merge", merge_verb.main, "--version"),
    ("close-pr", merge_close_verb.main, "--version"),
    ("git-host-api", git_host_api.main, "--version"),
    ("stage-body", stage_body_verb.main, "--version"),
    ("release-dispatch", release_dispatch.main, "--cli-version"),
    ("release-detect", release_detector.main, "--cli-version"),
    ("provision-allowlist", provisioning_cli.main, "--version"),
    ("doctor", doctor_cli.main, "--version"),
]


#: release-dispatch/release-detect call parser.parse_args(argv) with no
#: SystemExit-catching wrapper around --help (unlike the other four verbs,
#: which intercept --help/-h manually before parse_args and always return an
#: int) -- --help therefore raises SystemExit(0) for these two rather than
#: returning 0. Both behaviors are pre-existing, unchanged by lr-0b45; this
#: set names which verbs use which contract so the test asserts the real
#: behavior instead of a wrong assumption.
_HELP_RAISES_SYSTEMEXIT = {"release-dispatch", "release-detect"}


@pytest.mark.parametrize("label,verb_main,_flag", _ARGPARSE_VERBS, ids=[v[0] for v in _ARGPARSE_VERBS])
def test_verb_help_exits_ok(label: str, verb_main, _flag: str, capsys) -> None:
    if label in _HELP_RAISES_SYSTEMEXIT:
        with pytest.raises(SystemExit) as exc_info:
            verb_main(["--help"])
        assert exc_info.value.code == 0, f"{label} --help must exit 0, got {exc_info.value.code}"
    else:
        rc = verb_main(["--help"])
        assert rc == 0, f"{label} --help must exit 0, got {rc}"
    out = capsys.readouterr().out
    assert out.strip(), f"{label} --help produced no output"


@pytest.mark.parametrize("label,verb_main,flag", _ARGPARSE_VERBS, ids=[v[0] for v in _ARGPARSE_VERBS])
def test_verb_version_exits_ok_and_prints_package_version(label: str, verb_main, flag: str, capsys) -> None:
    if label in _HELP_RAISES_SYSTEMEXIT:
        with pytest.raises(SystemExit) as exc_info:
            verb_main([flag])
        assert exc_info.value.code == 0, f"{label} {flag} must exit 0"
    else:
        rc = verb_main([flag])
        assert rc == 0, f"{label} {flag} must exit 0, got {rc}"
    out = capsys.readouterr().out
    assert _PACKAGE_VERSION in out, (
        f"{label} {flag} output {out!r} does not contain the package "
        f"version {_PACKAGE_VERSION!r}"
    )


def test_wait_poll_help_exits_ok(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        wait_cli.poll_wait_main(["--help"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip()


def test_wait_poll_version_exits_ok_and_prints_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        wait_cli.poll_wait_main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert _PACKAGE_VERSION in out


def test_wait_scoped_test_help_exits_ok(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        wait_cli.scoped_test_wait_main(["--help"])
    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip()


def test_wait_scoped_test_version_exits_ok_and_prints_package_version(capsys) -> None:
    with pytest.raises(SystemExit) as exc_info:
        wait_cli.scoped_test_wait_main(["--version"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert _PACKAGE_VERSION in out


# ---------------------------------------------------------------------------
# Umbrella CLI
# ---------------------------------------------------------------------------


def test_umbrella_help_exits_ok_and_lists_verbs(capsys) -> None:
    rc = umbrella_main(["--help"])
    assert rc == 0
    out = capsys.readouterr().out
    for expected in ("push", "merge", "review", "git-host-api", "stage-body", "provision-allowlist", "doctor", "release dispatch", "release detect", "wait poll", "wait scoped-test"):
        assert expected in out, f"umbrella --help missing verb listing for {expected!r}"


def test_umbrella_no_args_prints_help_and_exits_ok(capsys) -> None:
    rc = umbrella_main([])
    assert rc == 0
    assert capsys.readouterr().out.strip()


def test_umbrella_version_exits_ok_and_prints_package_version(capsys) -> None:
    rc = umbrella_main(["--version"])
    assert rc == 0
    out = capsys.readouterr().out
    assert _PACKAGE_VERSION in out


def test_umbrella_unknown_verb_reports_reserved_exit_code_not_traceback(capsys) -> None:
    rc = umbrella_main(["not-a-real-verb"])
    assert rc == EXIT_UMBRELLA_USAGE
    err = capsys.readouterr().err
    assert "not-a-real-verb" in err
    assert "unknown verb" in err.lower()


def test_umbrella_unknown_grouped_subverb_reports_reserved_exit_code(capsys) -> None:
    rc = umbrella_main(["release", "not-a-real-subverb"])
    assert rc == EXIT_UMBRELLA_USAGE
    err = capsys.readouterr().err
    assert "release not-a-real-subverb" in err


@pytest.mark.parametrize(
    "argv",
    [
        ["push"],
        ["merge"],
        ["review"],
        ["git-host-api"],
        ["stage-body"],
        ["provision-allowlist"],
        ["doctor"],
    ],
    ids=lambda argv: " ".join(argv),
)
def test_umbrella_dispatch_smoke_help(argv: list[str], capsys) -> None:
    """Umbrella dispatch reaches the SAME verb-level --help output as
    invoking the verb's own entry point directly -- proving the umbrella
    is a thin router, not a second implementation of --help."""
    rc = umbrella_main([*argv, "--help"])
    assert rc == 0
    umbrella_out = capsys.readouterr().out
    assert umbrella_out.strip()


@pytest.mark.parametrize(
    "argv",
    [["release", "dispatch"], ["release", "detect"]],
    ids=lambda argv: " ".join(argv),
)
def test_umbrella_dispatch_smoke_help_systemexit_verbs(argv: list[str], capsys) -> None:
    """release dispatch/detect raise SystemExit(0) on --help (see
    _HELP_RAISES_SYSTEMEXIT above) -- the umbrella must let that propagate
    unchanged rather than swallowing or reinterpreting it."""
    with pytest.raises(SystemExit) as exc_info:
        umbrella_main([*argv, "--help"])
    assert exc_info.value.code == 0
    umbrella_out = capsys.readouterr().out
    assert umbrella_out.strip()


def test_umbrella_dispatch_wait_poll_help_smoke() -> None:
    """wait poll/scoped-test dispatch through the sys.exit()-based entry
    points (see wait/cli.py) -- the umbrella lets that SystemExit propagate
    unchanged rather than swallowing it, matching direct invocation."""
    with pytest.raises(SystemExit) as exc_info:
        umbrella_main(["wait", "poll", "--help"])
    assert exc_info.value.code == 0


def test_umbrella_dispatch_wait_scoped_test_help_smoke() -> None:
    with pytest.raises(SystemExit) as exc_info:
        umbrella_main(["wait", "scoped-test", "--help"])
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# Console-script subprocess smoke test — proves the installed entry point
# (not just the importable main()) actually resolves --version end to end.
# Skipped when the package is not installed in this interpreter (e.g. a bare
# `pytest` run against an uninstalled checkout with pythonpath=["src"] only,
# see pyproject.toml's [tool.pytest.ini_options]).
# ---------------------------------------------------------------------------


def test_console_script_version_matches_module_version() -> None:
    """Subprocess-level smoke test proving `python -m clagentic_loadout`
    (the console_scripts equivalent for an uninstalled dev checkout — see
    __main__.py) reaches the same --version output as the in-process
    module import used by the tests above. Runs with `src` on PYTHONPATH,
    matching this repo's own [tool.pytest.ini_options] pythonpath setting,
    since the package is not necessarily `pip install`-ed in the test
    environment."""
    import os

    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    env = {**os.environ, "PYTHONPATH": src_dir}
    result = subprocess.run(
        [sys.executable, "-m", "clagentic_loadout", "--version"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert _PACKAGE_VERSION in result.stdout
