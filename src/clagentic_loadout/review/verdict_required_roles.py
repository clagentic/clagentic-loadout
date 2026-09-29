"""review.verdict_required_roles — user-level config declaring which caller
ROLES must never post a fenceless review-post comment (lr-9ba589 fold-in #1).

BACKGROUND: the stamp/intent check `transport.body_env` now carries
(`carries_review_status` / `expect_verdict_route`) closes the seventh
incident's exact reproduction -- a body staged WITH `review_status` reaching
a plain-route invocation. It does NOT catch the sibling shape a fold-in
review identified as the shape that actually burned the ORIGINATING
incident: a body staged WITHOUT `review_status` at all (pure prose, no
verdict field anywhere), posted WITHOUT either verdict flag. Content and
route AGREE in that shape -- the stamp comparison passes cleanly, because
both sides say "plain" -- so the post succeeds fenceless with no signal at
either stage or post time. `TestPlainCommentStillWorksUnderNonBreakingDefault
::test_plain_comment_staged_and_posted_plain_still_succeeds` proves this
exact case still succeeds; that is not a gap in that test, it is a true
statement about what the stamp check alone can and cannot catch, since a
stamp comparison has no way to know that a GIVEN caller's role should never
be allowed to post a fenceless comment at all.

THIS MODULE closes that residual gap on a DIFFERENT axis: not "does the
staged content agree with the invocation," but "is this caller's ROLE one
that must always carry a verdict, regardless of what it staged." A
deployment declares `review.verdict_required_roles` (a list of role/caller
name strings) in the USER-LEVEL config file
(`transport.provider_config.load_user_config_section`, the SAME loader/
config-root convention `transport.read_host_guard`'s
`READ_HOST_CONFIG_SECTION`/`READ_HOST_CONFIG_KEY` already uses -- no second
YAML parser, no second config path, same section-naming convention:
`review:` mirrors `read_host_guard:` as a top-level section named after the
capability it governs). When the EFFECTIVE caller (the attested,
caller-bound identity `review.verb._run` already resolves before this check
runs -- see that module's own caller-binding block) is a member of that
list, and the invocation carries NEITHER `--verdict-review-status` NOR
`--verdict-findings`, this refuses with a reserved exit code and a
corrective message naming the two flags, BEFORE the staged body is consumed
and BEFORE any network call -- a retry needs no restaging, mirroring the
non-destructive posture every `transport.body_env` stamp-mismatch refusal
already has.

APPLIES TO BOTH BODY-INGESTION ROUTES (`--body-env` AND `--body-stdin`):
unlike the stamp check (which only exists because `--body-env` staging
produces a stamp to compare against), this check has nothing to do with
staged provenance -- it is purely "this caller's role + this invocation's
own flags," both known before either body-ingestion route is even touched.
A role-declared reviewer's fenceless post is exactly as wrong via
`--body-stdin` as via `--body-env`; scoping this check to one route would
leave the other one fully exposed to the identical incident shape for any
caller who (deliberately or not) reaches for the escape-hatch route instead.

DEFAULT: the config key ABSENT or an EMPTY list means NO roles are
role-required -- fully non-breaking, matching `read_host_guard`'s own
permissive-until-configured default and every other opt-in guard in this
package (`push.namespace_guard`, `push.host_guard`, `transport.git_host_api`'s
`known_bad_owners`). An unconfigured deployment sees zero behavior change:
every existing caller of `review-post`, on either body-ingestion route,
continues to work exactly as before this module shipped.

MALFORMED CONFIG IS A HARD ERROR, NEVER PERMISSIVE (reusing
`transport.read_host_guard`'s own fail-closed pattern, `InvalidReadHostConfigError`
"FAIL-OPEN FIX" and "ABSENT-VS-PRESENT-NULL FIX"): a PRESENT
`review.verdict_required_roles` value that is not a list of non-empty
strings -- including an explicit `verdict_required_roles: null` -- is a hard
config error (`InvalidVerdictRequiredRolesConfigError`), raised BEFORE any
credential mint or I/O, never silently degraded to "no roles declared." Only
a genuinely ABSENT key (the section/key not present in the user-level config
file at all) means "unconfigured" -- see `_load_verdict_required_roles`'s own
"ABSENT-VS-PRESENT-NULL" membership check, the same `key in section` pattern
`read_host_guard._load_configured_allowed_hosts` already uses rather than a
bare `.get()` (which cannot distinguish "absent" from "present and null").
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    USER_CONFIG_FILENAME,
    load_user_config_section,
)

#: Top-level section in the USER-LEVEL <config_root>/config.yaml carrying
#: this check's operator-declared role list. Named after the capability it
#: governs (review-post's verdict requirement), mirroring
#: transport.read_host_guard.READ_HOST_CONFIG_SECTION's own
#: capability-named-section convention -- NOT nested under an existing
#: section, since this is a distinct policy axis from both
#: `read_host_guard:` (host anchoring) and `merge:` (repo-tier gate
#: declarations read_host_guard/gate_config already own): this is a
#: USER-LEVEL, identity-adjacent policy ("which roles must always verdict"),
#: not a repo-committed gate declaration.
CONFIG_SECTION = "review"

#: Key within CONFIG_SECTION holding the list of role/caller-name strings
#: that must always post a review-post comment via the verdict route
#: (--verdict-review-status or --verdict-findings). A bare role/caller name,
#: never an agent name (CLAUDE.md rule 1) -- the same vocabulary
#: merge.gate_config's required_reviewer_roles/authorized_roles already use.
CONFIG_KEY = "verdict_required_roles"


class InvalidVerdictRequiredRolesConfigError(ValueError):
    """Raised when CONFIG_SECTION.CONFIG_KEY in the user-level config file
    holds a value that is not a list of non-empty role-name strings --
    reusing transport.read_host_guard.InvalidReadHostConfigError's own
    fail-closed pattern: a malformed or present-but-null config value must
    NEVER be treated as "unconfigured" (which would silently drop the
    protection an operator believed they had just turned on). Fires BEFORE
    any credential is resolved or body is read/consumed.
    """

    def __init__(self, config_path: Path, *, received: object, detail: str | None = None) -> None:
        received_type = type(received).__name__
        message = (
            f"{config_path}: [{CONFIG_SECTION}].{CONFIG_KEY} holds a "
            f"{received_type} ({received!r}), which is not a valid "
            f"verdict-required-roles list"
        )
        if detail:
            message += f" ({detail})"
        message += (
            ". Accepted form: a YAML list of non-empty role-name strings "
            '(e.g. ["reviewer", "security"]). Fix the '
            f"{CONFIG_KEY!r} value under the {CONFIG_SECTION!r} section in "
            f"{config_path}. Refusing before any credential is resolved or "
            "body is read/consumed -- a malformed config value is never "
            "treated as 'no roles declared' (which would silently disable "
            "the requirement the operator was trying to set)."
        )
        super().__init__(message)
        self.config_path = config_path
        self.received = received


def _config_file_path(config_root: str | Path | None) -> Path:
    """Resolve the on-disk path of the user-level config file *config_root*
    (or DEFAULT_USER_CONFIG_ROOT when None) points at -- used only to name
    the offending file in InvalidVerdictRequiredRolesConfigError's message,
    mirroring transport.read_host_guard._config_file_path exactly."""
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    return root / USER_CONFIG_FILENAME


def load_verdict_required_roles(config_root: str | Path | None = None) -> frozenset[str]:
    """Resolve CONFIG_SECTION.CONFIG_KEY from the user-level config file.

    Returns an EMPTY frozenset when the section/key is ABSENT (unconfigured
    -- no roles are verdict-required, the fully non-breaking default) or
    present as an explicit empty list (an operator's own deliberate
    "no roles required" declaration -- indistinguishable in effect from
    absence, exactly like read_host_guard's own empty-vs-absent handling one
    layer down does NOT need to distinguish here, since both cases mean
    "check never fires").

    Raises InvalidVerdictRequiredRolesConfigError when the key IS present
    (per an explicit `CONFIG_KEY in section` membership check -- the same
    ABSENT-VS-PRESENT-NULL pattern transport.read_host_guard.
    _load_configured_allowed_hosts uses, so an explicit
    `verdict_required_roles: null` is never silently treated as "absent")
    but its value is not a list of non-empty strings.
    """
    section = load_user_config_section(CONFIG_SECTION, config_root=config_root)
    if CONFIG_KEY not in section:
        return frozenset()
    raw = section.get(CONFIG_KEY)
    if not isinstance(raw, list):
        raise InvalidVerdictRequiredRolesConfigError(
            _config_file_path(config_root), received=raw
        )
    non_string_entries = [entry for entry in raw if not isinstance(entry, str) or not entry.strip()]
    if non_string_entries:
        raise InvalidVerdictRequiredRolesConfigError(
            _config_file_path(config_root),
            received=raw,
            detail=(
                f"list entry {non_string_entries[0]!r} "
                f"(type {type(non_string_entries[0]).__name__}) is not a "
                f"non-empty string"
            ),
        )
    return frozenset(entry.strip() for entry in raw)


class VerdictRequiredRoleRefusedError(Exception):
    """Raised when the effective caller is a role declared in
    CONFIG_SECTION.CONFIG_KEY, and the invocation carries neither
    --verdict-review-status nor --verdict-findings -- see
    check_verdict_required_role's own docstring for the full contract.
    Caught at the review-post CLI boundary and mapped to
    review.verb.EXIT_VERDICT_ROUTE_INTENT_MISMATCH: this is the SAME
    incident-class failure the stamp/intent check
    (transport.body_env.VerdictIntentMismatchError) already reports under
    that code -- both mean "this invocation would have posted a fenceless
    comment where a verdict was required" -- so a caller/harness catching
    that exit code already handles this refusal correctly with no new
    exit-code table entry needed."""


def check_verdict_required_role(
    caller: str,
    *,
    verdict_review_status: str | None,
    verdict_findings: bool,
    config_root: str | Path | None = None,
) -> None:
    """Refuse an invocation whose EFFECTIVE *caller* is declared
    verdict-required (CONFIG_SECTION.CONFIG_KEY) but supplies neither
    *verdict_review_status* nor *verdict_findings*.

    Checked by review.verb._run BEFORE any body is read or consumed on
    EITHER body-ingestion route (--body-env or --body-stdin) and BEFORE any
    network call -- see this module's own "APPLIES TO BOTH BODY-INGESTION
    ROUTES" docstring section. A refusal here never touches a staged
    --body-env file at all (this check runs before read_body_bytes is ever
    called), so a --body-env caller's staged pair survives untouched and a
    retry with the corrective flags needs no restaging -- the same
    non-destructive posture every transport.body_env stamp-mismatch refusal
    already has.

    *caller* must be the ATTESTED, caller-bound identity the verb has
    already resolved (transport.caller_binding.bind_caller), never a raw
    --caller argv string taken at face value -- an unattested caller string
    is exactly the shape lr-c75c9a's caller-binding fix exists to prevent
    from silently steering role-scoped behavior.

    Raises:
        InvalidVerdictRequiredRolesConfigError: the config value itself is
            malformed (propagated from load_verdict_required_roles, not
            caught here -- a malformed config must refuse exactly like a
            role match would, never be silently treated as "no match").
        VerdictRequiredRoleRefusedError: *caller* is declared
            verdict-required and neither verdict flag was supplied.
    """
    required_roles = load_verdict_required_roles(config_root)
    if not required_roles or caller not in required_roles:
        return
    if verdict_review_status is not None or verdict_findings:
        return
    raise VerdictRequiredRoleRefusedError(
        f"review-post: caller {caller!r} is declared in "
        f"[{CONFIG_SECTION}].{CONFIG_KEY} (verdict-required roles) in the "
        f"user-level config file, but this invocation supplies NEITHER "
        f"--verdict-review-status NOR --verdict-findings -- posting would "
        f"be a FENCELESS comment from a role this deployment requires to "
        f"always carry a merge-gate verdict (the exact incident shape this "
        f"check exists to close: content and route agreeing on 'plain' "
        f"gives no signal at either stage or post time that a verdict was "
        f"expected). Refusing BEFORE the staged body is consumed and "
        f"before any network call -- add --verdict-review-status <status> "
        f"--verdict-head-sha <sha> (or --verdict-findings "
        f"--verdict-head-sha <sha>) to this invocation. No restaging is "
        f"needed: a --body-env staged pair, if any, is left untouched by "
        f"this refusal."
    )


__all__ = [
    "CONFIG_KEY",
    "CONFIG_SECTION",
    "InvalidVerdictRequiredRolesConfigError",
    "VerdictRequiredRoleRefusedError",
    "check_verdict_required_role",
    "load_verdict_required_roles",
]
