"""guard.director_mutation — lead/director authority-surface checkers
(lr-1cc4df, sub-slice SE3 of the guard-bash per-role checker port; sub-epic
lr-19ae42, grand-epic lr-5a8d Wave C).

THIS IS THE HIGHEST-AUTHORITY-SENSITIVITY SURFACE IN THE ENTIRE GUARD PORT
(lr-19ae42 comment #2): a mis-collapse here changes what a lead/director
identity can mutate. See `guard-policy.md`'s Slice-2 role-keyed port
convention and Slice-5 "Post-landing hardening" sections for the two
MANDATORY acceptance criteria every bare-verb affirmative grant in this
module must satisfy (both are wired in below — see each function's
docstring for exactly where).

WHY A SEPARATE MODULE FROM `role_allowlist.py` (no-god-file, CLAUDE.md):
`role_allowlist.py` is already >1600 lines after SE1/SE2. This slice's two
reference functions total ~1071 lines on their own — folding them into the
same file would make it a god file. `role_allowlist.BashRole.LEAD` and
`role_allowlist.check_lead_command` (added by this slice) compose this
module's functions exactly as `role_allowlist` already composes
`shell_parsing`/`bash_admission`/`scratch_policy` — this module performs no
parsing or scratch-containment of its own, reusing those primitives
directly.

PORT PATTERN (mirrors `guard.write_scope`/`guard.role_allowlist`'s own
documented convention — see those modules' docstrings for the full
five-point convention; the short version applied here):

1. STRIP THE HOOK SHELL. Every function here is a pure function over an
   explicit command string plus caller-supplied config — no stdin read, no
   process exit, no hook-contract awareness (CLAUDE.md rule 6a).
2. IDENTITY -> ROLE. The reference's `_check_director_clagentic` and
   `_check_director_lead_mutation` both take an `identity: str` parameter
   that is, in the reference, one of a small set of internal agent names
   (a "director" identity, or one of several named "lead" identities
   sharing a common suffix convention). CLAUDE.md rule 1 forbids any of
   those names surviving the port. This module's functions take an
   `identity_label: str` parameter instead — an OPAQUE display string a
   caller supplies for use ONLY in denial-message text (e.g. the caller's
   own role name, "lead", or a caller-chosen session label) — never
   compared against, branched on, or used to select behavior. Every
   reference identity this checker fires for collapses into ONE role,
   `BashRole.LEAD` (added to `role_allowlist.BashRole` by this slice) — see
   "FLAGGED LOSSY COLLAPSE POINTS" below for the one place this collapse
   needed explicit scrutiny.
3. NO HARDCODED MACHINE/OPERATOR PATHS OR TRANSPORT-VERB NAMES. Every
   reference literal that names a fixed script path, forge hostname, or a
   specific harness's IPC verb (`clagentic-relay`) is generalized to
   caller-supplied config — see `DirectorClagenticConfig`/
   `DirectorLeadMutationConfig` below.
4. NO PERMISSIVE FALLBACK. Every function here returns a closed (ok, reason)
   tuple; there is no "unhandled case, warn and allow" branch anywhere
   (the reference's relay-era warn-and-allow path for an unrecognized
   agent_type is dead code today and has no analogue here at all — see
   FLAGGED LOSSY COLLAPSE POINTS).
5. REUSE LANDED PRIMITIVES; NEVER DUPLICATE A BOUNDARY. This module performs
   NO shell-word normalization, compound-detection, or scratch-containment
   of its own — it composes `guard.shell_parsing.normalize_shell_words` /
   `has_unresolved_ansi_c_quote` / `split_segments` / `cmd_head` directly
   (the SAME primitives `role_allowlist.py` and `bash_admission.py`
   already use), never a second, forked copy of any of them.

WHAT THIS MODULE PORTS:

  - `check_director_identity_discipline` — port of the reference's
    `_check_director_clagentic` (reference guard-bash.py ll.3619-3709):
    caller-identity discipline on a configurable "relay-shaped" IPC verb's
    open/post/close subcommands (reference: `clagentic-relay conversation`,
    generalized to `DirectorClagenticConfig.relay_verb_pattern` — no
    specific binary name hardcoded here).
  - `check_lead_mutation` (SE3 PR2, THIS PR, lr-1cc4df) — port of the
    reference's `_check_director_lead_mutation` (reference guard-bash.py
    ll.4312-4689): the mutation-verb-family deny dispatch (git write / file
    mutation / package mutation / systemctl mutation), the ship-invocation
    named anti-pattern, the forge-PR-mutation denies (forgejo-curl
    POST/PATCH/DELETE, raw curl/wget PR-mutation, `gh pr` mutation
    subcommands), shell write redirection, and the credentials-file write
    deny — extending `role_allowlist.check_lead_command` (see that
    function's own docstring for how the two PRs compose) rather than
    adding a second `BashRole.LEAD` entry point.

THE ATTESTED-ACTING-SUBAGENT SEAM (SE3 PR2, THIS PR OWNS IT — see
"ATTESTATION SEAM DESIGN" below): the reference's mutation checker embeds a
"forgejo-curl acting-subagent carve-out" (reference ll.4367-4503) that
resolves an ATTESTED invoking identity via a specific harness's
session-sidecar file and a specific harness's per-agent allowlist-function
dispatch table, to decide whether a `--caller <name>` flag on a
forgejo-curl-shaped invocation is trustworthy enough to defer to that
NAMED agent's own (narrower) allowlist instead of denying it as a lead's
own direct PR-mutation attempt. PR1 (`guard-policy.md`'s SE3 PR1 section)
scope-trimmed this ENTIRE mechanism out because PR1's own checker had no
mutation-deny surface to carve an exception INTO — omitting it there was
strictly no-wider than the reference. THIS PR is different: it lands the
actual mutation-verb deny that carve-out exists to soften, so silently
dropping the carve-out here would be a REAL, un-flagged narrowing of lead
authority relative to the reference (every reference invocation the
carve-out legitimately admitted would now hard-deny with no escape valve).
Per this task's dispatch instruction, the carve-out is ported as a
CALLER-SUPPLIED ATTESTATION-PROVIDER SEAM (`ActingSubagentResolver`) — a
pluggable interface a caller's own harness adapter implements — never a
hardcoded sidecar-file path or agent-name dispatch table (CLAUDE.md rule 1,
rule 6a, hard rule 2 "integration-first, orchestration-agnostic": this
module does not own agent spawning or agent-to-agent transport, and must
not integrate point-to-point with one specific harness's identity model).
See "ATTESTATION SEAM DESIGN" below for the full interface and its
fail-closed contract; a caller passing `None` (the default) gets a checker
that is STILL STRICTLY NO WIDER than omitting the carve-out entirely (no
regression relative to PR1's posture) — the seam only ever ADDS admission
when a caller explicitly wires a resolver in, exactly mirroring the
reference's own carve-out's WIDEN-ONLY effect.

ATTESTATION SEAM DESIGN
------------------------
`ActingSubagentResolver` (a frozen dataclass of two caller-supplied
callables, not a class hierarchy — mirrors this module's existing
`DirectorClagenticConfig` zero-argument-callable-signal convention rather
than inventing a new composition style):

  - `resolve_attested_identity: Callable[[], str]` — returns the ATTESTED
    identity of the process actually invoking this Bash call (reference:
    `resolve_attested_identity(hook_name=HOOK_NAME)`, imported from a
    specific harness's `_agent_identity_default` module), or `""` when no
    attestation signal is available. This module never reads an
    environment variable, a sidecar file, or any harness-specific state
    itself to answer this question — a caller's own harness adapter
    resolves it and injects the ANSWER as a callable, exactly as
    `DirectorClagenticConfig.teams_context_signal` /
    `relay_acting_as_env_signal` already establish for the sibling PR1
    checker. An empty-string return is "unattested" (the ordinary case for
    a genuinely distinct acting subagent whose own session sets its own
    ambient signals, not this lead session's) — NOT a mismatch signal.
  - `check_acting_role_command: Callable[[str, str], tuple[bool, str]]` —
    given `(caller_name, forgejo_curl_invocation_segment)`, returns whether
    THAT named acting role's own (separately vetted, narrower) allowlist
    admits the exact forgejo-curl invocation segment. Reference: `_ALLOWLIST_
    FN.get(caller_name)`, a dispatch table keyed on a fixed set of internal
    agent names. This module never enumerates agent names or maintains a
    dispatch table itself — a caller's own harness adapter (which already
    knows its own role registry, e.g. by composing `role_allowlist.
    check_bash_call` for the role `caller_name` maps to) supplies this as a
    single callable. `caller_name` here is the bare token extracted from the
    command's own `--caller <name>` flag — NEVER validated against a fixed
    name list by this module (there is no such list to validate against);
    the CALLABLE the caller supplies is the sole authority on whether that
    name denotes a real, distinct, non-lead role at all.
  - `ineligible_caller_names: frozenset[str]` — caller-supplied names this
    carve-out must NEVER defer to even if `check_acting_role_command` would
    otherwise admit the command (reference: a fixed one-entry ineligible
    set naming the release-gate identity — that role's allowlist is
    read-only/no-method-restricted by design and must not become an
    accidental POST bypass surface for a lead). A caller supplies whichever
    of its OWN role names plays that part; this module hardcodes none.

FAIL-CLOSED MISMATCH CHECK (reference tome #700 T3-gh, lr-c62abb — the
verified-exploit fix, ported in full since dropping it would silently
reopen that exact borrow): before ANY deferral to the named caller's own
allowlist, `check_lead_mutation` binds the claimed `--caller <name>` to the
ATTESTED invoking identity. If `resolve_attested_identity()` returns a
NON-EMPTY string that DIFFERS from the claimed name, the command is refused
fail-closed — a session may mint/use only its own credential; a
well-formed `--caller` flag naming a DIFFERENT role is not, by itself,
proof the invoking process actually IS that role. An unattested invocation
(the resolver returns `""`) is NOT treated as a mismatch — that is the
ORDINARY case for a genuinely distinct acting subagent whose own session
sets its own ambient signals, not this lead session's; the carve-out
proceeds exactly as it would with no resolver wired in at all. This mirrors
the reference's own `EXIT_CALLER_MISMATCH` semantics precisely: "chain
resolves nothing" and "chain resolves to the same name" are both
non-mismatches; only "chain resolves to a DIFFERENT non-empty name" fails
closed. No WARN-only rollout mode is ported (reference
`CREW_CALLER_BINDING_MODE=warn`) — that is a bounded MIGRATION-WINDOW
feature-flag for a live fleet's staged rollout, not a shell-command policy
decision this module should own; a caller wanting a staged rollout
implements it in its own harness adapter's resolver callable (e.g. having
`resolve_attested_identity` return `""` during its own warn-only window),
never inside this module.

WHY THIS IS A SEAM, NOT A DROP: per this task's dispatch instruction,
silently omitting the carve-out in THIS PR (unlike PR1) would be an
un-flagged narrowing of documented lead authority — a lead session
legitimately dispatching a crew reviewer subagent's own forgejo-curl POST
would now hard-deny where the reference correctly deferred to that
subagent's own narrower credential. The seam above ports the reference's
actual DECISION LOGIC (attestation-bound deferral, ineligible-caller
exclusion, fail-closed mismatch) faithfully, while replacing its two
harness-specific LOOKUPS (a sidecar file, a fixed agent-name dispatch
table) with caller-supplied callables — the same "port pattern
decision-logic, seam the harness-specific lookup" split this module's
sibling `check_director_identity_discipline` already applies to
`teams_context_signal`/`relay_acting_as_env_signal`.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Callable

from clagentic_loadout.guard.shell_parsing import (
    has_unresolved_ansi_c_quote,
    normalize_shell_words,
)

#: Bare-token grammar for a `--caller <name>` value extracted from a
#: forgejo-curl-shaped invocation below. Mirrors `role_allowlist.
#: _ROLE_TOKEN_RE` exactly (alphanumeric/hyphen/underscore, 1-64 chars, no
#: leading hyphen) — re-declared here rather than imported, since that name
#: is private to `role_allowlist` and this module must not depend on
#: `role_allowlist` (the dependency runs the other way: `role_allowlist.
#: check_lead_command` composes THIS module, not vice versa — see
#: `guard.write_scope`'s "reuse landed primitives; never duplicate a
#: boundary" convention, applied here as "share the GRAMMAR, not a
#: cross-module import that would invert the dependency direction").
_ROLE_TOKEN_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


@dataclass(frozen=True)
class DirectorClagenticConfig:
    """Caller-supplied configuration for
    `check_director_identity_discipline` (reference: the reference
    deployment's `_check_director_clagentic`, ll.3619-3709).

    relay_verb_pattern: matches the caller's own relay-shaped IPC command
        invocation up through its subcommand token (e.g. a compiled pattern
        for `<relay-binary> conversation`) — this module never hardcodes a
        specific relay binary name (CLAUDE.md rule 1); a caller wires its
        own installed IPC verb name in. The reference's exact shape is
        `clagentic-relay conversation <subcommand> ...`; this module
        generalizes only the leading two tokens (binary + noun) via this
        pattern, and always inspects `tokens[2]` as the subcommand itself
        (open/post/close) — a caller supplying a differently-shaped IPC verb
        whose subcommand is not the third whitespace-delimited token cannot
        use this checker's subcommand dispatch as-is.
    teams_context_signal: a caller-supplied zero-argument callable returning
        True when this session is running under an alternate teammate-spawn
        transport that does not use the relay's `conversation open` path at
        all (reference: `_is_teams_context`, gated on a feature-flag env var
        this module does not read itself — CLAUDE.md rule 6a forbids a
        product module reading an env var as part of its own policy
        decision). Defaults to a callable that always returns False (the
        reference's own default posture: the identity check applies unless a
        caller's own environment signal says otherwise) — a caller composes
        its own environment/feature-flag read and passes the boolean result
        in via this callable, this module never inspects `os.environ`
        itself.
    relay_acting_as_env_signal: a caller-supplied zero-argument callable
        returning the acting-identity string a caller's own relay-spawned
        session environment has already established (reference:
        `CLAGENTIC_RELAY_ACTING_AS`), or an empty string when unset. Same
        rationale as *teams_context_signal* — this module reads no
        environment variable itself.
    """

    relay_verb_pattern: "object"  # re.Pattern[str], see check function below
    teams_context_signal: "object" = None  # Callable[[], bool] | None
    relay_acting_as_env_signal: "object" = None  # Callable[[], str] | None

    def __post_init__(self) -> None:
        if self.teams_context_signal is None:
            object.__setattr__(self, "teams_context_signal", lambda: False)
        if self.relay_acting_as_env_signal is None:
            object.__setattr__(self, "relay_acting_as_env_signal", lambda: "")


def check_director_identity_discipline(
    command: str,
    *,
    identity_label: str,
    config: DirectorClagenticConfig,
) -> tuple[bool, str]:
    """Return (ok, reason) — port of the reference deployment's
    `_check_director_clagentic` (reference guard-bash.py ll.3619-3709).

    Enforces caller-identity discipline on a configurable relay-shaped IPC
    command's `open`/`post`/`close` conversation subcommands: a lead/
    director session must make its own identity EXPLICIT in the command it
    issues (an `--opener`/`--from` flag, or the close subcommand's `--reason`
    text), rather than relying on a transport default that could silently
    attribute the action to the wrong party. See each subcommand branch
    below for the exact reference-preserved rule.

    ROLE-KEYED, NOT IDENTITY-BRANCHED (module docstring point 2):
    *identity_label* is used ONLY inside denial-message text (echoed back so
    an operator/log reader can see which session's own identity was
    expected in the command) — this function's control flow never compares
    it, branches on it, or treats any particular value specially. The
    reference's own `identity` parameter is used identically (message
    interpolation only, never a conditional) — this is a faithful,
    non-lossy port of that parameter's actual role, not a narrowing.

    Tokenization: conservative whitespace-split for subcommand dispatch
    (matches the reference's own posture exactly — the reference comment
    on this function notes tokenization is deliberately conservative here,
    since the values this function inspects for identity discipline are
    flag VALUES a caller's own command construction controls, not
    attacker-controlled narrative data this checker must defend against
    quote-based evasion for). The `close` subcommand's `--reason` value
    extraction uses `shlex.split` (matching the reference exactly) so a
    `--reason` value containing embedded `--word` tokens is captured intact
    rather than a naive split stopping at the first internal flag-looking
    token; on a shlex failure this falls back to the conservative
    whitespace token list, matching the reference's own fallback.

    Returns (True, "") immediately for any command that is not, in fact,
    this relay-shaped IPC verb's `conversation <subcommand>` invocation at
    all — this function has no opinion on any other command; a caller's
    other admission checks (the mutation-verb-family dispatch, or any other
    role/verb rule) decide those independently, exactly as the reference's
    own caller only invokes `_check_director_clagentic` after already
    recognizing the command as this specific IPC verb shape.

    ANSI-C-QUOTE EVASION ANALYSIS (this module's own MANDATORY review per
    `guard-policy.md`'s Slice-5 "Post-landing hardening" section, applied
    here explicitly since this checker's shape differs from every prior
    bare-verb-grant checker that section targets): every branch below is a
    REQUIRED-PRESENCE check ("deny unless flag X is present" / "deny unless
    identity string appears in --reason"), never a forbidden-SUBSTRING scan
    feeding a bare-verb affirmative grant — the shape the mandatory
    normalize-and-hard-deny pattern exists to close. An ANSI-C-fragmented
    `--opener`/`--from` flag (e.g. `$'--opener'`) does not equal the literal
    token `--opener` under a plain whitespace split, so fragmenting it can
    only make the ABSENCE check fire MORE readily (a stricter, not laxer,
    outcome) — the same holds for `close`'s `shlex.split`-based `--reason`
    extraction: `shlex` does not understand the `$'...'` ANSI-C grammar at
    all and raises `ValueError` on it, falling back to the conservative
    `tokens` list (a truncated, narrower reason_text), which likewise can
    only make the identity-embed check MORE likely to (correctly) deny, not
    less. No path in this function converts a denied command into an
    admitted one via ANSI-C quoting — the mandatory hard-deny gate other
    checkers in this module family need for a bare-verb AFFIRMATIVE grant
    does not apply to this function, which has no such grant of its own
    (every return True is either "not this verb" or "the required flag/
    text was genuinely present").
    """
    tokens = command.split()

    if len(tokens) < 3:
        return True, ""

    if not config.relay_verb_pattern.match(" ".join(tokens[:2])):
        return True, ""

    subcommand = tokens[2]

    if subcommand == "open":
        if config.teams_context_signal():
            return True, ""
        acting_as = (config.relay_acting_as_env_signal() or "").strip()
        if "--opener" not in tokens and not acting_as:
            return False, (
                f"relay conversation open in a {identity_label!r} session "
                f"requires --opener (caller-identity discipline). Add "
                f"--opener {identity_label} to the command, or ensure the "
                f"acting-as environment signal is set (a relay-spawned "
                f"session sets this automatically)."
            )
        return True, ""

    if subcommand == "post":
        if "--from" not in tokens:
            return False, (
                f"relay conversation post in a {identity_label!r} session "
                f"requires --from (caller-identity discipline). Add "
                f"--from {identity_label} to the command."
            )
        return True, ""

    if subcommand == "close":
        try:
            close_tokens = shlex.split(command)
        except ValueError:
            close_tokens = tokens  # fallback: best-effort token list
        reason_text = None
        for index, token in enumerate(close_tokens):
            if token == "--reason" and index + 1 < len(close_tokens):
                reason_text = close_tokens[index + 1]
                break
        if reason_text is None:
            return False, (
                f"relay conversation close in a {identity_label!r} session "
                f"requires --reason containing caller identity "
                f"(caller-identity discipline). "
                f'Add --reason "{identity_label} closed: <description>".'
            )
        if identity_label not in reason_text:
            return False, (
                f"relay conversation close --reason must embed caller "
                f"identity {identity_label!r}. "
                f"Current --reason text does not contain {identity_label!r}. "
                f'Use: --reason "{identity_label} closed: <description>".'
            )
        return True, ""

    return True, ""


# ---------------------------------------------------------------------------
# SE3 PR2 (lr-1cc4df): mutation-verb-family deny dispatch, port of the
# reference's `_check_director_lead_mutation` (reference guard-bash.py
# ll.4312-4689). See module docstring "WHAT THIS MODULE PORTS" and
# "ATTESTATION SEAM DESIGN" for the full scope and the harness-attestation
# seam this section wires in.
# ---------------------------------------------------------------------------


def _lead_scan_target(command: str) -> str:
    """Return the string mutation-deny checks below should scan (reference
    `_director_lead_scan_target`, ll.4125-4144).

    Normalizes quoted argv spans via `shell_parsing.normalize_shell_words`
    (the SAME shell-word-normalization pre-pass every other checker in this
    package's guard surface uses) so a mutation-verb token or redirect
    operator bash would actually SPLICE onto a neighboring word is visible
    in its real joined form, while a token appearing only inside a
    genuinely isolated quoted narrative span (a lore comment body, a task
    description) stays inert. Falls back to the raw (unnormalized) string
    on ambiguity (unparseable quoting, quoted command substitution) —
    deny-on-ambiguity, matching every other quote-aware check in this
    package.

    This is the ONE normalization pass every deny check below scans,
    exactly mirroring the reference's own single-precompute-then-reuse
    posture (reference sec 4.2 point 3) rather than each check independently
    re-deriving its own quote-unaware view of `command`.
    """
    normalized = normalize_shell_words(command)
    return command if normalized is None else normalized


# --- Consolidated mutation-verb-family taxonomy (reference ll.4147-4309) ---
# Each family scans the SAME precomputed `_lead_scan_target` output —
# role-independent shell-command SHAPES with no identity coupling, ported
# near-verbatim (the reference itself already applies these identically
# regardless of WHICH director/lead identity fired the check).

_GIT_CMD_RE = re.compile(
    r"(?:^|[\s;|&(`])git(?:\s+--\S+)*(?:\s+-C\s+\S+)*\s+(\w[\w-]*)(?:\s+(-?-?\w[\w-]*))?"
)
_GIT_WRITE_ONE: frozenset[str] = frozenset(
    {
        "add", "commit", "push", "merge", "rebase", "reset",
        "checkout", "cherry-pick", "restore", "clean", "am", "apply",
        "format-patch", "update-ref",
    }
)
_GIT_WRITE_TWO: frozenset[str] = frozenset(
    {
        "stash drop", "stash pop", "stash clear", "stash push",
        "branch -d", "branch -D",
        "fetch --prune",
        "notes add", "notes append", "notes edit", "notes remove", "notes prune",
        "tag -d", "tag -f",
    }
)


def _match_git_write(scan_target: str) -> str | None:
    """Return the matched 'subcommand [subsubcommand]' string, or None
    (reference `_match_git_write`)."""
    for m in _GIT_CMD_RE.finditer(scan_target):
        sub1 = m.group(1)
        sub2 = m.group(2)
        combined = f"{sub1} {sub2}" if sub2 else sub1
        if sub1 in _GIT_WRITE_ONE or combined in _GIT_WRITE_TWO:
            return combined
    return None


#: rm, mv, cp, tee (non-staging), dd, truncate, chmod, chown, ln, install,
#: sed -i, perl -i (reference `_FILE_MUTATION_RE`).
_FILE_MUTATION_RE = re.compile(
    r"(?:^|[\s;|&(])"
    r"(rm|mv|cp|tee|dd|truncate|chmod|chown|ln|install"
    r"|sed\s+-i|perl\s+-i"
    r")"
    r"(?:\s|$)"
)

#: `python3 -c` with an `open(..., 'w')` write mode. NOT quote-masked
#: (deliberate exception, mirrors reference `_PY_OPEN_WRITE_RE` exactly):
#: the quotes this check must see ARE the `-c` script text delimiters, not
#: narrative data to protect from argument smuggling.
_PY_OPEN_WRITE_RE = re.compile(
    r"(?:^|[\s;|&(])python3?\s+[^;|&]*-c\s+['\"].*open\s*\(['\"]?[^'\"]+['\"]?\s*,\s*['\"]w"
)


def _match_file_mutation(scan_target: str, raw_command: str) -> str | None:
    """Reference `_match_file_mutation`. `raw_command` is the pre-mask
    command text — the `python3 -c open(...)` sub-case legitimately lives
    inside argv quoting the normalized scan target may have altered, so it
    is checked against the raw command, exactly as the reference does."""
    if _FILE_MUTATION_RE.search(scan_target):
        return "file mutation primitive"
    if _PY_OPEN_WRITE_RE.search(raw_command):
        return "python3 -c open(...,'w')"
    return None


_PKG_MUTATION_RE = re.compile(
    r"(?:^|[\s;|&])"
    r"(apt(?:-get)?|dnf|yum|pacman|pip3?|pipx|npm|pnpm|yarn|cargo|brew|gem"
    r"|go\s+(?:get|install)"
    r"|docker\s+(?:run|build|exec|kill|rm|stop|start|restart)"
    r"|service)"
    r"(?:\s|$)"
)


def _match_pkg_mutation(scan_target: str) -> str | None:
    if _PKG_MUTATION_RE.search(scan_target):
        return "package install or docker mutation"
    return None


#: systemctl mutation subcommands only — read-only inspection is permitted
#: (reference `_SYSTEMCTL_RE`).
_SYSTEMCTL_RE = re.compile(
    r"(?:^|[\s;|&])systemctl\s+"
    r"(start|stop|restart|reload|enable|disable|mask|unmask|daemon-reload"
    r"|kill|reset-failed|link|preset|revert|edit|set-property"
    r"|add-wants|add-requires|switch-root"
    r"|poweroff|reboot|halt|suspend|hibernate|hybrid-sleep)"
    r"(?:\s|$)"
)


def _match_systemctl_mutation(scan_target: str) -> str | None:
    if _SYSTEMCTL_RE.search(scan_target):
        return "systemctl mutation subcommand"
    return None


def _git_write_reason(identity_label: str, matched: str) -> str:
    return (
        f"git write op {matched!r} forbidden in {identity_label!r} session. "
        f"Leads dispatch a builder role for code work; they do not commit "
        f"or push directly."
    )


def _file_mutation_reason(identity_label: str, matched: str) -> str:
    del matched
    return (
        f"file mutation command forbidden in {identity_label!r} session. "
        f"Leads are read-only on the codebase; dispatch a builder role for "
        f"file changes."
    )


def _pkg_mutation_reason(identity_label: str, matched: str) -> str:
    del matched
    return (
        f"package install or docker mutation forbidden in {identity_label!r} "
        f"session. Leads do not mutate the toolchain or container state; "
        f"dispatch a builder role."
    )


def _systemctl_mutation_reason(identity_label: str, matched: str) -> str:
    del matched
    return (
        f"systemctl mutation subcommand forbidden in {identity_label!r} "
        f"session. Leads may run systemctl status/show/is-active/"
        f"list-units for diagnostics only."
    )


@dataclass(frozen=True)
class LeadMutationConfig:
    """Caller-supplied configuration for `check_lead_mutation` (reference:
    `_check_director_lead_mutation`, ll.4312-4689).

    push_verb_pattern: matches a caller's own no-direct-push named
        anti-pattern invocation (reference: the reference deployment's own
        sanctioned landing-tool script and its post-landing subdirectory
        scripts, `_check_director_lead_mutation`'s "landing-tool invocation
        and post-landing scripts" block) — a caller supplies its own
        installed push-transport script pattern; this module hardcodes no
        specific script path (CLAUDE.md rule 1).
        Optional: a caller with no such standalone push script may omit
        this and rely on the git-write family match alone.
    forge_pr_mutation_verb_patterns: verb-prefix regexes recognizing a
        caller's own forge-PR-mutation wrapper invocation shape (reference:
        the `forgejo-curl POST/PATCH/DELETE` bare-binary-name/path match) —
        matched against `_lead_scan_target` output, denied unconditionally
        for a bare lead-issued call (the attestation seam below is the ONLY
        escape valve, exactly as in the reference).
    forge_host_patterns: hostnames/host-shape fragments recognized inside a
        raw curl/wget PR/issue-comment mutation URL (reference: the
        Forgejo/GitHub host literals baked into `_CURL_MUTATION_RE`'s
        companion `_PR_PATH_RE` composition) — a caller supplies its own
        forge host(s); this module has no default so a caller integrating
        against a self-hosted forge is never silently unprotected by an
        assumption about which host it runs.
    review_runner_patterns: absolute-path or verb-prefix regexes for a
        caller's own crew-reviewer-invocation surfaces (reference: a narrow
        two-script carve-out for the reviewer/security-scanner runner
        scripts, ll.4360-4365) — these read a PR diff and post one review
        comment; they do not mutate repos or PRs. A caller supplies its own
        review runner script pattern(s) here; checked BEFORE the mutation-verb
        dispatch, mirroring the reference's own ordering rationale (a
        review body argument may contain redirect-looking characters that
        would otherwise false-trigger the shell-write-redirect deny below).
    relay_body_redirect_prefix: an exact path PREFIX (not a full path) this
        module treats as the sole carved-out shell-write-redirect target
        (reference: the fixed `/run/clagentic-relay/spawn-homes/<id>/
        relay-body.json` envelope-posting path) — a caller supplies its own
        per-spawn envelope-staging path prefix; `None` (the default) means
        no carve-out at all, so EVERY write-redirect denies (a strict
        SUBSET of the reference's admitted set, never a widening default).
    credential_file_patterns: path-shape regexes for a caller's own
        credentials/settings-file locations (reference: `~/.claude/
        settings.json` / `~/.netrc` — home-relative literals) — a caller
        supplies its own credential-file path shape(s); no home-directory
        literal is hardcoded here (CLAUDE.md rule 10).
    acting_subagent_resolver: the harness-attestation seam (see module
        docstring "ATTESTATION SEAM DESIGN") — `None` (the default) means
        no carve-out is available at all for this call (strictly no wider
        than PR1's own scope-trim posture); a caller wanting the reference's
        acting-subagent deferral behavior supplies an
        `ActingSubagentResolver`.
    """

    push_verb_pattern: "object" = None  # re.Pattern[str] | None
    forge_pr_mutation_verb_patterns: tuple["object", ...] = ()  # tuple[re.Pattern[str], ...]
    forge_host_patterns: tuple[str, ...] = ()
    review_runner_patterns: tuple["object", ...] = ()  # tuple[re.Pattern[str], ...]
    relay_body_redirect_prefix: str | None = None
    credential_file_patterns: tuple["object", ...] = ()  # tuple[re.Pattern[str], ...]
    acting_subagent_resolver: "ActingSubagentResolver | None" = None


@dataclass(frozen=True)
class ActingSubagentResolver:
    """The harness-attestation carve-out seam (see module docstring
    "ATTESTATION SEAM DESIGN" for the full design rationale).

    A caller's own harness adapter constructs one instance of this,
    implementing both callables against its own identity-attestation
    transport and its own role registry — this module never resolves
    either question itself.
    """

    resolve_attested_identity: Callable[[], str]
    check_acting_role_command: Callable[[str, str], tuple[bool, str]]
    ineligible_caller_names: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        for name in self.ineligible_caller_names:
            if not _ROLE_TOKEN_RE.match(name):
                raise ValueError(
                    f"ineligible_caller_names entry {name!r} is not a bare "
                    f"token (expected alphanumeric/hyphen/underscore, "
                    f"1-64 chars, no leading hyphen)."
                )


_FORGEJO_CURL_INVOCATION_RE = re.compile(r"forgejo-curl(?:\s|$).*")
_CALLER_FLAG_RE = re.compile(r"--caller[=\s]+([a-zA-Z0-9][a-zA-Z0-9_-]{0,63})")


def _try_acting_subagent_carve_out(
    command: str, *, identity_label: str, resolver: ActingSubagentResolver
) -> tuple[bool | None, str]:
    """Return (verdict, mismatch_detail) — port of the forge-PR-mutation
    deny's acting-subagent carve-out (reference ll.4367-4503; tome #700
    T3-gh binding fix, lr-c62abb).

    verdict is True (admit), False (fail-closed deny), or None (carve-out
    does not apply — fall through to the ordinary mutation-verb dispatch).
    *mismatch_detail* is populated ONLY on a False verdict (the attested
    identity string, for the caller's denial-message text) and is `""` in
    every other case.

    Returning False here is a HARD, FINAL deny (a fail-closed attested-
    identity mismatch) — the caller must not fall through to any other
    check on a False verdict. Returning None means this carve-out simply
    was not reachable for this command (no forgejo-curl invocation, or no
    `--caller` flag at all) and the caller proceeds exactly as it would
    with no resolver configured.
    """
    m_invocation = _FORGEJO_CURL_INVOCATION_RE.search(command)
    if m_invocation is None:
        return None, ""
    forgejo_invocation = m_invocation.group(0)
    m_caller = _CALLER_FLAG_RE.search(forgejo_invocation)
    if m_caller is None:
        return None, ""
    caller_name = m_caller.group(1)

    # Bind --caller to the attested invoker BEFORE any other branch of this
    # carve-out runs (tome #700 T3-gh: the verified exploit is exactly a
    # mismatched --caller naming an identity the invoking process is not
    # attested as). Fail-closed ONLY when the resolver returns a NON-EMPTY
    # identity that DIFFERS from caller_name; an unattested invocation
    # (resolver returns "") is not a new failure mode and proceeds as
    # before (mirrors the reference's own EXIT_CALLER_MISMATCH semantics).
    attested_identity = resolver.resolve_attested_identity()
    if attested_identity and attested_identity != caller_name:
        return False, attested_identity

    if caller_name in resolver.ineligible_caller_names:
        return None, ""

    caller_ok, _caller_reason = resolver.check_acting_role_command(
        caller_name, forgejo_invocation
    )
    if caller_ok:
        return True, ""
    return None, ""


_MUTATION_VERB_FAMILIES: tuple[tuple[str, "object", "object"], ...] = (
    ("git_write", lambda scan, raw: _match_git_write(scan), _git_write_reason),
    ("file_mutation", _match_file_mutation, _file_mutation_reason),
    ("package_mutation", lambda scan, raw: _match_pkg_mutation(scan), _pkg_mutation_reason),
    (
        "systemctl_mutation",
        lambda scan, raw: _match_systemctl_mutation(scan),
        _systemctl_mutation_reason,
    ),
)

_GH_PR_MUTATION_RE = re.compile(
    r"(?:^|[\s;|&(])gh\s+pr\s+"
    r"(create|merge|close|edit|comment|review|reopen)"
    r"(?:\s|$)"
)

_CURL_MUTATION_RE = re.compile(r"(?:^|[\s;|&(])(?:curl|wget)\b")
_CURL_METHOD_RE = re.compile(
    r"(?:"
    r"-X\s+(?:POST|PATCH|PUT|DELETE)"
    r"|--request\s+(?:POST|PATCH|PUT|DELETE)"
    r"|--method=(?:POST|PATCH|PUT|DELETE)"
    r")",
    re.IGNORECASE,
)
_PR_PATH_RE = re.compile(
    r"/repos/[^/\s]+/[^/\s]+/"
    r"(?:pulls(?:/|\b)|issues/[0-9]+/comments)",
)


def check_lead_mutation(
    command: str,
    *,
    identity_label: str,
    config: "LeadMutationConfig | None" = None,
) -> tuple[bool, str]:
    """Return (ok, reason) — port of the reference deployment's
    `_check_director_lead_mutation` (reference guard-bash.py ll.4312-4689).

    Enforces the structural deny on code/git/system mutation for a
    lead/director session: git write ops, file mutation primitives, package/
    docker mutation, systemctl mutation subcommands, a caller-configured
    no-direct-push named anti-pattern, forge-PR-mutation (forgejo-curl
    POST/PATCH/DELETE, raw curl/wget PR mutation, `gh pr` mutation
    subcommands), shell write redirection, and a caller-configured
    credentials/settings-file write deny.

    Composition with `check_director_identity_discipline`: a caller
    composing `role_allowlist.check_lead_command` runs THIS function first
    (mirrors the reference's own ordering — `_check_director_lead_mutation`
    runs before `_check_director_clagentic` in the reference's `main()`) so
    the structural mutation deny always applies even to a relay-shaped IPC
    command; only once this function admits does the narrower identity-
    discipline check (which has no opinion on any command that isn't the
    configured relay verb's conversation subcommand shape) get consulted.

    ANSI-C-quote evasion (mandatory review, `guard-policy.md`'s "Post-
    landing hardening" section): this is the SOLE enforcement surface for
    the lead/director mutation guard (no allowlist fallback — a missed verb
    here falls through to the final `return True` at the bottom). `_lead_
    scan_target` falls back to the RAW command when normalization can't
    resolve an ANSI-C escape, but the raw string still contains the intact
    `$'...'` wrapper hiding its verb from every family matcher below — a
    raw-string fallback is not actually deny-on-ambiguity for this shape.
    This function hard-denies BEFORE the family dispatch runs whenever
    normalization failed AND an ANSI-C opener is present (reference
    lr-8916), rather than letting a hidden verb ride the fallback through
    to an unearned ALLOW.

    ATTESTATION SEAM: see module docstring "ATTESTATION SEAM DESIGN". When
    `config.acting_subagent_resolver` is `None` (the default), the
    forge-PR-mutation deny below applies unconditionally to every
    forgejo-curl POST/PATCH/DELETE-shaped invocation — no carve-out is
    reachable, exactly PR1's own "strictly no wider than the reference"
    scope-trim posture. When a resolver IS supplied, the carve-out is tried
    FIRST (mirroring the reference's own ordering — the carve-out runs
    before the general forgejo-curl write-op deny) and can either ADMIT the
    command (a genuinely attested, distinct, non-lead acting role whose own
    allowlist independently admits it), hard-DENY it (a detected
    attested-identity mismatch, tome #700 T3-gh), or decline to apply (fall
    through to the ordinary mutation-verb dispatch below, unaffected).
    """
    cfg = config if config is not None else LeadMutationConfig()

    for pattern in cfg.review_runner_patterns:
        if pattern.match(command):
            return True, ""

    if cfg.acting_subagent_resolver is not None:
        carve_out_verdict, attested_identity = _try_acting_subagent_carve_out(
            command,
            identity_label=identity_label,
            resolver=cfg.acting_subagent_resolver,
        )
        if carve_out_verdict is True:
            return True, ""
        if carve_out_verdict is False:
            return False, (
                f"forgejo-curl --caller does not match the ATTESTED invoking "
                f"identity {attested_identity!r} for {identity_label!r} "
                f"session — the acting-subagent carve-out requires the "
                f"invoking process to actually BE the named --caller, not "
                f"merely that the named role's own allowlist would admit "
                f"the command. An identity may mint/use only its own "
                f"credential. Refused "
                f"fail-closed before any deferral to the named role's "
                f"allowlist."
            )
        # carve_out_verdict is None: not this carve-out's concern, fall through.

    if normalize_shell_words(command) is None and has_unresolved_ansi_c_quote(command):
        return False, (
            f"unresolvable ANSI-C ($'...') quote span forbidden in "
            f"{identity_label!r} session (deny-on-ambiguity). A command "
            f"whose quoting cannot be confidently normalized may hide a "
            f"mutation verb; leads must not run it directly."
        )

    scan_target = _lead_scan_target(command)

    for _family_name, matcher, reason_fn in _MUTATION_VERB_FAMILIES:
        matched = matcher(scan_target, command)
        if matched is not None:
            return False, reason_fn(identity_label, matched)

    if cfg.push_verb_pattern is not None and cfg.push_verb_pattern.search(scan_target):
        return False, (
            f"push-transport invocation forbidden in {identity_label!r} "
            f"session. Leads do not "
            f"push code; dispatch a builder role."
        )

    for pattern in cfg.forge_pr_mutation_verb_patterns:
        if pattern.search(scan_target):
            return False, (
                f"forge-PR-mutation write op forbidden in {identity_label!r} "
                f"session. Leads do not open PRs or "
                f"mutate forge resources directly; dispatch a builder role "
                f"to do code work and open PRs."
            )

    if cfg.forge_host_patterns:
        host_alt = "|".join(re.escape(h) for h in cfg.forge_host_patterns)
        host_re = re.compile(host_alt)
        if (
            _CURL_MUTATION_RE.search(scan_target)
            and _CURL_METHOD_RE.search(scan_target)
            and host_re.search(scan_target)
            and _PR_PATH_RE.search(scan_target)
        ):
            return False, (
                f"raw curl/wget PR/comment mutation forbidden in "
                f"{identity_label!r} session. Leads "
                f"dispatch a builder role for code work; they do not mutate "
                f"PRs or post comments directly."
            )

    m_gh = _GH_PR_MUTATION_RE.search(scan_target)
    if m_gh:
        subcmd = m_gh.group(1)
        return False, (
            f"gh pr mutation subcommand {subcmd!r} forbidden in "
            f"{identity_label!r} session. Leads "
            f"dispatch a builder role for code work; they do not create or "
            f"mutate PRs directly."
        )

    check_no_relay = scan_target
    if cfg.relay_body_redirect_prefix is not None:
        relay_body_re = re.compile(
            r"(?:>>?|&>)\s*" + re.escape(cfg.relay_body_redirect_prefix) + r"\S*"
        )
        check_no_relay = relay_body_re.sub("", scan_target)

    if re.search(r"(?<![|<])(?:>>?|&>)\s*[^\s&]", check_no_relay):
        return False, (
            f"shell write redirection forbidden in {identity_label!r} "
            f"session. Leads do not write files via "
            f"shell redirection; use read-only tooling instead."
        )

    if re.search(r"<<HEREDOC.*>", check_no_relay):
        return False, (
            f"heredoc-to-file forbidden in {identity_label!r} session. "
            f"Leads do not write files; dispatch a "
            f"builder role for file creation/edits."
        )

    for pattern in cfg.credential_file_patterns:
        if pattern.search(scan_target):
            if re.search(r"(?:>>?|&>|sed\s+-i|perl\s+-i|tee)", scan_target):
                return False, (
                    f"write to a credentials/settings file forbidden in "
                    f"{identity_label!r} session. Leads do not modify "
                    f"credentials or settings files directly."
                )

    return True, ""


__all__ = [
    "ActingSubagentResolver",
    "DirectorClagenticConfig",
    "LeadMutationConfig",
    "check_director_identity_discipline",
    "check_lead_mutation",
]
