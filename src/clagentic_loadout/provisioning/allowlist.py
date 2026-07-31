"""allowlist.py — per-role permission-allowlist fragment generation (lr-4e04).

Given a role and its declared verb set (roles.py), emits the permission-
allowlist fragment a harness's settings file needs so that role's agent can
invoke those verbs without hitting a permission-prompt wall.

REJECTED shape (operator directive, lr-4e04 task description): a single
FLAT GLOBAL allowlist covering every verb for every role. This module only
ever generates a PER-ROLE fragment — the caller names one role per call,
gets back exactly that role's entries, and nothing leaks in from any other
role's verb set. Coordinate note: a companion `allowlist_export` utility in
the reference deployment covers the same guard/settings-generation MECHANICS
for that deployment's own guard-hook port; this module is loadout's own
role-scoped generator for its own verbs, not a reimplementation of that
port.

Each verb the role owns contributes TWO allowlist entries — a harness
permission allowlist commonly needs both the "any arguments" wildcard form
and the bare-invocation form to match how different tool-call shapes get
recorded:

    Bash(<verb>:*)
    Bash(<verb> *)

`<verb>` here is the CONSOLE-SCRIPT command name (`loadout-push`, not the
umbrella label `push`) — that is the actual command string a spawned
agent's shell would invoke, and therefore the string a Bash-command
allowlist entry must match against.
"""

from __future__ import annotations

import json

from clagentic_loadout.provisioning.roles import KNOWN_VERBS

#: Verb label (as declared in roles.py / repo-local config `roles:`
#: section) -> the actual console-script command name installed to PATH
#: (pyproject.toml [project.scripts], docs/verbs.md). This is the SAME
#: verb-label vocabulary roles.py validates against — kept as its own
#: mapping here (rather than re-deriving from KNOWN_VERBS by string
#: manipulation) so a future verb whose console-script name does not follow
#: the "loadout-<label>" pattern is a one-line change here, not a silent
#: mismatch.
VERB_CONSOLE_SCRIPTS: dict[str, str] = {
    "push": "loadout-push",
    "review-post": "loadout-review-post",
    "merge": "loadout-merge",
    "git-host-api": "loadout-git-host-api",
    "stage-body": "loadout-stage-body",
    "release-dispatch": "loadout-release-dispatch",
    "release-detect": "loadout-release-detect",
    "poll-wait": "loadout-poll-wait",
    "scoped-test-wait": "loadout-scoped-test-wait",
    "doctor": "loadout-doctor",
}

assert set(VERB_CONSOLE_SCRIPTS) == set(KNOWN_VERBS), (
    "VERB_CONSOLE_SCRIPTS must stay in lockstep with roles.KNOWN_VERBS — "
    "a verb label known to one and not the other is a packaging bug, not "
    "a runtime config error, so this fails at import time."
)


class UnknownVerbError(ValueError):
    """Raised when a role's declared verb set contains a label with no
    known console-script mapping — should be unreachable in practice, since
    roles.load_role_verbs already validates against the same KNOWN_VERBS
    set, but this module does not trust that invariant blindly when called
    directly with a caller-supplied verb list."""


def generate_role_fragment(role: str, verbs: tuple[str, ...] | list[str]) -> list[str]:
    """Return the sorted list of Bash-allowlist entry strings for *role*'s
    declared *verbs*.

    Two entries per verb (`Bash(<console-script>:*)` and
    `Bash(<console-script> *)`), for every verb in *verbs* — nothing else.
    This function has no notion of "every verb loadout ships"; it only ever
    sees the one role's own declared set, which is what makes the
    global-fragment shape structurally unreachable through this API (a
    caller would have to explicitly pass every verb's label as this role's
    own set to get that effect — never the default/generated behavior).

    Raises:
        UnknownVerbError: *verbs* contains a label with no console-script
            mapping (see VERB_CONSOLE_SCRIPTS).
    """
    entries: list[str] = []
    for verb in verbs:
        script = VERB_CONSOLE_SCRIPTS.get(verb)
        if script is None:
            raise UnknownVerbError(
                f"role {role!r}: verb {verb!r} has no known console-script "
                f"mapping. Known verbs: {', '.join(sorted(VERB_CONSOLE_SCRIPTS))}."
            )
        entries.append(f"Bash({script}:*)")
        entries.append(f"Bash({script} *)")
    return sorted(entries)


def render_fragment_json(role: str, verbs: tuple[str, ...] | list[str]) -> str:
    """Render *role*'s allowlist fragment as a copy-pasteable JSON array
    literal (the shape a harness's `permissions.allow` list expects) — the
    default, safe, print-only output path (see cli.py)."""
    return json.dumps(generate_role_fragment(role, verbs), indent=2)


__all__ = [
    "VERB_CONSOLE_SCRIPTS",
    "UnknownVerbError",
    "generate_role_fragment",
    "render_fragment_json",
]
