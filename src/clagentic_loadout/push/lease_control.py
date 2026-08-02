"""push.lease_control — explicit, printed `--force-with-lease` resolution
(lr-f57f13, D5 DECIDED; credentialed-fetch fix per a pre-merge security
review).

THE DEFECT THIS CLOSES: `push.verb` previously derived `force_with_lease`
SOLELY from `identity.pin_commits_to_bot_identity`'s return value (did
bot-identity re-authoring rewrite this branch's commit SHAs) with NO CLI
override anywhere. That derivation is a non-sequitur: re-authoring is
loadout's OWN act, and whether it happened says nothing about the state of
the remote. Combined with loadout never fetching before pushing, this
silently forced a `--force-with-lease` evaluation against a STALE local
remote-tracking ref on essentially every push where re-authoring fired
(which is most pushes) — turning a genuine, ordinary conflict into
`(stale info)`, the one rejection shape git prints with NO explanatory hint
block at all (unlike "fetch first," which gets five `hint:` lines).

THE FIX, three required parts (this task's own D5 DECIDED text):
  1. Fetch the remote-tracking ref before any lease evaluation (or refuse to
     force against a ref known to be unrefreshed) — `resolve_lease` below
     performs a `git fetch` of the target ref immediately before returning
     a `force_with_lease=True` resolution, UNLESS the caller has forced
     lease-off (no lease to evaluate) or the caller has already fetched via
     some other mechanism this call cannot see (best-effort: a `git fetch`
     failure here degrades to a printed warning, never a hard failure —
     see `LeaseResolution.fetch_warning`).
  2. An explicit CLI flag (`push.verb`'s `--force-with-lease` /
     `--no-force-with-lease`) that always wins over the derived value.
  3. PRINT the resolved lease state and its origin at push time — never
     infer it silently. `LeaseResolution.origin` is a caller-facing label
     (e.g. "cli-flag", "history-rewritten (auto)", "default-false") folded
     directly into `git_push_with_token`'s own `lease_origin` parameter, so
     a lease-related failure message carries its own derivation with no
     separate lookup needed.

NOT A REWRITE OF THE DERIVATION HEURISTIC: `history_rewritten` (did
bot-identity re-authoring change this branch's commit SHAs) is STILL a
legitimate default signal for "this push may need to force" — a rewritten
history genuinely IS a non-fast-forward against the remote's previous copy
of this branch. What changes is that (a) an explicit flag always overrides
it, (b) the remote-tracking ref is refreshed before the value is trusted,
and (c) the origin is always visible rather than silently assumed.

CREDENTIALED FETCH, NOT AN AMBIENT ONE (pre-merge security review, fixed
here): the FIRST shipped version of the pre-lease fetch ran a bare, uncredentialed
`subprocess.run(["git", "fetch", ...])` — authenticating via whatever
ambient credential helper happened to be configured for the remote, wholly
outside the minted-token path `git_push_with_token` enforces (an identity-
integrity defect on a public product, not merely a hygiene issue). It also
folded raw `git fetch` stderr VERBATIM into the printed warning with no
redaction — untrusted server-controlled text that can carry URL userinfo
or an Authorization:/Credential: header value on a 401/403. `resolve_lease`
now takes *token* and fetches via `push.git_push.git_fetch_with_token`
(the SAME `_credentialed_git_env` envelope `git_push_with_token` itself
uses), and `GitFetchError`'s own message (already redacted by
`git_fetch_with_token` via `push.push_redaction.redact_push_secrets`) is
what becomes `fetch_warning` — never `result.stderr` read directly here.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.push.git_push import GitFetchError, git_fetch_with_token

#: Caller-facing origin labels (printed, never silently inferred — see
#: module docstring, part 3).
LEASE_ORIGIN_CLI_FORCE = "cli-flag(--force-with-lease)"
LEASE_ORIGIN_CLI_NO_FORCE = "cli-flag(--no-force-with-lease)"
LEASE_ORIGIN_HISTORY_REWRITTEN = "history-rewritten(auto)"
LEASE_ORIGIN_DEFAULT_FALSE = "default-false(no-rewrite)"


@dataclass(frozen=True)
class LeaseResolution:
    """The resolved force-with-lease decision for one push, plus WHY (see
    module docstring, part 3 — never silently inferred)."""

    force_with_lease: bool
    origin: str
    fetch_attempted: bool
    fetch_warning: str | None = None


def resolve_lease(
    *,
    cli_force_with_lease: bool | None,
    history_rewritten: bool,
    remote: str,
    branch: str,
    project_root: Path,
    token: str,
) -> LeaseResolution:
    """Resolve the force-with-lease decision for this push (module
    docstring, D5 DECIDED, all three required parts).

    *cli_force_with_lease*: the caller's explicit `--force-with-lease` /
    `--no-force-with-lease` value, or None when neither flag was supplied
    (falls back to the *history_rewritten* signal). An explicit flag ALWAYS
    wins over the derived value — see LEASE_ORIGIN_CLI_FORCE/
    LEASE_ORIGIN_CLI_NO_FORCE.

    *history_rewritten*: `identity.pin_commits_to_bot_identity`'s return —
    still a legitimate default SIGNAL (not a silent derivation any more; see
    module docstring "NOT A REWRITE OF THE DERIVATION HEURISTIC"), used only
    when no explicit CLI value was given.

    *token*: the SAME minted token this invocation will push with —
    required so the pre-lease fetch runs through the SAME credentialed
    envelope (`push.git_push.git_fetch_with_token` /
    `_credentialed_git_env`) rather than an ambient credential helper (see
    module docstring, "CREDENTIALED FETCH, NOT AN AMBIENT ONE").

    When the resolved decision is to force, this function fetches *branch*
    from *remote* FIRST, credentialed with *token* — a lease evaluated
    against a ref this process just refreshed is evaluating against current
    remote state, not a stale local copy (module docstring, part 1). A
    fetch failure is NEVER a hard failure here: it degrades to
    `fetch_warning` (an already-redacted string via `GitFetchError`'s own
    message — the caller prints it with no further redaction step needed)
    and the push still proceeds with the CALLER'S requested lease value —
    refusing to push at all over a diagnostic pre-step failing would be a
    new failure mode this task's scope does not ask for; the push's own
    result (now correctly classified even on a stale-info rejection, see
    push.git_push) is still the authoritative outcome either way.
    """
    if cli_force_with_lease is True:
        force = True
        origin = LEASE_ORIGIN_CLI_FORCE
    elif cli_force_with_lease is False:
        force = False
        origin = LEASE_ORIGIN_CLI_NO_FORCE
    elif history_rewritten:
        force = True
        origin = LEASE_ORIGIN_HISTORY_REWRITTEN
    else:
        force = False
        origin = LEASE_ORIGIN_DEFAULT_FALSE

    if not force:
        return LeaseResolution(force_with_lease=False, origin=origin, fetch_attempted=False)

    try:
        git_fetch_with_token(remote, branch, token, project_root)
    except GitFetchError as exc:
        # str(exc) is ALREADY REDACTED -- git_fetch_with_token built it via
        # push.push_redaction.redact_push_secrets before raising. No second
        # redaction pass is applied (or needed) here.
        warning = (
            f"pre-lease {exc} -- proceeding with the resolved lease value "
            f"anyway; the remote-tracking ref this lease evaluates against "
            f"may be stale, which can surface as a '(stale info)' rejection."
        )
        return LeaseResolution(
            force_with_lease=force, origin=origin, fetch_attempted=True, fetch_warning=warning,
        )
    return LeaseResolution(force_with_lease=force, origin=origin, fetch_attempted=True)


__all__ = [
    "LEASE_ORIGIN_CLI_FORCE",
    "LEASE_ORIGIN_CLI_NO_FORCE",
    "LEASE_ORIGIN_DEFAULT_FALSE",
    "LEASE_ORIGIN_HISTORY_REWRITTEN",
    "LeaseResolution",
    "resolve_lease",
]
