"""test_push_github_mint_parity.py — GitHub-source auth/mint seam-contract
parity vs a reference deployment's own GitHub token-minting client
(lr-ee9044).

An internal deployment's own dispatch platform wants to promote
loadout-push to its PRIMARY push surface, gated on proving loadout-push's
GitHub token-acquisition path is FUNCTIONALLY EQUIVALENT to that
deployment's mint call (a role + repo mint request, owner-scope checked).
The push MECHANISM (git_push_with_token) and the Forgejo auth source were
already proven at parity (lr-035b75, lr-51ed); this file is the missing
GitHub-source half.

The reference contract, read directly from that deployment's own mint
client:
  1. ROLE is a bare identifier ('builder' | 'reviewer' | 'merger' | 'security'
     | 'reader'), validated against a fixed allowed set BEFORE any I/O.
  2. REPO is 'owner/repo'; when an owner_check is configured, the extracted
     owner is compared against it and refused on mismatch -- BEFORE any
     credential read or subprocess exec.
  3. On any mint failure (bad owner, bad role, missing creds, non-zero exit,
     empty stdout) the call FAILS CLOSED: no token is returned, the caller
     gets nothing to push with.

loadout's equivalent seam is NOT a hardcoded gatekeeper client (conformance
rule 6a forbids that) -- it is transport.credential_provider's TokenProvider
protocol, wired to a per-platform command via
transport.provider_config.resolve_platform_provider("github"), then called
as `resolve_token(role, provider, repo="owner/repo")` from push.verb. This
test proves that SEAM, using a synthetic/fake command (never a real
gatekeeper import, per CLAUDE.md rule 6a / conformance rule 6a), asserts the
same role+repo contract shape the reference mint call makes, and asserts
identical fail-closed behavior on mint failure -- WITHOUT ever hardcoding an
agent name or org (workspace rule 11 / repo rule 1).

The owner/org SCOPE check itself is a separate, earlier gate in loadout
(push.namespace_guard.check_namespace_allowed, called before token
resolution in push.verb) -- functionally the same "refuse an out-of-scope
owner before any credential is touched" property the reference deployment's
own owner_check parameter gives its mint call, just factored as its own
module rather than a keyword arg on the mint call. Covered here directly
against the provider seam (a fake command told to enforce owner scoping the
way a real gatekeeper-style mint command would) so the CALL SHAPE parity is
proven at the seam the reference deployment actually uses (role + repo), not
just the surrounding verb-level namespace gate (already covered by
test_push_verb.py's TestNamespaceGuard).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from clagentic_loadout.transport.credential_provider import (
    CommandTokenProvider,
    CredentialProviderError,
    resolve_token,
)

#: Mirrors the reference deployment's own mint_github_token("builder", ...)
#: role literal -- AMoS's own build-role identifier. Not hardcoded anywhere
#: in loadout product code; this test supplies it exactly as a real
#: deployment's --caller/config would.
BUILDER_ROLE = "builder"

#: A synthetic org name, standing in for the reference deployment's own
#: allowed-org check. Purely a test fixture value -- never a real
#: deployment's org, and never imported from anywhere in loadout
#: (conformance rule 6a: synthetic registry, no real gatekeeper, invented
#: names).
_FAKE_ALLOWED_ORG = "synthetic-test-org"


def _write_fake_mint_script(tmp_path: Path, *, behavior: str) -> list[str]:
    """Write a tiny synthetic 'mint command' that stands in for gatekeeper,
    and return the argv (python3 interpreter + script path) a
    CommandTokenProvider execs.

    *behavior* selects the fixture's response shape:
      "success"          -- prints a fake token to stdout, exits 0.
      "owner_mismatch"    -- mirrors the reference deployment's own
                             owner_check refusal: exits
                             non-zero with a diagnostic on stderr when the
                             --repo argument's owner does not equal
                             _FAKE_ALLOWED_ORG, mimicking a real
                             gatekeeper-style mint command's own scope
                             enforcement.
      "mint_failure"      -- always exits non-zero (a broker/creds failure
                             unrelated to owner scoping).

    Never imports or shells out to a real gatekeeper binary -- this is a
    Python script this test authors and controls end to end, matching
    conformance rule 6a (synthetic registry, no real gatekeeper).
    """
    script = tmp_path / f"fake_mint_{behavior}.py"
    if behavior == "success":
        body = (
            "import sys\n"
            "print('fake-github-token-xyz')\n"
            "sys.exit(0)\n"
        )
    elif behavior == "owner_mismatch":
        body = (
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "repo = argv[argv.index('--repo') + 1] if '--repo' in argv else ''\n"
            "owner = repo.split('/', 1)[0] if '/' in repo else ''\n"
            f"if owner != {_FAKE_ALLOWED_ORG!r}:\n"
            "    print('mint FAILED: owner not in allowed scope', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('fake-github-token-xyz')\n"
            "sys.exit(0)\n"
        )
    elif behavior == "mint_failure":
        body = (
            "import sys\n"
            "print('mint FAILED: broker unreachable', file=sys.stderr)\n"
            "sys.exit(1)\n"
        )
    else:
        raise ValueError(f"unknown behavior {behavior!r}")
    script.write_text(body, encoding="utf-8")
    return [sys.executable, str(script), "mint", "--role", "{role}", "--repo", "{repo}"]


class TestGithubMintSeamRoleContract:
    """Same ROLE-identifier contract the reference mint call enforces: the
    role travels to the mint command verbatim, as a bare identifier — never
    silently renamed, defaulted, or dropped."""

    def test_builder_role_reaches_the_configured_mint_command(self, tmp_path):
        argv = _write_fake_mint_script(tmp_path, behavior="success")
        provider = CommandTokenProvider(argv)
        token = resolve_token(BUILDER_ROLE, provider, repo="synthetic-test-org/some-repo")
        assert token == "fake-github-token-xyz"

    def test_role_is_substituted_not_hardcoded(self, tmp_path):
        """The mint command must receive the CALLER's role value, not a
        loadout-hardcoded one -- proven by using a role other than 'builder'
        and confirming a mint script that only accepts that exact role still
        succeeds (i.e. the substitution is live, not a fixed string)."""
        script = tmp_path / "fake_mint_role_echo.py"
        script.write_text(
            "import sys\n"
            "argv = sys.argv[1:]\n"
            "role = argv[argv.index('--role') + 1] if '--role' in argv else ''\n"
            "if role != 'reviewer':\n"
            "    print(f'unexpected role {role!r}', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print('fake-token-for-reviewer')\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        argv = [sys.executable, str(script), "mint", "--role", "{role}", "--repo", "{repo}"]
        provider = CommandTokenProvider(argv)
        token = resolve_token("reviewer", provider, repo="synthetic-test-org/some-repo")
        assert token == "fake-token-for-reviewer"


class TestGithubMintSeamOwnerScopeContract:
    """Same owner/org SCOPE-CHECK semantics as the reference deployment's
    own mint call (owner_check=<allowed org>): an out-of-scope
    owner is refused, in-scope owner succeeds, and the check happens as part
    of the SAME mint call the reference deployment's own owner_check
    parameter gates (not a separately-bypassable step)."""

    def test_in_scope_owner_succeeds(self, tmp_path):
        argv = _write_fake_mint_script(tmp_path, behavior="owner_mismatch")
        provider = CommandTokenProvider(argv)
        token = resolve_token(
            BUILDER_ROLE, provider, repo=f"{_FAKE_ALLOWED_ORG}/some-repo"
        )
        assert token == "fake-github-token-xyz"

    def test_out_of_scope_owner_fails_closed(self, tmp_path):
        argv = _write_fake_mint_script(tmp_path, behavior="owner_mismatch")
        provider = CommandTokenProvider(argv)
        with pytest.raises(CredentialProviderError):
            resolve_token(BUILDER_ROLE, provider, repo="some-other-org/some-repo")


class TestGithubMintSeamFailClosedContract:
    """Same FAIL-CLOSED behavior as the reference deployment's own mint call
    on any mint failure: no token is ever returned, the caller gets an
    exception it must handle (reference: SystemExit via a fail-closed exit
    helper; loadout: CredentialProviderError) — never a silently-empty or
    fabricated token."""

    def test_mint_failure_raises_never_returns_empty_or_fake_token(self, tmp_path):
        argv = _write_fake_mint_script(tmp_path, behavior="mint_failure")
        provider = CommandTokenProvider(argv)
        with pytest.raises(CredentialProviderError):
            resolve_token(BUILDER_ROLE, provider, repo="synthetic-test-org/some-repo")

    def test_mint_command_not_found_fails_closed(self, tmp_path):
        """Mirrors the reference deployment's own fail-closed-on-missing-
        binary behavior -- a misconfigured/absent mint command must never be
        treated as 'no auth needed'."""
        argv = [str(tmp_path / "does-not-exist"), "--role", "{role}", "--repo", "{repo}"]
        provider = CommandTokenProvider(argv)
        with pytest.raises(CredentialProviderError):
            resolve_token(BUILDER_ROLE, provider, repo="synthetic-test-org/some-repo")

    def test_empty_stdout_fails_closed(self, tmp_path):
        """Mirrors the reference deployment's own mint call's empty-stdout
        fail-closed check (a gatekeeper bug producing exit 0 with no token on
        stdout must not be treated as success)."""
        script = tmp_path / "fake_mint_empty.py"
        script.write_text("import sys\nsys.exit(0)\n", encoding="utf-8")
        argv = [sys.executable, str(script), "--role", "{role}", "--repo", "{repo}"]
        provider = CommandTokenProvider(argv)
        with pytest.raises(CredentialProviderError):
            resolve_token(BUILDER_ROLE, provider, repo="synthetic-test-org/some-repo")


class TestGithubProviderSeamNeverHardcodesGatekeeper:
    """Conformance rule 6a: the provider seam under test must be wireable
    with a synthetic mint command and no real gatekeeper import anywhere in
    the call path -- this test asserts that by construction (the argv passed
    to CommandTokenProvider is entirely test-authored, see
    _write_fake_mint_script), and by checking the credential_provider module
    itself imports no gatekeeper client."""

    def test_credential_provider_module_imports_no_gatekeeper_client(self):
        import clagentic_loadout.transport.credential_provider as cp_module

        source = Path(cp_module.__file__).read_text(encoding="utf-8")
        # The module docstring may NAME gatekeeper as a documentation example
        # of a command a deployment might configure -- that is prose, not an
        # import. What conformance rule 6a actually forbids is a real import
        # statement pulling a gatekeeper client into this seam.
        assert "import gatekeeper" not in source
        assert "from gatekeeper" not in source
        assert "clagentic_gatekeeper" not in source.replace(
            "[clagentic: gatekeeper](https://github.com/clagentic/clagentic-gatekeeper)", ""
        )
