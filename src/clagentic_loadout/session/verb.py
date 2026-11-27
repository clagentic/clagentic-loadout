"""session.verb -- `loadout-session`: build a role's settings layer, or launch the
runtime with it.

  loadout-session layer  --role ROLE [--write]
  loadout-session launch --role ROLE [--runtime COMMAND] [-- RUNTIME_ARGS...]

`layer` prints the layer as one JSON object. With `--write` it writes the layer to
``~/.clagentic/loadout/session/<role>.json`` and prints only that path. `launch`
builds and writes the layer exactly as `layer --write` does, then execs
``<runtime> --settings <that path> <runtime args>``. The layer is carried by the
settings flag only; nothing here writes a user-level settings file or a file in a
repository, and nothing calls another tool but the runtime `launch` execs.

With no user-level config, no `session_profile` section, or a missing required
key, the verb says what is missing on stderr, prints no layer, starts nothing,
and exits 0.

Exit codes (the reserved range for this verb):

  0    ok, including the missing-config report
  1    usage error
  114  a `session_profile` value is malformed
  115  the runtime command is not on PATH
  116  the layer could not be written
  117  the runtime could not be started
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Callable, Sequence

from clagentic_loadout._version import get_version
from clagentic_loadout.session import launch as launcher
from clagentic_loadout.session.config import (
    CONFIG_SECTION,
    SessionConfigError,
    SessionProfile,
    load_session_profile,
)
from clagentic_loadout.session.layer import BuiltLayer, build_layer
from clagentic_loadout.session.pin import LocalCheck, Resolver

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CONFIG_INVALID = 114
EXIT_RUNTIME_NOT_FOUND = 115
EXIT_WRITE_FAILED = 116
EXIT_EXEC_FAILED = 117

_PROG = "loadout-session"

ExecFn = Callable[[str, list[str]], None]


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=_PROG,
        description=(
            f"{_PROG} -- build one role's runtime settings layer from the user-level "
            f"`{CONFIG_SECTION}` config section, or launch the runtime with it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            f"  {_PROG} layer --role builder\n"
            f"  {_PROG} layer --role builder --write\n"
            f"  {_PROG} launch --role builder -- --resume\n"
            "\n"
            "The same file `layer --write` prints serves any launcher that accepts a "
            "settings file.\n"
            "Exit codes: 0 ok (or config missing, reported), 1 usage, 114 malformed config "
            "value, 115 runtime not on PATH, 116 layer not writable, 117 runtime not startable."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{_PROG} {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    sub = parser.add_subparsers(dest="command", metavar="{layer,launch}")
    layer = sub.add_parser("layer", help="Print the role's settings layer as JSON.")
    layer.add_argument("--role", required=True, help="Role key; opaque, never checked against a roster.")
    layer.add_argument(
        "--write",
        action="store_true",
        help="Write the layer to the loadout runtime dir and print only its path.",
    )
    launch = sub.add_parser(
        "launch",
        help="Write the layer, then exec the runtime with --settings <path>. "
        "Arguments after -- go to the runtime.",
    )
    launch.add_argument("--role", required=True, help="Role key; opaque, never checked against a roster.")
    launch.add_argument(
        "--runtime",
        default=None,
        help="Runtime command to exec; overrides the config value (default: claude).",
    )
    return parser


def _say(message: str) -> None:
    print(f"{_PROG}: {message}", file=sys.stderr)


def _load_and_build(
    role: str,
    *,
    config_root: str | Path | None,
    resolver: Resolver | None,
    is_local: LocalCheck | None,
) -> tuple[SessionProfile, BuiltLayer] | int:
    """The profile and built layer, or an exit code once the problem is reported."""
    if not launcher.valid_role(role):
        _say(f"--role {role!r} must be a plain token (letters, digits, '.', '_', '-')")
        return EXIT_USAGE
    try:
        loaded = load_session_profile(config_root=config_root)
        if loaded.profile is None:
            for line in loaded.missing:
                _say(line)
            return EXIT_OK
        for key in loaded.profile.unknown_keys:
            _say(f"ignoring unknown key {CONFIG_SECTION}.{key} in {loaded.config_path}")
        built = build_layer(loaded.profile, role, resolver=resolver, is_local=is_local)
    except SessionConfigError as exc:
        _say(f"config error: {exc}")
        return EXIT_CONFIG_INVALID
    for warning in built.warnings:
        _say(f"warning: {warning}")
    return loaded.profile, built


def _split_runtime_args(argv: Sequence[str]) -> tuple[list[str], list[str]]:
    args = list(argv)
    if "--" in args:
        cut = args.index("--")
        return args[:cut], args[cut + 1 :]
    return args, []


def main(
    argv: list[str] | None = None,
    *,
    config_root: str | Path | None = None,
    runtime_base: str | Path | None = None,
    resolver: Resolver | None = None,
    is_local: LocalCheck | None = None,
    exec_fn: ExecFn | None = None,
) -> int:
    """CLI entrypoint. Returns the process exit code (`launch` does not return
    once the runtime is exec'd). The keyword arguments are for tests and
    embedding callers."""
    if argv is None:
        argv = sys.argv[1:]
    own_argv, runtime_args = _split_runtime_args(argv)
    parser = _build_arg_parser()
    try:
        args = parser.parse_args(own_argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    if args.command is None:
        parser.print_usage(sys.stderr)
        return EXIT_USAGE
    if runtime_args and args.command != "launch":
        _say("arguments after -- are accepted only by `launch`")
        return EXIT_USAGE

    prepared = _load_and_build(
        args.role, config_root=config_root, resolver=resolver, is_local=is_local
    )
    if isinstance(prepared, int):
        return prepared
    profile, built = prepared

    if args.command == "layer" and not args.write:
        print(json.dumps(built.layer, indent=2))
        return EXIT_OK

    # Resolve the runtime before writing, so a missing command leaves no file behind.
    runtime_path = command = ""
    if args.command == "launch":
        try:
            runtime_path, command = launcher.resolve_runtime(
                args.runtime, profile.runtime_command
            )
        except launcher.RuntimeNotFoundError as exc:
            _say(str(exc))
            return EXIT_RUNTIME_NOT_FOUND

    try:
        settings_path = launcher.write_layer(built.layer, args.role, base=runtime_base)
    except OSError as exc:
        _say(f"cannot write the layer for role {args.role!r}: {exc}")
        return EXIT_WRITE_FAILED

    if args.command == "layer":
        print(settings_path)
        return EXIT_OK

    run = exec_fn or os.execv
    try:
        run(runtime_path, [command, "--settings", str(settings_path), *runtime_args])
    except OSError as exc:
        _say(f"cannot start {runtime_path!r}: {exc}")
        return EXIT_EXEC_FAILED
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
