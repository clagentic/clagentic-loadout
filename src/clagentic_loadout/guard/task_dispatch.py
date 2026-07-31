"""guard.task_dispatch — role-keyed Task/Agent dispatch admission (lr-59dd37,
port of the reference deployment's guard-task.py; lr-5a8d epic Wave C, the
first of this task's three small standalone guard ports).

PORT PATTERN — mirrors `guard.write_scope`'s established convention
(lr-fd279d, the PATTERN-SETTER for this whole decomposition):

1. STRIP THE HOOK SHELL. The reference file is a PreToolUse/Task stdin-
   JSON-in/exit-code-out hook: it reads a payload, branches on `agent_type`
   (a fixed set of internal agent names) and a harness-specific session
   sidecar file (`/tmp/lore-agent-name-<session_id>`) to classify a
   director/lead session, then returns an exit code. None of that harness
   plumbing is ported (CLAUDE.md rule 6a — no hard dependency on a specific
   harness's hook contract, no sidecar-file read, no env-var read for
   identity). Every function here is a pure function over explicit typed
   inputs; a caller's own harness adapter reads the payload/sidecar, maps
   its own identity model onto this module's role/session types, and calls
   in.

2. AGENT NAMES -> ROLES. The reference keys its per-identity Task-dispatch
   allowlist off six fixed agent names. This module reuses
   `guard.role_allowlist.BashRole` — the SAME role vocabulary the Bash-
   command admission decomposition already established — rather than
   inventing a second, parallel role enum for a second dispatch surface of
   the same identities (module docstring's "reuse landed primitives" rule
   applies to VOCABULARY, not just code). `TaskDispatchConfig` maps each
   `BashRole` a caller wants to grant Task-dispatch capability to its own
   allowed-subagent-type set; a role with no entry in the config denies
   every dispatch (fail-closed, no permissive default).

3. THE DIRECTOR/LEAD DENYLIST IS A SEPARATE CONCERN, NOT A ROLE BRANCH. The
   reference's second enforcement path (an operator-driven director/lead
   session denied from dispatching a NAMED CREW ROLE via the Agent tool,
   forcing crew dispatch through the reviewed relay/orchestration path
   instead) does not key off `BashRole` at all — it fires for a session that
   is *not* a spawned crew agent in the first place. `check_lead_agent_dispatch`
   takes the candidate `subagent_type` string and a caller-supplied
   `crew_role_names` set (the bare names a caller's own role registry
   considers "named crew roles" in its OWN vocabulary) instead of this
   module hardcoding any fixed identity roster — CLAUDE.md rule 1: no
   agent name literal anywhere in this file.

4. THE RELAY-ACTIVE BRANCH IS DEAD CODE, NOT PORTED. The reference's
   "relay env vars present" branch is documented in its own file as
   unreachable in practice (the relay transport this project's own history
   retired) and only flips a warn message; `check_lead_agent_dispatch`
   therefore always returns "allowed" for a bare-Agent-tool dispatch
   (mirroring the ONLY reachable reference behavior), never re-deriving a
   relay-liveness signal this module has no business owning (rule 6a: no
   dependency on a specific transport's liveness state).

5. NO PERMISSIVE FALLBACK FOR AN UNMAPPED ROLE. `check_task_dispatch` raises
   `ValueError` for a `BashRole` absent from `TaskDispatchConfig.role_grants`
   — a caller passing an unconfigured role is a config error, never a
   silent allow (mirrors `guard.role_allowlist.check_bash_call`'s own
   contract for the same reason).

WHAT THIS MODULE DELIBERATELY DOES NOT PORT:
  - Agent identity DETECTION (the four-path chain in the reference's
    internal agent-detection helper module: payload `agent_type`, session
    sidecar, per-spawn sidecar, an internal-tooling identity environment
    variable, spawn-meta JSON) — that is harness-specific attestation
    plumbing (rule 6a); a caller resolves its own attested role/identity
    upstream and passes the result in.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from clagentic_loadout.guard.role_allowlist import BashRole


@dataclass(frozen=True)
class TaskDispatchConfig:
    """Caller-supplied Task-dispatch allowlist, keyed by `BashRole`.

    role_grants: maps a `BashRole` to the set of `subagent_type` values that
        role may dispatch via Task. A role absent from this mapping is a
        config error at `check_task_dispatch` call time (fail-closed — see
        module docstring point 5), not a silent allow. An explicit EMPTY
        frozenset for a present role means "this role dispatches no Task
        calls at all" (reference: the reference deployment's several
        never-dispatch identities) — a caller that wants "no Task
        capability" for a role should map it here rather than omitting it,
        since omission raises instead of denying.
    """

    role_grants: dict[BashRole, frozenset[str]] = field(default_factory=dict)


def check_task_dispatch(
    role: BashRole,
    subagent_type: str,
    config: TaskDispatchConfig,
) -> tuple[bool, str]:
    """Return (ok, reason): is *role* permitted to dispatch
    `Task(subagent_type=subagent_type)`?

    An empty `subagent_type` (a Task call with no named target) is treated
    identically to any other value — a caller wanting the reference's
    "leaf-node fallback" behavior for a specific role expresses that via
    `TaskDispatchConfig.role_grants`, not a special case in this function.

    Raises `ValueError` if *role* has no entry in
    `config.role_grants` (module docstring point 5 — unmapped role is a
    config/sequencing error, never a silent allow).
    """
    if role not in config.role_grants:
        raise ValueError(
            f"BashRole {role!r} has no TaskDispatchConfig.role_grants entry; "
            f"a caller must explicitly grant (possibly empty) Task-dispatch "
            f"capability for every role it evaluates."
        )

    allowed = config.role_grants[role]
    if subagent_type in allowed:
        return True, ""

    if not allowed:
        return False, (
            f"role {role.value!r} does not dispatch Task calls at all; "
            f"attempted dispatch to subagent_type={subagent_type!r} denied. "
            f"This role is a leaf node — return a bounce_target or "
            f"suggested_downstream and let the orchestrator dispatch instead."
        )

    return False, (
        f"role {role.value!r} attempted Task dispatch to "
        f"subagent_type={subagent_type!r}, which is not in its allowed "
        f"roster {sorted(allowed)!r}. If this subagent type should be "
        f"allowed for this role, extend the caller's TaskDispatchConfig."
    )


def check_lead_agent_dispatch(
    subagent_type: str,
    crew_role_names: frozenset[str],
) -> tuple[bool, str]:
    """Return (ok, reason): may a lead/director session (a session that is
    NOT itself a spawned crew role — see module docstring point 3) dispatch
    `Agent(subagent_type=subagent_type)`?

    This is the reference's second, INDEPENDENT enforcement path
    (`_is_director_or_lead_session` + the crew-role denylist), evaluated
    ONLY once a caller's own harness adapter has already determined the
    calling session is not itself an attested crew role (the reference's
    `agent_type not in _CREW_AGENTS` branch) — `check_task_dispatch` above
    is the correct function for an attested crew role's OWN Task-dispatch
    admission.

    *crew_role_names* is a caller-supplied set of bare role-name strings
    (its own vocabulary for "these identities are named crew roles that
    bypass the crew dispatch/review path if invoked directly via Agent") —
    this module hardcodes no agent name (CLAUDE.md rule 1).

    An empty *subagent_type* is allowed (reference: a generic Task call with
    no named target is not a crew-role-bypass attempt). Only a
    *subagent_type* that case-sensitively matches an entry in
    *crew_role_names* is denied — every other value (researcher/utility/
    catch-all subagent types) is allowed, matching the reference's DENYLIST
    (not allowlist) posture: this function widens gracefully to any new,
    non-crew subagent type a caller's harness introduces without an edit
    here, per the reference module's own documented rationale.
    """
    if not subagent_type:
        return True, ""

    if subagent_type in crew_role_names:
        return False, (
            f"a lead/director session attempted Agent dispatch to "
            f"subagent_type={subagent_type!r}, a named crew role. Dispatching "
            f"a crew role directly via the Agent tool from an orchestrating "
            f"session bypasses the crew's dispatch/review path. Dispatch "
            f"crew work through the reviewed orchestration path instead."
        )

    return True, ""


__all__ = [
    "TaskDispatchConfig",
    "check_lead_agent_dispatch",
    "check_task_dispatch",
]
