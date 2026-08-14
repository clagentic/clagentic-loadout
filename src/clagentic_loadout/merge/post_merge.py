"""merge.post_merge — post-merge step executor + its config surface (lr-77d6).

Ported from the reference implementation's post-merge steps mechanism (its
merge tool's `run_post_merge_steps` + a companion `parse_cmd` helper). The
source stays primary until its own separate CUT OVER + RETIRE + VERIFY-GONE
task; this module is loadout-merge's OWN capability, not an import of the
reference's tool (CLAUDE.md rule 1 — no internal team vocabulary; this is
not a port of an internal-deployment-specific concept, it is a merge-verb
feature).

WHAT THIS RUNS: an ordered list of steps in the merged repo's project root,
each a shell-free subprocess invocation, executed ONLY after `merge.verb`'s
gate chain has ALREADY merged the PR (see verb._run step 8 — this module is
never called on any refusal path). A step is:

    cmd:          str, required   — the command to run
    description:  str, optional   — logged before execution
    on_failure:   "warn" | "fail" (default "warn")
    detaches:     bool, optional (default false) — see lr-53556a below.

`cmd` may ALSO be a list of strings (an explicit argv) instead of a shell-
quoted string — see `_resolve_argv` below. Both forms support a leading
`VAR=VALUE` env-assignment-prefix convention identical to the reference
implementation's (`env={**os.environ, **assignments}`, always `shell=False`).

THE BUG THIS PORT FIXES AT PORT TIME (lr-77d6, do not carry across): the
reference `_post_merge.parse_cmd` does `shlex.split(cmd)` then execs the
result with `shell=False`. shlex.split has no concept of shell operators —
`&&`, `||`, `|`, `;` all become ORDINARY ARGV TOKENS, so a cmd string like
"git fetch origin && git switch --detach X" runs as ONE command
(`git fetch origin && git switch --detach X` as literal argv, i.e. `git`
`fetch` `origin` `&&` `git` `switch` ...) and silently misparses — reference
repro: this ran as a single `git fetch` invocation with `&&` as a bogus
positional arg, exit 129. The fix is NOT `shell=True` (a config-sourced
string executed through a shell is an injection surface — CLAUDE.md rule 1
non-negotiable, and the design requirement on lr-77d6 explicitly forbids it).

Instead, BOTH of the following are provided (the task's "prefer both"):
  1. `_resolve_argv` REJECTS a shell-operator token (`&&`, `||`, `|`, `;`,
     `>`, `>>`, `<`) found anywhere in a plain cmd STRING, at parse time,
     with a clear PostMergeConfigError naming the offending token and the
     step — never a silent argv misparse.
  2. A step's `cmd` may instead be a LIST of strings — an explicit argv, or
     a list of "steps" each already shell-quote-free — for any caller that
     needs a genuine multi-command sequence: express it as MULTIPLE step
     dicts (each with its own on_failure), not as one shell-operator string.
     A caller that wants "fetch, then switch" writes two ordered steps.

THE lr-53556a HANG (defense-in-depth; the root fix lives in a different
repo/module entirely — this is hardening loadout's OWN executor against ANY
future daemon-spawning step, not a fix to that root cause): every step above
is run via `subprocess.run(argv, capture_output=True, ...)`, which
`communicate()`-waits for the child's stdout/stderr PIPE to reach EOF. A step
whose command spawns a long-lived daemon that inherits fds 1/2 and holds them
open (e.g. a daemon that double-forks without first closing/redirecting its
own inherited stdio) never delivers that EOF — `communicate()` blocks
forever, AFTER the merge itself already succeeded. `detaches: true` (see
`validate_post_merge_steps` and `run_post_merge_steps`) is the escape hatch: a
step so flagged is invoked fire-and-forget (`stdin`/`stdout`/`stderr` =
DEVNULL, `start_new_session=True`, never captured or awaited) instead of
through the ordinary capture-and-wait path, so no inherited pipe from THIS
step's subprocess.Popen call can ever block the merge process, regardless of
what its child does with its own fds afterward. A detached step's exit code
is never observed, so `on_failure: fail` combined with `detaches: true` is
REJECTED at validation time (see `validate_post_merge_steps`) rather than
silently ignored — "fail the merge on this step's exit code" and "never wait
for this step's exit code" are contradictory requests on the same step, and a
config author who typed both almost certainly meant one or the other, not a
silently-dropped guarantee.

THE lr-d6e52b HARDENING (bounded timeout + liveness verification -- the
"third option" between BLOCK FOREVER and VERIFY NOTHING): lr-53556a's
`detaches: true` closed the "block forever" half for a step that ITSELF
double-forks past this executor's own control -- but it left two gaps this
task closes, both OPT-IN / CONFIG-GATED so a caller that never asks for
either sees byte-identical behavior to before this feature:

  1. BOUNDED PER-STEP TIMEOUT (`timeout_seconds`, per step, and/or
     `post_merge_step_timeout_seconds`, a repo-tier default under `merge:` --
     see `merge.post_merge_config`) for the ORDINARY (non-detached, awaited)
     path: `subprocess.run(..., timeout=N)`. A step that hangs (e.g. it was
     never flagged `detaches: true` but its command forks a daemon anyway --
     exactly the config-authoring mistake this hardens against) now fails
     LOUDLY (`PostMergeStepTimeoutError`, always terminal regardless of
     `on_failure` -- see `run_post_merge_steps`) after the bound elapses,
     instead of blocking the whole merge process indefinitely. `None`
     (neither key set, the default) means NO timeout is applied --
     `subprocess.run`'s own default, unbounded wait -- because a fixed
     default the caller never chose could turn an already-passing, merely
     SLOW step into a new spurious failure the moment this ships (the
     product's own "loadout is a SHIPPED TOOL with EXTERNAL USERS,
     additive-only" posture -- see `merge.post_merge_config`'s own docstring
     for the full trade-off write-up).

  2. LIVENESS VERIFICATION (`liveness_probe`, per step, ONLY meaningful on a
     `detaches: true` step -- see `validate_post_merge_steps`) for the
     "detaches verifies nothing" half: `detaches: true` alone proves only
     that THIS process's `Popen` call returned, never that whatever daemon
     the step intended to (re)start is actually up. A configured
     `liveness_probe` polls a caller-supplied, generic ARGV command
     (`probe.cmd`) at `probe.poll_interval_seconds` and asserts its stdout
     ADVANCES across one poll interval -- the task's own, deliberately
     endorsed formulation, and the one implemented here: a fixed wall-clock
     wait either flakes on a slow restart or wastes time waiting past an
     already-live daemon, while "the probe's own reported value changed
     between two samples one interval apart" is a genuine liveness signal
     (the daemon did SOMETHING between the two samples, e.g. advanced its own
     heartbeat counter/timestamp) rather than a proxy for elapsed time. This
     module has NO built-in notion of what a "heartbeat" is, what a
     "sentinel" is, or where either lives on disk -- `probe.cmd` is an
     arbitrary argv the SAME shell-operator-free, `shell=False` resolution
     `_resolve_argv` already applies to a step's own `cmd`; a consumer wanting
     to check e.g. a heartbeat file's mtime, a `systemctl show` timestamp
     field, or an HTTP health endpoint supplies the command that reads it.
     Absent `liveness_probe` (the default), a `detaches: true` step behaves
     exactly as it did before this task: fire-and-forget, no verification at
     all -- this is an OPT-IN, additive verification layer, never a new
     requirement imposed on an existing `detaches: true` step that never
     configured one.

THE lr-4d5ef9 CADENCE FIX (see `_verify_liveness`'s own docstring for the
full write-up): the original lr-d6e52b implementation captured its baseline
sample EAGERLY, immediately after `Popen` returned, racing the daemon it
was trying to observe -- under load, or whenever the daemon's own
transition completes faster than one `poll_interval` (e.g. a
300ms-heartbeat-write racing a 500ms poll), that race
could make a live, already-advanced daemon look dead. The fix moves the
baseline sample to BEFORE the detached step's `Popen` call, establishing a
genuine happens-before relationship via program order rather than any
timing assumption about how fast the daemon transitions relative to
`poll_interval` -- `_capture_liveness_baseline` runs first, then `Popen`,
then `_verify_liveness` polls forward from that race-free baseline at the
unchanged `poll_interval`/`max_polls` budget.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

#: Tokens that only mean something to a shell — never valid inside a plain
#: argv, so their presence in a cmd STRING (not a list-form step) is refused
#: outright rather than silently misparsed into a bogus argv (the bug this
#: port fixes; see module docstring).
_SHELL_OPERATOR_TOKENS = frozenset({"&&", "||", "|", ";", ">", ">>", "<"})

_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

#: Values allowed for a step's `on_failure` key.
ON_FAILURE_WARN = "warn"
ON_FAILURE_FAIL = "fail"
_VALID_ON_FAILURE = frozenset({ON_FAILURE_WARN, ON_FAILURE_FAIL})

#: Key opting a step into fire-and-forget (detached) invocation (lr-53556a —
#: see module docstring, "THE lr-53556a HANG"). Default `False`: byte-
#: identical to this feature never having existed for every step that never
#: sets it.
STEP_KEY_DETACHES = "detaches"

#: Key (lr-d6e52b) bounding an ORDINARY (non-detached, awaited) step's
#: `subprocess.run` wait -- an int/float count of seconds, or `None`
#: (the default: no bound, unchanged wait-forever `subprocess.run` behavior).
#: See module docstring, "THE lr-d6e52b HARDENING", part 1. Meaningless (and
#: rejected at validation time -- see `validate_post_merge_steps`) on a step
#: that also sets `detaches: true`, since a detached step is never awaited at
#: all; a detaching step's OWN liveness bound is `liveness_probe` instead.
STEP_KEY_TIMEOUT_SECONDS = "timeout_seconds"

#: Key (lr-d6e52b) declaring an OPTIONAL liveness probe for a `detaches: true`
#: step -- see module docstring, "THE lr-d6e52b HARDENING", part 2, and
#: `LivenessProbe`/`_verify_liveness` below for the full contract. Absent
#: (the default) on a `detaches: true` step: fire-and-forget, unchanged from
#: before this task. Meaningless (and rejected at validation time) on a step
#: that does NOT set `detaches: true` -- an awaited step's own exit code
#: already tells the caller whether it succeeded; a liveness probe answers a
#: question only a fire-and-forget step's caller cannot otherwise answer.
STEP_KEY_LIVENESS_PROBE = "liveness_probe"

#: Sub-keys within a `liveness_probe` mapping.
LIVENESS_PROBE_KEY_CMD = "cmd"
LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS = "poll_interval_seconds"
LIVENESS_PROBE_KEY_MAX_POLLS = "max_polls"

#: Built-in default poll interval (seconds) between two liveness-probe
#: samples, when a `liveness_probe` mapping omits `poll_interval_seconds`.
#: Only takes effect for a step that already opted into `liveness_probe` at
#: all -- never applied to a step with no `liveness_probe` key.
DEFAULT_LIVENESS_POLL_INTERVAL_SECONDS = 5

#: Built-in default number of poll attempts (each `poll_interval_seconds`
#: apart) before giving up and declaring the probe stalled, when a
#: `liveness_probe` mapping omits `max_polls`. Two samples (one interval)
#: is the MINIMUM needed to observe an advance at all -- see module
#: docstring, "THE lr-d6e52b HARDENING", part 2 -- so this default allows a
#: few extra intervals for a daemon that is merely slow to start producing
#: probe output, without waiting unboundedly.
DEFAULT_LIVENESS_MAX_POLLS = 6

#: Process exit code a caller (merge.verb) uses when a `on_failure: fail`
#: step fails — reserved in the merge verb's own exit-code range so a
#: post-merge failure is distinguishable from every gate-chain refusal code.
EXIT_POST_MERGE_FAILED = 28


class PostMergeConfigError(ValueError):
    """Raised when a post_merge_steps entry is malformed: not a mapping, a
    missing/empty `cmd`, an invalid `on_failure` value, (the lr-77d6 fix) a
    plain cmd STRING containing a shell-operator token, a non-bool `detaches`
    value, (lr-53556a) `detaches: true` combined with `on_failure: fail`, or
    (lr-d6e52b) a malformed `timeout_seconds`/`liveness_probe` value, or
    either of those keys combined with the WRONG `detaches` value for what
    that key requires (see `validate_post_merge_steps`). Always raised
    BEFORE any subprocess runs — a malformed step never partially executes.
    """


class PostMergeStepFailedError(RuntimeError):
    """Raised by `run_post_merge_steps` when a step with `on_failure: fail`
    exits non-zero (or fails to parse/resolve). Carries the diagnostic
    message; the caller (merge.verb) translates this to
    EXIT_POST_MERGE_FAILED. Never raised for an `on_failure: warn` step —
    those log and continue."""


class PostMergeStepTimeoutError(RuntimeError):
    """Raised by `run_post_merge_steps` (lr-d6e52b) when an ORDINARY
    (non-detached) step exceeds its resolved `timeout_seconds` bound. ALWAYS
    terminal -- raised regardless of the step's own `on_failure` value, never
    downgraded to a warn-and-continue: a step that hung past its own declared
    bound has demonstrated it is not behaving as configured, which is exactly
    the class of surprise `on_failure: warn` is not meant to paper over (an
    ordinary non-zero exit is an expected, plannable outcome a config author
    can choose to warn on; an unbounded hang is not). The caller (merge.verb)
    translates this to EXIT_POST_MERGE_FAILED, the same as
    PostMergeStepFailedError."""


class PostMergeLivenessError(RuntimeError):
    """Raised by `run_post_merge_steps` (lr-d6e52b) when a `detaches: true`
    step's configured `liveness_probe` fails to observe an advance across
    ANY consecutive poll pair within `max_polls` samples -- the daemon the
    step intended to (re)start never produced evidence of being alive. ALWAYS
    terminal, for the same reason as PostMergeStepTimeoutError: an unmet
    liveness assertion is not an ordinary command failure `on_failure: warn`
    was designed to tolerate. The caller (merge.verb) translates this to
    EXIT_POST_MERGE_FAILED."""


def _split_env_assignments(tokens: list[str]) -> tuple[list[str], dict[str, str]]:
    """Strip leading `VAR=VALUE` tokens from *tokens*, returning the
    remaining argv and the stripped assignments as a dict. Mirrors the
    reference implementation's env-assignment-prefix support exactly (kept
    at port time per the task's explicit requirement) — callers pass the
    result to `subprocess.run(..., env={**os.environ, **assignments},
    shell=False)`.
    """
    remaining = list(tokens)
    assignments: dict[str, str] = {}
    while remaining and _ENV_ASSIGN_RE.match(remaining[0]):
        key, _, value = remaining.pop(0).partition("=")
        assignments[key] = value
    return remaining, assignments


def _resolve_argv(cmd: str | list, *, step_label: str) -> tuple[list[str], dict[str, str]]:
    """Resolve a step's `cmd` value to (argv, env_assignments).

    `cmd` may be:
      - a shell-quoted STRING (shlex.split, same as the reference
        implementation) — REJECTED with PostMergeConfigError if it contains
        a shell-operator token (lr-77d6 fix; see module docstring). This is
        a config-validation-time error, raised before any subprocess is
        built.
      - a LIST of strings — an explicit argv, no shlex parsing, no shell-
        operator ambiguity possible by construction (this is the "list-form"
        half of the lr-77d6 fix: a caller with a genuine multi-token command
        can hand loadout-merge the exact argv it wants run).

    In both forms, a leading `VAR=VALUE` token (or tokens) is stripped as an
    env-assignment prefix (see `_split_env_assignments`).
    """
    if isinstance(cmd, list):
        if not cmd:
            raise PostMergeConfigError(f"{step_label}: cmd list must not be empty.")
        if not all(isinstance(tok, str) for tok in cmd):
            raise PostMergeConfigError(
                f"{step_label}: cmd list entries must all be strings, got {cmd!r}."
            )
        return _split_env_assignments(list(cmd))

    if not isinstance(cmd, str):
        raise PostMergeConfigError(
            f"{step_label}: cmd must be a string or a list of strings, got "
            f"{type(cmd).__name__}."
        )
    if not cmd.strip():
        raise PostMergeConfigError(f"{step_label}: cmd must not be empty.")

    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        raise PostMergeConfigError(
            f"{step_label}: malformed command (shell-quoting parse error): {exc}."
        ) from exc

    shell_ops_found = _SHELL_OPERATOR_TOKENS.intersection(tokens)
    if shell_ops_found:
        raise PostMergeConfigError(
            f"{step_label}: cmd string {cmd!r} contains shell operator token(s) "
            f"{sorted(shell_ops_found)!r}. Shell operators are never executed "
            f"as shell syntax here (shell=False, always) -- they would silently "
            f"become literal, meaningless argv tokens instead. Express a "
            f"multi-command sequence as SEPARATE ordered post_merge_steps "
            f"entries, or pass cmd as a list of strings (an explicit argv) "
            f"instead of a shell-quoted string."
        )

    if not tokens:
        raise PostMergeConfigError(
            f"{step_label}: cmd {cmd!r} parsed to an empty argv after "
            f"shell-quote splitting."
        )

    return _split_env_assignments(tokens)


def _validate_timeout_seconds(value, *, step_label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_TIMEOUT_SECONDS!r} must be an int or "
            f"float number of seconds, got {value!r}."
        )
    if value <= 0:
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_TIMEOUT_SECONDS!r} must be > 0, got "
            f"{value!r}."
        )


def _validate_liveness_probe(value, *, step_label: str) -> None:
    if not isinstance(value, dict):
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r} must be a mapping, "
            f"got {type(value).__name__}."
        )
    if LIVENESS_PROBE_KEY_CMD not in value:
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r} missing required key "
            f"{LIVENESS_PROBE_KEY_CMD!r}."
        )
    # The probe's own cmd goes through the SAME argv resolver a step's own
    # cmd does -- an arbitrary, shell-operator-free argv, generic by
    # construction (see module docstring, "THE lr-d6e52b HARDENING", part 2).
    _resolve_argv(
        value[LIVENESS_PROBE_KEY_CMD],
        step_label=f"{step_label}.{STEP_KEY_LIVENESS_PROBE}",
    )
    poll_interval = value.get(
        LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS, DEFAULT_LIVENESS_POLL_INTERVAL_SECONDS
    )
    if isinstance(poll_interval, bool) or not isinstance(poll_interval, (int, float)):
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r}."
            f"{LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS!r} must be an int or "
            f"float number of seconds, got {poll_interval!r}."
        )
    if poll_interval <= 0:
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r}."
            f"{LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS!r} must be > 0, got "
            f"{poll_interval!r}."
        )
    max_polls = value.get(LIVENESS_PROBE_KEY_MAX_POLLS, DEFAULT_LIVENESS_MAX_POLLS)
    if isinstance(max_polls, bool) or not isinstance(max_polls, int):
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r}."
            f"{LIVENESS_PROBE_KEY_MAX_POLLS!r} must be an int, got {max_polls!r}."
        )
    if max_polls < 2:
        raise PostMergeConfigError(
            f"{step_label}: {STEP_KEY_LIVENESS_PROBE!r}."
            f"{LIVENESS_PROBE_KEY_MAX_POLLS!r} must be >= 2 -- observing an "
            f"advance requires at least two samples one poll interval apart, "
            f"so a single poll can never be mistaken for a liveness signal; "
            f"got {max_polls!r}."
        )


def validate_post_merge_steps(steps: list) -> None:
    """Validate an entire post_merge_steps list at config-load time, before
    any step runs. Raises PostMergeConfigError on the first malformed entry
    (missing/malformed cmd, shell-operator token, invalid on_failure,
    non-bool detaches, detaches+on_failure:fail combined — lr-53556a, or
    (lr-d6e52b) a malformed `timeout_seconds`/`liveness_probe`, or either
    combined with the wrong `detaches` value for what it requires) — never
    lets a config-shape defect surface only mid-run after earlier steps
    already executed."""
    if not isinstance(steps, list):
        raise PostMergeConfigError(
            f"post_merge_steps must be a list, got {type(steps).__name__}."
        )
    for i, step in enumerate(steps):
        label = f"post_merge_steps[{i}]"
        if not isinstance(step, dict):
            raise PostMergeConfigError(f"{label}: must be a mapping, got {step!r}.")
        if "cmd" not in step:
            raise PostMergeConfigError(f"{label}: missing required key 'cmd'.")
        _resolve_argv(step["cmd"], step_label=label)
        on_failure = step.get("on_failure", ON_FAILURE_WARN)
        if on_failure not in _VALID_ON_FAILURE:
            raise PostMergeConfigError(
                f"{label}: on_failure must be one of {sorted(_VALID_ON_FAILURE)!r}, "
                f"got {on_failure!r}."
            )
        detaches = step.get(STEP_KEY_DETACHES, False)
        if not isinstance(detaches, bool):
            raise PostMergeConfigError(
                f"{label}: {STEP_KEY_DETACHES!r} must be a bool, got "
                f"{type(detaches).__name__}."
            )
        if detaches and on_failure == ON_FAILURE_FAIL:
            raise PostMergeConfigError(
                f"{label}: {STEP_KEY_DETACHES!r}: true is incompatible with "
                f"on_failure: {ON_FAILURE_FAIL!r} — a detached step's exit "
                f"code is never awaited, so there is no exit code for "
                f"on_failure: {ON_FAILURE_FAIL!r} to gate on. Use "
                f"on_failure: {ON_FAILURE_WARN!r} (the default) for a "
                f"detached step, or drop {STEP_KEY_DETACHES!r} if this step "
                f"must actually be awaited."
            )

        if STEP_KEY_TIMEOUT_SECONDS in step:
            if detaches:
                raise PostMergeConfigError(
                    f"{label}: {STEP_KEY_TIMEOUT_SECONDS!r} is incompatible "
                    f"with {STEP_KEY_DETACHES!r}: true -- a detached step is "
                    f"never awaited, so there is no wait for a timeout to "
                    f"bound. Use {STEP_KEY_LIVENESS_PROBE!r} to verify a "
                    f"detached step instead."
                )
            _validate_timeout_seconds(step[STEP_KEY_TIMEOUT_SECONDS], step_label=label)

        if STEP_KEY_LIVENESS_PROBE in step:
            if not detaches:
                raise PostMergeConfigError(
                    f"{label}: {STEP_KEY_LIVENESS_PROBE!r} requires "
                    f"{STEP_KEY_DETACHES!r}: true -- an awaited step's own "
                    f"exit code already reports success/failure; a liveness "
                    f"probe exists to answer that question for a "
                    f"fire-and-forget step, which this one is not."
                )
            _validate_liveness_probe(step[STEP_KEY_LIVENESS_PROBE], step_label=label)


def _run_liveness_probe_once(argv: list[str], *, cwd: str) -> str:
    """Run a liveness-probe argv once, returning its stripped stdout (empty
    string on any non-zero exit or on stderr-only output -- a probe that
    cannot currently report a value is treated as "no signal yet", the same
    as a probe that has not been polled at all, never as an error that
    aborts the whole run early). `shell=False` always -- the probe's cmd was
    already resolved through the SAME shell-operator-free `_resolve_argv` a
    step's own cmd goes through."""
    result = subprocess.run(
        argv,
        shell=False,
        capture_output=True,
        text=True,
        cwd=cwd,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _capture_liveness_baseline(probe_config: dict, *, cwd: str, label: str) -> str:
    """Sample `probe_config['cmd']` ONCE and return its value, to be used as
    the pre-launch baseline `_verify_liveness` compares later samples
    against (see that function's docstring for why this must happen BEFORE
    the detached step's own `Popen` call, not after -- lr-4d5ef9).
    """
    probe_argv, _probe_env_overrides = _resolve_argv(
        probe_config[LIVENESS_PROBE_KEY_CMD], step_label=f"{label}.{STEP_KEY_LIVENESS_PROBE}"
    )
    return _run_liveness_probe_once(probe_argv, cwd=cwd)


def _verify_liveness(
    probe_config: dict,
    *,
    cwd: str,
    label: str,
    cmd_repr: str,
    baseline_sample: str,
) -> None:
    """Poll `probe_config['cmd']` and assert its reported value ADVANCES
    away from *baseline_sample*, within `max_polls` samples total (see
    module docstring, "THE lr-d6e52b HARDENING", part 2, and
    `_validate_liveness_probe` for the config shape/defaults).

    "Advances" means: a polled sample is non-empty, *baseline_sample* is
    non-empty, and they DIFFER. An empty polled sample (probe not yet
    reporting -- e.g. a heartbeat file that does not exist until the daemon
    finishes starting) never counts as a match to compare against; a daemon
    that is merely slow to start still gets the full `max_polls` budget to
    begin reporting. An empty *baseline_sample* (the probe's target did not
    exist yet even before the daemon was launched -- e.g. a heartbeat file
    first created by the daemon itself) means ANY non-empty polled sample
    counts as an advance, since there is no earlier state to have missed.

    THE lr-4d5ef9 CADENCE FIX: the original lr-d6e52b formulation captured
    its baseline sample ITSELF, eagerly, immediately after the caller's
    `Popen` call returned -- racing the very daemon it was trying to
    observe. Under load, or whenever the daemon's own transition completes
    faster than one `poll_interval` (e.g. a 300ms heartbeat write racing a
    500ms poll interval), that eager sample can land AFTER the daemon has
    already reached its final state, collapsing every later
    sample to the same value with nothing earlier to compare against -- a
    live, already-advanced daemon reported dead. There is no poll cadence
    that can fix this by adjusting WHEN the baseline is taken relative to
    `Popen`, because there is no assumption this module is allowed to make
    about how fast the daemon's own transition is relative to
    `poll_interval` (that assumption -- "the daemon is slower than the
    probe polls" -- IS the defect). The correct synchronization fix is
    instead to never race the baseline against the daemon AT ALL: the
    caller (`run_post_merge_steps`) captures *baseline_sample* via
    `_capture_liveness_baseline` BEFORE the detached step's `Popen` call is
    even made, establishing a genuine happens-before relationship (program
    order, not wall-clock racing) between "the baseline was read" and "the
    daemon could have done anything." This function then only ever POLLS
    forward from there, at the caller's own `poll_interval`/`max_polls`
    budget -- unchanged from before -- comparing each new sample against
    that race-free baseline.

    Raises PostMergeLivenessError if no advancing sample is observed within
    the budget -- the caller (`run_post_merge_steps`) treats this as ALWAYS
    terminal, regardless of the step's own `on_failure` (see
    PostMergeLivenessError's own docstring).
    """
    probe_argv, _probe_env_overrides = _resolve_argv(
        probe_config[LIVENESS_PROBE_KEY_CMD], step_label=f"{label}.{STEP_KEY_LIVENESS_PROBE}"
    )
    poll_interval = probe_config.get(
        LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS, DEFAULT_LIVENESS_POLL_INTERVAL_SECONDS
    )
    max_polls = probe_config.get(LIVENESS_PROBE_KEY_MAX_POLLS, DEFAULT_LIVENESS_MAX_POLLS)

    for poll_index in range(1, max_polls):
        time.sleep(poll_interval)
        current_sample = _run_liveness_probe_once(probe_argv, cwd=cwd)
        if baseline_sample and current_sample and current_sample != baseline_sample:
            print(
                f"merge: post-merge {label}: liveness CONFIRMED for {cmd_repr} "
                f"-- probe {probe_argv!r} advanced ({baseline_sample!r} -> "
                f"{current_sample!r}) after {poll_index} poll interval(s)",
                file=sys.stderr,
            )
            return

    raise PostMergeLivenessError(
        f"post-merge {label} liveness check FAILED for {cmd_repr} -- probe "
        f"{probe_argv!r} never reported an advancing value (baseline "
        f"{baseline_sample!r}) across {max_polls} sample(s) ({poll_interval}s "
        f"apart). The detached process was launched, but nothing confirms "
        f"the daemon it was meant to (re)start is actually alive."
    )


def run_post_merge_steps(
    steps: list[dict],
    project_root: str | Path,
    *,
    deployment_env_overrides: dict[str, str] | None = None,
    default_timeout_seconds: int | float | None = None,
) -> None:
    """Execute post_merge_steps IN ORDER inside *project_root*.

    Called ONLY after a successful merge (merge.verb._run step 8 calls this
    after `backend.merge_pr` succeeds) — never on any gate-refusal or
    merge-failure path.

    Each step:
        cmd             (str | list[str], required) — see `_resolve_argv`.
        description     (str, optional)             — logged before
                                                        execution.
        on_failure      (str, optional)              — "warn" (default) or
                                                        "fail".
        detaches        (bool, optional)             — "false" (default) or
                                                        "true"; see below.
        timeout_seconds (int | float, optional, lr-d6e52b) — bounds an
                                                        ORDINARY (non-
                                                        detached) step's
                                                        wait; see below.
                                                        Rejected combined
                                                        with `detaches: true`.
        liveness_probe  (dict, optional, lr-d6e52b)  — verifies a `detaches:
                                                        true` step's daemon
                                                        actually came up; see
                                                        below. Rejected on a
                                                        non-detached step.

    on_failure="warn": log the error/non-zero exit and continue to the next
        step.
    on_failure="fail": raise PostMergeStepFailedError immediately — the
        caller translates this to EXIT_POST_MERGE_FAILED. No further steps
        run.

    EVERY ordinary (non-detached) step emits an explicit PASS or FAIL line
    to stderr, on success as well as failure, carrying the raw exit code and
    the RESOLVED cwd the step actually executed in (lr-843900): a gate that
    is silent on success is indistinguishable from a gate that never ran at
    all, and a repo-relative step command's correctness depends entirely on
    which directory it executed in — see merge.pre_checks_config's own
    module docstring for the concrete failure mode this closes (a repo-
    relative validator that no-ops and exits 0 when run from the wrong cwd).

    A step whose process never launches at all (its binary is missing from
    PATH, or present but not executable — `FileNotFoundError`/
    `PermissionError`, both `OSError` subclasses) is treated identically to
    a step that launched and exited non-zero: `on_failure="warn"` logs and
    continues, `on_failure="fail"` raises `PostMergeStepFailedError`. A
    launch failure is never allowed to propagate as a raw, uncaught
    exception — from the caller's perspective it is indistinguishable from
    an ordinary non-zero exit.

    detaches=true (lr-53556a — see module docstring, "THE lr-53556a HANG"):
        the step is launched fire-and-forget via `subprocess.Popen` with
        `stdin`/`stdout`/`stderr` all redirected to `DEVNULL` and
        `start_new_session=True` (the child gets its own session/process
        group, fully severed from this process's own stdio and process
        group) — NEVER `capture_output=True`, and its exit is never awaited
        (no `.wait()`/`.communicate()` call at all). This is for a step that
        intentionally spawns a long-lived daemon: nothing about the parent
        blocks on whatever that daemon does with its own fds afterward. A
        detached step's exit code is consequently never observed, so it is
        always logged as "detached (not awaited)" and `on_failure` is not
        consulted for it — `validate_post_merge_steps` rejects
        `detaches: true` combined with `on_failure: fail` up front (see that
        function's docstring), so this executor never has to decide what a
        contradictory combination would even mean at run time.

    timeout_seconds (lr-d6e52b — see module docstring, "THE lr-d6e52b
        HARDENING", part 1): only meaningful for an ORDINARY (non-detached)
        step. The resolved bound is this step's own `timeout_seconds` if
        set, else *default_timeout_seconds* (the repo-tier
        `post_merge_step_timeout_seconds` default -- see
        `merge.post_merge_config.resolve_post_merge_step_timeout_seconds`),
        else `None` (no bound, `subprocess.run`'s own unbounded wait --
        byte-identical to this feature never existing). On expiry, raises
        PostMergeStepTimeoutError -- ALWAYS terminal, regardless of
        `on_failure` (see that error's own docstring for why).

    liveness_probe (lr-d6e52b — see module docstring, "THE lr-d6e52b
        HARDENING", part 2, and `_verify_liveness`): only meaningful, and
        only consulted, for a `detaches: true` step. When present, runs
        AFTER the fire-and-forget `Popen` call returns, polling the probe's
        own `cmd` and asserting its reported value advances across at least
        one consecutive poll pair within the probe's `max_polls` budget. On
        failure, raises PostMergeLivenessError -- ALWAYS terminal, same
        reasoning as PostMergeStepTimeoutError. Absent (the default): no
        change from before this task -- fire-and-forget, unverified.

    `deployment_env_overrides` (lr-52d7, see `merge.post_merge_config.
    resolve_env_overrides` for the full seam design): an OPTIONAL mapping
    applied to EVERY step's subprocess environment, layered UNDER that
    step's own `VAR=VALUE` prefix (a step's explicit, repo-local,
    committed-config assignment always wins over a deployment-wide default
    for the same name). This is the supported channel for a deployment to
    inject a machine-specific value (e.g. `HOME` in an isolated-HOME spawn
    harness) WITHOUT hardcoding it into the repo's own committed
    `.clagentic/loadout/config.yaml` (CLAUDE.md rule 1). `None`/`{}` (the
    default) is
    byte-identical to this parameter never having existed: every step's
    resulting env is exactly `{**os.environ, **step_assignments}` as before
    this feature, whenever `deployment_env_overrides` contributes nothing.

    `default_timeout_seconds` (lr-d6e52b, see `merge.post_merge_config.
    resolve_post_merge_step_timeout_seconds` for the repo-tier config key
    this mirrors): an OPTIONAL repo-wide fallback bound applied to any
    ORDINARY step that does not set its own `timeout_seconds`. `None` (the
    default) is byte-identical to this parameter never having existed: every
    step with no `timeout_seconds` of its own keeps `subprocess.run`'s
    unbounded wait, exactly as before this feature.

    The whole list is validated (`validate_post_merge_steps`) BEFORE any
    step executes, so a malformed step later in the list is caught up front
    rather than after earlier steps already ran with side effects.
    """
    validate_post_merge_steps(steps)
    root = str(project_root)
    active_deployment_overrides = deployment_env_overrides or {}

    for i, step in enumerate(steps):
        cmd = step["cmd"]
        description = step.get("description", "")
        on_failure = step.get("on_failure", ON_FAILURE_WARN)
        detaches = step.get(STEP_KEY_DETACHES, False)
        label = f"step {i + 1}"

        if description:
            print(f"merge: post-merge {label}: {description}", file=sys.stderr)
        print(
            f"merge: post-merge {label}: running: {cmd!r} (cwd={root!r})",
            file=sys.stderr,
        )

        argv, env_overrides = _resolve_argv(cmd, step_label=f"post_merge_steps[{i}]")
        combined_overrides = {**active_deployment_overrides, **env_overrides}
        step_env = {**os.environ, **combined_overrides} if combined_overrides else None

        if detaches:
            liveness_probe = step.get(STEP_KEY_LIVENESS_PROBE)
            baseline_sample = None
            if liveness_probe is not None:
                # lr-4d5ef9: capture the liveness baseline BEFORE launching
                # the detached step -- see _verify_liveness's own docstring,
                # "THE lr-4d5ef9 CADENCE FIX", for why this ordering (a
                # genuine happens-before via program order) is what removes
                # the race, rather than any adjustment to poll timing after
                # the fact.
                baseline_sample = _capture_liveness_baseline(
                    liveness_probe, cwd=root, label=label
                )

            # lr-53556a: fire-and-forget. No PIPE is ever created for this
            # child, so there is no fd for an inherited-open grandchild
            # daemon to hold open against us -- and start_new_session=True
            # severs the child into its own session/process group so it is
            # never a member of this process's own group either. Never
            # .wait()/.communicate() -- awaiting defeats the entire point.
            subprocess.Popen(
                argv,
                shell=False,
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                **({"env": step_env} if step_env is not None else {}),
            )
            print(
                f"merge: post-merge {label}: detached (not awaited): {cmd!r}",
                file=sys.stderr,
            )
            if liveness_probe is not None:
                # lr-d6e52b: independent liveness verification -- ALWAYS
                # terminal on failure (see PostMergeLivenessError), never
                # gated by this step's own on_failure (which validate_
                # post_merge_steps already forbids being "fail" for a
                # detached step in the first place).
                _verify_liveness(
                    liveness_probe,
                    cwd=root,
                    label=label,
                    cmd_repr=repr(cmd),
                    baseline_sample=baseline_sample,
                )
            continue

        resolved_timeout = step.get(STEP_KEY_TIMEOUT_SECONDS, default_timeout_seconds)
        try:
            result = subprocess.run(
                argv,
                shell=False,
                capture_output=True,
                text=True,
                cwd=root,
                timeout=resolved_timeout,
                **({"env": step_env} if step_env is not None else {}),
            )
        except subprocess.TimeoutExpired as exc:
            # lr-d6e52b: ALWAYS terminal, regardless of this step's own
            # on_failure -- see PostMergeStepTimeoutError's own docstring for
            # why a hang is never eligible for warn-and-continue.
            raise PostMergeStepTimeoutError(
                f"post-merge {label} TIMED OUT after {resolved_timeout}s "
                f"(never returned): {cmd!r}. This step was not flagged "
                f"detaches: true -- if it intentionally spawns a long-lived "
                f"daemon, either flag it detaches: true (with an optional "
                f"liveness_probe to verify the daemon actually came up) or "
                f"raise its timeout_seconds."
            ) from exc
        except OSError as exc:
            # The process never launched at all -- e.g. the binary is not on
            # PATH (FileNotFoundError) or is present but not executable
            # (PermissionError). Both are OSError subclasses. Without this
            # branch, either propagates straight out of run_post_merge_steps
            # as a raw traceback, bypassing on_failure entirely -- a launch
            # failure must be indistinguishable, from the caller's
            # perspective, from the step exiting non-zero (see module
            # docstring / the on_failure contract above).
            msg = f"post-merge {label} failed to launch ({exc}): {argv[0]!r}"
            if on_failure == ON_FAILURE_FAIL:
                raise PostMergeStepFailedError(msg) from exc
            print(
                f"merge: post-merge {label}: warning: {msg}, continuing",
                file=sys.stderr,
            )
            continue

        if result.stdout.strip():
            print(f"merge: post-merge {label}: stdout: {result.stdout.strip()}", file=sys.stderr)
        if result.stderr.strip():
            print(f"merge: post-merge {label}: stderr: {result.stderr.strip()}", file=sys.stderr)

        if result.returncode != 0:
            msg = f"post-merge {label} failed (exit {result.returncode}): {cmd!r}"
            print(
                f"merge: post-merge {label}: FAIL (exit={result.returncode}, "
                f"cwd={root!r}): {cmd!r}",
                file=sys.stderr,
            )
            if on_failure == ON_FAILURE_FAIL:
                raise PostMergeStepFailedError(msg)
            print(
                f"merge: post-merge {label}: warning: command exited "
                f"{result.returncode}, continuing",
                file=sys.stderr,
            )
        else:
            print(
                f"merge: post-merge {label}: PASS (exit=0, cwd={root!r}): {cmd!r}",
                file=sys.stderr,
            )


__all__ = [
    "DEFAULT_LIVENESS_MAX_POLLS",
    "DEFAULT_LIVENESS_POLL_INTERVAL_SECONDS",
    "EXIT_POST_MERGE_FAILED",
    "LIVENESS_PROBE_KEY_CMD",
    "LIVENESS_PROBE_KEY_MAX_POLLS",
    "LIVENESS_PROBE_KEY_POLL_INTERVAL_SECONDS",
    "ON_FAILURE_FAIL",
    "ON_FAILURE_WARN",
    "STEP_KEY_DETACHES",
    "STEP_KEY_LIVENESS_PROBE",
    "STEP_KEY_TIMEOUT_SECONDS",
    "PostMergeConfigError",
    "PostMergeLivenessError",
    "PostMergeStepFailedError",
    "PostMergeStepTimeoutError",
    "run_post_merge_steps",
    "validate_post_merge_steps",
]
