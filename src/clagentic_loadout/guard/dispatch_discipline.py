"""guard.dispatch_discipline — warn-only in-session-edit dispatch discipline
(lr-59dd37, port of the reference deployment's guard-dispatch.py; lr-5a8d
epic Wave C).

PORT PATTERN — mirrors `guard.write_scope`'s established convention:

1. STRIP THE HOOK SHELL. The reference file reads a PreToolUse/Edit|Write
   JSON payload from stdin, resolves agent identity via a four-path
   detection chain (a harness-internal attestation module: payload
   `agent_type`, a harness session sidecar file, a per-spawn sidecar, an
   internal-tooling identity environment variable, and a spawn-meta JSON
   directory) and emits a `hookSpecificOutput` `additionalContext` JSON
   block on stdout. None of that harness plumbing
   is ported (CLAUDE.md rule 6a — no hard dependency on a specific harness's
   hook contract, no sidecar-file read, no env-var read). This module keeps
   ONLY the two decision questions the reference actually answers —
   "is this file path operator-trivial" and "what message should a
   dispatch-discipline warning carry" — as pure functions over explicit
   inputs. A caller's own harness adapter resolves whether the calling
   session is an attested agent (by whatever means its own harness
   supports) and passes that boolean in.

2. WARN-ONLY, NEVER A GATE. The reference NEVER blocks (`sys.exit(0)`
   unconditionally) — a broken dispatch-discipline check must never
   interrupt a session. This module preserves that exactly:
   `check_dispatch_discipline` returns a warning message (or None for
   silent-allow) — it has no "deny" return value at all, unlike every other
   guard module in this package. A caller's harness adapter is responsible
   for surfacing the returned message as advisory context, never as a block.

3. NO AGENT-NAME BRANCH TO PORT. Unlike `guard.task_dispatch`, this hook has
   no per-identity behavior difference in the reference beyond "is this
   session ANY attested agent at all" (boolean) vs. "is this an operator-
   driven orchestrating session" — there is no role enum to key on here.
   `check_dispatch_discipline` takes `is_named_agent: bool` directly, mirroring
   `guard.credential_paths`'s own precedent for a role-INDEPENDENT check
   (that module's docstring point 1).

4. NO HARDCODED REDIRECT TARGET NAME. The reference's warning message
   hardcodes a specific builder-identity agent name and fixed landing-tool
   script names as the dispatch-target guidance. CLAUDE.md rule 1 forbids
   baking an agent name into product code — `DispatchGuidance` is a
   caller-supplied dataclass carrying the builder-role label and the
   redirect prose a caller's own harness wants surfaced; this module
   composes it into the final message but never invents the label itself.

5. TRIVIAL-PATH CLASSIFICATION IS PORTED VERBATIM (a pure predicate with no
   identity or harness coupling at all): `*.md` files, `.gitignore`/
   `.gitattributes`, `LICENSE`/`LICENSE.*`, and any path under a `docs`,
   `.crew`, or `.lore` directory segment are operator-trivial and never
   trigger the warning, regardless of caller.

ANSI-C HARD-DENY GATE — N/A, DOCUMENTED RATHER THAN OMITTED (per this task's
dispatch instruction and the SE3/SE4 precedent in docs/guard-policy.md):
this module's `check_dispatch_discipline` is a required-presence /
classification check over a Write/Edit `file_path` string and a boolean —
it contains no bare-verb affirmative Bash-command grant and no forbidden-
substring scan of a shell command line at all (it never even receives a
command string; Write/Edit tool calls carry a `file_path`, not shell
syntax). The SE1/SE2 mandatory "hard-deny before every bare-verb grant"
gate (`guard.role_allowlist.check_ansi_c_quote_denied`) exists specifically
to close an ANSI-C-quote evasion of a Bash-command substring/prefix match;
there is no such evasion surface here, and this function never denies
anything at all (point 2 above) — there is no grant for an evasion to widen
in the first place. Bolting the gate on regardless would be inapplicable
ceremony over a string that is never shell-parsed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

_TRIVIAL_BASENAMES: frozenset[str] = frozenset({".gitignore", ".gitattributes"})
_TRIVIAL_BASENAME_PREFIXES: tuple[str, ...] = ("LICENSE",)
_TRIVIAL_DIR_SEGMENTS: frozenset[str] = frozenset({"docs", ".crew", ".lore"})


def is_trivial_path(file_path: str) -> bool:
    """Return True when *file_path* is operator-trivial (documentation / VCS
    metadata) and should never trigger a dispatch-discipline warning.

    Trivial set (reference `_is_trivial_path`, ported verbatim):
      - basename `*.md`
      - `.gitignore`, `.gitattributes`
      - `LICENSE` / `LICENSE.*`
      - any path segment named `docs`, `.crew`, or `.lore`

    Everything else (source files, scripts, configs) is build territory and
    is eligible to trigger the warning.
    """
    try:
        p = Path(file_path)
    except (TypeError, ValueError):
        return False

    basename = p.name

    if basename.lower().endswith(".md"):
        return True

    if basename in _TRIVIAL_BASENAMES:
        return True

    for prefix in _TRIVIAL_BASENAME_PREFIXES:
        if basename == prefix or basename.startswith(prefix + "."):
            return True

    try:
        parts = p.resolve().parts
    except (OSError, RuntimeError, ValueError):
        parts = p.parts

    if any(part in _TRIVIAL_DIR_SEGMENTS for part in parts):
        return True

    return False


@dataclass(frozen=True)
class DispatchGuidance:
    """Caller-supplied prose for the dispatch-discipline warning message.

    No agent name or script literal is baked into this module (CLAUDE.md
    rule 1) — a caller composing this module into its own harness adapter
    supplies its own builder-role label and redirect instructions.

    builder_role_label: the human-readable label for the role a caller wants
        build-territory edits dispatched to (e.g. "the builder role" or a
        caller's own product-vocabulary term — never a bare agent name
        baked in here).
    redirect_instructions: free-form prose describing how a caller's own
        harness performs that dispatch (e.g. which tool/verb to invoke).
    override_env_var_name: the name of the caller's own operator-override
        environment variable, surfaced in the message only as guidance text
        — this module never reads any environment variable itself
        (CLAUDE.md rule 6a).
    """

    builder_role_label: str
    redirect_instructions: str
    override_env_var_name: str = ""


def check_dispatch_discipline(
    file_path: str,
    is_named_agent: bool,
    guidance: DispatchGuidance,
    override_active: bool = False,
) -> str | None:
    """Return an advisory warning message, or None for silent allow.

    This function NEVER denies (module docstring point 2) — its return
    value is advisory context for a caller's harness to surface, never a
    block signal. Conditions for silent allow (return None), evaluated in
    this order, mirroring the reference's own suppression-condition order:

      1. *override_active* — the caller's own operator-override signal is
         already resolved to a boolean by the time it reaches this function
         (this module reads no environment variable itself, rule 6a).
      2. *is_named_agent* — an attested agent editing its own build
         territory is legitimate; this hook exists to catch an
         *orchestrating* (non-agent) session doing build work in-session.
      3. `is_trivial_path(file_path)` — operator-trivial paths never warn.

    Any other Edit/Write call returns a warning message built from
    *guidance*.
    """
    if override_active:
        return None
    if is_named_agent:
        return None
    if is_trivial_path(file_path):
        return None

    override_line = (
        f"\n\nTo suppress this warning for an intentional in-session edit, "
        f"set: {guidance.override_env_var_name}=1"
        if guidance.override_env_var_name
        else ""
    )
    return (
        f"DISPATCH GUARD - In-session edit of build-territory code detected.\n\n"
        f"  File: {file_path}\n\n"
        f"This session is editing build-territory code directly. Build work "
        f"should be dispatched to {guidance.builder_role_label}. "
        f"{guidance.redirect_instructions}"
        f"{override_line}\n\n"
        f"Continuing with the edit as requested (warn only -- never blocked)."
    )


__all__ = [
    "DispatchGuidance",
    "check_dispatch_discipline",
    "is_trivial_path",
]
