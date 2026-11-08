"""verb.py -- `clagentic-loadout update`.

Installs the release built from a source (a checkout) into the self-managed
per-release venv layout, verifies every console script on --help, atomically
repoints the live venv symlink and keeps the previous release as the rollback
target.

Single implementation of the switch: this verb does NOT reimplement it. It
drives the installer script shipped in the source checkout
(scripts/install.sh, venv tier), so the release id, build, verification,
switch and pruning rules live in exactly one place. The verb adds source
resolution, a dry-run plan, a nothing-to-switch short circuit and a JSON
result. Data dir and bin dir are resolved by the installer itself
(CLAGENTIC_LOADOUT_HOME / CLAGENTIC_LOADOUT_BIN_DIR, HOME-derived defaults,
the empty-HOME fail-fast) and read back from its --print-plan output.

Reserved exit codes (the installer's own codes are never passed through):

    0   EXIT_OK              switched, or nothing to switch, or --dry-run.
    1   EXIT_USAGE           bad CLI usage (argparse-level).
    2   EXIT_NO_SOURCE       no usable source: none given or configured, the
                             path is missing, or it carries no installer.
    3   EXIT_INSTALL_FAILED  the build or verification failed, or the switch
                             failed; the live release is untouched.
    4   EXIT_INSTALLER_ERROR the installer refused or died for another reason
                             (unresolvable HOME, no interpreter, killed).
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    USER_CONFIG_FILENAME,
    load_user_config_section,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_NO_SOURCE = 2
EXIT_INSTALL_FAILED = 3
EXIT_INSTALLER_ERROR = 4

CONFIG_SECTION = "update"
CONFIG_KEY_SOURCE = "source"

#: The installer script's own exit code for a failed build/verify/switch.
_INSTALLER_EXIT_INSTALL_FAILED = 3

_INSTALLER_RELATIVE = Path("scripts") / "install.sh"
_PLAN_KEYS = ("data_dir", "bin_dir", "release_id", "current_release")
_PREVIOUS_RE = re.compile(r"\(previous: ([^)]*)\)")


class UpdateError(Exception):
    """A refusal carrying the exit code the verb returns for it."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="update",
        description=(
            "Install the release built from a source checkout through the "
            "verified per-release venv switch: build into venvs/<release id>, "
            "run every console script with --help, repoint the live venv "
            "symlink, keep the previous release as the rollback target. A "
            "failed build or verification leaves the live release untouched. "
            "The source is --source, else the update.source key of the user "
            "config file. Data and bin dirs resolve as scripts/install.sh "
            "resolves them (--data-dir/--bin-dir, CLAGENTIC_LOADOUT_HOME/"
            "CLAGENTIC_LOADOUT_BIN_DIR, then HOME-derived defaults). Prints a "
            "JSON result on stdout. Exits "
            f"{EXIT_NO_SOURCE} when no usable source resolves, "
            f"{EXIT_INSTALL_FAILED} when the build or verification fails, "
            f"{EXIT_INSTALLER_ERROR} when the installer errors for another reason."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"update {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="Checkout to build the release from (must contain scripts/install.sh). "
        f"Default: the {CONFIG_SECTION}.{CONFIG_KEY_SOURCE} key of the user config file.",
    )
    parser.add_argument(
        "--data-dir",
        default=None,
        dest="data_dir",
        help="Base dir holding venvs/ and the live venv symlink "
        "(default: CLAGENTIC_LOADOUT_HOME, else the HOME-derived default).",
    )
    parser.add_argument(
        "--bin-dir",
        default=None,
        dest="bin_dir",
        help="Dir receiving the console-script symlinks "
        "(default: CLAGENTIC_LOADOUT_BIN_DIR, else the HOME-derived default).",
    )
    parser.add_argument(
        "--config-root",
        default=None,
        dest="config_root",
        help="Override the user-level loadout config root "
        f"(default: {DEFAULT_USER_CONFIG_ROOT}).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="Print the resolved plan (source, release id, data dir, bin dir, "
        "current and target release) and change nothing.",
    )
    return parser


def resolve_source(cli_source: str | None, config_root: str | Path | None) -> Path:
    """The source path: --source, else update.source from the user config.
    Raises UpdateError naming both places when neither yields a value."""
    if cli_source and cli_source.strip():
        source = cli_source.strip()
        origin = "--source"
    else:
        section = load_user_config_section(CONFIG_SECTION, config_root=config_root)
        configured = section.get(CONFIG_KEY_SOURCE)
        root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
        if not isinstance(configured, str) or not configured.strip():
            raise UpdateError(
                "no release source: --source was not given and "
                f"{CONFIG_SECTION}.{CONFIG_KEY_SOURCE} is not set in "
                f"{root / USER_CONFIG_FILENAME}",
                EXIT_NO_SOURCE,
            )
        source = configured.strip()
        origin = f"{CONFIG_SECTION}.{CONFIG_KEY_SOURCE} in {root / USER_CONFIG_FILENAME}"
    path = Path(source).expanduser()
    if not path.exists():
        raise UpdateError(f"source from {origin} does not exist: {path}", EXIT_NO_SOURCE)
    return path.resolve()


def _installer_for(source: Path) -> Path:
    script = source / _INSTALLER_RELATIVE
    if not source.is_dir() or not script.is_file():
        raise UpdateError(
            f"source {source} is not a checkout carrying {_INSTALLER_RELATIVE}; "
            "update drives that installer and cannot build from a bare "
            "sdist or wheel path",
            EXIT_NO_SOURCE,
        )
    return script


def _installer_argv(script: Path, source: Path, args: argparse.Namespace) -> list[str]:
    argv = ["/bin/sh", str(script), "--installer", "venv", "--source", str(source),
            "--no-seed-config"]
    if args.data_dir:
        argv += ["--data-dir", args.data_dir]
    if args.bin_dir:
        argv += ["--bin-dir", args.bin_dir]
    return argv


def _run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _installer_error(proc: subprocess.CompletedProcess[str], what: str) -> UpdateError:
    tail = (proc.stderr or "").strip().splitlines()
    detail = tail[-1] if tail else "no output"
    return UpdateError(
        f"installer {what} (exit {proc.returncode}): {detail}", EXIT_INSTALLER_ERROR
    )


def read_plan(argv: list[str]) -> dict[str, str]:
    """Run the installer's --print-plan and parse its key=value lines."""
    proc = _run([*argv, "--print-plan"])
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        raise _installer_error(proc, "could not resolve the plan")
    plan: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, sep, value = line.partition("=")
        if sep and key in _PLAN_KEYS:
            plan[key] = value
    missing = [k for k in _PLAN_KEYS if k not in plan]
    if missing:
        raise UpdateError(
            f"installer plan output lacked {missing}; got {proc.stdout!r}",
            EXIT_INSTALLER_ERROR,
        )
    return plan


def _live_release(data_dir: str) -> str | None:
    live = Path(data_dir) / "venv"
    if not live.is_symlink():
        return None
    target = str(Path(str(live.readlink())))
    return target[len("venvs/"):] if target.startswith("venvs/") else None


def _result(
    *,
    previous: str | None,
    live: str | None,
    verified: bool | None,
    switched: bool,
    plan: dict[str, str],
    source: Path,
    dry_run: bool = False,
    reason: str | None = None,
) -> dict:
    out = {
        "previous_release": previous,
        "live_release": live,
        "verified": verified,
        "switched": switched,
        "target_release": plan["release_id"],
        "source": str(source),
        "data_dir": plan["data_dir"],
        "bin_dir": plan["bin_dir"],
        "dry_run": dry_run,
    }
    if reason:
        out["reason"] = reason
    return out


def run_update(args: argparse.Namespace) -> tuple[int, dict | None]:
    source = resolve_source(args.source, args.config_root)
    script = _installer_for(source)
    argv = _installer_argv(script, source, args)
    plan = read_plan(argv)
    current = plan["current_release"] or None
    target = plan["release_id"]

    if args.dry_run:
        return EXIT_OK, _result(
            previous=current, live=current, verified=None, switched=False,
            plan=plan, source=source, dry_run=True,
        )
    if current == target:
        print(
            f"update: live release {current} already equals the source's release; "
            "nothing to switch.",
            file=sys.stderr,
        )
        return EXIT_OK, _result(
            previous=current, live=current, verified=None, switched=False,
            plan=plan, source=source, reason="already live",
        )

    proc = _run(argv)
    sys.stderr.write(proc.stderr)
    if proc.returncode == _INSTALLER_EXIT_INSTALL_FAILED:
        raise UpdateError(
            f"build or verification of release {target} from {source} failed; "
            f"live release is still {current or 'none'}",
            EXIT_INSTALL_FAILED,
        )
    if proc.returncode != 0:
        raise _installer_error(proc, "failed")

    live = _live_release(plan["data_dir"])
    previous = current
    found = _PREVIOUS_RE.search(proc.stderr)
    if found:
        previous = None if found.group(1) == "none" else found.group(1)
    return EXIT_OK, _result(
        previous=previous, live=live, verified=True, switched=live != current,
        plan=plan, source=source,
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    if argv is None:
        argv = sys.argv[1:]
    if any(a in ("--help", "-h") for a in argv):
        parser.print_help()
        return EXIT_OK
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    try:
        code, result = run_update(args)
    except UpdateError as exc:
        print(f"update: {exc}", file=sys.stderr)
        return exc.exit_code
    print(json.dumps(result, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    sys.exit(main())
