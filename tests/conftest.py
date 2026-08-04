"""tests/conftest.py — shared pytest fixtures.

Session-wide test infrastructure only; no product-code behavior lives here
(CLAUDE.md code-craft rule 5: never modify an existing test to make it
pass). This file exists so PRE-EXISTING tests that predate lr-82c385's
`--caller`/attested-invoker binding, and are about an entirely orthogonal
concern (verify-comment, body-env, verdict-block composition, credential/
authority role resolution, etc.), do not each individually have to learn
about a feature they were never written to exercise.

PEACHES finding (PR #97 comment 14518) on the FIRST version of this fixture:
a blanket autouse patch that replaced `git_host_api.bind_caller` with a
no-op silently disabled enforcement across ~100+ pre-existing tests --
every one of them passed, but the DEFAULT code path (no `identity_provider=`
override) was never actually exercising `bind_caller`'s real comparison
logic at all, which is exactly the coverage gap PEACHES flagged.

**Fix: `bind_caller` itself is NEVER patched.** Instead,
`_default_identity_matches_parsed_caller` wraps every verb `main()` that now
calls `transport.caller_binding.bind_caller` (lr-c75c9a extended this
wrapper from `git_host_api.main` alone to `push.verb.main`,
`review.verb.main`, `acquire.verb.main`, `merge.verb.main`,
`merge.close_verb.main`, and `merge.post_merge_verb.main` -- every verb this
task wired the fail-closed binding into) so that, for any test call that
omits `identity_provider=`, the ATTESTED IDENTITY supplied to the real,
unpatched `bind_caller` is derived from that SAME invocation's own
`--caller`/`--role` argv value -- i.e. "this test's identity always matches
its own caller/role," which is genuinely true of every one of those
pre-existing tests' intent (none of them are testing an
attested-identity MISMATCH; that is what
`tests/test_git_host_api_caller_attested_invoker_binding.py` and
`tests/test_caller_binding_conformance.py` explicitly and exclusively
cover, always by injecting their own `identity_provider=`, which this
wrapper never overrides -- an explicit keyword argument a test supplies
always wins). `bind_caller`'s real equality check therefore runs, and
passes, on every one of those pre-existing invocations -- the happy path is
genuinely covered, not stubbed out.

**Opt-out marker, for a test that wants a wrapped verb's UNWRAPPED default
behavior** (i.e. the real production `transport.attestation.
resolve_identity`, reading this process's actual env/config/OS user):
`@pytest.mark.no_caller_binding_autofill`. No test in this suite currently
needs this — real attestation resolution is exactly what
test_transport_attestation.py exercises directly, and the dedicated
mismatch-binding test files always inject their own `identity_provider=` —
but the marker exists so a future test that specifically wants the real
chain (e.g. a smoke test asserting the installed entry point resolves a
real OS user with zero mocking) has an explicit, discoverable way to ask
for it, rather than reaching for a second ad-hoc patching mechanism.

**Session/environment isolation for the REAL `resolve_identity` fallthrough
path (lr-dbc905 follow-up).** The comment above at
`_wrap_main_with_identity_autofill` ("caller_value is None: ... letting
main() fall through to the REAL resolve_identity is correct and harmless")
is true for `bind_caller`'s own comparison (skipped entirely when
`caller_explicit=False`), but every wrapped verb's `main()` still calls
`resolve_identity_fn()` UNCONDITIONALLY to compute the `attested_identity`
value passed into `bind_caller` -- before `bind_caller` ever looks at
`caller_explicit`. When `identity_provider` is not injected (an omitted
`--caller`/`--role`), that is the REAL, unwrapped `transport.attestation.
resolve_identity()`, called with zero explicit arguments, which reads the
live process `os.environ` and the import-time-bound `attestation.
DEFAULT_USER_CONFIG_ROOT` (`~/.config/clagentic/loadout` by default) with
NO isolation from either. In an ordinary CI/dev environment neither
resolves to anything (both layers 1/2 decline, falls through to the
built-in OS-user layer, always succeeds) -- but inside a crew-spawned agent
session, `CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH` and/or a
config-file `attestation.sidecars` adapter keyed on `CLAUDE_CODE_SESSION_ID`
can resolve a LIVE, session-specific identity (or, worse, FAIL CLOSED with
`AttestationError` when the env var names a sidecar file that happens not
to exist at read time) purely because of which agent session happens to be
running the suite -- a suite result that depends on the identity of the
process invoking it is not a gate (repo CLAUDE.md hard rule 6: the suite
must pass with a synthetic registry and no real deployment identity).
`_isolate_real_attestation_chain` below closes this: an autouse fixture
that (1) redirects `attestation.DEFAULT_USER_CONFIG_ROOT` to a per-test
tmp directory with no `config.yaml` at all (so layers 1/2's config-file
tier always resolves to `{}`, matching a bare/unconfigured install) and (2)
scrubs the env vars a live crew/harness spawn environment sets that could
otherwise steer the chain (`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_ENV`,
`CLAGENTIC_LOADOUT_ATTESTED_IDENTITY_SIDECAR_PATH`,
`CLAGENTIC_SUBAGENT_ID`, `CLAUDE_CODE_SESSION_ID`) -- so the REAL chain,
whenever it does run, deterministically falls through to the built-in
OS-user layer (`SOURCE_BUILTIN`) exactly as a bare install would, in every
environment this suite runs in. `attestation.DEFAULT_USER_CONFIG_ROOT` is
the correct patch target, not `provider_config.DEFAULT_USER_CONFIG_ROOT`:
`attestation.py` binds its own independent copy of the name via
`from ... import DEFAULT_USER_CONFIG_ROOT` at import time, so
`resolve_identity`'s own bare-name lookup (`config_root if config_root is
not None else DEFAULT_USER_CONFIG_ROOT`) resolves against `attestation`'s
own module namespace, never `provider_config`'s.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.acquire import verb as acquire_verb
from clagentic_loadout.merge import close_verb as merge_close_verb
from clagentic_loadout.merge import post_merge_verb as merge_post_merge_verb
from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import attestation, git_host_api

#: Env vars a live crew/harness spawn environment may set that could steer
#: the REAL `attestation.resolve_identity()` chain toward a session-specific
#: identity (or a fail-closed AttestationError) -- see
#: `_isolate_real_attestation_chain`'s own docstring below for the full
#: rationale. Scrubbed on every test regardless of whether that test's own
#: verb call ever reaches the unwrapped chain, so the suite's result never
#: depends on which agent session happens to be running it.
_ATTESTATION_ENV_VARS_TO_SCRUB: tuple[str, ...] = (
    attestation.ATTESTED_IDENTITY_ENV_VAR,
    attestation.ATTESTED_IDENTITY_SIDECAR_PATH_ENV_VAR,
    "CLAGENTIC_SUBAGENT_ID",
    "CLAUDE_CODE_SESSION_ID",
)


def _parse_flag_value_from_argv(argv: list[str], flag: str) -> str | None:
    """Extract the value following a literal *flag* token in *argv*, exactly
    mirroring how ``argparse`` (via each verb's own ``_build_arg_parser``)
    would bind it -- ``None`` when *flag* is absent (an omitted --caller/
    --role is never checked by ``bind_caller`` regardless, per its own
    docstring, so returning ``None`` here is always safe)."""
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


def _wrap_main_with_identity_autofill(module, *, flag: str):
    """Return a wrapper around ``module.main`` that injects an
    ``identity_provider=`` matching *flag*'s (``--caller`` or ``--role``)
    argv value whenever a test call omits one -- see this module's own
    docstring for the full rationale. Shared by every verb this fixture
    wraps, rather than one bespoke closure per verb."""
    real_main = module.main

    def _wrapped_main(argv=None, **kwargs):
        if "identity_provider" not in kwargs:
            caller_value = _parse_flag_value_from_argv(
                list(argv) if argv is not None else [], flag
            )
            if caller_value is not None:
                identity = attestation.Identity(caller_value, attestation.SOURCE_CONFIGURED)
                kwargs["identity_provider"] = lambda: identity
            # caller_value is None (an omitted --caller/--role): bind_caller
            # never checks the identity on that path anyway (see its own
            # docstring), so leaving identity_provider unset here and
            # letting main() fall through to the REAL resolve_identity is
            # correct and harmless -- there is nothing to compare against.
        return real_main(argv, **kwargs)

    return _wrapped_main


#: (module, flag) pairs this fixture wraps -- one entry per verb that calls
#: transport.caller_binding.bind_caller (lr-c75c9a). git_host_api and
#: push/review/acquire consume --caller; merge/close_verb/post_merge_verb
#: consume --role.
_WRAPPED_VERB_MODULES: tuple[tuple[object, str], ...] = (
    (git_host_api, "--caller"),
    (push_verb, "--caller"),
    (review_verb, "--caller"),
    (acquire_verb, "--caller"),
    (merge_verb, "--role"),
    (merge_close_verb, "--role"),
    (merge_post_merge_verb, "--role"),
)


@pytest.fixture(autouse=True)
def _default_identity_matches_parsed_caller(request, monkeypatch):
    """Autouse repo-wide; only wraps each verb's `main` (not `._run`
    directly, and NEVER `bind_caller` itself -- see module docstring).

    A test opts out via `@pytest.mark.no_caller_binding_autofill` to get
    the real, unwrapped verb `main` (production default: real
    `transport.attestation.resolve_identity`).
    """
    if "no_caller_binding_autofill" in request.node.keywords:
        return

    for module, flag in _WRAPPED_VERB_MODULES:
        monkeypatch.setattr(
            module, "main", _wrap_main_with_identity_autofill(module, flag=flag)
        )


@pytest.fixture(autouse=True)
def _isolate_real_attestation_chain(monkeypatch, tmp_path):
    """Autouse repo-wide, ALWAYS applied (no opt-out marker -- unlike
    `_default_identity_matches_parsed_caller` above, there is no legitimate
    reason for a test in this suite to want the REAL chain reading this
    process's actual live env/config-root; `test_transport_attestation.py`
    exercises that chain's logic entirely via explicit `env=`/`config_root=`/
    `providers=` injection, never by relying on ambient state).

    See this module's own docstring, "Session/environment isolation for the
    REAL resolve_identity fallthrough path," for the full rationale: every
    wrapped verb's `main()` calls the real, unwrapped
    `transport.attestation.resolve_identity()` with zero explicit arguments
    whenever a test omits `--caller`/`--role` (so `_default_identity_
    matches_parsed_caller` above never gets to inject its own
    `identity_provider=`) -- that call reads live `os.environ` and the
    import-time-bound `attestation.DEFAULT_USER_CONFIG_ROOT` with no
    isolation otherwise, so this suite's result would silently depend on
    which agent/session environment happens to be running it.

    Redirects `attestation.DEFAULT_USER_CONFIG_ROOT` to a per-test `tmp_path`
    subdirectory that is guaranteed to contain no `config.yaml` (a bare,
    unconfigured install's exact posture) -- NOT `transport.provider_config.
    DEFAULT_USER_CONFIG_ROOT`, a separate module attribute `attestation.py`
    holds its own independent copy of via `from ... import
    DEFAULT_USER_CONFIG_ROOT`; patching the `provider_config` copy would not
    reach `resolve_identity`'s own bare-name lookup at all. Scrubs the env
    vars a live crew/harness spawn could set that would otherwise steer
    layers 1/2 of the chain toward a session-specific identity (or a
    fail-closed `AttestationError` when a sidecar path names a file that
    happens not to exist at read time) before that redirect even matters.
    With both isolated, the real chain -- whenever a test's own call path
    does reach it -- deterministically falls through to the built-in
    OS-user layer (`SOURCE_BUILTIN`, always available via `getpass.
    getuser()`), exactly as a bare install would, in every environment this
    suite runs in.
    """
    isolated_config_root = tmp_path / "isolated-loadout-config-root"
    monkeypatch.setattr(attestation, "DEFAULT_USER_CONFIG_ROOT", isolated_config_root)
    for env_var in _ATTESTATION_ENV_VARS_TO_SCRUB:
        monkeypatch.delenv(env_var, raising=False)


def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers",
        "no_caller_binding_autofill: opt this test out of the autouse "
        "identity-matches-caller default wrapper around every "
        "bind_caller-calling verb's main() (lr-82c385, extended lr-c75c9a) "
        "-- use the REAL, unwrapped resolve_identity chain instead.",
    )
