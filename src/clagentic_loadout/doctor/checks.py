"""doctor.checks — the individual deployment-conformance checks (lr-e625).

Every check function is pure and testable in isolation: it takes explicit
inputs (config roots, repo roots, env mappings, an injectable probe runner),
never reads a hidden global, never prints, and never calls sys.exit — see
CheckResult below. cli.py is the only place outcome maps to a process exit
code or stdout/stderr text, matching every other verb in this package
(credential_provider.CredentialProviderError, roles.InvalidRoleConfigError,
etc. all follow the same "raise/return a value, let the CLI translate"
split).

Seven checks, matching the task's numbered scope plus one added later
(lr-f9a01b, item 3b below):

  1. `check_credentials` — for each configured platform
     (transport.provider_config.PROVIDER_KIND_COMMAND), resolve the
     token_command_forgejo / token_command_github argv, confirm the command
     exists, is executable, and is not world-writable, then run it with a
     probe caller (never a real role name — see PROBE_CALLER) and classify
     the outcome: ok / not-configured (static provider, nothing to probe) /
     not-found / not-executable / world-writable / downstream-failure. This
     is deliberately NOT the same as actually minting a token for a real
     caller — a doctor run must never consume/rotate a real credential as a
     side effect of a health check.
  2. `check_github_app_slugs_coverage` — the exact lr-e41f gap: every caller
     the deployment's DECLARED CALLER REGISTRY names MUST have a
     `github_app.slugs.<caller>` entry once ANY per-caller slug map exists at
     all; flags callers with no slugs entry AND slugs entries with no
     matching caller (both directions — an orphaned slugs entry is stale
     config, not a failure, but doctor names it as different that a stale
     read is not silently indistinguishable from a live gap). The caller
     registry is `github_app.callers` (transport.github_app_config.
     CONFIG_KEY_CALLERS) when a deployment has declared one, falling back to
     provisioning.roles' bare-role-name taxonomy only as a reference default
     — lr-46a83a: `slugs` is keyed by whatever string a deployment's harness
     passes as `--caller` at runtime, which is NOT guaranteed to be a bare
     role name (an agent-name-keyed deployment is equally valid), so this
     check must never assume the role taxonomy IS the caller key-space.
  2b. `check_attestation_source_configured` (lr-8e1593, revives the one
     legitimate hardening from lr-424519) — WARN when `github_app.callers`
     is declared but no transport.attestation source (layer 1 configured-env,
     or layer 2 sidecar single-path/adapter-list) is configured at all: every
     invocation falls through to the built-in OS-user layer, which
     bind_caller then compares against the declared callers -- the exact
     lr-1cd33c 'root' attribution failure shape. Presence-only check; never
     resolves a real identity.
  3. `check_repo_loadout_schema` — validates a repo's `.loadout/config.yaml`
     by running it through every section owner this package already ships
     (wait.config, provisioning.roles, merge.post_merge_config,
     merge.pre_checks_config, merge.gate_config — lr-0a03c3 added the last
     two: pre_checks, merge_requirements, required_reviewer_roles,
     authorized_roles, all under the existing `merge:` section) rather than
     inventing a second schema — a malformed section is caught by the SAME
     loader/validator the verb that actually reads it at runtime would use,
     so doctor can never diverge from what "valid" means for that section.
     A `merge:` section present but omitting `required_reviewer_roles`
     entirely is one such FAIL (`RequiredReviewerRolesNotDeclaredError`,
     lr-638945 — see merge.gate_config's own docstring, "ABSENCE SEMANTICS,"
     for the fail-open-on-silent-absence gap this closes). Also cross-checks
     every role named in `required_reviewer_roles`/`authorized_roles`
     against this repo's own role declaration, with SEVERITY split on
     whether the repo actually wrote one (lr-638945 comment #1 — the live
     clagentic-github incident: `required_reviewer_roles: [reviewer,
     security]` with no `security` key in `roles:`, doctor reporting a
     clean 6/6):
       - This repo's own `roles:` section is PRESENT (not a fallback) and a
         gate role matches no key in it -> FAIL (`ok=False`) — the config as
         written can never satisfy its own gate ("an unsatisfiable gate is
         worse than an absent one, because it reads as protection," comment
         #1, verbatim).
       - This repo declares NO `roles:` section at all (falls back to
         `provisioning.roles.DEFAULT_ROLE_VERBS`, a reference default, not
         this repo's own declaration) and a gate role matches nothing in
         that reference set -> WARN (`ok` stays True) — not provably
         unsatisfiable, since the deployment may resolve roles through a
         mechanism doctor cannot see; role vocabulary is deliberately
         open-ended (a repo may invent role names), so a fallback-only
         mismatch is a review prompt, never a hard rejection of an
         unrecognized-but-legitimate role name.

     Also flags a `credentials:` section (repo-local credentials config is
     never honored — transport.provider_config's own rejection rule; doctor
     surfaces it as a conformance finding, not just a runtime stderr
     warning).
  3b. `check_dead_crew_post_merge_config` (lr-f9a01b) — a DIFFERENT config
     surface than `check_repo_loadout_schema` above: `.crew/<role>.yaml`
     files (crew-dispatch config, not this package's own repo-local
     `.clagentic/loadout/config.yaml`) can declare a `post_merge_steps` key
     that `merge.post_merge_config.load_post_merge_steps` has NEVER read —
     both the missing-section and missing-key path there are documented,
     silent no-ops. FAILs when a `.crew/*.yaml` file declares
     `post_merge_steps` while the repo's own live config never explicitly
     declares the key — a repo in that shape gets a clean `loadout-merge`
     exit with `steps_run=0` and never deploys, with nothing in that run's
     output naming the file the steps actually needed to live in. THIS IS
     DOCTOR-ONLY AND POST-HOC: it fires only when someone runs
     `loadout-doctor --repo-root`. `merge.verb._run`'s step 10 surfaces the
     SAME cross-check as a loud, non-blocking WARNING at the point a merge
     actually happens — the path that runs unattended — so the class is
     caught even when nobody thinks to run doctor; see that module's
     docstring, "DEAD .crew/<role>.yaml post_merge_steps CROSS-CHECK".
  4. `check_builder_identity_config` (lr-0a03c3) — DEPLOYMENT-TIER: validates
     the USER-LEVEL config.yaml's `builder_identity:` section
     (push.identity_config) and `review.reviewer_logins:` map
     (review.login_config), the SAME loaders `push.verb`'s commit
     re-authoring and a `--required-reviewer` login-override resolution
     would use — never a second schema. Both sections are OPTIONAL; a
     deployment that configures neither is a no-op pass (there is nothing to
     validate, not a gap — commit re-authoring and the login-override tier
     are both opt-in features).
  5. Exit-code / CLI hygiene is cli.py's concern, not this module's — see
     that module's docstring.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

from clagentic_loadout.doctor.credential_validity import (
    CREDENTIAL_STATE_OK,
    CredentialProbeResult,
    probe_forgejo_credential,
    probe_github_credential,
)
from clagentic_loadout.merge.gate_config import (
    CONFIG_KEY_AUTHORIZED_ROLES,
    CONFIG_KEY_REQUIRED_REVIEWER_ROLES,
    InvalidMergeGateConfigError,
    load_authorized_roles,
    load_merge_requirements,
    load_required_reviewer_roles,
)
from clagentic_loadout.merge.post_merge import PostMergeConfigError
from clagentic_loadout.merge.post_merge_config import (
    CONFIG_KEY_POST_MERGE_STEPS,
    CONFIG_SECTION_MERGE,
    CREW_CONFIG_DIR_NAME,
    find_crew_yaml_files_declaring_post_merge_steps,
    load_post_merge_steps,
    post_merge_steps_key_declared,
)
from clagentic_loadout.merge.pre_checks_config import load_pre_checks
from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.provisioning.roles import (
    CONFIG_SECTION_ROLES,
    DEFAULT_ROLE_VERBS,
    InvalidRoleConfigError,
    load_role_verbs,
)
from clagentic_loadout.push.identity_config import (
    InvalidBuilderIdentityConfigError,
    load_builder_identity,
)
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_MARKER,
    LEGACY_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
)
from clagentic_loadout.review.login_config import (
    CONFIG_KEY_REVIEWER_LOGINS,
    CONFIG_SECTION_REVIEW,
)
from clagentic_loadout.transport.attestation import (
    ATTESTATION_CONFIG_KEY_IDENTITY_ENV,
    ATTESTATION_CONFIG_KEY_SIDECAR_PATH,
    ATTESTATION_CONFIG_KEY_SIDECARS,
    ATTESTATION_CONFIG_SECTION,
    ATTESTED_IDENTITY_ENV_VAR,
    ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR,
)
from clagentic_loadout.transport.credential_provider import (
    CredentialProviderError,
    TokenProvider,
    resolve_token,
)
from clagentic_loadout.transport.git_host_api import _resolve_git_host_base
from clagentic_loadout.transport.github_app_config import (
    CONFIG_KEY_SLUGS,
    CONFIG_SECTION_GITHUB_APP,
    read_configured_callers,
    read_configured_slugs,
)
from clagentic_loadout.transport.provider_config import (
    CONFIG_SECTION_CREDENTIALS,
    InvalidProviderConfigError,
    PROVIDER_KIND_COMMAND,
    has_repo_local_credentials_section,
    load_user_config_section,
    resolve_platform_provider,
    resolve_provider_kind_and_command,
)
from clagentic_loadout.wait.config import (
    InvalidScopedTestConfigError,
    load_scoped_test_patterns,
)

#: Bare probe-caller token used to exercise a configured token-command
#: helper without ever touching a real role's identity or consuming a real
#: mint (a probe run is expected to fail downstream of the helper actually
#: executing -- see check_credentials's "failure_mode" classification; this
#: is a health check, not a credential resolution). Deliberately NOT a real
#: role name in
#: provisioning.roles.DEFAULT_ROLE_VERBS or anywhere in a deployment's own
#: `roles:` taxonomy, so a probe run can never be mistaken for (or collide
#: with) a real caller's own mint.
PROBE_CALLER = "loadout-doctor-probe"

#: Hard ceiling on how long a probe invocation is allowed to run -- doctor
#: is a fast health check, not a long-poll; a hung credential-minting
#: command must not hang the whole conformance run.
PROBE_TIMEOUT_SECONDS = 10.0

PLATFORMS: tuple[str, ...] = (PLATFORM_FORGEJO, PLATFORM_GITHUB)


@dataclass(frozen=True)
class CheckResult:
    """One check's outcome. `ok` gates the doctor verb's overall exit code;
    `resolved` carries every value the check actually inspected (conformance
    rule 4: report resolved values, never a stale guess) so a failure's
    stderr/report line is self-diagnosing without re-running doctor with
    extra flags."""

    name: str
    ok: bool
    summary: str
    resolved: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 1. Credentials: token_command_* helper existence/permissions + probe.
# ---------------------------------------------------------------------------


def _classify_command_path(command_str: str) -> tuple[str, str | None]:
    """Resolve the executable half of a configured command string and
    classify its on-disk state: ("ok", path) | ("not-found", path) |
    ("not-executable", path) | ("world-writable", path).

    Extracts argv[0] via shlex.split (lr-b2d1c3 parity fix -- see
    check_credentials's own docstring for why this must match the real
    minting path's word-splitting exactly) rather than a bare whitespace
    .split(); a malformed/unbalanced-quote command string degrades to
    "not-found" here (a value the real minting path would also fail to
    parse) rather than raising out of a health check.
    """
    try:
        tokens = shlex.split(command_str)
    except ValueError:
        return "not-found", None
    first_token = tokens[0] if tokens else ""
    if not first_token:
        return "not-found", None

    resolved_path = first_token if os.sep in first_token else shutil.which(first_token)
    if not resolved_path or not os.path.exists(resolved_path):
        return "not-found", resolved_path

    try:
        file_stat = os.stat(resolved_path)
    except OSError:
        return "not-found", resolved_path

    if not (file_stat.st_mode & stat.S_IXUSR):
        return "not-executable", resolved_path

    if file_stat.st_mode & stat.S_IWOTH:
        return "world-writable", resolved_path

    return "ok", resolved_path


def check_credentials(
    *,
    config_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    probe_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> list[CheckResult]:
    """One CheckResult per platform (PLATFORMS) whose configured provider
    kind is "command" (transport.provider_config.PROVIDER_KIND_COMMAND).

    A platform configured as "static" (the default, no command helper at
    all) or with no signal in either env or the user-level config file
    yields a `not-configured` (ok=True) result -- there is no helper to
    validate, and that is a legitimate deployment shape, not a gap.

    For a "command" platform, resolves the SAME argv provider_config would
    build (env var wins over config file, per platform -- reusing
    `resolve_provider_kind_and_command` rather than re-deriving the
    precedence rule a second time), then:

      1. Confirms the command's executable half exists on PATH/disk, is
         executable, and is not world-writable -- classifying whichever
         condition fails first (see `_classify_command_path`).
      2. Only if (1) passes: runs the command with PROBE_CALLER appended/
         substituted exactly like a real resolve_token call would (via
         `probe_runner`, injectable for tests -- production default execs
         the real argv with `shell=False`, capped at PROBE_TIMEOUT_SECONDS).
         The probe's own exit code/stdout is NEVER treated as "the helper is
         broken" -- a real minting command legitimately refuses an
         unrecognized probe caller (e.g. "no such role"). This step
         classifies WHICH failure mode occurred:
           - "ok": probe exited 0 with non-empty stdout (a helper willing to
             mint for an arbitrary/unrecognized caller -- unusual but not a
             doctor-detectable defect).
           - "downstream-refusal": probe exited non-zero or empty stdout --
             the EXPECTED shape for a correctly configured helper refusing
             an unrecognized probe caller. Reported as ok=True: the helper
             itself ran and produced a coherent refusal, which is exactly
             what "the helper exists and works" means for a probe call this
             module deliberately does not expect to succeed.
           - "missing-mapping" is never inferred from the probe alone (a
             generic non-zero exit cannot distinguish "no such caller" from
             any other downstream failure) -- see check_github_app_slugs_coverage
             for the ONE mapping-completeness check this module makes, which
             is config-shape-based, not probe-based.

    Never mints/consumes a REAL credential: PROBE_CALLER is a fixed sentinel
    that is not a real role/caller in any deployment's own taxonomy.
    """
    active_env = env if env is not None else dict(os.environ)
    results: list[CheckResult] = []

    for platform in PLATFORMS:
        try:
            kind, command_str = resolve_provider_kind_and_command(
                platform, env=active_env, config_root=config_root
            )
        except InvalidProviderConfigError as exc:
            # A doctor check reports an unrecognized provider kind as a
            # finding, never lets it propagate as an uncaught exception out
            # of a health-check run.
            results.append(
                CheckResult(
                    name=f"credentials:{platform}",
                    ok=False,
                    summary=f"{platform}: invalid provider config -- {exc}",
                    resolved={"platform": platform, "error": str(exc)},
                )
            )
            continue

        if kind != PROVIDER_KIND_COMMAND:
            results.append(
                CheckResult(
                    name=f"credentials:{platform}",
                    ok=True,
                    summary=f"{platform}: provider kind {kind!r} (no command helper configured)",
                    resolved={"platform": platform, "provider_kind": kind},
                )
            )
            continue

        path_state, resolved_path = _classify_command_path(command_str or "")
        resolved = {
            "platform": platform,
            "provider_kind": kind,
            "configured_command": command_str,
            "resolved_path": resolved_path,
        }

        if path_state != "ok":
            results.append(
                CheckResult(
                    name=f"credentials:{platform}",
                    ok=False,
                    summary=(
                        f"{platform}: token_command helper {path_state} "
                        f"(configured_command={command_str!r}, "
                        f"resolved_path={resolved_path!r})"
                    ),
                    resolved={**resolved, "failure_mode": path_state},
                )
            )
            continue

        # shlex.split (lr-b2d1c3): the SAME word-splitting
        # transport.provider_config.resolve_platform_provider uses to build
        # the real minting argv -- a quoted/space-bearing token_command must
        # be probed identically to how it is minted, so doctor can never
        # pass/fail differently than the live mint on the same config value.
        argv = shlex.split(command_str) + [PROBE_CALLER]
        try:
            if probe_runner is not None:
                proc = probe_runner(argv)
            else:
                proc = subprocess.run(
                    argv, shell=False, capture_output=True, timeout=PROBE_TIMEOUT_SECONDS
                )
        except subprocess.TimeoutExpired:
            results.append(
                CheckResult(
                    name=f"credentials:{platform}",
                    ok=False,
                    summary=f"{platform}: probe call to {command_str!r} timed out after {PROBE_TIMEOUT_SECONDS}s",
                    resolved={**resolved, "failure_mode": "probe-timeout"},
                )
            )
            continue
        except OSError as exc:
            results.append(
                CheckResult(
                    name=f"credentials:{platform}",
                    ok=False,
                    summary=f"{platform}: probe call to {command_str!r} failed to start -- {exc}",
                    resolved={**resolved, "failure_mode": "probe-exec-failed"},
                )
            )
            continue

        probe_stdout = proc.stdout.decode("utf-8", errors="replace").strip() if isinstance(proc.stdout, bytes) else str(proc.stdout or "").strip()
        if proc.returncode == 0 and probe_stdout:
            failure_mode = "ok"
            summary = f"{platform}: token_command helper resolved OK (probe caller minted)"
        else:
            failure_mode = "downstream-refusal"
            summary = (
                f"{platform}: token_command helper ran and refused the probe "
                f"caller (exit={proc.returncode}) -- expected shape for a "
                f"correctly configured helper"
            )
        results.append(
            CheckResult(
                name=f"credentials:{platform}",
                ok=True,
                summary=summary,
                resolved={
                    **resolved,
                    "failure_mode": failure_mode,
                    "probe_exit_code": proc.returncode,
                },
            )
        )

    return results


# ---------------------------------------------------------------------------
# 1b. Credential VALIDITY: what does the HOST say about a real, already-
#     resolved credential (lr-0eeb0c) -- distinct from check_credentials
#     above, which only validates the token_command HELPER's own
#     existence/permissions via a deliberately-fake PROBE_CALLER that is
#     EXPECTED to be refused. This check mints/resolves a REAL credential for
#     a REAL caller (opt-in via *caller* -- omitted, this check is a no-op,
#     since there is no real identity to validate without one) and asks the
#     host itself what it thinks of it, distinguishing five states never
#     collapsed into each other -- see doctor.credential_validity's own
#     module docstring for the full state taxonomy and the malformed-token
#     evidence this check exists to catch.
# ---------------------------------------------------------------------------


def _credential_probe_check_result(platform: str, probe: CredentialProbeResult) -> CheckResult:
    """Translate one CredentialProbeResult into a CheckResult. Only
    CREDENTIAL_STATE_OK is `ok=True` -- every other state (including
    UNREACHABLE, an infrastructure fault rather than a credential fault) is
    reported as a FINDING (`ok=False`) here, because doctor's job is to
    surface "this identity's credential is not currently usable" regardless
    of WHY; a caller wanting to distinguish infrastructure from credential
    faults reads `resolved['state']`, which always carries the full,
    un-collapsed classification."""
    ok = probe.state == CREDENTIAL_STATE_OK
    summary = f"{platform}: credential-validity={probe.state} -- {probe.detail}"
    if probe.remaining_lifetime_seconds is not None:
        summary += f" (remaining_lifetime_seconds={probe.remaining_lifetime_seconds})"
    return CheckResult(
        name=f"credential_validity:{platform}",
        ok=ok,
        summary=summary,
        resolved={
            "platform": platform,
            "state": probe.state,
            "token_sha256": probe.token_sha256,
            "remaining_lifetime_seconds": probe.remaining_lifetime_seconds,
            **probe.resolved,
        },
    )


def check_credential_validity(
    *,
    caller: str | None,
    config_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    repo_root: str | Path | None = None,
    token_provider_forgejo: TokenProvider | None = None,
    token_provider_github: TokenProvider | None = None,
    opener=None,
) -> list[CheckResult]:
    """One CheckResult per platform confirming what the HOST says about
    *caller*'s currently-resolved credential -- never a config-file read
    (see doctor.credential_validity's own module docstring, NEVER READ
    CONFIG).

    *caller* is REQUIRED to be non-None/non-empty for this check to run at
    all: unlike check_credentials' deliberately-fake PROBE_CALLER (which
    validates the HELPER, not a real identity), this check resolves a REAL
    credential through the SAME `transport.credential_provider`/
    `transport.provider_config` seam every mutating verb in this package
    already uses -- there is no real identity to validate without a real
    caller name, so an omitted/empty *caller* returns an EMPTY list (a
    no-op, not a failure): doctor's default run never mints a credential as
    a side effect of a health check unless the operator explicitly asked it
    to, by naming who to check.

    *token_provider_forgejo* / *token_provider_github* are injectable
    (mainly for tests, and for a caller that already holds a concrete
    TokenProvider) -- defaults to whatever `resolve_platform_provider`
    resolves from *config_root*/*env*, the identical provider-selection rule
    `check_credentials` and every push/review/merge verb already use.

    Each platform's credential resolution failure (CredentialProviderError --
    e.g. the configured command helper itself refused, or is misconfigured)
    is reported as its own FINDING here (`ok=False`), distinct from any of
    the five CREDENTIAL_STATES: a check that cannot even RESOLVE a
    credential to probe is a different, earlier-stage gap than one that
    resolved a credential the host then rejected.

    Never pushes, never mutates, never logs the resolved token (see
    doctor.credential_validity's own module docstring).
    """
    if not caller:
        return []

    active_env = env if env is not None else dict(os.environ)
    results: list[CheckResult] = []

    for platform, injected_provider in (
        (PLATFORM_FORGEJO, token_provider_forgejo),
        (PLATFORM_GITHUB, token_provider_github),
    ):
        provider = injected_provider
        if provider is None:
            try:
                provider = resolve_platform_provider(
                    platform,
                    repo_root=repo_root,
                    config_root=config_root,
                    env=active_env,
                )
            except InvalidProviderConfigError as exc:
                results.append(
                    CheckResult(
                        name=f"credential_validity:{platform}",
                        ok=False,
                        summary=f"{platform}: invalid provider config -- {exc}",
                        resolved={"platform": platform, "error": str(exc)},
                    )
                )
                continue

        try:
            token = resolve_token(caller, provider)
        except CredentialProviderError as exc:
            results.append(
                CheckResult(
                    name=f"credential_validity:{platform}",
                    ok=False,
                    summary=f"{platform}: could not resolve a credential for caller {caller!r} -- {exc}",
                    resolved={"platform": platform, "caller": caller, "error": str(exc)},
                )
            )
            continue

        if platform == PLATFORM_FORGEJO:
            git_host_base = _resolve_git_host_base(
                None, env=active_env, config_root=config_root
            )
            probe = probe_forgejo_credential(git_host_base, token, opener=opener)
        else:
            probe = probe_github_credential(token, opener=opener)

        results.append(_credential_probe_check_result(platform, probe))

    return results


# ---------------------------------------------------------------------------
# 2. github_app.slugs coverage -- the exact lr-e41f gap.
# ---------------------------------------------------------------------------


def check_github_app_slugs_coverage(
    *,
    repo_root: str | Path | None = None,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    config_root: str | Path | None = None,
) -> CheckResult:
    """Every caller in the deployment's DECLARED CALLER REGISTRY MUST have a
    `github_app.slugs.<caller>` entry once ANY per-caller slugs map is
    configured at all. Flags BOTH directions (the exact lr-e41f gap was
    direction 1: code referenced a caller string that had no slugs entry):

      1. registry callers with NO matching slugs entry.
      2. slugs entries with NO matching registry caller (stale config -- not
         itself a failure, since a slug map may deliberately serve callers
         outside the loaded registry -- reported as a finding for
         visibility, not ok=False, since "extra" entries are not a
         functional gap the way "missing" entries are).

    CALLER KEY-SPACE (lr-46a83a): `github_app.slugs` is keyed by whatever
    string a deployment's own harness passes as review.verb's `--caller` at
    runtime -- see transport.github_app_config.CONFIG_KEY_CALLERS's
    docstring. That is NOT necessarily provisioning.roles' bare ROLE-name
    taxonomy (builder/reviewer/merger/lead): a deployment's token-helper may
    dispatch on a spawned-process/worker IDENTIFIER instead (mapped to
    gatekeeper roles internally), in which case `slugs` is keyed by that
    identifier too, and comparing it against `load_role_verbs()` is
    validating the WRONG key-space entirely -- a doctor FALSE-POSITIVE (an
    identifier-keyed deployment can never satisfy a role-keyed expectation)
    that also hides the real gap (a caller-registry entry with no slug is
    real regardless of which key-space is in use).

    Resolution order for the expected-caller set:
      1. `github_app.callers` (user-level config, CONFIG_KEY_CALLERS) --
         the deployment's OWN declaration of its caller key-space. Always
         wins when present: this is the authoritative source once a
         deployment has stated it, taking precedence over the role-taxonomy
         default even when that default happens to be non-empty.
      2. provisioning.roles.load_role_verbs() -- the REFERENCE default for a
         deployment that has not declared `github_app.callers` (e.g. one
         that genuinely does key slugs by bare role name, matching the seed
         taxonomy exactly). Used only when (1) is absent.

    When NO `slugs` map is configured at all (only the single global `slug`
    fallback, or nothing), this check is a no-op pass: the pre-lr-d72d single
    -global-slug shape is a legitimate, fully supported deployment shape
    with no per-caller coverage question to ask.
    """
    slugs = read_configured_slugs(config_root)
    if not slugs:
        return CheckResult(
            name="github_app_slugs_coverage",
            ok=True,
            summary="no github_app.slugs map configured (single-global-slug or unconfigured shape; no per-caller coverage to check)",
            resolved={"slugs_configured": False},
        )

    declared_callers_list = read_configured_callers(config_root)
    if declared_callers_list is not None:
        caller_source = "github_app.callers"
        declared_callers = set(declared_callers_list)
    else:
        try:
            role_verbs = load_role_verbs(repo_root, config_relative_path=config_relative_path)
        except InvalidRoleConfigError as exc:
            return CheckResult(
                name="github_app_slugs_coverage",
                ok=False,
                summary=f"could not resolve the role taxonomy to check slugs coverage against -- {exc}",
                resolved={"slugs_configured": True, "error": str(exc)},
            )
        caller_source = "provisioning.roles (reference default)"
        declared_callers = set(role_verbs)

    slug_callers = set(slugs)

    missing_slugs = sorted(declared_callers - slug_callers)
    orphaned_slugs = sorted(slug_callers - declared_callers)

    ok = not missing_slugs
    if ok and not orphaned_slugs:
        summary = (
            f"every declared caller ({', '.join(sorted(declared_callers)) or '<none>'}) "
            f"has a github_app.slugs entry (caller registry: {caller_source})"
        )
    elif not ok:
        summary = (
            f"{CONFIG_SECTION_GITHUB_APP}.{CONFIG_KEY_SLUGS} is missing an entry for: "
            f"{', '.join(missing_slugs)} (a caller the "
            f"deployment's caller registry [{caller_source}] declares has no slug "
            f"to resolve its own bot login from on a GET /user 403)"
        )
    else:
        summary = (
            f"every declared caller has a slugs entry; {len(orphaned_slugs)} "
            f"orphaned slugs entr{'y' if len(orphaned_slugs) == 1 else 'ies'} with no "
            f"matching declared caller (caller registry: {caller_source}): "
            f"{', '.join(orphaned_slugs)}"
        )

    return CheckResult(
        name="github_app_slugs_coverage",
        ok=ok,
        summary=summary,
        resolved={
            "slugs_configured": True,
            "caller_source": caller_source,
            "declared_callers": sorted(declared_callers),
            "slug_callers": sorted(slug_callers),
            "missing_slugs": missing_slugs,
            "orphaned_slugs": orphaned_slugs,
        },
    )


# ---------------------------------------------------------------------------
# 2b. attestation source configured whenever github_app.callers is declared
#     (lr-8e1593, revives the one legitimate hardening from lr-424519).
# ---------------------------------------------------------------------------


def check_attestation_source_configured(
    *,
    config_root: str | Path | None = None,
    env: dict[str, str] | None = None,
) -> CheckResult:
    """WARN when `github_app.callers` is non-empty but no attestation
    source (transport.attestation layer 1 or layer 2) is configured at
    all (lr-8e1593, folding in lr-2e1f82).

    A deployment that has declared a caller registry has clearly gone to
    the trouble of naming which callers it expects to see on
    `--caller`/`--role` -- but if NO attestation source is configured,
    `transport.attestation.resolve_identity` can only ever fall through to
    the built-in OS-user layer, which `bind_caller` then compares against
    those declared caller names. On a shared/service host that resolves to
    the literal OS user (often `root`), that comparison deterministically
    refuses every declared caller (the exact chat-agent 'root' attribution
    failure lr-1cd33c root-caused) -- a silent, structural misconfiguration
    doctor can catch without ever reading a real credential or identity
    value.

    This is a WARN (`ok=True`, not a hard failure): a deployment MAY
    intentionally rely on the built-in OS-user fallback (e.g. a
    single-user host where the OS user genuinely IS the one declared
    caller) -- doctor surfaces the gap for review, it does not assert that
    shape is always wrong. No `github_app.callers` declared at all is a
    no-op pass (nothing to check attestation coverage against).

    Checks presence only -- never reads or resolves an actual identity
    value (this is a config-shape check, not a live identity resolution;
    resolve_identity() is never called here).
    """
    declared_callers = read_configured_callers(config_root)
    if not declared_callers:
        return CheckResult(
            name="attestation_source_configured",
            ok=True,
            summary="no github_app.callers declared -- nothing to check attestation coverage against",
            resolved={"callers_declared": False},
        )

    active_env = env if env is not None else dict(os.environ)
    section = load_user_config_section(ATTESTATION_CONFIG_SECTION, config_root=config_root)

    has_configured_env_source = bool(
        active_env.get(ATTESTED_IDENTITY_ENV_VAR) or section.get(ATTESTATION_CONFIG_KEY_IDENTITY_ENV)
    )
    has_sidecar_single_path = bool(
        active_env.get(ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR)
        or section.get(ATTESTATION_CONFIG_KEY_SIDECAR_PATH)
    )
    sidecars_list = section.get(ATTESTATION_CONFIG_KEY_SIDECARS)
    has_sidecar_adapter_list = isinstance(sidecars_list, list) and bool(sidecars_list)

    has_any_source = has_configured_env_source or has_sidecar_single_path or has_sidecar_adapter_list

    resolved = {
        "callers_declared": True,
        "declared_callers": sorted(declared_callers),
        "configured_env_source": has_configured_env_source,
        "sidecar_single_path_source": has_sidecar_single_path,
        "sidecar_adapter_list_source": has_sidecar_adapter_list,
    }

    if has_any_source:
        return CheckResult(
            name="attestation_source_configured",
            ok=True,
            summary=(
                f"github_app.callers declares {len(declared_callers)} caller(s); "
                f"an attestation source is configured"
            ),
            resolved=resolved,
        )

    return CheckResult(
        name="attestation_source_configured",
        ok=True,
        summary=(
            f"WARN: github_app.callers declares {', '.join(sorted(declared_callers))} "
            f"but no attestation source is configured ({ATTESTED_IDENTITY_ENV_VAR}, "
            f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR}, "
            f"{ATTESTATION_CONFIG_SECTION}.{ATTESTATION_CONFIG_KEY_IDENTITY_ENV}, "
            f"{ATTESTATION_CONFIG_SECTION}.{ATTESTATION_CONFIG_KEY_SIDECAR_PATH}, and "
            f"{ATTESTATION_CONFIG_SECTION}.{ATTESTATION_CONFIG_KEY_SIDECARS} are all "
            f"unset) -- resolve_identity() will fall through to the built-in OS-user "
            f"layer for every invocation, which bind_caller then compares against the "
            f"declared callers above (a common 'root' attribution failure shape)"
        ),
        resolved=resolved,
    )


# ---------------------------------------------------------------------------
# 3. Per-repo repo-local config schema validation.
# ---------------------------------------------------------------------------


#: Top-level sections this package's own verbs already own within the
#: repo-local config file (wait.config's `wait:`, provisioning.roles'
#: `roles:`, merge.post_merge_config's `merge:`). Kept local rather than
#: importing each module's own CONFIG_SECTION_* constant into one place
#: elsewhere -- this set exists ONLY to name "which sections does doctor
#: know how to validate," not to become a second registry those modules
#: must keep in lockstep with (each validate step below imports its own
#: loader directly; this tuple is documentation, not a coupling point).
KNOWN_CONFIG_SECTIONS: tuple[str, ...] = ("wait", "roles", "merge")


def check_repo_loadout_schema(
    repo_root: str | Path,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> CheckResult:
    """Validate `<repo_root>/<config_relative_path>` (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path`) by running each of its known
    top-level sections through the SAME loader/validator the verb that
    actually reads that section at runtime uses
    (wait.config.load_scoped_test_patterns, provisioning.roles.
    load_role_verbs, merge.post_merge_config.load_post_merge_steps) -- never
    a second, doctor-only schema that could silently drift from what "valid"
    means to the verb that consumes it.

    A missing file (at BOTH the new and legacy path) is `ok=True` (nothing
    to validate; every section falls back to its own default). A LEGACY-only
    repo (the `.loadout/` marker dir exists, `.clagentic/loadout/` does not)
    is a WARN finding (`ok=True`, but flagged in `resolved.legacy_dir_present`
    and named in the summary) -- migration-incomplete, not a schema error
    (lr-446c35; the fleet migration is tracked separately, lr-a645aa). A
    `credentials:` top-level section is flagged as a finding (`ok=False`) --
    repo-local credentials config is NEVER honored by
    transport.provider_config (lr-0818 security decision); doctor surfaces
    that as a conformance gap in the repo's own committed config, not merely
    a runtime stderr warning a caller might not be watching for. An
    unrecognized top-level section (neither a KNOWN_CONFIG_SECTIONS entry
    nor `credentials`) is reported for visibility but does not fail the
    check -- a future verb's own section landing here is not itself
    evidence of a config error.
    """
    repo_root_path = Path(repo_root)
    legacy_dir_present = (repo_root_path / LEGACY_CONFIG_MARKER).is_dir()
    # warn=False: doctor reports the legacy-dir finding itself, through
    # legacy_dir_present/the summary text below -- it must not ALSO trigger
    # resolve_repo_config_path's own stderr deprecation line as a side
    # effect of a read-only health check.
    config_path = resolve_repo_config_path(
        repo_root_path, config_relative_path=config_relative_path, warn=False
    )
    if not config_path.exists():
        summary = f"{config_path}: not present (every section falls back to its own default)"
        if legacy_dir_present:
            summary += (
                f"; WARN: legacy {LEGACY_CONFIG_MARKER}/ dir present with no "
                f"config.yaml inside it -- migrate to {config_relative_path}"
            )
        return CheckResult(
            name="repo_loadout_schema",
            ok=True,
            summary=summary,
            resolved={
                "config_path": str(config_path),
                "exists": False,
                "legacy_dir_present": legacy_dir_present,
            },
        )

    # Top-level section enumeration only (which keys the file declares) --
    # deliberately NOT a second validating parser: every section's actual
    # CONTENT is validated below through that section's own owning loader.
    # A malformed/unreadable file is reported as its own error rather than
    # raised, matching every other check in this module (doctor never lets
    # a bad repo config crash a health-check run).
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        return CheckResult(
            name="repo_loadout_schema",
            ok=False,
            summary=f"{config_path}: could not be read as YAML -- {exc}",
            resolved={
                "config_path": str(config_path),
                "exists": True,
                "legacy_dir_present": legacy_dir_present,
                "error": str(exc),
            },
        )
    raw = parsed if isinstance(parsed, dict) else {}

    errors: list[str] = []
    unknown_sections = sorted(
        key for key in raw if key not in KNOWN_CONFIG_SECTIONS and key != CONFIG_SECTION_CREDENTIALS
    )

    if has_repo_local_credentials_section(repo_root, config_relative_path=config_relative_path):
        errors.append(
            f"{CONFIG_SECTION_CREDENTIALS!r} section present -- repo-local credentials "
            f"config is never honored (transport.provider_config); configure "
            f"the credentials tier at the user level instead"
        )

    try:
        load_scoped_test_patterns(repo_root, config_relative_path=config_relative_path)
    except InvalidScopedTestConfigError as exc:
        errors.append(f"wait: {exc}")

    declared_role_verbs: dict[str, tuple[str, ...]] | None = None
    try:
        declared_role_verbs = load_role_verbs(repo_root, config_relative_path=config_relative_path)
    except InvalidRoleConfigError as exc:
        errors.append(f"roles: {exc}")

    try:
        load_post_merge_steps(repo_root, config_relative_path=config_relative_path)
    except PostMergeConfigError as exc:
        errors.append(f"merge: {exc}")

    try:
        load_pre_checks(repo_root, config_relative_path=config_relative_path)
    except PostMergeConfigError as exc:
        errors.append(f"merge.pre_checks: {exc}")

    unknown_gate_roles: list[str] = []
    unsatisfiable_gate_roles: list[str] = []
    try:
        load_merge_requirements(repo_root, config_relative_path=config_relative_path)
        required_reviewer_roles = load_required_reviewer_roles(
            repo_root, config_relative_path=config_relative_path
        )
        authorized_roles = load_authorized_roles(
            repo_root, config_relative_path=config_relative_path
        )
    except InvalidMergeGateConfigError as exc:
        errors.append(f"merge (gate declaration): {exc}")
    else:
        # lr-638945 comment #1 (ADDITIONAL EVIDENCE, the live clagentic-github
        # incident): a gate role that matches no key in this repo's own role
        # declaration can never produce a verdict -- but the SEVERITY of that
        # gap depends on whether the config, AS WRITTEN, could possibly have
        # satisfied it:
        #
        #   - `roles:` section PRESENT in THIS repo's own config (raw, not a
        #     fallback) -> the repo has fully specified its own role
        #     taxonomy. A gate role missing from it is UNSATISFIABLE by
        #     construction -- this exact shape is the live incident comment
        #     #1 recorded (required_reviewer_roles: [reviewer, security] with
        #     no `security` key in roles:, doctor reporting 6/6 clean). FAIL
        #     (`ok=False`): "an unsatisfiable gate is worse than an absent
        #     one, because it reads as protection" (comment #1, verbatim).
        #   - `roles:` section ABSENT -> load_role_verbs falls back to
        #     DEFAULT_ROLE_VERBS, a REFERENCE default, not this repo's own
        #     declaration (module docstring, provisioning.roles). A gate role
        #     outside that reference set is not PROVABLY unsatisfiable here --
        #     the deployment may resolve roles through a mechanism doctor
        #     cannot see (e.g. an external harness/allowlist that never
        #     touched this repo's `roles:` section at all). WARN
        #     (`ok` stays True) is the correct severity for this shape only.
        roles_section_present = CONFIG_SECTION_ROLES in raw
        known_roles = set(
            declared_role_verbs if declared_role_verbs is not None else DEFAULT_ROLE_VERBS
        )
        gate_roles = {*required_reviewer_roles, *authorized_roles}
        missing_from_known = sorted(gate_roles - known_roles)
        if roles_section_present:
            unsatisfiable_gate_roles = missing_from_known
            if unsatisfiable_gate_roles:
                for bad_role in unsatisfiable_gate_roles:
                    gates_naming_it = sorted(
                        {
                            gate_key
                            for gate_key, roles_in_gate in (
                                (CONFIG_KEY_REQUIRED_REVIEWER_ROLES, required_reviewer_roles),
                                (CONFIG_KEY_AUTHORIZED_ROLES, authorized_roles),
                            )
                            if bad_role in roles_in_gate
                        }
                    )
                    errors.append(
                        f"merge (gate declaration): {config_path}: role "
                        f"{bad_role!r} is required by "
                        f"merge.{'/merge.'.join(gates_naming_it)} but is not "
                        f"declared as a key under {CONFIG_SECTION_ROLES!r} in "
                        f"this same file -- {bad_role!r} has no verb set and "
                        f"can never emit a verdict, so this gate is "
                        f"unsatisfiable: an unsatisfiable gate is worse than "
                        f"an absent one, because it reads as protection. Fix "
                        f"by one of: (1) add `{bad_role}:` under "
                        f"{CONFIG_SECTION_ROLES!r} with the verb set that "
                        f"role needs (e.g. `git-host-api`, `review-post` for "
                        f"a reviewer/security-class role); (2) remove "
                        f"{bad_role!r} from the gate list(s) named above if "
                        f"it was named in error; or (3) if no reviewer "
                        f"verdict should be required at all, declare "
                        f"`{CONFIG_KEY_REQUIRED_REVIEWER_ROLES}: []` as an "
                        f"explicit opt-out instead."
                    )
        else:
            unknown_gate_roles = missing_from_known

    # WARN, not a schema error: the config file WAS found and validated (via
    # the legacy-path fallback above) -- but the repo has not yet migrated
    # its marker dir off the legacy .loadout/ home (lr-446c35).
    is_using_legacy_path = config_path.name == "config.yaml" and (
        config_path.parent.name == LEGACY_CONFIG_MARKER
        and str(config_path) == str(repo_root_path / LEGACY_CONFIG_RELATIVE_PATH)
    )

    ok = not errors
    if ok:
        summary = f"{config_path}: schema OK ({', '.join(sorted(set(raw) & set(KNOWN_CONFIG_SECTIONS))) or 'no known sections present'})"
    else:
        summary = f"{config_path}: {len(errors)} schema error(s) -- " + "; ".join(errors)
    if is_using_legacy_path:
        summary += (
            f"; WARN: reading legacy {LEGACY_CONFIG_RELATIVE_PATH} -- migrate to "
            f"{config_relative_path}"
        )
    if unknown_gate_roles:
        summary += (
            f"; WARN: gate role(s) {', '.join(unknown_gate_roles)} match no "
            f"key in the reference role taxonomy (this repo declares no "
            f"{CONFIG_SECTION_ROLES!r} section of its own) -- not "
            f"provably unsatisfiable (the deployment may resolve roles "
            f"elsewhere), but worth reviewing"
        )

    return CheckResult(
        name="repo_loadout_schema",
        ok=ok,
        summary=summary,
        resolved={
            "config_path": str(config_path),
            "exists": True,
            "legacy_dir_present": legacy_dir_present,
            "using_legacy_path": is_using_legacy_path,
            "top_level_sections": sorted(raw),
            "unknown_sections": unknown_sections,
            "unknown_gate_roles": unknown_gate_roles,
            "unsatisfiable_gate_roles": unsatisfiable_gate_roles,
            "errors": errors,
        },
    )


# ---------------------------------------------------------------------------
# 3b. Dead-config trap: post_merge_steps declared in .crew/<role>.yaml,
#     which loadout-merge NEVER reads (lr-f9a01b).
# ---------------------------------------------------------------------------

def check_dead_crew_post_merge_config(repo_root: str | Path) -> CheckResult:
    """Flag a repo that declares `post_merge_steps` inside a
    `.crew/<role>.yaml` file while its OWN `.clagentic/loadout/config.yaml`
    (the only file `merge.post_merge_config.load_post_merge_steps` ever
    reads -- see that function's docstring) never explicitly declares the
    key at all (lr-f9a01b).

    THE CLASS THIS CLOSES: `.crew/<role>.yaml`'s own starter template
    documents a `post_merge_steps` section (with `cmd`/`description`/
    `on_failure` examples) in the SAME file that also carries
    `merge_allowed`/`scope`/`branch_conventions` -- fields loadout-adjacent
    tooling genuinely does read from `.crew/*.yaml`. Nothing distinguishes
    "this key is live here" from "this key is decorative here" at the point
    a repo author edits the file, and `load_post_merge_steps` returns `[]`
    (a documented, silent no-op) for a repo whose REAL config
    (`.clagentic/loadout/config.yaml`) never mentions `post_merge_steps`
    at all -- so a repo can declare deploy steps in `.crew/<role>.yaml`,
    get a clean `loadout-merge` exit with `steps_run=0`, and never deploy,
    with nothing in that run's own output naming the file the steps
    actually needed to live in.

    THIS IS A DOCTOR-ONLY, POST-HOC CHECK -- it fires only when someone
    invokes `loadout-doctor --repo-root`. `merge.verb._run`'s step 10 also
    surfaces this SAME cross-check (via the SAME
    `find_crew_yaml_files_declaring_post_merge_steps` scan this check
    calls) as a loud, non-blocking WARNING at the point a merge actually
    happens -- the path that runs unattended -- so the silent-no-op shape
    is caught even when nobody thinks to run doctor. See that module's
    docstring, "DEAD .crew/<role>.yaml post_merge_steps CROSS-CHECK", for
    why that surface warns rather than refuses.

    Uses `merge.post_merge_config.find_crew_yaml_files_declaring_
    post_merge_steps` (a flat, non-recursive scan of `<repo_root>/.crew/
    *.yaml`, matching how `.crew/<role>.yaml` files are always named, one
    per role, never nested) for a top-level `post_merge_steps` key OR a
    `post_merge_steps` key nested one level under a `merge:` section (the
    shape a repo author who has seen this package's own `merge:`-section
    convention might reasonably also try in `.crew/*.yaml`, even though
    neither shape is ever read there).

    A repo with NO `.crew/` directory, or whose `.crew/*.yaml` files never
    mention `post_merge_steps` anywhere, is `ok=True` -- nothing here for
    this check to flag; the vast majority of repos.

    A repo where at least one `.crew/*.yaml` file mentions `post_merge_steps`
    AND `.clagentic/loadout/config.yaml`'s own `merge:` section never
    EXPLICITLY names the `post_merge_steps` key (checked via
    `merge.post_merge_config.post_merge_steps_key_declared`, not merely
    "the resolved step list happens to be empty" -- see that function's own
    docstring for why the distinction matters here) is `ok=False` -- naming
    every offending `.crew/*.yaml` file and the one file
    (`.clagentic/loadout/config.yaml`) `post_merge_steps` must actually
    live in to run.

    A repo where `.crew/*.yaml` ALSO mentions `post_merge_steps` but
    `.clagentic/loadout/config.yaml` ALREADY declares the key explicitly
    (even as `post_merge_steps: []`, a repo's own informed choice of zero
    steps) is `ok=True` -- the LIVE config already has the last word at the
    correct file; a leftover/decorative mention in `.crew/*.yaml` is not
    itself an error this check flags (it is dead documentation, not a dead
    deploy -- `check_repo_loadout_schema`'s own schema validation is the
    place a malformed LIVE config is caught).

    A `.crew/*.yaml` file that is unreadable/malformed YAML is skipped for
    THIS check (reported neither ok nor not-ok by it) -- a malformed
    `.crew/*.yaml` file is a conformance concern for whatever external
    schema owns that file's shape, outside this package's own schema, not
    something doctor should raise an exception over.
    """
    repo_root_path = Path(repo_root)
    crew_dir = repo_root_path / CREW_CONFIG_DIR_NAME

    offending_files = find_crew_yaml_files_declaring_post_merge_steps(repo_root_path)

    if not offending_files:
        return CheckResult(
            name="dead_crew_post_merge_config",
            ok=True,
            summary=(
                f"{crew_dir}: no {CONFIG_KEY_POST_MERGE_STEPS!r} declared "
                f"in any .crew/*.yaml file (nothing to cross-check)"
            ),
            resolved={"crew_dir": str(crew_dir), "offending_files": []},
        )

    live_key_declared = post_merge_steps_key_declared(repo_root_path)
    live_config_path = resolve_repo_config_path(repo_root_path, warn=False)

    if live_key_declared:
        live_steps = load_post_merge_steps(repo_root_path)
        return CheckResult(
            name="dead_crew_post_merge_config",
            ok=True,
            summary=(
                f"{', '.join(offending_files)} also mention "
                f"{CONFIG_KEY_POST_MERGE_STEPS!r}, but {live_config_path} "
                f"already declares its own live "
                f"{CONFIG_KEY_POST_MERGE_STEPS!r} -- the .crew/*.yaml "
                f"mention is dead documentation, not a dead deploy"
            ),
            resolved={
                "crew_dir": str(crew_dir),
                "offending_files": offending_files,
                "live_config_path": str(live_config_path),
                "live_steps_count": len(live_steps),
            },
        )

    return CheckResult(
        name="dead_crew_post_merge_config",
        ok=False,
        summary=(
            f"{', '.join(offending_files)} declare "
            f"{CONFIG_KEY_POST_MERGE_STEPS!r}, but loadout-merge NEVER reads "
            f"that key from .crew/*.yaml -- it reads ONLY "
            f"{live_config_path}'s own {CONFIG_SECTION_MERGE!r}."
            f"{CONFIG_KEY_POST_MERGE_STEPS!r}, which is absent here. These "
            f"steps will silently never run (a merge will exit 0 with "
            f"steps_run=0). Move the post_merge_steps list into "
            f"{live_config_path} under a {CONFIG_SECTION_MERGE!r}: section "
            f"to make it live."
        ),
        resolved={
            "crew_dir": str(crew_dir),
            "offending_files": offending_files,
            "live_config_path": str(live_config_path),
            "live_steps_count": 0,
        },
    )


# ---------------------------------------------------------------------------
# 4. DEPLOYMENT-TIER identity sections: builder_identity, review.reviewer_logins.
# ---------------------------------------------------------------------------


def check_builder_identity_config(
    *,
    config_root: str | Path | None = None,
) -> CheckResult:
    """Validate the USER-LEVEL config.yaml's `builder_identity:` section
    (push.identity_config) and `review.reviewer_logins:` map
    (review.login_config) — the SAME loaders the real consumers use, never a
    second schema (lr-0a03c3).

    Both sections are OPTIONAL and independent:

      - `builder_identity` absent: no-op (commit re-authoring is an opt-in
        feature — `push.identity.pin_commits_to_bot_identity`'s own
        `fail_closed_on_missing` is the caller's decision point for whether
        that absence is acceptable, not doctor's).
      - `builder_identity` present but malformed (missing/empty `name` or
        `email`): a FAILING finding — a deployment that started configuring
        this section presumably wants it to work, so a malformed entry is a
        conformance gap doctor surfaces, not a silent partial feature.
      - `review.reviewer_logins` malformed values degrade to "no override"
        at the loader layer (review.login_config's own additive-tier
        contract) rather than raising — this check mirrors that by reporting
        a non-mapping `reviewer_logins` value as a finding (still useful
        deployment-config visibility) without treating a single bad entry as
        fatal to the overall check.

    Never reads a repo-local config file — this is a DEPLOYMENT-TIER check
    only, matching both loaders' own "no repo_root parameter" contract
    (lr-0818-class identity-escalation reasoning; see push.identity_config
    and review.login_config module docstrings for the full rationale).
    """
    resolved: dict = {"config_root": str(config_root) if config_root is not None else None}
    errors: list[str] = []

    try:
        name, email = load_builder_identity(config_root=config_root)
    except InvalidBuilderIdentityConfigError as exc:
        errors.append(f"builder_identity: {exc}")
        name = email = None
    resolved["builder_identity_configured"] = name is not None and email is not None

    review_section = load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root)
    reviewer_logins = review_section.get(CONFIG_KEY_REVIEWER_LOGINS)
    if reviewer_logins is None:
        resolved["reviewer_logins_configured"] = False
    elif not isinstance(reviewer_logins, dict):
        errors.append(
            f"{CONFIG_SECTION_REVIEW}.{CONFIG_KEY_REVIEWER_LOGINS} must be a mapping "
            f"of role -> login, got {type(reviewer_logins).__name__}"
        )
        resolved["reviewer_logins_configured"] = True
    else:
        resolved["reviewer_logins_configured"] = True
        resolved["reviewer_logins_roles"] = sorted(reviewer_logins)

    ok = not errors
    if ok:
        summary = (
            f"builder_identity: {'configured' if resolved['builder_identity_configured'] else 'not configured (no-op)'}; "
            f"{CONFIG_SECTION_REVIEW}.{CONFIG_KEY_REVIEWER_LOGINS}: "
            f"{'configured for ' + ', '.join(resolved.get('reviewer_logins_roles', [])) if resolved['reviewer_logins_configured'] else 'not configured (no-op)'}"
        )
    else:
        summary = f"{len(errors)} finding(s) -- " + "; ".join(errors)

    return CheckResult(
        name="builder_identity_config",
        ok=ok,
        summary=summary,
        resolved={**resolved, "errors": errors},
    )


__all__ = [
    "CREW_CONFIG_DIR_NAME",
    "KNOWN_CONFIG_SECTIONS",
    "PLATFORMS",
    "PROBE_CALLER",
    "PROBE_TIMEOUT_SECONDS",
    "CheckResult",
    "check_attestation_source_configured",
    "check_builder_identity_config",
    "check_credential_validity",
    "check_credentials",
    "check_dead_crew_post_merge_config",
    "check_github_app_slugs_coverage",
    "check_repo_loadout_schema",
]
