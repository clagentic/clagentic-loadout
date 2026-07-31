"""test_reviewer_no_disk_staging_or_hand_authored_fence.py — suite-wide
regression guard (lr-30c0d0, folding in the salvaged ask from a closed
internal-deployment task, lr-dde08e).

WHAT THIS GUARDS AGAINST: the deadlock this task fixes (observed against a
Forgejo deployment, lr-72abca) happened because a reviewer ROLE had no way
to post the merge gate's required fenced ```review-result``` block other
than (a) a
second, disk-staged body-file path, or (b) hand-authoring the triple-backtick
fence and piping it through a shell producer -- both of which guard-bash.py's
argv backtick scan (correctly) refuses, or which reopen a disk-staging class
of hole this platform has deliberately built OUT of every verb (see
transport.git_host_api's own module docstring: "--body-stdin is the SOLE
body path... no --body-file staging, no /tmp shim, no second content
source"). This test asserts BOTH halves of that contract stay true for every
verb a reviewer-shaped ROLE can invoke:

  1. No --body-file / disk-staging flag exists on any verb a reviewer role
     can reach (provisioning.roles.DEFAULT_ROLE_VERBS is the source of
     truth for which verbs a role invokes).
  2. The canonical tool-owned-fence path (transport.git_host_api's
     --expect-verdict-block) exists and requires no backtick anywhere in its
     stdin contract or its own argv -- the CLI surface a reviewer role
     actually needs to post a verdict never requires hand-authoring the
     fence through the shell.

PARAMETRIZED OVER THE ROLE SET so a role added later to DEFAULT_ROLE_VERBS is
covered automatically, per the task's explicit "catching agents/roles added
later" requirement -- this module never enumerates named agents (CLAUDE.md
rule 1 / lr-30c0d0's "roles not agent names"); it walks the role->verb-set
config surface that already exists for exactly this purpose
(provisioning.roles).
"""

from __future__ import annotations

import argparse

import pytest

from clagentic_loadout.provisioning.roles import DEFAULT_ROLE_VERBS
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import git_host_api

#: verb label -> the argparse-parser-building callable for that verb, for
#: every verb label DEFAULT_ROLE_VERBS references that posts a comment/PR/
#: review body over the network. "push" (loadout-push) is excluded here: it
#: opens/updates a PR via --title/--body flags that are ordinary PR
#: metadata, never the merge-gate's fenced verdict contract this guard is
#: about, and push.verb has its own parser-shape tests already.
_VERB_ARG_PARSER_BUILDERS = {
    "git-host-api": git_host_api._build_arg_parser,
    "review-post": review_verb._build_arg_parser,
}

#: Flag-name substrings that would indicate a disk-staging body path (the
#: exact class of hole every verb here is deliberately built without --
#: matches e.g. "--body-file", "--comment-file", "--diff-file-staging".
#: --body-env (lr-10a996 BODY-TRANSPORT half) deliberately does NOT match
#: any of these markers -- see TestBodyEnvIsNotDiskStaging below for why it
#: is categorically different from the --body-file shape this guard exists
#: to catch: it takes NO caller-supplied path (zero argv value at all) and
#: reads a single FIXED location this codebase itself computes
#: (transport.body_env.resolve_body_path), never a value a caller names.
_DISK_STAGING_FLAG_MARKERS = ("file", "-path", "staging")

#: The reviewer role set: every role in DEFAULT_ROLE_VERBS whose declared
#: verb set includes at least one comment/PR/review-posting verb. Computed
#: from the config surface itself, never a hardcoded name list, so a role
#: renamed or added later is picked up automatically.
_REVIEWER_ROLES = tuple(
    sorted(
        role
        for role, verbs in DEFAULT_ROLE_VERBS.items()
        if set(verbs) & set(_VERB_ARG_PARSER_BUILDERS)
    )
)


def _flag_strings(parser: argparse.ArgumentParser) -> list[str]:
    flags = []
    for action in parser._actions:
        flags.extend(action.option_strings)
    return flags


class TestReviewerRoleSetIsNonEmpty:
    def test_at_least_one_role_reaches_a_posting_verb(self) -> None:
        # A guard that silently parametrizes over zero roles would pass
        # vacuously and catch nothing -- lock in that the seed config
        # actually exercises this.
        assert _REVIEWER_ROLES, (
            "no role in DEFAULT_ROLE_VERBS reaches a comment/PR/review-"
            "posting verb -- this regression guard would be vacuous"
        )
        assert "reviewer" in _REVIEWER_ROLES


@pytest.mark.parametrize("role", _REVIEWER_ROLES)
class TestNoReviewerReachableVerbHasDiskStagingFlag:
    def test_no_disk_staging_flag_on_any_reachable_posting_verb(self, role: str) -> None:
        posting_verbs = set(DEFAULT_ROLE_VERBS[role]) & set(_VERB_ARG_PARSER_BUILDERS)
        assert posting_verbs, f"role {role!r} unexpectedly has no posting verbs"
        for verb in posting_verbs:
            parser = _VERB_ARG_PARSER_BUILDERS[verb]()
            flags = _flag_strings(parser)
            offending = [
                f for f in flags
                if any(marker in f.lower() for marker in _DISK_STAGING_FLAG_MARKERS)
            ]
            assert not offending, (
                f"role {role!r} reaches verb {verb!r}, which exposes a "
                f"disk-staging-shaped flag {offending!r} -- --body-stdin "
                f"(or --expect-verdict-block on top of it) is the sole "
                f"body path; no second, disk-staged content source is "
                f"permitted."
            )

    def test_every_reachable_posting_verb_is_body_stdin_only(self, role: str) -> None:
        posting_verbs = set(DEFAULT_ROLE_VERBS[role]) & set(_VERB_ARG_PARSER_BUILDERS)
        for verb in posting_verbs:
            parser = _VERB_ARG_PARSER_BUILDERS[verb]()
            flags = _flag_strings(parser)
            assert "--body-stdin" in flags or verb == "review-post", (
                f"role {role!r} reaches verb {verb!r}, which has no "
                f"--body-stdin flag at all -- every comment/PR/review-"
                f"posting verb this platform ships takes its body from "
                f"stdin, never a bare positional/flag string that could be "
                f"hand-typed with an embedded fence."
            )


class TestGitHostApiExposesToolOwnedFencePath:
    """The canonical reviewer route this task adds: --expect-verdict-block.
    Both --caller (reviewer) and --pr-sha (git-host-api) reach this same
    parser via DEFAULT_ROLE_VERBS' "git-host-api" verb label."""

    def test_expect_verdict_block_flag_exists(self) -> None:
        parser = git_host_api._build_arg_parser()
        assert "--expect-verdict-block" in _flag_strings(parser)

    def test_expect_verdict_block_help_never_instructs_a_hand_authored_fence(self) -> None:
        parser = git_host_api._build_arg_parser()
        # RawDescriptionHelpFormatter keeps the epilog unwrapped, so its own
        # lines are the ones a caller would actually copy verbatim -- unlike
        # the free-flowing --help description text (which legitimately
        # mentions the ```review-result``` fence name in prose), the
        # EXAMPLE COMMAND LINE the caller types must never itself embed a
        # backtick: that is precisely the hand-authored-fence trap this task
        # fixes.
        example_line = next(
            line for line in parser.epilog.splitlines() if "review_status" in line
        )
        assert "`" not in example_line

    def test_build_verdict_block_is_the_single_fence_source_reused_here(self) -> None:
        # merge.verdict.build_verdict_block is imported directly into
        # git_host_api (not re-implemented) -- the module-level identity
        # check locks in "one source of truth," not just "produces the same
        # string by coincidence."
        from clagentic_loadout.merge.verdict import build_verdict_block

        assert git_host_api.build_verdict_block is build_verdict_block

    def test_no_backtick_required_in_the_body_stdin_contract(self) -> None:
        # The structured stdin JSON --expect-verdict-block actually requires
        # (body + review_status) has no backtick anywhere in its shape --
        # confirmed at the function level, not just by example prose.
        result = git_host_api.build_expected_verdict_body(
            b'{"body":"LGTM.","review_status":"clean"}',
            reviewer="reviewer",
            pr_number=1,
            expected_head_sha="a" * 40,
        )
        # The FENCE itself legitimately contains backticks (that is its
        # whole shape) -- this asserts the INPUT never needed one, not that
        # the output is backtick-free.
        caller_input = b'{"body":"LGTM.","review_status":"clean"}'
        assert b"`" not in caller_input
        assert "```review-result" in result  # tool-constructed, not caller-typed


class TestReviewPostExposesToolOwnedFencePathBothPlatforms:
    """The MANDATORY, fail-closed sibling this task (lr-482c20) adds on
    "review-post" -- --verdict-review-status -- closes the gap
    --expect-verdict-block (Forgejo-only, transport.git_host_api) leaves
    open: GitHub had NO tool-owned verdict-fence route at all, and a
    reviewer role reaching "review-post" (both platforms) could otherwise
    post an ordinary --body-stdin comment with a hand-typed fence in prose,
    bypassing --expect-verdict-block's guarantees entirely."""

    def test_verdict_review_status_flag_exists(self) -> None:
        parser = review_verb._build_arg_parser()
        assert "--verdict-review-status" in _flag_strings(parser)
        assert "--verdict-head-sha" in _flag_strings(parser)

    def test_verdict_review_status_help_never_instructs_a_hand_authored_fence(self) -> None:
        parser = review_verb._build_arg_parser()
        example_line = next(
            line for line in parser.epilog.splitlines() if "review_status" in line
        )
        assert "`" not in example_line

    def test_build_verdict_block_is_the_single_fence_source_reused_here(self) -> None:
        # merge.verdict.build_verdict_block is imported directly into
        # review.verb (not re-implemented) -- the SAME source
        # transport.git_host_api's --expect-verdict-block already reuses --
        # one authoring source across both the Forgejo-only and the
        # both-platform routes.
        from clagentic_loadout.merge.verdict import build_verdict_block

        assert review_verb.build_verdict_block is build_verdict_block

    def test_no_backtick_required_in_the_body_stdin_contract(self) -> None:
        caller_input = b'{"body":"LGTM.","review_status":"clean"}'
        assert b"`" not in caller_input
        prose, review_status = review_verb.validate_review_verdict_body_stdin_content(
            caller_input
        )
        assert review_status == "clean"
        fence = review_verb.build_verdict_block("reviewer", review_status, "a" * 40, 1)
        assert "```review-result" in fence  # tool-constructed, not caller-typed


@pytest.mark.parametrize("verb_label", tuple(_VERB_ARG_PARSER_BUILDERS))
class TestBodyEnvIsNotDiskStaging:
    """--body-env (lr-10a996 BODY-TRANSPORT half) is a SECOND body-
    ingestion route this class of guard must NOT flag, and this test class
    exists to make that distinction an explicit, asserted contract rather
    than an implicit side effect of a marker list that happens not to
    match. --body-env is categorically unlike the disk-staging --body-file
    shape this guard is built to catch:

      1. It takes NO value -- a bare boolean switch, so it can never
         introduce a per-invocation argv substring the way a caller-typed
         path value could.
      2. The path it reads is computed ENTIRELY by this codebase
         (transport.body_env.resolve_body_path) -- no flag, env var, or
         config value lets a caller redirect it to an arbitrary location.

    See transport.body_env's own module docstring for the full trade-off
    analysis against the previously-rejected --body-file re-add."""

    def test_body_env_flag_exists_and_is_a_bare_switch(self, verb_label: str) -> None:
        parser = _VERB_ARG_PARSER_BUILDERS[verb_label]()
        flags = _flag_strings(parser)
        assert "--body-env" in flags
        action = next(a for a in parser._actions if "--body-env" in a.option_strings)
        assert action.nargs == 0, (
            "--body-env must take no value -- any value slot would let a "
            "caller vary the argv per invocation, defeating the entire "
            "point of this ingestion route."
        )

    def test_body_env_help_never_mentions_a_caller_supplied_path(self, verb_label: str) -> None:
        parser = _VERB_ARG_PARSER_BUILDERS[verb_label]()
        action = next(a for a in parser._actions if "--body-env" in a.option_strings)
        # The help text documents a FIXED path this codebase computes, never
        # instructs a caller to supply/choose one.
        assert "FIXED" in action.help
