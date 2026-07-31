"""guard.env_prefix — exactly-anchored env-var-assignment-prefix admission
(lr-5a8d, folded-in lr-24b2, task comment #1).

PROBLEM this replaces: the reference deployment's guard-bash.py admits an
optional leading env-assignment token ahead of a real verb invocation
(``CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=<slug> loadout-review-post ...``) using
an UNCONSTRAINED grammar — ``[A-Za-z_][A-Za-z0-9_]*=\\S+`` — that admits ANY
variable name and ANY non-whitespace value, not just the one variable and
value-shape its own comments and error messages claim. That grammar is
exactly the shape a sibling rule in the same file (the correct one,
guarding ``CLAGENTIC_SUBAGENT_ID``/``CREW_SPAWN_AGENT_ID``) already
documents as unsafe: a generic ``VAR=value`` prefix can smuggle a
metacharacter-bearing value (e.g. ``${IFS}``-based word-splitting) that a
naive ``\\S+`` value class does not exclude.

THE FIX: this module admits EXACTLY ONE env-assignment shape —
``CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=<slug>`` — end-anchored on both the
variable name and the value's character class. No other variable name is
ever admitted through this seam, and the value itself is restricted to a
safe token grammar (alphanumeric, hyphen, underscore) with no shell
metacharacters of any kind. A command with no env-prefix at all is
unaffected — this module's job is narrow: given a command line, tell the
caller whether ITS OWN leading token is the one safe env-prefix shape, and
if so, return the command with that prefix stripped so a caller's own verb
classifier evaluates the real command underneath.
"""

from __future__ import annotations

import re

#: The ONE variable name this module ever admits as an env-assignment
#: prefix. Never generalized to an arbitrary variable — see module
#: docstring for why a generic grammar here is a known, previously-shipped
#: defect this module exists to not reproduce.
ALLOWED_ENV_PREFIX_VAR = "CLAGENTIC_LOADOUT_GITHUB_APP_SLUG"

#: Safe-token value grammar: alphanumeric, hyphen, underscore, 1-128 chars,
#: no leading hyphen (mirrors the value-class discipline the reference
#: deployment's OWN correct sibling rule, CLAGENTIC_SUBAGENT_ID/
#: CREW_SPAWN_AGENT_ID admission, already uses) — no shell metacharacter of
#: any kind is representable in this class, so no `${IFS}`/`;`/backtick/
#: `$(...)` word-splitting or command-substitution smuggling is possible
#: through the value position.
_SAFE_VALUE_RE = r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"

#: End-anchored on the variable name (no other name matches, ever) and on
#: the value's character class (only the safe-token grammar above matches
#: — an arbitrary `\S+` is never accepted). Anchored at the start of the
#: command; requires at least one whitespace character before the rest of
#: the command line follows.
_ENV_PREFIX_RE = re.compile(
    rf"^{re.escape(ALLOWED_ENV_PREFIX_VAR)}=({_SAFE_VALUE_RE})\s+(\S.*)$"
)


def strip_allowed_env_prefix(command: str) -> tuple[str | None, str]:
    """If *command* begins with EXACTLY the one admitted env-assignment
    prefix (``CLAGENTIC_LOADOUT_GITHUB_APP_SLUG=<safe-token-value>``,
    end-anchored on both name and value grammar), return
    ``(value, remainder)`` where *remainder* is the command line with that
    prefix and its trailing whitespace removed.

    If *command* has no such prefix — including a command with SOME
    env-assignment-shaped prefix that does not match this exact grammar
    (wrong variable name, or a value containing anything outside the safe-
    token class) — returns ``(None, command)`` UNCHANGED. This module never
    strips a near-miss prefix and hands the caller a still-prefixed
    remainder; a caller's own downstream verb classifier sees either the
    fully-stripped safe case, or the ENTIRE original command line to
    evaluate (and, in the near-miss case, correctly fail to recognize any
    known verb at its head, since the bogus prefix is still glued on).

    This function performs NO command execution and no environment
    mutation — it is a pure string classifier, meant to run ahead of a
    caller's own verb-allowlist check.
    """
    match = _ENV_PREFIX_RE.match(command)
    if not match:
        return None, command
    return match.group(1), match.group(2)


__all__ = [
    "ALLOWED_ENV_PREFIX_VAR",
    "strip_allowed_env_prefix",
]
