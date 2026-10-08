"""merge.pre_check_env -- the environment a pre_check child process receives.

A pre_check runs code from the PR head before that PR is merged, as a child of
the merger's process. The merger's environment carries identity and attestation
material (per-spawn and session sidecar variables, loadout's own sidecar-path
and identity variables) and may carry credentials. This module removes the
variables it recognises from the environment the child is *handed*.

What that does and does not protect against: the scrub controls only the
environment passed to the child. The child still runs as the merger's user, so
it can read that user's files (including credential files under `HOME` such as
`~/.git-credentials` and `~/.netrc`, which `HOME` passing through leaves in
reach) and the merger's own, unscrubbed `/proc/<pid>/environ`. A variable the
patterns below do not recognise is passed on. Withholding those would need a
process sandbox, which loadout does not provide.

The child gets the parent environment MINUS a denylist, not an allowlist, so
existing checks keep PATH, HOME, locale and virtualenv variables. A name is
removed when it is:

  1. an attestation/identity variable loadout reads, taken from the code that
     reads it (`transport.attestation.attestation_env_var_names`): the two
     env-tier overrides, the variable the configured identity layer names, and
     every configured sidecar adapter's `session_id_env`;
  2. in loadout's own `CLAGENTIC_LOADOUT_` namespace, every variable of which
     is read by loadout itself (provider selection, token command, sidecar
     paths, telemetry webhook token);
  3. credential-shaped (case-insensitive): a name containing TOKEN, SECRET,
     PASSWORD, PASSWD, CREDENTIAL, API_KEY, ACCESS_KEY or PRIVATE_KEY; a name
     ending `_KEY`, `_PAT`, `_PASS` or `_DSN`; `SSH_AUTH_SOCK`, `DATABASE_URL`,
     `GOOGLE_APPLICATION_CREDENTIALS`; and the `AWS_*`, `AZURE_*`, `GH_*`,
     `FORGEJO_*`, `BAO_*` and `VAULT_*` families. PATH, HOME, LANG, LC_*,
     VIRTUAL_ENV, PYTHONPATH and TMPDIR are not credential-shaped and survive;
  4. a variable that points at a credential source: `GIT_ASKPASS`,
     `SSH_ASKPASS`, `SUDO_ASKPASS`, `NETRC`, `KUBECONFIG`, `DOCKER_CONFIG` and
     the `GIT_CONFIG_*` family (`GIT_CONFIG_GLOBAL`, `GIT_CONFIG_SYSTEM`,
     `GIT_CONFIG_COUNT`, `GIT_CONFIG_KEY_n` and `GIT_CONFIG_VALUE_n` can name
     or inject a credential helper). `HOME` itself is kept by design.

  5. a git repository/location selector (`GIT_DIR`, `GIT_WORK_TREE`,
     `GIT_INDEX_FILE`, `GIT_COMMON_DIR`, `GIT_OBJECT_DIRECTORY`,
     `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_NAMESPACE`,
     `GIT_CEILING_DIRECTORIES`, `GIT_DISCOVERY_ACROSS_FILESYSTEM`,
     `GIT_PREFIX`). Left in place they would point git at a repository other
     than the one the check runs in, so the check and every git command loadout
     runs for the merge result resolve the repository from the working directory.

A deployment keeps a named variable deliberately through the deployment-tier
`merge.pre_checks_env_passthrough` list (`pre_checks_config`); a passed-through
name survives rules 1 to 4. Rule 5 is not overridable: naming a selector in the
list is a configuration error.
"""

from __future__ import annotations

import fnmatch
import os
from pathlib import Path
from typing import Iterable, Mapping

from clagentic_loadout.transport.attestation import attestation_env_var_names

#: Prefix of every environment variable loadout's own code reads.
LOADOUT_ENV_PREFIX = "CLAGENTIC_LOADOUT_"

#: Credential-shaped name patterns, matched case-insensitively against the
#: whole name. A name CONTAINING a credential word, ENDING in a credential
#: suffix, or starting with a credential-bearing vendor prefix is denied.
CREDENTIAL_NAME_PATTERNS = (
    "*TOKEN*",
    "*SECRET*",
    "*PASSWORD*",
    "*PASSWD*",
    "*CREDENTIAL*",
    "*API_KEY*",
    "*ACCESS_KEY*",
    "*PRIVATE_KEY*",
    "*_KEY",
    "*_PAT",
    "*_PASS",
    "*_DSN",
    "SSH_AUTH_SOCK",
    "DATABASE_URL",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "AWS_*",
    "AZURE_*",
    "GH_*",
    "FORGEJO_*",
    "BAO_*",
    "VAULT_*",
    "GIT_ASKPASS",
    "SSH_ASKPASS",
    "SUDO_ASKPASS",
    "NETRC",
    "KUBECONFIG",
    "DOCKER_CONFIG",
    "GIT_CONFIG_*",
)


#: Variables that select which git repository, work tree, index or object store
#: a git command operates on. Never passed to a check and never overridable.
GIT_LOCATION_ENV_NAMES = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_COMMON_DIR",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
        "GIT_CEILING_DIRECTORIES",
        "GIT_DISCOVERY_ACROSS_FILESYSTEM",
        "GIT_PREFIX",
    }
)


def is_git_location_name(name: str) -> bool:
    """True when *name* is a git repository/location selector variable."""
    return name.upper() in GIT_LOCATION_ENV_NAMES


def without_git_location(env: Mapping[str, str]) -> dict[str, str]:
    """A copy of *env* without the git repository/location selectors."""
    return {name: value for name, value in env.items() if not is_git_location_name(name)}


def is_denied_pre_check_name(name: str, *, attestation_names: Iterable[str] = ()) -> bool:
    """True when *name* must not reach a pre_check child process."""
    upper = name.upper()
    if name in set(attestation_names) or upper.startswith(LOADOUT_ENV_PREFIX):
        return True
    if upper in GIT_LOCATION_ENV_NAMES:
        return True
    return any(fnmatch.fnmatchcase(upper, pattern) for pattern in CREDENTIAL_NAME_PATTERNS)


def pre_check_env(
    env: Mapping[str, str] | None = None,
    *,
    passthrough: Iterable[str] = (),
    config_root: str | Path | None = None,
) -> dict[str, str]:
    """A copy of *env* (default `os.environ`) without the denied variables,
    except the names in *passthrough*.

    *config_root* is the user-level config root the attestation names are read
    from (default: the standard one).
    """
    source = dict(env) if env is not None else dict(os.environ)
    attestation_names = attestation_env_var_names(env=source, config_root=config_root)
    kept = set(passthrough)
    return {
        name: value
        for name, value in source.items()
        if not is_git_location_name(name)
        and (name in kept or not is_denied_pre_check_name(name, attestation_names=attestation_names))
    }


__all__ = [
    "CREDENTIAL_NAME_PATTERNS",
    "GIT_LOCATION_ENV_NAMES",
    "LOADOUT_ENV_PREFIX",
    "is_denied_pre_check_name",
    "is_git_location_name",
    "pre_check_env",
    "without_git_location",
]
