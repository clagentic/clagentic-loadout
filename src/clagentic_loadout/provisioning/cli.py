"""cli.py — the `loadout-provision-allowlist` console script (lr-4e04).

Given a role, resolves that role's declared verb set (roles.py — repo-local
`.clagentic/loadout/config.yaml` `roles:` section, falling back to
roles.DEFAULT_ROLE_VERBS) and emits its permission-allowlist fragment
(allowlist.py). Default behavior PRINTS the fragment as a copy-pasteable
JSON array to stdout (safe: no filesystem mutation). `--write` opts into an
idempotent in-place merge (writer.py) into a harness settings file, whose
location is itself parameterized (settings_path.py: --settings-file flag,
CLAGENTIC_LOADOUT_SETTINGS_FILE env var, or a HOME-derived default with the
same empty-HOME fail-fast discipline scripts/install.sh already applies).

See docs/provisioning.md for the end-to-end integrator workflow this verb
is one step of.
"""

from __future__ import annotations

import argparse
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.provisioning.allowlist import (
    UnknownVerbError,
    generate_role_fragment,
    render_fragment_json,
)
from clagentic_loadout.provisioning.roles import (
    InvalidRoleConfigError,
    load_role_verbs,
)
from clagentic_loadout.provisioning.settings_path import (
    SettingsPathError,
    resolve_settings_path,
)
from clagentic_loadout.provisioning.writer import (
    SettingsWriteError,
    merge_fragment_into_settings,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_ROLE_CONFIG_INVALID = 2
EXIT_SETTINGS_PATH_INVALID = 3
EXIT_SETTINGS_WRITE_FAILED = 4


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="provision-allowlist",
        description=(
            "provision-allowlist -- emit a ROLE's Bash permission-allowlist "
            "fragment for the loadout verbs that role invokes. Per-role "
            "only: there is no global, all-verbs fragment. Default: print "
            "the fragment (safe, copy-pasteable). --write merges it "
            "idempotently into a harness settings file."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  provision-allowlist --role builder\n"
            "  provision-allowlist --role reviewer --repo-root .\n"
            "  provision-allowlist --role merger --write "
            "--settings-file ~/.claude/settings.json\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"provision-allowlist {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--role",
        default=None,
        help="Role to generate the allowlist fragment for (e.g. builder, "
        "reviewer, merger, lead, or any role declared in the target "
        "repo's .clagentic/loadout/config.yaml roles: section). Required.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        dest="repo_root",
        help="Repo root to read .clagentic/loadout/config.yaml's roles: "
        "section from. Omit to use only the built-in reference role "
        "mapping (roles.DEFAULT_ROLE_VERBS).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Merge the fragment into the resolved settings file in "
        "place (idempotent: never duplicates, reorders, or removes "
        "existing entries). Omit to print only (the default, safe "
        "path).",
    )
    parser.add_argument(
        "--settings-file",
        default=None,
        dest="settings_file",
        help="Explicit harness settings-file path for --write. Falls "
        "back to CLAGENTIC_LOADOUT_SETTINGS_FILE, then a HOME-derived "
        "default (~/.config/clagentic/loadout/settings.json). Ignored "
        "without --write.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable — matches push.verb.main's own
    contract: --help is intercepted manually before parse_args, and any
    SystemExit parse_args/--version raises is caught and remapped to an
    int, rather than propagating)."""
    if argv is None:
        argv = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    if not args.role:
        print("provision-allowlist: --role is required.", file=sys.stderr)
        return EXIT_USAGE

    try:
        role_verbs = load_role_verbs(args.repo_root)
        verbs = role_verbs.get(args.role)
        if verbs is None:
            known = ", ".join(sorted(role_verbs)) or "<none>"
            print(
                f"provision-allowlist: role {args.role!r} is not declared "
                f"(repo_root={args.repo_root!r}). Known roles: {known}.",
                file=sys.stderr,
            )
            return EXIT_ROLE_CONFIG_INVALID
    except InvalidRoleConfigError as exc:
        print(f"provision-allowlist: {exc}", file=sys.stderr)
        return EXIT_ROLE_CONFIG_INVALID

    try:
        fragment_json = render_fragment_json(args.role, verbs)
        fragment = generate_role_fragment(args.role, verbs)
    except UnknownVerbError as exc:
        print(f"provision-allowlist: {exc}", file=sys.stderr)
        return EXIT_ROLE_CONFIG_INVALID

    if not args.write:
        print(fragment_json)
        return EXIT_OK

    try:
        settings_path = resolve_settings_path(args.settings_file)
    except SettingsPathError as exc:
        print(f"provision-allowlist: {exc}", file=sys.stderr)
        return EXIT_SETTINGS_PATH_INVALID

    try:
        merge_fragment_into_settings(settings_path, fragment)
    except SettingsWriteError as exc:
        print(f"provision-allowlist: {exc}", file=sys.stderr)
        return EXIT_SETTINGS_WRITE_FAILED

    print(
        f"provision-allowlist: merged {len(fragment)} entries for role "
        f"{args.role!r} into {settings_path}.",
        file=sys.stderr,
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
