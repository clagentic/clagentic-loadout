"""push.namespace_guard — config-driven allowed-owner/namespace check.

Wave B slice 3 (lr-09ca, tome #688). The reference push transport (lr-ea62)
hardcoded a single allowed GitHub org literal ("clagentic") and refused any
other owner with
EXIT_NAMESPACE_DENIED. Repo CLAUDE.md hard rule 1 forbids `clagentic`
appearing anywhere in product code as a hardcoded owner check — namespaces
arrive via config, never as a baked brand string.

This module keeps the DENY capability (a real safety guard: an agent's push
scope should be bounded to known-good namespaces) while making the allowed
set entirely caller-supplied input. There is no default allowed-namespace
list baked into this module — callers wire ALLOWED_NAMESPACES_ENV_VAR or
pass an explicit set. An empty/unset allowlist is PERMISSIVE (no allowlist
configured -> no namespace restriction is enforced), matching the
standalone-deployment posture the rest of this package uses elsewhere
(review.contract's ReviewBackend, transport.git_host_api's known_bad_owners)
— restrictive behavior is opt-in via explicit config, not implicit in the
absence of it. A deployment that wants a hard allowlist sets the env var (or
passes allowed_namespaces explicitly); a standalone/dev deployment with no
config configured is not silently locked out of every repo.
"""

from __future__ import annotations

import os

from clagentic_loadout.push.errors import NamespaceDeniedError

#: Env var carrying a comma-separated allowed-owner/namespace list. Unset or
#: empty means "no allowlist configured" (permissive — see module docstring).
ALLOWED_NAMESPACES_ENV_VAR = "CLAGENTIC_LOADOUT_ALLOWED_NAMESPACES"


def resolve_allowed_namespaces(
    explicit: frozenset[str] | None = None,
    *,
    env: dict[str, str] | None = None,
) -> frozenset[str]:
    """Resolve the allowed-namespace set.

    Precedence:
      1. *explicit* (caller-supplied set, e.g. from a loaded config file or
         a --allowed-namespace CLI flag repeated N times) — always wins when
         not None, even if empty (an explicit empty set is a real choice:
         "restrict to nothing", handled by the caller's own validation, not
         silently reinterpreted as permissive here).
      2. ALLOWED_NAMESPACES_ENV_VAR, comma-separated, whitespace-trimmed,
         empty entries dropped.
      3. Empty frozenset (no restriction configured — permissive default).

    *env* overrides os.environ for tests; defaults to the real process
    environment.
    """
    if explicit is not None:
        return frozenset(explicit)
    active_env = env if env is not None else os.environ
    raw = active_env.get(ALLOWED_NAMESPACES_ENV_VAR, "")
    if not raw.strip():
        return frozenset()
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def check_namespace_allowed(
    owner: str,
    repo: str,
    *,
    allowed_namespaces: frozenset[str],
) -> None:
    """Refuse *owner* if an allowlist is configured and *owner* is absent
    from it.

    An EMPTY allowed_namespaces means "no allowlist configured" — every
    owner is permitted (permissive default, see module docstring). A
    NON-EMPTY allowed_namespaces enforces membership: *owner* must appear in
    it verbatim (case-sensitive — git-host owner names are case-sensitive on
    both Forgejo and GitHub).

    Raises NamespaceDeniedError before any credential is resolved or git
    operation attempted — a namespace refusal is deterministic and must
    never partially execute.
    """
    if not allowed_namespaces:
        return
    if owner not in allowed_namespaces:
        raise NamespaceDeniedError(
            f"push target {owner}/{repo} is not in the configured allowed-"
            f"namespace set ({sorted(allowed_namespaces)!r}). Set "
            f"{ALLOWED_NAMESPACES_ENV_VAR} (comma-separated) or pass an "
            f"explicit allowed-namespace list to permit this owner. "
            f"Refusing before any credential is resolved or git operation "
            f"attempted — this refusal is deterministic; do not retry "
            f"without changing the configured allowlist or the target "
            f"owner."
        )


__all__ = [
    "ALLOWED_NAMESPACES_ENV_VAR",
    "check_namespace_allowed",
    "resolve_allowed_namespaces",
]
