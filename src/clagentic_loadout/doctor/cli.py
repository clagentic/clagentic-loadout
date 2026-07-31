"""cli.py — the `loadout-doctor` console script (lr-e625).

Runs the deployment-conformance check suite (doctor.checks) and prints a
per-check report to stdout, one line per CheckResult, plus a summary line.
Exits EXIT_OK only when every check passed; EXIT_CHECKS_FAILED when at least
one check's `ok` is False. This is a READ-ONLY verb: no check in this
package mutates config, mints a real credential, or writes to the repo —
running `loadout-doctor` is always safe to run repeatedly, including in CI.

Reserved exit-code range for this verb (CLI-NAMING-STANDARD.md convention —
see push.verb / provisioning.cli for the same shape):

    0   EXIT_OK              every check passed.
    1   EXIT_USAGE           bad CLI usage (argparse-level).
    2   EXIT_CHECKS_FAILED   at least one check's `ok` is False.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.doctor.checks import (
    CheckResult,
    check_attestation_source_configured,
    check_builder_identity_config,
    check_credentials,
    check_github_app_slugs_coverage,
    check_repo_loadout_schema,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_CHECKS_FAILED = 2


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="doctor",
        description=(
            "doctor -- deployment-conformance check suite. Validates "
            "token_command_* credential helpers (existence, permissions, a "
            "probe call), github_app.slugs coverage against the deployment's "
            "own role taxonomy, builder_identity / "
            "review.reviewer_logins deployment-tier config, "
            "attestation source presence when github_app.callers is "
            "declared, and "
            "(inside a repo) per-repo .clagentic/loadout/config.yaml schema "
            "-- including merge.pre_checks / merge_requirements / "
            "required_reviewer_roles / authorized_roles -- "
            "(WARNs on a legacy .loadout/ dir; a merge: section "
            "present but omitting required_reviewer_roles entirely is a "
            "FAIL -- declare real reviewer roles or an explicit "
            "required_reviewer_roles: [] opt-out; a gate role matching no "
            "key in the repo's own declared roles: section is also a FAIL "
            "-- an unsatisfiable gate; the same mismatch WARNs instead when "
            "the repo declares no roles: section at all, since that check "
            "falls back to a reference role taxonomy rather than this "
            "repo's own declaration). "
            "Read-only: never "
            "mutates config, never mints a real credential. Exits "
            f"{EXIT_CHECKS_FAILED} if any check fails."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  doctor\n"
            "  doctor --repo-root .\n"
            "  doctor --config-root ~/.config/clagentic/loadout\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"doctor {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        dest="repo_root",
        help="Repo root to check .clagentic/loadout/config.yaml schema and "
        "resolve the role taxonomy (github_app.slugs coverage check) "
        "against. Omit to skip the repo-scoped checks and use only the "
        "built-in reference role mapping for slugs coverage.",
    )
    parser.add_argument(
        "--config-root",
        default=None,
        dest="config_root",
        help="Override the USER-LEVEL loadout config root (default: "
        "~/.config/clagentic/loadout). Mainly for tests/synthetic runs.",
    )
    return parser


def _format_result_line(result: CheckResult) -> str:
    status = "OK  " if result.ok else "FAIL"
    return f"[{status}] {result.name}: {result.summary}"


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself — matches push.verb.main / provisioning.cli.main's own
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

    repo_root: str | None = args.repo_root
    config_root: str | None = args.config_root

    results: list[CheckResult] = []
    results.extend(check_credentials(config_root=config_root))
    results.append(
        check_github_app_slugs_coverage(repo_root=repo_root, config_root=config_root)
    )
    results.append(check_builder_identity_config(config_root=config_root))
    results.append(check_attestation_source_configured(config_root=config_root))
    if repo_root is not None:
        results.append(check_repo_loadout_schema(Path(repo_root)))

    for result in results:
        print(_format_result_line(result))

    failed = [r for r in results if not r.ok]
    passed_count = len(results) - len(failed)
    print(f"doctor: {passed_count}/{len(results)} checks passed.", file=sys.stderr)

    return EXIT_CHECKS_FAILED if failed else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
