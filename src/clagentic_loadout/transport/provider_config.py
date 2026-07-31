"""provider_config.py — per-platform TokenProvider selection (lr-af6e).

ONE factory (`resolve_platform_provider`) that every verb's token resolution
goes through: platform ("forgejo" | "github") -> a concrete TokenProvider.
The rule "which provider kind + which command a platform uses" lives here
exactly once — git_host_api, review.verb, push.verb, and merge.verb all call
this factory rather than each re-deriving provider selection from env/config
independently.

RATIONALE (operator scope note, task lr-af6e): a deployment names Forgejo and
GitHub providers INDEPENDENTLY because their credential-minting processes
differ today — e.g. Forgejo pointed at a self-fetch process, GitHub pointed
at a gatekeeper-style mint command. When the git-host platform's own next
major version lets one minting service handle both platforms, the switch to
a single shared command is a config-only change (set both platforms' provider
config to the same command) — zero code, because the selection rule already
treats the two platforms as independent config, never as a hardcoded pairing.

Selection precedence per platform, highest first:
  1. Env var: CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO /
     CLAGENTIC_LOADOUT_TOKEN_PROVIDER_GITHUB = "static" | "command", plus
     CLAGENTIC_LOADOUT_TOKEN_COMMAND_FORGEJO / _GITHUB for the command
     argv string (only consulted when the provider kind is "command").
  2. Config file: USER-LEVEL config, <config_root>/config.yaml (default
     ~/.config/clagentic/loadout/config.yaml — the same config-root
     convention StaticTokenProvider's `.env` lookup already uses, see
     `config_root` below), section `credentials:`, keys
     `token_provider_forgejo` / `token_provider_github` ("static" |
     "command") and `token_command_forgejo` / `token_command_github` (an
     argv string, same quoting rule as the env var).

     NEVER repo-local (fixed direction, security decision on task lr-0818):
     a cloned repo's own repo-local config file is never consulted for this
     tier. A `credentials:` section found there names an arbitrary command
     the credential factory would exec — wiring that up from a repo-local,
     possibly-committed file would let a cloned hostile repo's checked-in
     config choose the command a caller's git-host token gets minted through
     (arbitrary command execution via the credential factory, the same
     attack class as an untrusted repo's committed `.vscode/settings.json`
     naming a task's shell command). See `_load_credentials_section` /
     `_reject_repo_local_credentials_section` below: a repo-local
     `credentials:` section is REJECTED with a stderr warning, never
     silently honored.
  3. Default: "static" (StaticTokenProvider) — unchanged behavior from
     before this task; a deployment that sets nothing keeps working exactly
     as it did.

(A verb's own `token_provider` CLI-injection parameter, e.g. review.verb's
`main(token_provider=...)`, is a SEPARATE, higher-precedence override at the
verb level — a caller that already has a concrete TokenProvider bypasses
this factory entirely rather than passing through it. See each verb's own
docstring.)

Command-string quoting rule: the command value (env var or config file) is
split with shlex.split — standard POSIX shell word-splitting/quoting rules
apply (quote an argument containing spaces, escape embedded quotes), but NO
shell is ever invoked to run it; splitting happens in this process only,
then the resulting argv list is passed to CommandTokenProvider, which execs
it directly (shell=False).

STRUCTURED OUTPUT OPT-IN (lr-43c8d7): an OPTIONAL per-platform boolean,
`token_command_emits_json_forgejo` / `token_command_emits_json_github`
(env: CLAGENTIC_LOADOUT_TOKEN_COMMAND_EMITS_JSON_FORGEJO / _GITHUB),
defaulting to `False`, forwarded verbatim to
`CommandTokenProvider(emit_structured_output=...)`. Same precedence as the
command/kind pair above (env wins over config; absent means `False`, i.e.
"read stdout as a bare token" — byte-identical to before this task). A
deployment whose configured minting command already prints a bare token
sets nothing here at all and sees no behavior change; a deployment whose
command supports the `{token, app_slug}` JSON shape opts in explicitly.
"""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

import yaml

from clagentic_loadout.platform_detect import PLATFORM_FORGEJO, PLATFORM_GITHUB
from clagentic_loadout.release.secrets_config import DEFAULT_CONFIG_ROOT as _ROLES_CONFIG_ROOT
from clagentic_loadout.transport.credential_provider import (
    CommandTokenProvider,
    StaticTokenProvider,
    TokenProvider,
)
from clagentic_loadout.repo_config import resolve_repo_config_path
from clagentic_loadout.wait.config import DEFAULT_CONFIG_RELATIVE_PATH

#: Provider-kind literal for the standalone .env-file fallback.
PROVIDER_KIND_STATIC = "static"
#: Provider-kind literal for the command/exec provider.
PROVIDER_KIND_COMMAND = "command"

_VALID_PROVIDER_KINDS = (PROVIDER_KIND_STATIC, PROVIDER_KIND_COMMAND)

#: User-level loadout config root -- the SAME base directory
#: StaticTokenProvider's role-scoped `.env` lookup uses (one directory up
#: from release.secrets_config.DEFAULT_CONFIG_ROOT, which adds the `roles`
#: leaf). The credentials config-file tier's `config.yaml` lives directly
#: under this root: ~/.config/clagentic/loadout/config.yaml. Overridable
#: (mainly for tests) via the `config_root` parameter on
#: resolve_platform_provider / _load_credentials_section.
DEFAULT_USER_CONFIG_ROOT = _ROLES_CONFIG_ROOT.parent

#: Filename of the user-level credentials config file, under `config_root`.
USER_CONFIG_FILENAME = "config.yaml"

#: Env var names, one pair per platform. No shared/generic
#: CLAGENTIC_LOADOUT_TOKEN_PROVIDER var exists deliberately -- the operator
#: scope note requires forgejo and github to be independently nameable, so a
#: platform-agnostic fallback var would just reintroduce the coupling this
#: task removes.
TOKEN_PROVIDER_ENV_VAR_FORGEJO = "CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO"
TOKEN_PROVIDER_ENV_VAR_GITHUB = "CLAGENTIC_LOADOUT_TOKEN_PROVIDER_GITHUB"
TOKEN_COMMAND_ENV_VAR_FORGEJO = "CLAGENTIC_LOADOUT_TOKEN_COMMAND_FORGEJO"
TOKEN_COMMAND_ENV_VAR_GITHUB = "CLAGENTIC_LOADOUT_TOKEN_COMMAND_GITHUB"
#: Structured-output opt-in (lr-43c8d7) — see module docstring, "STRUCTURED
#: OUTPUT OPT-IN". A truthy string ("1", "true", "yes", case-insensitive)
#: enables it; anything else (including unset/empty) leaves the default
#: `False`.
TOKEN_COMMAND_EMITS_JSON_ENV_VAR_FORGEJO = "CLAGENTIC_LOADOUT_TOKEN_COMMAND_EMITS_JSON_FORGEJO"
TOKEN_COMMAND_EMITS_JSON_ENV_VAR_GITHUB = "CLAGENTIC_LOADOUT_TOKEN_COMMAND_EMITS_JSON_GITHUB"

#: Top-level section this module owns within the repo-local config file
#: (see wait.config's module docstring for the one-file/one-section-per-verb
#: convention this reuses).
CONFIG_SECTION_CREDENTIALS = "credentials"
CONFIG_KEY_PROVIDER_FORGEJO = "token_provider_forgejo"
CONFIG_KEY_PROVIDER_GITHUB = "token_provider_github"
CONFIG_KEY_COMMAND_FORGEJO = "token_command_forgejo"
CONFIG_KEY_COMMAND_GITHUB = "token_command_github"
#: Structured-output opt-in config keys (lr-43c8d7) — see module docstring.
CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO = "token_command_emits_json_forgejo"
CONFIG_KEY_COMMAND_EMITS_JSON_GITHUB = "token_command_emits_json_github"

_ENV_VARS_BY_PLATFORM = {
    PLATFORM_FORGEJO: (TOKEN_PROVIDER_ENV_VAR_FORGEJO, TOKEN_COMMAND_ENV_VAR_FORGEJO),
    PLATFORM_GITHUB: (TOKEN_PROVIDER_ENV_VAR_GITHUB, TOKEN_COMMAND_ENV_VAR_GITHUB),
}
_CONFIG_KEYS_BY_PLATFORM = {
    PLATFORM_FORGEJO: (CONFIG_KEY_PROVIDER_FORGEJO, CONFIG_KEY_COMMAND_FORGEJO),
    PLATFORM_GITHUB: (CONFIG_KEY_PROVIDER_GITHUB, CONFIG_KEY_COMMAND_GITHUB),
}
_EMITS_JSON_ENV_VARS_BY_PLATFORM = {
    PLATFORM_FORGEJO: TOKEN_COMMAND_EMITS_JSON_ENV_VAR_FORGEJO,
    PLATFORM_GITHUB: TOKEN_COMMAND_EMITS_JSON_ENV_VAR_GITHUB,
}
_EMITS_JSON_CONFIG_KEYS_BY_PLATFORM = {
    PLATFORM_FORGEJO: CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO,
    PLATFORM_GITHUB: CONFIG_KEY_COMMAND_EMITS_JSON_GITHUB,
}
#: Truthy string literals accepted for the emits-json opt-in (env var or
#: config-file value), case-insensitive. Anything else -- including unset,
#: empty, or a bare non-matching string -- resolves to False, the safe
#: default (lr-43c8d7).
_TRUTHY_STRINGS = frozenset({"1", "true", "yes"})


def _is_truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in _TRUTHY_STRINGS
    return False


class InvalidProviderConfigError(ValueError):
    """Raised when a platform's resolved provider-kind/command config is
    malformed — an unrecognized provider kind, or "command" selected with no
    command string configured. Callers translate this to their own
    exit-code convention; this module never calls sys.exit."""


def _read_yaml_mapping(path: Path) -> dict:
    """Read *path* as YAML, returning {} for any missing/unreadable/
    non-mapping file rather than raising -- both the user-level config file
    and a repo-local one are OPTIONAL, and a malformed file is treated the
    same as an absent one at this layer (this module never fails startup
    over an optional config file it merely reads for its own section)."""
    if not path.exists():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return raw if isinstance(raw, dict) else {}


def load_user_config_section(section_name: str, *, config_root: str | Path | None = None) -> dict:
    """Load one top-level section from the USER-LEVEL loadout config file,
    <config_root>/config.yaml (default DEFAULT_USER_CONFIG_ROOT -- the same
    config-root convention StaticTokenProvider's `.env` lookup and this
    module's own `credentials:` tier use).

    Public, reusable entry point (lr-e570): any loadout module that needs a
    user-level config-file tier for its own settings (e.g.
    transport.git_host_api's git_host base-URL resolution) reads its own
    section through this ONE loader/config-root convention rather than each
    growing a second YAML parser or a different config path. NEVER reads a
    repo-local config file -- this is the user-level file only (see the
    module docstring's precedence section for why the credentials tier in
    particular never reads repo-local, lr-0818 -- the same reasoning applies
    to any section here: a cloned repo's own config must never silently
    steer a user-level setting).

    Returns {} when the file, the section, or config_root itself is
    missing/unreadable/malformed -- every caller's own section is OPTIONAL
    at this layer, matching `_read_yaml_mapping`'s own degrade-to-default
    contract.
    """
    root = Path(config_root) if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    config_path = root / USER_CONFIG_FILENAME
    section = _read_yaml_mapping(config_path).get(section_name)
    return section if isinstance(section, dict) else {}


def _load_credentials_section(config_root: str | Path | None) -> dict:
    """Load the `credentials:` section from the USER-LEVEL loadout config
    file, <config_root>/config.yaml (default DEFAULT_USER_CONFIG_ROOT --
    the same config-root convention StaticTokenProvider's `.env` lookup
    uses). NEVER reads a repo-local config file -- see the module
    docstring's precedence section for why (lr-0818)."""
    return load_user_config_section(CONFIG_SECTION_CREDENTIALS, config_root=config_root)


def _reject_repo_local_credentials_section(
    repo_root: str | Path | None,
    *,
    config_relative_path: str,
) -> None:
    """Warn to stderr (never raise, never silently honor) when a repo-local
    config file carries a `credentials:` section.

    A repo-local credentials section is NEVER consulted for provider
    selection (see module docstring) -- but silently ignoring it would let
    a misconfigured or hostile repo believe its committed config controls
    which command mints the caller's git-host token. Surfacing the
    misconfiguration is preferred over silent rejection (task lr-0818,
    security direction from the PR #22 audit).
    """
    if repo_root is None:
        return
    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path, warn=False
    )
    section = _read_yaml_mapping(config_path).get(CONFIG_SECTION_CREDENTIALS)
    if isinstance(section, dict) and section:
        print(
            f"WARNING: {config_path} has a {CONFIG_SECTION_CREDENTIALS!r} "
            f"section -- REJECTED. Repo-local credential-provider config is "
            f"never honored (a cloned repo's own committed config must "
            f"never be able to name the command that mints a caller's "
            f"git-host token). Configure the credentials tier at the user level "
            f"instead: {DEFAULT_USER_CONFIG_ROOT / USER_CONFIG_FILENAME}, "
            f"or via the CLAGENTIC_LOADOUT_TOKEN_PROVIDER_FORGEJO / _GITHUB "
            f"env vars.",
            file=sys.stderr,
        )


def _resolve_provider_kind_and_command(
    platform: str,
    *,
    env: dict[str, str],
    config_section: dict,
) -> tuple[str, str | None]:
    provider_env_var, command_env_var = _ENV_VARS_BY_PLATFORM[platform]
    provider_key, command_key = _CONFIG_KEYS_BY_PLATFORM[platform]

    # Precedence: env var wins over config-file value, per platform. A
    # platform with no signal in either source defaults to "static".
    kind = env.get(provider_env_var) or config_section.get(provider_key) or PROVIDER_KIND_STATIC
    if kind not in _VALID_PROVIDER_KINDS:
        raise InvalidProviderConfigError(
            f"platform {platform!r}: provider kind {kind!r} not recognized "
            f"(expected one of {_VALID_PROVIDER_KINDS!r}). Set "
            f"{provider_env_var} or the {CONFIG_SECTION_CREDENTIALS}."
            f"{provider_key} config key."
        )

    command = env.get(command_env_var) or config_section.get(command_key)
    return kind, command


def _resolve_command_emits_json(
    platform: str,
    *,
    env: dict[str, str],
    config_section: dict,
) -> bool:
    """Resolve the structured-output opt-in (lr-43c8d7) for *platform*: env
    var wins over the config-file value, defaulting to False (see module
    docstring, "STRUCTURED OUTPUT OPT-IN"). Only meaningful when the
    resolved provider kind is PROVIDER_KIND_COMMAND -- callers ignore this
    entirely for PROVIDER_KIND_STATIC, same as `command` above."""
    emits_json_env_var = _EMITS_JSON_ENV_VARS_BY_PLATFORM[platform]
    emits_json_key = _EMITS_JSON_CONFIG_KEYS_BY_PLATFORM[platform]
    env_value = env.get(emits_json_env_var)
    if env_value is not None and env_value.strip():
        return _is_truthy(env_value)
    return _is_truthy(config_section.get(emits_json_key))


def resolve_provider_kind_and_command(
    platform: str,
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
) -> tuple[str, str | None]:
    """Public wrapper around this module's own precedence rule (env var wins
    over the user-level config-file's `credentials:` section, defaulting to
    PROVIDER_KIND_STATIC — see module docstring) that resolves the provider
    KIND and, when relevant, the CONFIGURED COMMAND STRING for *platform*
    WITHOUT constructing a TokenProvider.

    A caller that wants to actually mint a token uses
    `resolve_platform_provider` instead — this function exists for a
    read-only inspector (doctor.checks.check_credentials) that needs to see
    exactly what would be resolved without instantiating a
    CommandTokenProvider or StaticTokenProvider, and without the repo-local
    `credentials:` rejection warning `resolve_platform_provider` prints as a
    side effect (this function takes no `repo_root`, so that side effect
    does not apply here at all — a health check inspects the USER-LEVEL tier
    only, exactly like every other provider-selection consumer).

    Args:
        platform: PLATFORM_FORGEJO or PLATFORM_GITHUB.
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.
        config_root: override the user-level config root (mainly for
            tests). Defaults to DEFAULT_USER_CONFIG_ROOT.

    Returns:
        (kind, command_str) — kind is PROVIDER_KIND_STATIC or
        PROVIDER_KIND_COMMAND; command_str is the raw configured command
        string (unsplit) when kind is PROVIDER_KIND_COMMAND and a command is
        configured, else None.

    Raises:
        InvalidProviderConfigError: *platform* is not recognized, or an
            unrecognized provider kind is configured.
    """
    if platform not in _ENV_VARS_BY_PLATFORM:
        raise InvalidProviderConfigError(
            f"platform {platform!r} not recognized. Expected "
            f"{PLATFORM_FORGEJO!r} or {PLATFORM_GITHUB!r}."
        )
    active_env = env if env is not None else dict(os.environ)
    config_section = _load_credentials_section(config_root)
    return _resolve_provider_kind_and_command(
        platform, env=active_env, config_section=config_section
    )


def has_repo_local_credentials_section(
    repo_root: str | Path | None,
    *,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
) -> bool:
    """Return True iff *repo_root*'s own repo-local config file (default
    `.clagentic/loadout/config.yaml`, falling back to the legacy
    `.loadout/config.yaml` path when only that one exists — see
    `repo_config.resolve_repo_config_path` — or *config_relative_path*
    override) carries a non-empty `credentials:` section — the same
    detection `_reject_repo_local_credentials_section` uses to print its
    stderr warning, exposed here as a plain boolean so a read-only inspector
    (doctor.checks.check_repo_loadout_schema) can report the finding through
    its own CheckResult without duplicating the file-read/section-lookup
    logic or triggering the stderr warning as a side effect of a health
    check. Never emits its own deprecation warning (`warn=False`) — that
    would double up with doctor's/the actual loaders' own warning."""
    if repo_root is None:
        return False
    config_path = resolve_repo_config_path(
        repo_root, config_relative_path=config_relative_path, warn=False
    )
    section = _read_yaml_mapping(config_path).get(CONFIG_SECTION_CREDENTIALS)
    return isinstance(section, dict) and bool(section)


def resolve_platform_provider(
    platform: str,
    *,
    repo_root: str | Path | None = None,
    config_relative_path: str = DEFAULT_CONFIG_RELATIVE_PATH,
    config_root: str | Path | None = None,
    env: dict[str, str] | None = None,
    static_config_root: Path | None = None,
) -> TokenProvider:
    """Resolve the TokenProvider configured for *platform* ("forgejo" |
    "github").

    Reads env vars first, then the USER-LEVEL `<config_root>/config.yaml`'s
    `credentials:` section (default ~/.config/clagentic/loadout/config.yaml
    — see module docstring for precedence and the exact key names), falling
    back to StaticTokenProvider when neither source names a provider —
    unchanged default behavior from before this task.

    Args:
        platform: PLATFORM_FORGEJO or PLATFORM_GITHUB.
        repo_root: OPTIONAL repo root, used ONLY to check for and warn
            about a repo-local `.loadout/config.yaml` `credentials:` section
            (see module docstring) -- NEVER consulted for provider
            selection itself. A caller with no repo context (the common
            case: every production call site passes none) simply skips
            that warning check.
        config_relative_path: override the repo-local config file's
            relative path used by the warning check above (mainly for
            tests).
        config_root: override the user-level config root the credentials
            config-file tier reads from (mainly for tests). Defaults to
            DEFAULT_USER_CONFIG_ROOT (~/.config/clagentic/loadout).
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.
        static_config_root: passed through to StaticTokenProvider when the
            resolved kind is "static" (mainly for tests).

    Raises:
        InvalidProviderConfigError: unrecognized provider kind, or "command"
            selected with no command configured.
    """
    if platform not in _ENV_VARS_BY_PLATFORM:
        raise InvalidProviderConfigError(
            f"platform {platform!r} not recognized. Expected "
            f"{PLATFORM_FORGEJO!r} or {PLATFORM_GITHUB!r}."
        )

    active_env = env if env is not None else dict(os.environ)
    _reject_repo_local_credentials_section(
        repo_root, config_relative_path=config_relative_path
    )
    config_section = _load_credentials_section(config_root)

    kind, command_str = _resolve_provider_kind_and_command(
        platform, env=active_env, config_section=config_section
    )

    if kind == PROVIDER_KIND_STATIC:
        return StaticTokenProvider(config_root=static_config_root)

    # kind == PROVIDER_KIND_COMMAND
    if not command_str or not command_str.strip():
        provider_env_var, command_env_var = _ENV_VARS_BY_PLATFORM[platform]
        _provider_key, command_key = _CONFIG_KEYS_BY_PLATFORM[platform]
        raise InvalidProviderConfigError(
            f"platform {platform!r}: provider kind {PROVIDER_KIND_COMMAND!r} "
            f"selected but no command is configured. Set {command_env_var} "
            f"or the {CONFIG_SECTION_CREDENTIALS}.{command_key} config key "
            f"to the argv string to exec."
        )
    try:
        argv = shlex.split(command_str)
    except ValueError as exc:
        raise InvalidProviderConfigError(
            f"platform {platform!r}: configured command string "
            f"{command_str!r} could not be parsed as a shell-quoted argv: "
            f"{exc}."
        ) from exc
    if not argv:
        raise InvalidProviderConfigError(
            f"platform {platform!r}: configured command string "
            f"{command_str!r} parsed to an empty argv."
        )
    emits_json = _resolve_command_emits_json(
        platform, env=active_env, config_section=config_section
    )
    return CommandTokenProvider(argv, emit_structured_output=emits_json)


__all__ = [
    "CONFIG_KEY_COMMAND_EMITS_JSON_FORGEJO",
    "CONFIG_KEY_COMMAND_EMITS_JSON_GITHUB",
    "CONFIG_KEY_COMMAND_FORGEJO",
    "CONFIG_KEY_COMMAND_GITHUB",
    "CONFIG_KEY_PROVIDER_FORGEJO",
    "CONFIG_KEY_PROVIDER_GITHUB",
    "CONFIG_SECTION_CREDENTIALS",
    "DEFAULT_USER_CONFIG_ROOT",
    "PROVIDER_KIND_COMMAND",
    "PROVIDER_KIND_STATIC",
    "TOKEN_COMMAND_EMITS_JSON_ENV_VAR_FORGEJO",
    "TOKEN_COMMAND_EMITS_JSON_ENV_VAR_GITHUB",
    "TOKEN_COMMAND_ENV_VAR_FORGEJO",
    "TOKEN_COMMAND_ENV_VAR_GITHUB",
    "TOKEN_PROVIDER_ENV_VAR_FORGEJO",
    "TOKEN_PROVIDER_ENV_VAR_GITHUB",
    "USER_CONFIG_FILENAME",
    "InvalidProviderConfigError",
    "has_repo_local_credentials_section",
    "load_user_config_section",
    "resolve_platform_provider",
    "resolve_provider_kind_and_command",
]
