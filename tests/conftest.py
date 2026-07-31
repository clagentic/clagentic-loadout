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
`_default_identity_matches_parsed_caller` wraps `git_host_api.main` so that,
for any test call that omits `identity_provider=`, the ATTESTED IDENTITY
supplied to the real, unpatched `bind_caller` is derived from that SAME
invocation's own `--caller` argv value -- i.e. "this test's identity always
matches its own caller," which is genuinely true of every one of those
pre-existing tests' intent (none of them are testing an
attested-identity MISMATCH; that is what
`tests/test_git_host_api_caller_attested_invoker_binding.py` explicitly and
exclusively covers, always by injecting its own `identity_provider=`, which
this wrapper never overrides -- an explicit keyword argument a test
supplies always wins). `bind_caller`'s real equality check therefore runs,
and passes, on every one of those ~100+ pre-existing invocations -- the
happy path is genuinely covered, not stubbed out.

**Opt-out marker, for a test that wants git_host_api.main's UNWRAPPED
default behavior** (i.e. the real production `transport.attestation.
resolve_identity`, reading this process's actual env/config/OS user):
`@pytest.mark.no_caller_binding_autofill`. No test in this suite currently
needs this — real attestation resolution is exactly what
test_transport_attestation.py exercises directly, and
test_git_host_api_caller_attested_invoker_binding.py always injects its own
`identity_provider=` — but the marker exists so a future test that
specifically wants the real chain (e.g. a smoke test asserting the
installed entry point resolves a real OS user with zero mocking) has an
explicit, discoverable way to ask for it, rather than reaching for a
second ad-hoc patching mechanism.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.transport import attestation, git_host_api


def _parse_caller_from_argv(argv: list[str]) -> str | None:
    """Extract the value following a literal ``--caller`` token in *argv*,
    exactly mirroring how ``argparse`` (via ``_build_arg_parser``) would
    bind it -- ``None`` when ``--caller`` is absent (an omitted --caller is
    never checked by ``bind_caller`` regardless, per its own docstring, so
    returning ``None`` here is always safe)."""
    for index, token in enumerate(argv):
        if token == "--caller" and index + 1 < len(argv):
            return argv[index + 1]
    return None


@pytest.fixture(autouse=True)
def _default_identity_matches_parsed_caller(request, monkeypatch):
    """Autouse repo-wide; only wraps `git_host_api.main` (not `._run`
    directly, and NEVER `bind_caller` itself -- see module docstring).

    A test opts out via `@pytest.mark.no_caller_binding_autofill` to get
    the real, unwrapped `git_host_api.main` (production default: real
    `transport.attestation.resolve_identity`).
    """
    if "no_caller_binding_autofill" in request.node.keywords:
        return

    real_main = git_host_api.main

    def _wrapped_main(argv=None, **kwargs):
        if "identity_provider" not in kwargs:
            caller_value = _parse_caller_from_argv(list(argv) if argv is not None else [])
            if caller_value is not None:
                identity = attestation.Identity(caller_value, attestation.SOURCE_CONFIGURED)
                kwargs["identity_provider"] = lambda: identity
            # caller_value is None (an omitted --caller): bind_caller never
            # checks the identity on that path anyway (see its own
            # docstring), so leaving identity_provider unset here and
            # letting main() fall through to the REAL resolve_identity is
            # correct and harmless -- there is nothing to compare against.
        return real_main(argv, **kwargs)

    monkeypatch.setattr(git_host_api, "main", _wrapped_main)


def pytest_configure(config: "pytest.Config") -> None:
    config.addinivalue_line(
        "markers",
        "no_caller_binding_autofill: opt this test out of the autouse "
        "identity-matches-caller default wrapper around git_host_api.main "
        "(lr-82c385) -- use the REAL, unwrapped resolve_identity chain "
        "instead.",
    )
