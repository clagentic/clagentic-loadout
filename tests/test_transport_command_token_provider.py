"""test_transport_command_token_provider.py — tests for
clagentic_loadout.transport.credential_provider.CommandTokenProvider
(lr-af6e).

Coverage:
  - happy path: a synthetic python -c command that echoes a token, with and
    without the trailing newline, and with the {role} template placeholder
    vs. the append-role-as-final-arg shape.
  - shell=False: a shell metacharacter in the role never escapes into
    command injection (this is asserted structurally -- argv is a list, no
    shell is invoked -- via a role value containing shell metacharacters
    that must be treated as a literal, inert argv element).
  - failure modes: nonzero exit, empty stdout, oversize stdout, command not
    found.
  - resolved-values error reporting: the raised error names the configured
    command shape and role, and (this is the actual security property under
    test) never includes the token value anywhere in the exception message.
  - empty configured argv is rejected at construction time.
  - {repo} template placeholder (lr-ea28): substitution, no-{repo}
    byte-parity with the pre-lr-ea28 argv shapes, fail-closed missing-repo.
  - argv-level option injection (lr-ea28 security fix, PR #26 review
    comment 12757): a role or repo value is validated against a bare-token
    grammar BEFORE substitution -- a leading '-' is not a shell
    metacharacter (shell=False already covers those) but IS a normal argv
    byte the exec'd command's own argument parser could read as a flag.
    Covers leading-dash/leading-dot/whitespace rejection on both role and
    each owner/repo segment, valid dotted/hyphenated names passing, and
    proof the rejected command never actually execs (subprocess.run is
    never reached).
"""

from __future__ import annotations

import os
import sys

import pytest

from clagentic_loadout.transport.credential_provider import (
    COMMAND_PROVIDER_MAX_OUTPUT_BYTES,
    COMMAND_REPO_PLACEHOLDER,
    COMMAND_ROLE_PLACEHOLDER,
    CommandTokenProvider,
    CredentialProviderError,
)

_PY = sys.executable


def _echo_argv(token: str, *, trailing_newline: bool = True) -> list[str]:
    """Build a synthetic 'python -c' command-provider argv that prints
    *token* to stdout (optionally without a trailing newline)."""
    end = "\\n" if trailing_newline else ""
    code = f"import sys; sys.stdout.write({token!r} + '{end}')"
    return [_PY, "-c", code]


class TestCommandTokenProviderHappyPath:
    def test_appends_role_as_final_arg_by_default(self):
        # No {role} placeholder in argv -> role is appended.
        code = "import sys; sys.stdout.write('tok-for-' + sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        assert provider.resolve_token("builder") == "tok-for-builder"

    def test_role_placeholder_template_substitution(self):
        code = "import sys; sys.stdout.write('tok-for-' + sys.argv[1] + '\\n')"
        provider = CommandTokenProvider(
            [_PY, "-c", code, COMMAND_ROLE_PLACEHOLDER]
        )
        assert provider.resolve_token("merger") == "tok-for-merger"

    def test_trailing_newline_is_stripped(self):
        provider = CommandTokenProvider(_echo_argv("tok-abc123", trailing_newline=True))
        assert provider.resolve_token("any-role") == "tok-abc123"

    def test_no_trailing_newline_still_works(self):
        provider = CommandTokenProvider(_echo_argv("tok-no-newline", trailing_newline=False))
        assert provider.resolve_token("any-role") == "tok-no-newline"

    def test_implements_token_provider_protocol(self):
        from clagentic_loadout.transport.credential_provider import TokenProvider

        provider = CommandTokenProvider(_echo_argv("tok"))
        assert isinstance(provider, TokenProvider)


class TestCommandTokenProviderShellSafety:
    def test_role_with_shell_metacharacters_is_rejected_before_exec(self):
        """lr-ea28 security fix (PR #26 review comment 12757): a role value
        containing shell metacharacters is now REJECTED by the safe-token
        grammar (_SAFE_ROLE_RE) before _build_argv ever substitutes it --
        superseding the prior "shell=False makes any value safe to pass
        through" assumption. shell=False alone protects against SHELL
        metacharacter interpretation (there is no shell to interpret them),
        but it does not protect the exec'd command's OWN argument parser
        from a value shaped like a flag or containing characters the
        deployment's minting CLI might not expect; the safe-token grammar
        closes that gap by rejecting anything outside a bare
        alnum/hyphen/underscore token, which structurally excludes every
        shell metacharacter too."""
        role_value = "role; touch /tmp/should-not-exist; echo pwned"
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="invalid characters"):
            provider.resolve_token(role_value)


class TestCommandTokenProviderFailureModes:
    def test_nonzero_exit_raises_with_resolved_command_and_role(self):
        code = "import sys; sys.exit(3)"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError) as exc_info:
            provider.resolve_token("some-role")
        message = str(exc_info.value)
        assert "exited 3" in message
        assert "some-role" in message
        assert _PY in message

    def test_stderr_surfaced_as_diagnostic_on_nonzero_exit(self):
        code = "import sys; sys.stderr.write('minting backend unreachable'); sys.exit(1)"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="minting backend unreachable"):
            provider.resolve_token("some-role")

    def test_empty_stdout_raises(self):
        code = "import sys; sys.exit(0)"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="empty stdout"):
            provider.resolve_token("some-role")

    def test_whitespace_only_stdout_raises(self):
        code = "import sys; sys.stdout.write('\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="empty stdout"):
            provider.resolve_token("some-role")

    def test_oversize_stdout_raises(self):
        oversize = COMMAND_PROVIDER_MAX_OUTPUT_BYTES + 1
        code = f"import sys; sys.stdout.write('a' * {oversize})"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="exceeding the"):
            provider.resolve_token("some-role")

    def test_command_not_found_raises(self):
        provider = CommandTokenProvider(["/nonexistent/path/to/nowhere-lr-af6e"])
        with pytest.raises(CredentialProviderError, match="not found"):
            provider.resolve_token("some-role")

    def test_empty_argv_rejected_at_construction(self):
        with pytest.raises(CredentialProviderError, match="empty"):
            CommandTokenProvider([])

    def test_timeout_raises(self):
        code = "import time; time.sleep(5)"
        provider = CommandTokenProvider([_PY, "-c", code], timeout=0.1)
        with pytest.raises(CredentialProviderError, match="timed out"):
            provider.resolve_token("some-role")


class TestCommandTokenProviderNeverLeaksToken:
    def test_token_never_appears_in_nonzero_exit_error_message(self):
        """The security property under test: even when a command exits
        nonzero AFTER printing something to stdout, the raised error must
        never quote that STDOUT VALUE (which could coincidentally be a
        partial/garbled token) -- only the configured command shape and
        role are named. The secret-looking value here is produced entirely
        at runtime (read from an env var the test sets) rather than baked
        into the command's source code, so it cannot leak in via the argv
        repr either -- isolating the actual property under test."""
        secret_looking_value = "tok-super-secret-abc123"
        code = (
            "import os, sys; "
            "sys.stdout.write(os.environ['LR_AF6E_TEST_SECRET']); "
            "sys.exit(1)"
        )
        provider = CommandTokenProvider([_PY, "-c", code])

        os.environ["LR_AF6E_TEST_SECRET"] = secret_looking_value
        try:
            with pytest.raises(CredentialProviderError) as exc_info:
                provider.resolve_token("some-role")
        finally:
            del os.environ["LR_AF6E_TEST_SECRET"]
        assert secret_looking_value not in str(exc_info.value)

    def test_resolved_token_via_resolve_token_never_in_provider_repr_message(self):
        from clagentic_loadout.transport.credential_provider import (
            resolve_token as module_resolve_token,
        )

        token_value = "tok-should-not-leak-9f8e7d"
        provider = CommandTokenProvider(_echo_argv(token_value))
        result = module_resolve_token("some-role", provider)
        assert result == token_value
        # A successful resolution raises nothing -- nothing to assert about
        # an exception message here; this test documents that the happy
        # path returns the token as data, never logs/prints it (no
        # print()/logging calls exist anywhere in CommandTokenProvider or
        # resolve_token -- see their source for the absence of any such
        # call).


class TestCommandTokenProviderRepoContext:
    """lr-ea28: {repo} template placeholder in a configured command's argv.

    Coverage: {repo} substitution (gatekeeper-mint-shaped argv); a command
    WITHOUT {repo} behaves byte-identically to before this feature (proven
    by reusing the exact pre-lr-ea28 happy-path assertions with a repo
    argument now supplied and ignored); fail-closed when {repo} is
    configured but the call site supplies no repo; substitution never
    touches a shell string (argv-token-only, same shell=False guarantee as
    role substitution).
    """

    def test_repo_placeholder_substituted_gatekeeper_mint_shape(self):
        # Mirrors the docstring's reference shape: mint --role <role> --repo
        # <owner>/<repo>. The synthetic command reports back exactly the
        # role and repo argv elements it received.
        code = (
            "import sys; "
            "sys.stdout.write(sys.argv[2] + '|' + sys.argv[4] + '\\n')"
        )
        provider = CommandTokenProvider(
            [_PY, "-c", code, "--role", COMMAND_ROLE_PLACEHOLDER, "--repo", COMMAND_REPO_PLACEHOLDER]
        )
        result = provider.resolve_token("merger", repo="some-owner/some-repo")
        assert result == "merger|some-owner/some-repo"

    def test_no_repo_placeholder_byte_identical_to_before_feature(self):
        """A configured command with no {repo} element must behave EXACTLY
        as it did before this feature existed -- proven by reusing the
        pre-lr-ea28 role-append and role-placeholder shapes verbatim, now
        with a repo argument supplied at the call site (and ignored)."""
        code_append = "import sys; sys.stdout.write('tok-for-' + sys.argv[1] + '\\n')"
        provider_append = CommandTokenProvider([_PY, "-c", code_append])
        assert provider_append.resolve_token("builder", repo="some-owner/some-repo") == "tok-for-builder"
        assert provider_append.resolve_token("builder") == "tok-for-builder"

        code_role = "import sys; sys.stdout.write('tok-for-' + sys.argv[1] + '\\n')"
        provider_role = CommandTokenProvider([_PY, "-c", code_role, COMMAND_ROLE_PLACEHOLDER])
        assert provider_role.resolve_token("merger", repo="some-owner/some-repo") == "tok-for-merger"
        assert provider_role.resolve_token("merger") == "tok-for-merger"

    def test_repo_placeholder_with_no_repo_supplied_fails_closed(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider(
            [_PY, "-c", code, "--repo", COMMAND_REPO_PLACEHOLDER]
        )
        with pytest.raises(CredentialProviderError) as exc_info:
            provider.resolve_token("some-role")
        message = str(exc_info.value)
        assert COMMAND_REPO_PLACEHOLDER in message
        assert "some-role" in message
        assert "no repo context" in message

    def test_repo_placeholder_with_explicit_none_repo_fails_closed(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider(
            [_PY, "-c", code, "--repo", COMMAND_REPO_PLACEHOLDER]
        )
        with pytest.raises(CredentialProviderError, match="no repo context"):
            provider.resolve_token("some-role", repo=None)

    def test_repo_with_shell_metacharacters_is_rejected_before_exec(self):
        """Mirrors the role-side fix: a repo value containing shell
        metacharacters is rejected by _SAFE_REPO_RE before _build_argv ever
        substitutes it -- the safe owner/repo grammar structurally excludes
        every shell metacharacter, so there is nothing left for shell=False
        to need to protect against for a value that passes validation."""
        repo_value = "owner/repo; touch /tmp/should-not-exist; echo pwned"
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo=repo_value)

    def test_repo_and_role_placeholders_both_substituted(self):
        code = "import sys; sys.stdout.write(sys.argv[1] + '|' + sys.argv[2] + '\\n')"
        provider = CommandTokenProvider(
            [_PY, "-c", code, COMMAND_ROLE_PLACEHOLDER, COMMAND_REPO_PLACEHOLDER]
        )
        result = provider.resolve_token("merger", repo="some-owner/some-repo")
        assert result == "merger|some-owner/some-repo"


class TestCommandTokenProviderArgvOptionInjection:
    """lr-ea28 security fix (PR #26 review comment 12757): {repo} (and
    {role}) substitutes into an already-split argv element -- shell=False
    already rules out shell metacharacter injection, but a value starting
    with '-' is not a shell metacharacter; it is a normal argv byte a
    getopt/argparse-style CLI (the deployment's own minting command) parses
    as the START OF A FLAG. Validated with a bare-token grammar
    (_SAFE_ROLE_RE / _SAFE_REPO_RE) BEFORE _build_argv ever substitutes,
    mirroring git_host_api._SAFE_CALLER_RE's treatment -- but enforced at the
    provider (the seam), not only at git_host_api's own call site, since
    push/review/merge's --caller/--role do not pre-validate the same way.

    A command that should never run on a rejected value asserts that by
    writing a sentinel to a temp marker file the test asserts does NOT
    exist afterward (never runs at all) rather than merely asserting on the
    return value, since CredentialProviderError itself is sufficient proof
    subprocess.run was never reached.
    """

    # -- role: leading-dash / leading-dot / whitespace rejected --------

    def test_leading_dash_role_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="invalid characters"):
            provider.resolve_token("--evil-flag")

    def test_whitespace_in_role_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="invalid characters"):
            provider.resolve_token("role with spaces")

    def test_valid_hyphenated_role_passes(self):
        code = "import sys; sys.stdout.write('tok-for-' + sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        assert provider.resolve_token("some-hyphenated_role123") == "tok-for-some-hyphenated_role123"

    # -- repo: leading-dash owner, leading-dash repo, whitespace --------

    def test_leading_dash_owner_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="--evil-flag/some-repo")

    def test_leading_dash_repo_segment_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="some-owner/--evil-flag")

    def test_leading_dot_owner_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="../some-repo")

    def test_whitespace_in_repo_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="some owner/some repo")

    def test_no_slash_repo_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="no-slash-here")

    def test_valid_dotted_hyphenated_repo_passes(self):
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        result = provider.resolve_token("some-role", repo="some-owner.co/some_repo.name-2")
        assert result == "some-owner.co/some_repo.name-2"

    def test_rejected_role_never_reaches_subprocess(self, tmp_path):
        """The rejection happens in _build_argv, BEFORE subprocess.run is
        ever called -- proven by a marker-file command that would create the
        file if (and only if) it actually ran."""
        marker = tmp_path / "should-not-be-created"
        code = f"import pathlib; pathlib.Path({str(marker)!r}).touch()"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError):
            provider.resolve_token("--evil-flag")
        assert not marker.exists()

    def test_rejected_repo_never_reaches_subprocess(self, tmp_path):
        marker = tmp_path / "should-not-be-created"
        code = f"import pathlib; pathlib.Path({str(marker)!r}).touch()"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError):
            provider.resolve_token("some-role", repo="--evil-flag/some-repo")
        assert not marker.exists()


class TestCommandTokenProviderRepoLeadingDotGithubProfile:
    """lr-fdc6ec: _SAFE_REPO_RE narrowed to admit a leading '.' per segment
    (GitHub mandates a repo literally named '.github' for the org-profile
    README -- github.com/<owner>/.github -- an unavoidable, legitimate repo
    name the prior grammar rejected). The two real security properties are
    preserved unconditionally and re-verified here alongside the new
    acceptance case:
      - a segment may never START WITH '-' (the actual argv-injection
        vector -- unaffected by this change, still rejected regardless of
        segment position or a leading '.').
      - a segment may never BE '.' or '..' AS A WHOLE SEGMENT (the actual
        path-traversal vector) -- including when embedded as an owner or
        repo segment, and including via a leading '../' where the '..'
        segment is followed by more path-shaped content (this is the exact
        case an earlier draft's unanchored '$' lookahead missed: '../x' was
        incorrectly accepted because the lookahead's '$' matched end of the
        overall source string, not end of the current segment).
    """

    # -- accept: leading '.' now admitted -------------------------------

    def test_dot_github_repo_segment_accepted(self):
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        result = provider.resolve_token("some-role", repo="clagentic/.github")
        assert result == "clagentic/.github"

    def test_plain_owner_repo_still_accepted(self):
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        result = provider.resolve_token("some-role", repo="owner/repo")
        assert result == "owner/repo"

    def test_dotted_hyphenated_underscored_repo_still_accepted(self):
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        result = provider.resolve_token("some-role", repo="o-w/r.e_p-o")
        assert result == "o-w/r.e_p-o"

    # -- reject: leading hyphen, either segment -------------------------

    def test_leading_hyphen_owner_segment_still_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="-flag/x")

    def test_leading_hyphen_repo_segment_still_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="o/-r")

    # -- reject: '.' / '..' as a WHOLE segment, either position ---------

    def test_repo_segment_dotdot_whole_segment_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="o/..")

    def test_owner_segment_dotdot_leading_with_trailing_path_rejected(self):
        """The bug the provenance comment documents: an unanchored '$' in
        the negative lookahead let '../x' slip through, because the
        lookahead's end-of-segment check matched end of the WHOLE source
        string, not end of the '..' segment itself. '../x' has zero '/'
        separators inside what should be a two-segment owner/repo grammar
        in the sense that its first segment IS '..' -- must still reject."""
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="../x")

    def test_repo_segment_dot_whole_segment_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="o/.")

    def test_owner_segment_dot_whole_segment_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="./x")

    # -- reject: structural malformation ---------------------------------

    def test_zero_slash_repo_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="no-slash-here")

    def test_multi_slash_repo_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="a/b/c")

    def test_empty_segment_repo_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="/b")


class TestSafeRoleAndRepoRegexTrailingNewlineAnchoring:
    """lr-3e3318 (BOBBIE, PR #133 audit): _SAFE_ROLE_RE and _SAFE_REPO_RE
    previously anchored with a bare '$', which in Python (without
    re.MULTILINE) matches at end-of-string OR just before a trailing
    newline -- so 'owner/repo\\n' passed validation. Anchored with \\A...\\Z
    instead; these tests lock the REJECT behavior for a trailing-newline
    value on both validators, in both the leading and embedded positions."""

    def test_repo_with_trailing_newline_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="owner/repo\n")

    def test_repo_with_leading_newline_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="\nowner/repo")

    def test_repo_with_embedded_newline_between_segments_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        with pytest.raises(CredentialProviderError, match="owner/repo"):
            provider.resolve_token("some-role", repo="owner\n/repo")

    def test_role_with_trailing_newline_rejected(self):
        code = "import sys; sys.stdout.write('should-not-run\\n')"
        provider = CommandTokenProvider([_PY, "-c", code])
        with pytest.raises(CredentialProviderError, match="invalid characters"):
            provider.resolve_token("some-role\n")

    def test_valid_repo_full_accept_matrix_still_holds(self):
        """CONSTRAINT (lr-3e3318): the full accept matrix locked by PR
        #133's tests must continue to hold unchanged after re-anchoring."""
        code = "import sys; sys.stdout.write(sys.argv[1] + '\\n')"
        provider = CommandTokenProvider([_PY, "-c", code, COMMAND_REPO_PLACEHOLDER])
        for repo in ("clagentic/.github", "owner/repo", "o-w/r.e_p-o"):
            assert provider.resolve_token("some-role", repo=repo) == repo
