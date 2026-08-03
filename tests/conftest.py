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


def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers",
        "no_caller_binding_autofill: opt this test out of the autouse "
        "identity-matches-caller default wrapper around every "
        "bind_caller-calling verb's main() (lr-82c385, extended lr-c75c9a) "
        "-- use the REAL, unwrapped resolve_identity chain instead.",
    )
