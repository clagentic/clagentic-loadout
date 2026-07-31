"""transport.attestation — resolves the ATTESTED INVOKING IDENTITY.

Task lr-82c385 (tome #700), the loadout-native half of the three-layer trust
model documented across `docs/merge-authority.md` §4 and
`transport.credential_provider`'s own module docstring: attested invoking
identity (1) -> crew/role (`--caller`, layer 2) -> credential grantor
(layer 3). Every seam downstream of layer (2) -- `credential_provider.
resolve_token`, `merge.authority.check_authority` -- has always, deliberately,
treated `--caller`/`--role` as an ALREADY-ATTESTED, opaque value (lr-e5eeab)
and refused to re-derive or re-verify it, precisely so this package never
reaches into a harness-specific identity sidecar/side-channel (the relay
lesson, CLAUDE.md rule 2). That left the (1)->(2) BINDING ITSELF -- "does the
`--caller` value on this invocation's argv actually match the identity this
deployment's own attestation source vouches for" -- entirely unenforced
inside loadout. This module resolves what the identity IS; `bind_caller` (in
`transport.git_host_api`) is the fail-closed enforcement point that compares
it against `--caller` BEFORE any network I/O.

Mirrors the Go reference contract shipped in clagentic-gatekeeper's T0
(lr-83549f, `internal/attestation`, PR #15 @ 9e9116c) -- SAME resolution
order, SAME "a provider that finds nothing falls through, any other error is
a hard failure" semantics, ported to Python for loadout's own transport
rather than importing across a language boundary. No agent names, org names,
or other deployment-specific identities are hardcoded anywhere in this
module (workspace rule 11 / build-to-share) -- see `resolve_identity`'s
config surface below for exactly what a deployment supplies.

Resolution order, per deployment (fixed; a deployment customizes or omits
each layer via config, but never reorders the chain):

  1. **Configured provider** -- a deployment points this module at its own
     identity source via the `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV` env
     var (names ANOTHER env var this process's own spawn env already
     carries the attested identity under) or the user-level config file's
     `attestation.identity_env` key (same config-root/loader convention
     `transport.provider_config` and `transport.git_host_api`'s git-host-
     base-URL tier already use). Whichever env var that name points at is
     read verbatim as `Identity(subject=..., source="configured")`. Takes
     precedence whenever it resolves to a non-empty value.
  2. **Sidecar adapter** -- reads a session-scoped identity file written by
     an external harness. Within this layer, THREE sources are tried in
     this fixed order, the first that resolves wins (lr-8e1593):

       (a) `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` (env) -- a
           single literal path. HIGHEST precedence within the layer,
           unchanged from before lr-8e1593 -- preserves compatibility with
           a harness that stamps this exact env var into a per-command
           spawn's environment. FAIL-CLOSED ON MISS (lr-1e16a4): unlike
           (b)/(c) below, this source is an EXPLICIT per-invocation claim
           -- the caller is naming a specific file it expects to hold ITS
           OWN identity. If that env var is SET but the file it names is
           absent, empty, or unreadable-as-empty, this layer does NOT fall
           through to (b), (c), or the built-in fallback; the WHOLE
           resolution chain fails closed with `AttestationError`. Falling
           through here would risk silently resolving a DIFFERENT identity
           (most concretely, a lower-precedence session-keyed adapter
           resolving a PARENT session's identity) for whatever this
           process's real identity was supposed to be -- a privilege-
           substitution shape at any caller-bound mint (`bind_caller`).
           Mirrors clagentic-gatekeeper's DomainA2A fail-closed-on-MISS
           fix (lr-2ca216). This trigger is "env var SET to a path that
           does not resolve," never "env var unset" -- an unset env var
           still declines this source ordinarily and falls through to
           (b)/(c) exactly as before (see `_ConfiguredEnvProvider`'s and
           this source's own resolve() for where that branch lives).
       (b) the config file's `attestation.identity_sidecar_path` key -- a
           single literal path, RETAINED for backward compatibility,
           unchanged semantics.
       (c) the config file's `attestation.sidecars` key (NEW, lr-8e1593)
           -- an ORDERED LIST of adapters, each
           `{dir, file_prefix, session_id_env}`. Walked in declared order;
           an adapter is SKIPPED (not an error) when its `session_id_env`
           is unset/empty in this process's environment, or the file it
           composes (`dir/file_prefix<session id>`) is absent. The first
           adapter that both has a non-empty session id AND whose composed
           file exists wins. This is what makes session-keyed sidecar
           discovery possible at all: (a)/(b) can only ever name ONE
           literal path per process, so a harness that runs many
           concurrently-live sessions (each with its own session-scoped
           sidecar file) has no way to point every invocation at "its own"
           file without per-invocation env-stamping -- (c) lets a
           deployment instead point this module at the *shape* of its
           sidecar convention (a directory + filename prefix) and have it
           find the right file for THIS invocation via whatever session-id
           env var this process already carries, no per-command stamping
           required. Mirrors clagentic-gatekeeper's Go reference adapter
           list (`internal/attestation/sidecar.go`, epic lr-0029bf) --
           see that module's `SidecarConfig`/`isSafePathSegment`/
           `requireContained` for the faithful-port source this list is
           ported from. A session id value that is NOT path-safe (contains
           a path separator, or is `.`/`..`) is REFUSED for that adapter
           (skip to the next adapter), never sanitized -- an attacker who
           controls the session-id env var must not be able to redirect
           the composed path outside `dir` by smuggling `../` into it.

     This module does NOT assume any specific harness/sidecar SHAPE beyond
     "a file containing exactly one identity value" (the file's entire
     stripped text content, first line only) -- every path here is
     supplied by config, never hardcoded, and this is the ONLY place a
     sidecar path ever enters this module: it is read, not interpreted as
     belonging to any named harness. Every resolved path (from any of the
     three sources) is opened atomically with `os.O_NOFOLLOW`, and every
     check runs against that same file descriptor (no separate
     lstat-then-reopen sequence, closing the residual TOCTOU window
     lr-904b1d fixed) -- a symlink (or any other non-regular directory
     entry) is refused with a hard `AttestationError`, never silently
     followed, matching the Go reference's own symlink-refusal fix (see
     `_SidecarFileProvider`'s own docstring for the full rationale: a
     planted symlink in a world-writable directory such as `/tmp` must
     never be able to redirect this read to an arbitrary file, since the
     resolved value feeds `bind_caller`'s authorization decision
     directly).

     No adapter across all three sources resolves -> this layer declines
     entirely -> the chain proceeds to the built-in fallback exactly as it
     did before this list existed; a deployment that configures nothing
     new here sees byte-identical behavior (acceptance criterion (b),
     lr-8e1593).
  3. **Built-in fallback** -- the OS-reported invoking user
     (`getpass.getuser()`, which itself falls back through `LOGNAME`/
     `USER`/`LNAME`/`USERNAME` env vars and finally the passwd database on
     POSIX). Always available, so a bare install has an attested source
     rather than failing open with no identity at all.

Every layer that is configured but has nothing to offer (missing env var,
a genuinely ABSENT sidecar file at sources (b)/(c)) falls through to the
next layer -- NOT a hard failure. Three things ARE hard failures, raised
immediately and never swallowed into a fall-through: (a) the sidecar path
resolving to a symlink or other non-regular directory entry (see layer 2
above -- a security refusal, not an ordinary decline), (b) source (a) of
layer 2 (the env-var single-path override) being explicitly SET but its
file being absent/empty/unreadable-as-empty (lr-1e16a4 -- an EXPLICIT
per-invocation claim that misses must not be silently demoted to
"unconfigured, try something lower-precedence"; see source (a) above for
the full rationale), and (c) the built-in fallback itself failing (should
never happen in practice; `getpass.getuser()` degrades all the way to a
`KeyError`/`OSError` on a truly identity-less environment), since there is
nothing left to fall through to either way.

This module resolves WHAT the identity is. It does not itself decide
whether that identity may act as any particular `--caller`/role value --
that binding-and-refusal decision is `transport.git_host_api.bind_caller`'s
job (see that function's docstring for the fail-closed comparison and its
own module docstring's "layer (1)->(2) binding" note).
"""

from __future__ import annotations

import errno
import getpass
import os
import stat
from pathlib import Path
from typing import Protocol, runtime_checkable

from clagentic_loadout.transport.provider_config import (
    DEFAULT_USER_CONFIG_ROOT,
    load_user_config_section,
)

#: Top-level config-file section this module owns within the USER-LEVEL
#: <config_root>/config.yaml -- same file/loader convention every other
#: user-level config tier in this package uses (transport.provider_config's
#: `credentials:` section, transport.git_host_api's `git_host:` section).
ATTESTATION_CONFIG_SECTION = "attestation"

#: Config-file key naming the env var that carries the configured-provider
#: identity value (layer 1). The value of THIS key is a NAME, never the
#: identity value itself -- mirrors CommandTokenProvider's argv-template
#: indirection: config never carries a live credential/identity string, only
#: where to find one.
ATTESTATION_CONFIG_KEY_IDENTITY_ENV = "identity_env"

#: Config-file key naming the sidecar file path (layer 2).
ATTESTATION_CONFIG_KEY_SIDECAR_PATH = "identity_sidecar_path"

#: Config-file key naming the ORDERED sidecar adapter list (layer 2, NEW
#: lr-8e1593): `attestation.sidecars: [{dir, file_prefix, session_id_env}]`.
#: See `_SidecarFileProvider._resolve_adapter` for the per-adapter
#: resolution rule, and this module's own docstring (layer 2, source (c))
#: for the full precedence rationale. No tool/harness name is ever
#: hardcoded here -- a deployment supplies its own `dir`/`file_prefix`/
#: `session_id_env` values; this module only walks the declared shape.
ATTESTATION_CONFIG_KEY_SIDECARS = "sidecars"

#: Per-adapter config keys within one entry of `attestation.sidecars`.
SIDECAR_ADAPTER_KEY_DIR = "dir"
SIDECAR_ADAPTER_KEY_FILE_PREFIX = "file_prefix"
SIDECAR_ADAPTER_KEY_SESSION_ID_ENV = "session_id_env"

#: Env var naming the env var that carries the configured-provider identity
#: value (layer 1) -- env-tier equivalent of ATTESTATION_CONFIG_KEY_IDENTITY_ENV,
#: takes precedence over the config-file key per this module's resolution
#: order (env wins over config-file, matching transport.provider_config's
#: own per-platform precedence rule).
ATTESTED_IDENTITY_ENV_VAR = "CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV"

#: Env var naming the sidecar file path (layer 2) -- env-tier equivalent of
#: ATTESTATION_CONFIG_KEY_SIDECAR_PATH.
ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR = "CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH"

#: Source labels, mirroring the Go reference's Identity.Source values
#: exactly (configured / sidecar / builtin) so a deployment correlating logs
#: across both languages sees the same vocabulary.
SOURCE_CONFIGURED = "configured"
SOURCE_SIDECAR = "sidecar"
SOURCE_BUILTIN = "builtin"


class AttestationError(Exception):
    """Raised when NO layer in the resolution chain -- including the
    built-in OS-user fallback -- can resolve an identity. Distinct from a
    single layer declining (which falls through silently); this is a hard
    failure of the whole chain, expected only on a truly identity-less
    environment (no configured provider, no sidecar, and even
    getpass.getuser() cannot resolve anything)."""


class Identity:
    """The attested invoking identity resolved by `resolve_identity`.

    `subject` is the attested identity value itself (an agent name, a
    service account, an OS username -- whatever the resolving layer
    produced; this module assigns no further meaning to it). `source`
    names which layer resolved it (SOURCE_CONFIGURED / SOURCE_SIDECAR /
    SOURCE_BUILTIN), for audit/debugging -- it is not itself part of any
    trust decision `bind_caller` makes.
    """

    __slots__ = ("subject", "source")

    def __init__(self, subject: str, source: str) -> None:
        self.subject = subject
        self.source = source

    def __repr__(self) -> str:  # pragma: no cover -- debug convenience only
        return f"Identity(subject={self.subject!r}, source={self.source!r})"

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Identity)
            and self.subject == other.subject
            and self.source == other.source
        )


@runtime_checkable
class IdentityProvider(Protocol):
    """One layer in the attestation-resolution chain.

    A provider that has no identity to offer for the current invocation
    returns None -- `resolve_identity` falls through to the next provider in
    order. A provider is never asked to raise for "I have nothing"; None is
    the well-formed empty answer. Implementations own their own resolution
    mechanism entirely.
    """

    def resolve(self) -> "Identity | None":
        """Return the attested identity, or None if this provider has
        nothing to offer for the current invocation."""
        ...


class _ConfiguredEnvProvider:
    """Layer 1: reads the identity value from an env var NAMED by
    ATTESTED_IDENTITY_ENV_VAR (env) or the config file's `identity_env` key.

    The configured NAME is itself resolved env-var-first, config-file-second
    (mirrors transport.provider_config's per-platform precedence). Returns
    None when no name is configured, or the named env var is unset/empty --
    both are "nothing to offer here," never a hard failure at this layer.
    """

    def __init__(self, *, env: dict[str, str], config_root) -> None:
        self._env = env
        self._config_root = config_root

    def resolve(self) -> "Identity | None":
        var_name = self._env.get(ATTESTED_IDENTITY_ENV_VAR)
        if not var_name:
            section = load_user_config_section(
                ATTESTATION_CONFIG_SECTION, config_root=self._config_root
            )
            var_name = section.get(ATTESTATION_CONFIG_KEY_IDENTITY_ENV)
        if not var_name:
            return None
        value = self._env.get(var_name)
        if not value or not value.strip():
            return None
        return Identity(subject=value.strip(), source=SOURCE_CONFIGURED)


def _is_safe_session_id(session_id: str) -> bool:
    """A session id is an opaque token read from the environment, never a
    path -- reject anything that could traverse out of a sidecar adapter's
    configured `dir` (lr-8e1593). REFUSE, do not sanitize: a value this
    function rejects causes the calling adapter to decline (skip to the
    next adapter / layer), never a best-effort rewrite of the value.
    Mirrors the Go reference's `isSafePathSegment`
    (`internal/attestation/sidecar.go`) -- non-empty, not `.`/`..`, no
    path separator in either OS form, and unchanged by `os.path.normpath`
    (catches anything else path-like our explicit checks missed)."""
    if session_id in ("", ".", ".."):
        return False
    if "/" in session_id or "\\" in session_id:
        return False
    if os.path.normpath(session_id) != session_id:
        return False
    return True


def _read_sidecar_identity_file(
    path_str: str, *, fail_closed_on_miss: bool = False
) -> "Identity | None":
    """Read one sidecar identity file at *path_str*, applying the SAME
    atomic O_NOFOLLOW+fstat read every sidecar source in this layer uses
    (lr-904b1d, extended unchanged to the adapter-list source by
    lr-8e1593) -- see `_SidecarFileProvider`'s own docstring for the full
    TOCTOU/symlink-hardening rationale this function implements once, for
    every caller in this layer.

    Returns None for a genuinely absent or empty-content file (a plain
    decline) -- UNLESS *fail_closed_on_miss* is True, in which case that
    same absent/empty/unreadable-as-empty condition raises
    `AttestationError` instead (lr-1e16a4: see the module docstring's
    "explicit source-(a) requested-but-absent" note below). Raises
    `AttestationError` unconditionally for a symlink or other non-regular
    directory entry at *path_str* (a hard failure, never silently demoted
    to "unconfigured" -- see the module docstring, layer 2), regardless of
    *fail_closed_on_miss*.

    *fail_closed_on_miss* exists because "absent" means something
    different depending on WHICH source resolved *path_str*: sources (b)
    (config single-path) and (c) (adapter list) are optional conveniences
    a deployment MAY configure, so an absent file there is an ordinary
    decline -- there was no explicit per-invocation claim to honor. Source
    (a) (the env-var override) is different: a caller that sets
    `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` to a specific path
    is making an explicit, per-spawn claim about WHERE its identity lives.
    If that exact file is missing, falling through to a lower-precedence
    source can resolve a DIFFERENT identity than the one the caller
    pointed at (e.g. a parent session's identity via the source-(c)
    adapter list) -- a privilege-substitution shape for any caller-bound
    mint (`transport.git_host_api.bind_caller`). Mirrors
    clagentic-gatekeeper's `internal/attestation` DomainA2A fail-closed-
    on-MISS fix (lr-2ca216): an explicitly-requested source that misses
    declines the WHOLE chain, not just this one layer.
    """
    try:
        fd = os.open(path_str, os.O_NOFOLLOW | os.O_RDONLY)
    except FileNotFoundError:
        if fail_closed_on_miss:
            raise AttestationError(
                f"attestation FAILED -- "
                f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} explicitly names "
                f"sidecar identity path {path_str!r}, but that file does "
                f"not exist. This is an explicit per-invocation identity "
                f"claim, not an optional convenience -- a MISS here "
                f"refuses the whole attestation chain rather than falling "
                f"through to a lower-precedence source (config sidecar "
                f"path, session-keyed adapter list, or the built-in "
                f"OS-user fallback), any of which could resolve a "
                f"DIFFERENT identity than the one this env var pointed "
                f"at."
            ) from None
        return None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            # Final path component is a symlink -- O_NOFOLLOW refused
            # the open outright. Same hard-failure treatment as any
            # other non-regular directory entry below: never silently
            # demoted to "unconfigured."
            raise AttestationError(
                f"attestation FAILED -- configured sidecar identity path "
                f"{path_str!r} is a symlink, refused by O_NOFOLLOW. A "
                f"symlink or other non-regular directory entry at this "
                f"path is refused unconditionally -- a planted symlink "
                f"in a world-writable directory must never be able to "
                f"redirect this read to an arbitrary file."
            ) from exc
        raise AttestationError(
            f"attestation FAILED -- could not open the configured "
            f"sidecar identity path {path_str!r}: {exc}."
        ) from exc

    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # Directory, device, socket, FIFO, etc. opened successfully
            # (O_NOFOLLOW only refuses a symlink component) but is not a
            # regular file -- refuse the same way a symlink would be.
            raise AttestationError(
                f"attestation FAILED -- configured sidecar identity "
                f"path {path_str!r} is not a regular file (mode "
                f"{oct(info.st_mode)!r}). A symlink or other "
                f"non-regular directory entry at this path is refused "
                f"unconditionally -- a planted symlink in a "
                f"world-writable directory must never be able to "
                f"redirect this read to an arbitrary file."
            )
        try:
            raw = os.read(fd, info.st_size).decode("utf-8")
        except OSError as exc:
            raise AttestationError(
                f"attestation FAILED -- could not read the configured "
                f"sidecar identity path {path_str!r}: {exc}."
            ) from exc
    finally:
        os.close(fd)

    first_line = raw.splitlines()[0].strip() if raw.strip() else ""
    if not first_line:
        if fail_closed_on_miss:
            raise AttestationError(
                f"attestation FAILED -- "
                f"{ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR} explicitly names "
                f"sidecar identity path {path_str!r}, but that file is "
                f"empty. Same fail-closed treatment as a MISSING file for "
                f"this explicitly-requested source -- see the "
                f"FileNotFoundError branch above for the full rationale."
            )
        return None
    return Identity(subject=first_line, source=SOURCE_SIDECAR)


class _SidecarFileProvider:
    """Layer 2: reads the identity value from a file NAMED by
    ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR (env), the config file's
    `identity_sidecar_path` key, or (lr-8e1593) the first hit in the
    config file's ordered `attestation.sidecars` adapter list.

    Reads the file's entire text content, stripped, first line only -- no
    assumption about the writing harness's own format beyond "one identity
    value, optionally followed by a trailing newline." Returns None when no
    path is configured, or a source (b)/(c) configured path is genuinely
    absent -- never a hard failure at this layer for a plain missing file
    on those two sources (an external harness that has not written its
    sidecar yet, or was never configured to, is not this module's problem
    to fail loudly over; the chain falls through to the built-in fallback
    instead). Source (a) -- the env-var single-path override -- is the ONE
    exception (lr-1e16a4): it is an EXPLICIT per-invocation claim, so a
    miss there fails the whole chain closed instead of falling through;
    see `resolve()`'s own comment at that call site and this module's
    top-level docstring (layer 2, source (a)) for the full rationale.

    ATOMIC O_NOFOLLOW READ (lr-904b1d, closing the residual TOCTOU left by
    an earlier lstat-then-read_text sequence -- pre-merge security-review
    finding on the prior symlink-refusal patch): the configured path is
    opened with `os.O_NOFOLLOW | os.O_RDONLY`
    and every subsequent check (regular-file test, content read) operates on
    the returned file descriptor via `os.fstat`/`os.read`, never re-touching
    the path string. A separate `lstat()` call followed by a *second*,
    independent `read_text()` open leaves a window between the two syscalls
    in which the path entry could be replaced (e.g. a regular file swapped
    for a symlink between the check and the read) -- a classic
    check-then-use race, distinct from (and in addition to) the plain
    symlink case. Collapsing the check and the read onto the SAME open file
    descriptor removes that window entirely: whatever `fstat(fd)` reports is
    guaranteed to describe the exact bytes `os.read(fd, ...)` subsequently
    returns, because both operate on the same already-resolved fd rather
    than re-resolving the path.

    `O_NOFOLLOW` makes `open()` itself refuse a symlink in the final path
    component (raising `OSError` with `errno.ELOOP`), which this method
    maps to the SAME `AttestationError` a non-regular directory entry
    (directory, device, socket, FIFO) gets from the `fstat` check below --
    preserving the exact symlink-hardening property the previous
    lstat-based implementation provided (see clagentic-gatekeeper's
    `internal/attestation/sidecar.go` reference and the still-relevant
    rationale below), just via a single atomic syscall pair instead of two
    independent ones.

    A planted symlink at the configured path -- trivial in a world-writable
    directory such as `/tmp`, which is exactly where a deployment's sidecar
    convention commonly lives -- must never be able to redirect this read
    to an arbitrary file elsewhere on disk; the resolved `subject` feeds
    `bind_caller`'s authorization decision directly, so a redirected read is
    a privilege-escalation primitive, not a cosmetic bug. Unlike a genuinely
    absent file (declines, falls through), a symlink or any other
    non-regular directory entry at the configured path is a HARD FAILURE:
    this layer raises `AttestationError` rather than silently declining, so
    a planted symlink can never be quietly bypassed by falling through to a
    lower-trust layer -- an attacker who can plant a symlink at this path
    should not be able to also demote the check to "as if the sidecar were
    merely unconfigured."
    """

    def __init__(self, *, env: dict[str, str], config_root) -> None:
        self._env = env
        self._config_root = config_root

    def resolve(self) -> "Identity | None":
        # (a) env single-path override -- HIGHEST precedence within this
        # layer, unchanged (preserves per-command subagent env stamping).
        # lr-1e16a4: this source is an EXPLICIT per-invocation claim, so a
        # miss here (absent/empty/unreadable-as-empty file) fails the
        # WHOLE chain closed rather than falling through to (b)/(c)/layer
        # 3 -- see `_read_sidecar_identity_file`'s `fail_closed_on_miss`
        # docstring for the full rationale. A symlink/non-regular entry at
        # this path is ALREADY a hard failure regardless (unchanged).
        path_str = self._env.get(ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR)
        if path_str:
            return _read_sidecar_identity_file(path_str, fail_closed_on_miss=True)

        section = load_user_config_section(
            ATTESTATION_CONFIG_SECTION, config_root=self._config_root
        )

        # (b) config single-path override -- retained, unchanged semantics.
        path_str = section.get(ATTESTATION_CONFIG_KEY_SIDECAR_PATH)
        if path_str:
            return _read_sidecar_identity_file(path_str)

        # (c) NEW (lr-8e1593): ordered adapter list. Walked in declared
        # order; the first adapter whose session id resolves AND whose
        # composed file exists wins. An adapter that has nothing to offer
        # is skipped (not an error) -- see module docstring, layer 2,
        # source (c).
        adapters = section.get(ATTESTATION_CONFIG_KEY_SIDECARS)
        if isinstance(adapters, list):
            for adapter in adapters:
                if not isinstance(adapter, dict):
                    continue
                identity = self._resolve_adapter(adapter)
                if identity is not None:
                    return identity

        return None

    def _resolve_adapter(self, adapter: dict) -> "Identity | None":
        """Resolve ONE `attestation.sidecars` list entry, or None when this
        adapter has nothing to offer for the current invocation (missing
        config keys, unset/empty session-id env var, unsafe session-id
        value, or a genuinely absent composed file -- all plain declines,
        never a hard failure; see the module docstring's source (c) for
        the full rule). Mirrors clagentic-gatekeeper's Go reference
        `sidecarProvider.Resolve` (`internal/attestation/sidecar.go`,
        epic lr-0029bf): same three required fields, same
        session-id-must-be-a-safe-single-path-component refusal, same
        skip-not-error semantics for anything short of a resolved file.
        """
        directory = adapter.get(SIDECAR_ADAPTER_KEY_DIR)
        file_prefix = adapter.get(SIDECAR_ADAPTER_KEY_FILE_PREFIX)
        session_id_env = adapter.get(SIDECAR_ADAPTER_KEY_SESSION_ID_ENV)
        if not directory or not file_prefix or not session_id_env:
            # Partially configured adapter entry -- treated as disabled,
            # never guessed at (mirrors the Go reference's `enabled()`).
            return None

        session_id = (self._env.get(session_id_env) or "").strip()
        if not session_id:
            # This adapter's harness is not active in this invocation's
            # environment -- decline, do not error.
            return None

        if not _is_safe_session_id(session_id):
            # A session id is an opaque token from the environment, never a
            # path -- refuse anything that could redirect the composed read
            # (separators, "..") rather than sanitizing it. Skip to the
            # next adapter; an attacker-controlled env var must not be able
            # to demote this to "no adapters configured" either, so this
            # is a decline of THIS adapter only, not the whole layer.
            return None

        path_str = os.path.join(str(directory), f"{file_prefix}{session_id}")
        return _read_sidecar_identity_file(path_str)


class _BuiltinOsUserProvider:
    """Layer 3: the OS-reported invoking user (`getpass.getuser()`), which
    itself falls through `LOGNAME`/`USER`/`LNAME`/`USERNAME` and finally the
    passwd database on POSIX. Always available in practice -- this is the
    "a bare install still has an attested source" guarantee -- so this is
    the ONE layer whose failure is NOT swallowed as "nothing to offer";
    `resolve_identity` treats a `getpass.getuser()` exception here as the
    whole chain's terminal failure (AttestationError), since there is
    nothing left to fall through to.
    """

    def resolve(self) -> "Identity | None":
        subject = getpass.getuser()
        if not subject:
            return None
        return Identity(subject=subject, source=SOURCE_BUILTIN)


def _default_chain(*, env: dict[str, str], config_root) -> list[IdentityProvider]:
    return [
        _ConfiguredEnvProvider(env=env, config_root=config_root),
        _SidecarFileProvider(env=env, config_root=config_root),
        _BuiltinOsUserProvider(),
    ]


def resolve_identity(
    *,
    env: dict[str, str] | None = None,
    config_root: str | Path | None = None,
    providers: list[IdentityProvider] | None = None,
) -> Identity:
    """Walk the attestation-resolution chain in FIXED order (configured
    provider -> sidecar adapter -> built-in OS-user fallback) and return the
    first identity found.

    Args:
        env: override the environment mapping (mainly for tests). Defaults
            to os.environ.
        config_root: override the user-level config root the configured/
            sidecar tiers' config-file lookups read from (mainly for
            tests). Defaults to
            transport.provider_config.DEFAULT_USER_CONFIG_ROOT.
        providers: override the ENTIRE provider chain (mainly for tests --
            e.g. injecting a fake configured-provider identity without
            touching real env/config). When supplied, `env`/`config_root`
            are ignored for chain CONSTRUCTION (the injected providers are
            used exactly as given).

    Raises:
        AttestationError: every provider in the chain -- including the
            built-in fallback -- returned None or raised. Expected only on a
            truly identity-less environment; a production process almost
            always resolves at least the built-in layer.
    """
    active_env = env if env is not None else dict(os.environ)
    resolved_config_root = config_root if config_root is not None else DEFAULT_USER_CONFIG_ROOT
    chain = (
        providers
        if providers is not None
        else _default_chain(env=active_env, config_root=resolved_config_root)
    )
    for provider in chain:
        identity = provider.resolve()
        if identity is not None:
            return identity
    raise AttestationError(
        "attestation FAILED -- no layer in the resolution chain (configured "
        "provider, sidecar adapter, built-in OS-user fallback) could resolve "
        "an attested invoking identity. This should not happen in practice "
        "(the built-in fallback resolves the OS-reported invoking user "
        "whenever one is available) -- check that this process has a "
        "resolvable OS user, or configure "
        f"{ATTESTED_IDENTITY_ENV_VAR} / the "
        f"{ATTESTATION_CONFIG_SECTION!r}.{ATTESTATION_CONFIG_KEY_IDENTITY_ENV!r} "
        "config key to point at a real identity source."
    )


__all__ = [
    "ATTESTATION_CONFIG_KEY_IDENTITY_ENV",
    "ATTESTATION_CONFIG_KEY_SIDECAR_PATH",
    "ATTESTATION_CONFIG_KEY_SIDECARS",
    "ATTESTATION_CONFIG_SECTION",
    "ATTESTED_IDENTITY_ENV_VAR",
    "ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR",
    "SIDECAR_ADAPTER_KEY_DIR",
    "SIDECAR_ADAPTER_KEY_FILE_PREFIX",
    "SIDECAR_ADAPTER_KEY_SESSION_ID_ENV",
    "SOURCE_BUILTIN",
    "SOURCE_CONFIGURED",
    "SOURCE_SIDECAR",
    "AttestationError",
    "Identity",
    "IdentityProvider",
    "resolve_identity",
]
