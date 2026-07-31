"""cli.py — the `clagentic-loadout` umbrella CLI: real verb dispatch.

lr-0b45 (install slice, gate for cutover-to-test). Before this, main()
ignored argv unconditionally (lr-5bf2 Slice 0 stub). This module now routes
`clagentic-loadout <verb> [<subverb>] ...` to each verb's own existing
argv-parsing entry point — it does not re-implement any verb's argument
handling, credential resolution, or gate logic; it is a thin registry +
dispatch layer only.

Reserved exit-code range for THIS module's own failures (never a child
verb's exit code, which is always passed through unchanged so scripts
depending on a verb's documented exit-code contract keep working whether
invoked via its own console script or via `clagentic-loadout <verb>`):

    0   OK (--help/--version handled, or dispatch delegated + verb returned 0)
    64  Unknown verb / no verb given (EX_USAGE, sysexits.h convention — chosen
        deliberately OUTSIDE every registered verb's own 0-27 range, per each
        verb module's own "Exit codes" table, so an umbrella-level usage
        error is never mistaken for a verb-level one by exit code alone).

A child verb's own exit code (e.g. push's EXIT_TOKEN_FETCH_FAILED = 2) is
returned to the OS completely unchanged — this module never remaps or
reinterprets it.
"""

from __future__ import annotations

import sys
from typing import Callable

from clagentic_loadout._version import get_version

#: Reserved for this module's own usage failures — see module docstring.
EXIT_UMBRELLA_USAGE = 64

_PROG = "clagentic-loadout"


def _push_main(argv: list[str]) -> int:
    from clagentic_loadout.push.verb import main as push_main

    return push_main(argv)


def _review_post_main(argv: list[str]) -> int:
    from clagentic_loadout.review.verb import main as review_main

    return review_main(argv)


def _merge_main(argv: list[str]) -> int:
    from clagentic_loadout.merge.verb import main as merge_main

    return merge_main(argv)


def _git_host_api_main(argv: list[str]) -> int:
    from clagentic_loadout.transport.git_host_api import main as git_host_api_main

    return git_host_api_main(argv)


def _stage_body_main(argv: list[str]) -> int:
    from clagentic_loadout.transport.stage_body_verb import main as stage_body_main

    return stage_body_main(argv)


def _release_dispatch_main(argv: list[str]) -> int:
    from clagentic_loadout.release.dispatch import main as release_dispatch_main

    return release_dispatch_main(argv)


def _release_detect_main(argv: list[str]) -> int:
    from clagentic_loadout.release.detector import main as release_detect_main

    return release_detect_main(argv)


def _wait_poll_main(argv: list[str]) -> int:
    from clagentic_loadout.wait.cli import poll_wait_main

    # poll_wait_main/scoped_test_wait_main call sys.exit() themselves and
    # return None (their console_scripts contract predates this umbrella —
    # see wait/cli.py's module docstring) rather than returning an int like
    # every other verb here. SystemExit is caught at the single dispatch
    # call site in main() below so both calling conventions terminate the
    # same way through this umbrella.
    poll_wait_main(argv)
    return 0  # unreachable in practice — poll_wait_main always calls sys.exit


def _wait_scoped_test_main(argv: list[str]) -> int:
    from clagentic_loadout.wait.cli import scoped_test_wait_main

    scoped_test_wait_main(argv)
    return 0  # unreachable in practice — scoped_test_wait_main always calls sys.exit


def _provision_allowlist_main(argv: list[str]) -> int:
    from clagentic_loadout.provisioning.cli import main as provision_allowlist_main

    return provision_allowlist_main(argv)


def _doctor_main(argv: list[str]) -> int:
    from clagentic_loadout.doctor.cli import main as doctor_main

    return doctor_main(argv)


#: verb name -> (one-line summary, entry-point callable taking the verb's
#: own argv and returning its process exit code). A verb with a
#: single-word name (push, merge) dispatches directly; a verb with a
#: subcommand (release dispatch, release detect, wait poll, wait
#: scoped-test) is registered under "release" / "wait" and demuxes on its
#: own next argv token below.
_SIMPLE_VERBS: dict[str, tuple[str, Callable[[list[str]], int]]] = {
    "push": ("Push a branch, open or update a PR.", _push_main),
    "review": ("Post one review comment and verify it landed.", _review_post_main),
    "merge": ("Run the merge-gate chain, then merge a PR.", _merge_main),
    "git-host-api": ("Make one authenticated Forgejo REST call.", _git_host_api_main),
    "stage-body": (
        "Stage a --body-env body + identity stamp (write side).",
        _stage_body_main,
    ),
    "provision-allowlist": (
        "Emit a role's Bash permission-allowlist fragment.",
        _provision_allowlist_main,
    ),
    "doctor": (
        "Run the deployment-conformance check suite (read-only).",
        _doctor_main,
    ),
}

#: Parent verb name -> {subverb name: (summary, callable)}.
_GROUPED_VERBS: dict[str, dict[str, tuple[str, Callable[[list[str]], int]]]] = {
    "release": {
        "dispatch": ("Fire a signed 'task shipped' release event.", _release_dispatch_main),
        "detect": ("Scan a v*-tag commit range and dispatch per-task.", _release_detect_main),
    },
    "wait": {
        "poll": ("Poll a file for a line-count/grep condition.", _wait_poll_main),
        "scoped-test": ("Run a scoped test command with a timeout.", _wait_scoped_test_main),
    },
}


def _iter_all_verb_labels() -> list[str]:
    labels = list(_SIMPLE_VERBS)
    for parent, children in _GROUPED_VERBS.items():
        labels.extend(f"{parent} {child}" for child in children)
    return sorted(labels)


def _print_help() -> None:
    lines = [
        f"{_PROG} — role-scoped agent tooling: verbs, attested identity, and",
        "merge-gate enforcement for autonomous coding agents.",
        "",
        f"Usage: {_PROG} <verb> [<subverb>] [args...]",
        f"       {_PROG} --help | --version",
        "",
        "Verbs:",
    ]
    for name, (summary, _fn) in sorted(_SIMPLE_VERBS.items()):
        lines.append(f"  {name:<24} {summary}")
    for parent, children in sorted(_GROUPED_VERBS.items()):
        for child, (summary, _fn) in sorted(children.items()):
            label = f"{parent} {child}"
            lines.append(f"  {label:<24} {summary}")
    lines.append("")
    lines.append(f"Run `{_PROG} <verb> --help` for a verb's own arguments.")
    print("\n".join(lines))


def _resolve_dispatch(argv: list[str]) -> tuple[Callable[[list[str]], int], list[str]] | None:
    """Resolve *argv* to (entry_point_callable, remaining_argv), or None if
    *argv* does not name a registered verb (including the empty-argv and
    unknown-verb cases)."""
    if not argv:
        return None
    verb = argv[0]
    if verb in _SIMPLE_VERBS:
        return _SIMPLE_VERBS[verb][1], argv[1:]
    if verb in _GROUPED_VERBS:
        children = _GROUPED_VERBS[verb]
        if len(argv) >= 2 and argv[1] in children:
            return children[argv[1]][1], argv[2:]
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint for the `clagentic-loadout` console script.

    Returns the process exit code (does not call sys.exit itself, except
    when delegating to a verb whose own entry point calls sys.exit directly
    — see the `_wait_*` wrappers above — in which case that SystemExit is
    allowed to propagate unchanged, matching how that verb behaves when
    invoked via its own standalone console script).
    """
    if argv is None:
        argv = sys.argv[1:]

    if not argv or argv[0] in ("--help", "-h"):
        _print_help()
        return 0

    if argv[0] == "--version":
        print(f"{_PROG} {get_version()}")
        return 0

    resolved = _resolve_dispatch(argv)
    if resolved is None:
        verb_label = " ".join(argv[:2]) if len(argv) >= 2 else argv[0]
        print(
            f"{_PROG}: unknown verb {verb_label!r}. Known verbs: "
            f"{', '.join(_iter_all_verb_labels())}. Run `{_PROG} --help` for details.",
            file=sys.stderr,
        )
        return EXIT_UMBRELLA_USAGE

    entry_point, remaining_argv = resolved
    return entry_point(remaining_argv)


if __name__ == "__main__":
    sys.exit(main())
