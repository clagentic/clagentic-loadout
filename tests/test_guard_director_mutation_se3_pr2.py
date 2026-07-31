"""test_guard_director_mutation_se3_pr2.py — `check_lead_mutation` unit
tests (lr-1cc4df, sub-epic lr-19ae42 sub-slice SE3 PR2, epic lr-5a8d Wave C).

Conformance (CLAUDE.md rule 6a): synthetic verb names, paths, roles, and
identity labels only — no real agent names, no LORE present, no real
machine identifiers.

Tests move with their subjects (CLAUDE.md rule 6) — this file is scoped to
the SE3 PR2 addition (`check_lead_mutation`, `LeadMutationConfig`,
`ActingSubagentResolver`), the mutation-verb-family deny dispatch and the
harness-attestation seam. PR1's `check_director_identity_discipline`
coverage stays in test_guard_director_mutation.py.
"""

from __future__ import annotations

import re

from clagentic_loadout.guard.director_mutation import (
    ActingSubagentResolver,
    LeadMutationConfig,
    check_lead_mutation,
)

_IDENTITY = "lead-x"


def _check(command: str, **config_overrides):
    config = LeadMutationConfig(**config_overrides) if config_overrides else None
    return check_lead_mutation(command, identity_label=_IDENTITY, config=config)


# ---------------------------------------------------------------------------
# Default-allow baseline: an ordinary read-only/non-mutating command is
# admitted with no config at all.
# ---------------------------------------------------------------------------


class TestDefaultAllowBaseline:
    def test_plain_read_command_admitted(self):
        ok, reason = _check("git status")
        assert ok is True, reason

    def test_lore_command_admitted(self):
        ok, reason = _check("lore task show lr-1")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# git-write mutation family
# ---------------------------------------------------------------------------


class TestGitWriteMutationFamily:
    def test_git_push_denied(self):
        ok, reason = _check("git push origin main")
        assert ok is False
        assert "git write op" in reason
        assert _IDENTITY in reason

    def test_git_commit_denied(self):
        ok, reason = _check("git commit -m 'wip'")
        assert ok is False

    def test_git_two_word_stash_drop_denied(self):
        ok, reason = _check("git stash drop")
        assert ok is False

    def test_git_branch_dash_d_denied(self):
        ok, reason = _check("git branch -d feature-x")
        assert ok is False

    def test_git_read_only_log_admitted(self):
        ok, reason = _check("git log --oneline")
        assert ok is True, reason

    def test_git_diff_admitted(self):
        ok, reason = _check("git diff HEAD~1")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# file mutation family
# ---------------------------------------------------------------------------


class TestFileMutationFamily:
    def test_rm_denied(self):
        ok, reason = _check("rm -rf /workspace/some-dir")
        assert ok is False
        assert "file mutation" in reason

    def test_sed_dash_i_denied(self):
        ok, reason = _check("sed -i 's/a/b/' /workspace/f.txt")
        assert ok is False

    def test_python_open_write_mode_denied(self):
        ok, reason = _check("python3 -c \"open('/workspace/f.txt','w')\"")
        assert ok is False
        assert "open(" in reason or "file mutation" in reason

    def test_cat_read_only_admitted(self):
        ok, reason = _check("cat /workspace/f.txt")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# package/docker mutation family
# ---------------------------------------------------------------------------


class TestPackageMutationFamily:
    def test_pip_install_denied(self):
        # Reference-faithful behavior: "install" is ALSO a member of the
        # file_mutation family's verb set, and file_mutation is dispatched
        # BEFORE package_mutation in _MUTATION_VERB_FAMILIES (mirrors the
        # reference's own family ordering exactly) -- "pip install" is
        # denied via the file_mutation family's bare "install" token match,
        # not the package family's "pip" match. Still a hard deny either
        # way; only the specific reason text differs.
        ok, reason = _check("pip install requests")
        assert ok is False
        assert "file mutation" in reason

    def test_pip_uninstall_denied_via_package_family(self):
        # "uninstall" is not in _FILE_MUTATION_RE's verb set, so this one
        # reaches the package_mutation family's bare "pip" match instead.
        ok, reason = _check("pip uninstall requests")
        assert ok is False
        assert "package" in reason

    def test_docker_run_denied(self):
        ok, reason = _check("docker run -it ubuntu bash")
        assert ok is False

    def test_docker_ps_admitted(self):
        ok, reason = _check("docker ps")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# systemctl mutation family
# ---------------------------------------------------------------------------


class TestSystemctlMutationFamily:
    def test_systemctl_restart_denied(self):
        ok, reason = _check("systemctl restart some-service")
        assert ok is False
        assert "systemctl" in reason

    def test_systemctl_status_admitted(self):
        ok, reason = _check("systemctl status some-service")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# caller-configured push-verb named anti-pattern
# ---------------------------------------------------------------------------


class TestPushVerbNamedAntiPattern:
    def test_configured_push_script_denied(self):
        ok, reason = _check(
            "python3 /opt/example-deploy/landing-tool.py",
            push_verb_pattern=re.compile(
                r"(?:^|\s)python3\s+/opt/example-deploy/(?:landing-tool\.py|post/\S+)"
            ),
        )
        assert ok is False
        assert "push-transport" in reason

    def test_no_push_verb_pattern_configured_admits(self):
        # No config supplied at all -- this named anti-pattern is entirely
        # caller-optional (module docstring: "Optional: a caller with no
        # such standalone push script may omit this").
        ok, reason = _check("python3 /opt/example-deploy/landing-tool.py")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# forge-PR-mutation verb-pattern deny (no attestation seam configured)
# ---------------------------------------------------------------------------


class TestForgePrMutationVerbPatternDeny:
    _FORGE_CURL_WRITE_RE = re.compile(
        r"(?:^|[\s/])forgejo-curl\b.*?(?:^|\s)(POST|PATCH|DELETE)(?:\s|$)"
    )

    def test_bare_forgejo_curl_post_denied(self):
        ok, reason = _check(
            "forgejo-curl POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
        )
        assert ok is False
        assert "forge-PR-mutation" in reason

    def test_forgejo_curl_get_admitted(self):
        ok, reason = _check(
            "forgejo-curl GET https://forge.example/api/v1/repos/o/r/pulls/1",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
        )
        assert ok is True, reason

    def test_gh_pr_merge_denied(self):
        ok, reason = _check("gh pr merge 12")
        assert ok is False
        assert "gh pr mutation" in reason

    def test_gh_pr_view_admitted(self):
        ok, reason = _check("gh pr view 12")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# raw curl/wget PR mutation deny (caller-configured forge host)
# ---------------------------------------------------------------------------


class TestRawCurlPrMutationDeny:
    def test_raw_curl_patch_to_pr_path_on_configured_host_denied(self):
        ok, reason = _check(
            "curl -X PATCH https://forge.example/api/v1/repos/o/r/pulls/5",
            forge_host_patterns=("forge.example",),
        )
        assert ok is False
        assert "raw curl/wget" in reason

    def test_raw_curl_get_admitted(self):
        ok, reason = _check(
            "curl https://forge.example/api/v1/repos/o/r/pulls/5",
            forge_host_patterns=("forge.example",),
        )
        assert ok is True, reason

    def test_unconfigured_host_never_matches(self):
        # No forge_host_patterns configured at all -- this deny is entirely
        # caller-optional, matching a caller integrating against no forge
        # or a forge this module has no assumption about.
        ok, reason = _check(
            "curl -X PATCH https://forge.example/api/v1/repos/o/r/pulls/5"
        )
        assert ok is True, reason

    def test_mutation_method_on_a_different_host_not_denied(self):
        ok, reason = _check(
            "curl -X PATCH https://not-configured.example/api/v1/repos/o/r/pulls/5",
            forge_host_patterns=("forge.example",),
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# shell write redirection deny + relay-body carve-out
# ---------------------------------------------------------------------------


class TestShellWriteRedirectionDeny:
    def test_write_redirect_denied(self):
        ok, reason = _check("echo hello > /workspace/f.txt")
        assert ok is False
        assert "shell write redirection" in reason

    def test_append_redirect_denied(self):
        ok, reason = _check("echo hello >> /workspace/f.txt")
        assert ok is False

    def test_no_redirect_admitted(self):
        ok, reason = _check("echo hello")
        assert ok is True, reason

    def test_configured_relay_body_prefix_carved_out(self):
        ok, reason = _check(
            "cat > /run/spawn-homes/abc/relay-body.json << 'JSONEOF'",
            relay_body_redirect_prefix="/run/spawn-homes/abc/relay-body.json",
        )
        assert ok is True, reason

    def test_unconfigured_relay_body_prefix_denies_every_redirect(self):
        # No relay_body_redirect_prefix configured -- the module docstring
        # requires this default to be a strict SUBSET of the reference's
        # admitted set (every write-redirect denies), never a widening
        # default.
        ok, reason = _check(
            "cat > /run/spawn-homes/abc/relay-body.json << 'JSONEOF'"
        )
        assert ok is False

    def test_heredoc_to_file_denied(self):
        # Reference-faithful behavior: the generic shell-write-redirection
        # regex (checked FIRST, matching the reference's own ordering
        # exactly) already matches the trailing "> /workspace/f.txt" in
        # this shape, so the dedicated heredoc-to-file branch below it is
        # unreached for this input -- the reference has this identical
        # ordering (its heredoc-to-file check is reachable only for a
        # <<HEREDOC form the generic redirect regex's own negative
        # lookbehind excludes, e.g. immediately preceded by "|" or "<").
        # Still a hard deny either way; only the specific reason differs.
        ok, reason = _check("cat <<HEREDOC\nbody\nHEREDOC > /workspace/f.txt")
        assert ok is False
        assert "shell write redirection" in reason


# ---------------------------------------------------------------------------
# credential-file write deny
# ---------------------------------------------------------------------------


class TestCredentialFileWriteDeny:
    _NETRC_RE = re.compile(r"(?:~|/home/[^/\s]+)/\.netrc")

    def test_write_to_configured_credential_path_denied(self):
        # Reference-faithful behavior (see TestShellWriteRedirectionDeny.
        # test_heredoc_to_file_denied for the identical ordering rationale):
        # the generic shell-write-redirection check runs BEFORE the
        # credential-file-specific check in both the reference and this
        # port, so a plain ">>" write already denies there first for this
        # shape. Still a hard deny either way; only the specific reason
        # text differs -- see
        # test_credential_write_reachable_when_redirect_is_carved_out below
        # for a case where the credential-specific branch IS reached.
        ok, reason = _check(
            "echo x >> ~/.netrc",
            credential_file_patterns=(self._NETRC_RE,),
        )
        assert ok is False
        assert "shell write redirection" in reason

    def test_credential_write_reachable_when_generic_redirect_excludes_it(self):
        # The generic shell-write-redirection regex's own negative lookbehind
        # `(?<![|<])` excludes a ">" immediately preceded by "<" -- reference-
        # faithful (see _RELAY_BODY_REDIRECT_RE.sub site: the pipe/redirect
        # exclusion is deliberate so a genuine input-redirect like
        # `cmd < a >b` isn't double-flagged). A "<>" glued form + a
        # credential-shaped path lets this test reach the credential-file
        # branch without first tripping the generic redirect deny OR any of
        # the mutation-verb families (no rm/mv/tee/sed -i/etc. token
        # present).
        ok, reason = _check(
            "true <>~/.netrc",
            credential_file_patterns=(self._NETRC_RE,),
        )
        assert ok is False
        assert "credentials/settings file" in reason

    def test_read_of_configured_credential_path_not_denied_by_this_check(self):
        # This deny only fires when a write-shaped operator/verb is ALSO
        # present (reference: paired with a >>/tee/sed -i/perl -i check) --
        # a bare read is not itself denied by THIS function (a caller's own
        # credential-path guard, e.g. guard.credential_paths, is the read
        # boundary).
        ok, reason = _check(
            "cat ~/.netrc",
            credential_file_patterns=(self._NETRC_RE,),
        )
        assert ok is True, reason

    def test_no_credential_file_patterns_configured_never_denies(self):
        ok, reason = _check("echo x >> ~/.netrc")
        # No credential_file_patterns configured -- falls through to the
        # generic shell-write-redirection deny instead (still denied, but
        # for the generic reason, not the credential-specific one).
        assert ok is False
        assert "shell write redirection" in reason


# ---------------------------------------------------------------------------
# review-runner carve-out
# ---------------------------------------------------------------------------


class TestReviewRunnerCarveOut:
    _REVIEW_RUNNER_RE = re.compile(r"^/workspace/scripts/review-runner(?:\s|$)")

    def test_configured_review_runner_carved_out_even_with_redirect_lookalike(self):
        # The carve-out must be evaluated BEFORE the shell-write-redirect
        # deny -- a review body argument containing ">" (markdown
        # blockquote) must not false-trigger.
        ok, reason = _check(
            "/workspace/scripts/review-runner post 'quote: > like this'",
            review_runner_patterns=(self._REVIEW_RUNNER_RE,),
        )
        assert ok is True, reason

    def test_unconfigured_review_runner_pattern_not_carved_out(self):
        ok, reason = _check("/workspace/scripts/review-runner post 'text'")
        assert ok is True, reason  # no redirect, no mutation verb -- admitted anyway


# ---------------------------------------------------------------------------
# ANSI-C-quote hard-deny (mandatory acceptance criterion, SE1/SE2/SE3-binding)
# ---------------------------------------------------------------------------


class TestAnsiCQuoteHardDeny:
    def test_resolvable_ansi_c_git_push_force_denied_by_family_match(self):
        # $'push' decodes cleanly to the literal "push" -- normalize_shell_
        # words resolves it, so this is caught by the ordinary git-write
        # family match, not the ANSI-C hard-deny gate.
        ok, reason = _check("git $'push' --force")
        assert ok is False
        assert "git write op" in reason

    def test_unresolvable_ansi_c_span_hard_denies(self):
        # \c is not a recognized ANSI-C escape -- normalize_shell_words
        # returns None, and the raw string still hides "push" inside the
        # intact $'...' wrapper from every family matcher. Must hard-deny
        # here rather than falling through to an unearned ALLOW.
        ok, reason = _check(r"git $'pu\csh' --force")
        assert ok is False
        assert "ANSI-C" in reason

    def test_ansi_c_gate_never_denies_a_resolvable_harmless_command(self):
        ok, reason = _check("git $'status'")
        assert ok is True, reason


# ---------------------------------------------------------------------------
# Attestation seam — no resolver configured (strictly no wider than PR1's
# own scope-trim posture: every forge-PR-mutation deny applies
# unconditionally).
# ---------------------------------------------------------------------------


class TestAttestationSeamNotConfigured:
    _FORGE_CURL_WRITE_RE = re.compile(
        r"(?:^|[\s/])forgejo-curl\b.*?(?:^|\s)(POST|PATCH|DELETE)(?:\s|$)"
    )

    def test_caller_flag_present_but_no_resolver_still_denies(self):
        ok, reason = _check(
            "forgejo-curl --caller some-other-role POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
        )
        assert ok is False
        assert "forge-PR-mutation" in reason


# ---------------------------------------------------------------------------
# Attestation seam — resolver configured, admits.
# ---------------------------------------------------------------------------


class TestAttestationSeamAdmits:
    _FORGE_CURL_WRITE_RE = re.compile(
        r"(?:^|[\s/])forgejo-curl\b.*?(?:^|\s)(POST|PATCH|DELETE)(?:\s|$)"
    )

    def _resolver(self, *, attested="some-other-role", ineligible=frozenset()):
        return ActingSubagentResolver(
            resolve_attested_identity=lambda: attested,
            check_acting_role_command=lambda name, cmd: (True, ""),
            ineligible_caller_names=ineligible,
        )

    def test_matching_attested_identity_admits(self):
        ok, reason = _check(
            "forgejo-curl --caller some-other-role POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=self._resolver(attested="some-other-role"),
        )
        assert ok is True, reason

    def test_unattested_invocation_still_admits(self):
        # Resolver returns "" -- unattested is the ORDINARY case for a
        # genuinely distinct acting subagent, not a mismatch.
        ok, reason = _check(
            "forgejo-curl --caller some-other-role POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=self._resolver(attested=""),
        )
        assert ok is True, reason

    def test_no_caller_flag_at_all_falls_through_to_ordinary_deny(self):
        ok, reason = _check(
            "forgejo-curl POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=self._resolver(),
        )
        assert ok is False
        assert "forge-PR-mutation" in reason

    def test_ineligible_caller_name_not_carved_out(self):
        ok, reason = _check(
            "forgejo-curl --caller merger-role POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=self._resolver(
                attested="merger-role", ineligible=frozenset({"merger-role"})
            ),
        )
        assert ok is False
        assert "forge-PR-mutation" in reason

    def test_named_roles_own_allowlist_denies_carve_out_falls_through(self):
        resolver = ActingSubagentResolver(
            resolve_attested_identity=lambda: "some-other-role",
            check_acting_role_command=lambda name, cmd: (False, "denied"),
        )
        ok, reason = _check(
            "forgejo-curl --caller some-other-role POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=resolver,
        )
        assert ok is False
        assert "forge-PR-mutation" in reason


# ---------------------------------------------------------------------------
# Attestation seam — fail-closed mismatch (tome #700 T3-gh, lr-c62abb).
# ---------------------------------------------------------------------------


class TestAttestationSeamFailClosedMismatch:
    _FORGE_CURL_WRITE_RE = re.compile(
        r"(?:^|[\s/])forgejo-curl\b.*?(?:^|\s)(POST|PATCH|DELETE)(?:\s|$)"
    )

    def test_mismatched_attested_identity_hard_denies_before_deferral(self):
        # The invoking process is attested as "role-a" but claims --caller
        # role-b -- even though role-b's own allowlist (were it consulted)
        # would admit the command, this must fail closed BEFORE any
        # deferral: an identity may mint/use only its own credential.
        resolver = ActingSubagentResolver(
            resolve_attested_identity=lambda: "role-a",
            check_acting_role_command=lambda name, cmd: (True, ""),
        )
        ok, reason = _check(
            "forgejo-curl --caller role-b POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=resolver,
        )
        assert ok is False
        assert "ATTESTED" in reason
        assert "role-a" in reason

    def test_matching_attested_identity_is_not_a_mismatch(self):
        resolver = ActingSubagentResolver(
            resolve_attested_identity=lambda: "role-b",
            check_acting_role_command=lambda name, cmd: (True, ""),
        )
        ok, reason = _check(
            "forgejo-curl --caller role-b POST https://forge.example/api/v1/repos/o/r/pulls",
            forge_pr_mutation_verb_patterns=(self._FORGE_CURL_WRITE_RE,),
            acting_subagent_resolver=resolver,
        )
        assert ok is True, reason


# ---------------------------------------------------------------------------
# ActingSubagentResolver config validation (mandatory: any caller-supplied
# config tuple/string flowing into a regex is validated at construction —
# guard-policy.md's "config-tuple->regex validation" rule, SE2 precedent).
# ---------------------------------------------------------------------------


class TestActingSubagentResolverValidation:
    def test_malformed_ineligible_caller_name_raises_at_construction(self):
        import pytest

        with pytest.raises(ValueError):
            ActingSubagentResolver(
                resolve_attested_identity=lambda: "",
                check_acting_role_command=lambda name, cmd: (True, ""),
                ineligible_caller_names=frozenset({"bad|name"}),
            )

    def test_well_formed_ineligible_caller_name_accepted(self):
        resolver = ActingSubagentResolver(
            resolve_attested_identity=lambda: "",
            check_acting_role_command=lambda name, cmd: (True, ""),
            ineligible_caller_names=frozenset({"merger-role"}),
        )
        assert "merger-role" in resolver.ineligible_caller_names
