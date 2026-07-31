"""stage_body_verb.py — `loadout-stage-body`: the sanctioned WRITE side for
`--body-env` (lr-199b99, lr-becdef follow-on).

THE GAP THIS CLOSES: lr-becdef shipped the READ side of the stale-body fix
(`transport.body_env.read_caller_body_bytes` — read-and-consume plus an
identity-stamp reject-on-mismatch requiring a matching
`body.<caller>.stamp.json` sidecar) but never gave a reviewer's `--body-env`
STAGE side a sanctioned way to WRITE that stamp. `transport.body_env.
stage_caller_body` (the write-side API) existed already — lr-becdef's own
module docstring says so explicitly ("this is the WRITE side a harness's own
staging step ... performs") — but nothing exposed it as a CLI a reviewer
role's shell-scoped harness could actually invoke. A reviewer staging via a
raw `printf > body.<caller>.json` writes ONLY the body file; the stamp
sidecar (`body.<caller>.stamp.json`) never gets written, so
`read_caller_body_bytes` fails closed with "no identity stamp staged" —
live incident: one reviewer-role instance hit a comment-post failure on a
real PR while a second, differently-routed reviewer instance landed cleanly
on the same PR (see "THE CROSS-REVIEWER-ROLE ASYMMETRY" below).

WHY A DEDICATED VERB, NOT "the verb stages-on-write" (task option (a)):
option (a) would mean `loadout-git-host-api --body-env` and
`loadout-review-post --body-env` themselves write the stamp at READ time —
but by definition there is nothing STAGED yet the first time a caller reaches
that code path; there is no body/stamp pair for the read call to have
constructed. Option (a) does not actually name a WRITE-time actor, it only
restates the read side's own contract. The task's option (b) — "a dedicated
stage helper/verb ... that writes BOTH body and stamp atomically" — is the
one that supplies an actual write-time actor, and this module is exactly
that: it is a THIN CLI WRAPPER around the already-existing
`transport.body_env.stage_caller_body` API (lr-becdef), reusing it rather
than re-implementing the stamp shape a second time.

WHY THIS KEEPS THE STAMP WRITE OFF THE RAW-PRINTF PATH (the operator's field
evidence on this task, quoted verbatim): a live guard-hook allowlist audit
during this task found the reviewer-role Bash allowlist admits ONLY
`body.json` / `body.<caller>.json` for the staging directory — it
categorically DENIES a `*.stamp.json` write via raw `printf`/`cat`/`echo`
redirection, and extending that allowlist to admit `*.stamp.json` would
either (a) let an agent hand-author a stamp with an ARBITRARY target_pr/
head_sha (defeating the whole point of the identity stamp being a value
this platform computes, not one a caller types), or (b) require a second,
narrower allowlist entry with its own drift risk. Routing the write through
THIS verb (already an allowlist-safe, argv-constant Bash invocation, exactly
like every other loadout console-script verb) means the agent allowlist
never needs a `*.stamp.json` entry at all — the verb is the ONLY thing that
ever opens that file for writing.

THE CROSS-REVIEWER-ROLE ASYMMETRY (task-required explanation): both
reviewer-role attempts on the same PR used the IDENTICAL raw-printf staging
shape and the IDENTICAL `loadout-git-host-api --body-env` read call — there
was never a difference in "residual stamp," "different invocation," or a
race between the two reviewer instances. Neither reviewer's raw-printf
staging step EVER wrote a stamp sidecar (no stamp-write mechanism existed
anywhere before this task), so `read_caller_body_bytes` should have failed
closed identically for both. The reviewer instance whose comment landed
cleanly did so through a DIFFERENT code path than the raw-printf/
`--body-env` route the other instance hit exit 15 on — the incident notes
for this defect record that reviewer's own forgejo-curl / `--body-stdin`
fallback landing comments WITHOUT the mandatory verdict fence (a
non-conformant path that must be ignored per those notes), which is
consistent with the OTHER instance's landed comment also having gone
through some other route than a stamp-verified `--body-env` read — there
was no working `--body-env` write-then-read pair available to EITHER
reviewer instance at the time, so "one succeeded, one failed" was never
evidence of a working stamp path; it reflects that no fix existed yet for
either instance's `--body-env` route, and whichever fallback path each
instance's own invocation happened to take produced different visible
outcomes. THIS FIX makes the outcome deterministic for both roles
identically: `loadout-stage-body` is the ONE write path any reviewer
instance's harness calls, producing a byte-identical stamp shape
(`transport.body_env.stage_caller_body`) regardless of which reviewer role
invokes it — there is no reviewer-specific branch anywhere in this module.

CLI/API signature (the exact contract a downstream integrator's sibling task
adopts):

    loadout-stage-body --caller <role> --target-pr <int> \\
        [--head-sha <sha>] \\
        < body.json

    (pass the body bytes via --body-stdin, which is the SAME flag name
    every other body-ingesting verb in this package uses — bare stdin is
    also accepted with no flag, mirroring review.verb's own
    stdin-is-the-default convention.)

    or, for the PR-CREATION path (lr-e1e2fb — see below), bind to the
    branch that will open the new PR instead of an existing PR number:

    loadout-stage-body --caller <role> --create-branch <branch-name> \\
        < body.json

`--target-pr` and `--create-branch` are MUTUALLY EXCLUSIVE — exactly one is
required. Reads the caller's intended comment/PR body from stdin (validated
with the SAME `transport.git_host_api.validate_body_stdin_content` every
other body-ingesting verb in this package already uses — a caller staging
malformed/empty content is refused here, BEFORE anything touches disk,
rather than surfacing later as an opaque `--body-env` read failure), then
calls `transport.body_env.stage_caller_body(caller=..., body_bytes=...,
target_pr=..., create_branch=..., head_sha=...)` — the SAME function
lr-becdef's own test suite already exercises as the write-side single
source of truth. This verb does not talk to any git host, mints no
credential, and makes no network call: staging is purely a local
filesystem write, exactly like the harness-side write step
`docs/integration.md` already documents, just moved behind a sanctioned
CLI instead of a hand-authored shell redirect.

NO CALLER-SUPPLIED FILESYSTEM PATH ANYWHERE (lr-e1e2fb, operator design
correction, PR #136 security audit): this verb PREVIOUSLY also accepted
`--body-file <path>` as an alternative to stdin for reading body CONTENT.
That flag is REMOVED — not because content-input-via-path is itself the
security defect (`push.verb`'s prior `--body-file` bypassed the identity
stamp entirely, which is what made it dangerous), but because the operator
directive governing this redesign is unconditional: no verb in this
package accepts a caller-supplied filesystem path for PR-body content, full
stop, so there is exactly one ingestion shape (stdin) to reason about
across every body-ingesting verb, not two. stdin remains the sole content
path; the STAGING LOCATION was always, and remains, fully computed by this
module (`resolve_caller_body_path`) — never caller-chosen.

Host-agnostic by construction (lr-becdef seq 3's requirement, carried
forward): `transport.body_env` has no platform branch at all — the SAME
staged body.<caller>.json + body.<caller>.stamp.json pair is read by
BOTH `transport.git_host_api`'s Forgejo `--body-env` route and
`review.verb`'s shared (Forgejo + GitHub) `--body-env` route. This verb
stages for either read path identically; there is nothing GitHub- or
Forgejo-specific about staging a body+stamp pair.

CREATE-MODE (lr-e1e2fb): `push.verb`'s PR-creation path has no PR number
yet at staging time by definition — `--create-branch <branch-name>` binds
the staged body to the git branch that will open the new PR instead. The
branch name reaching this flag is resolved by `push.verb` itself via
`git rev-parse --abbrev-ref HEAD` before staging (and the identical
resolution runs again before reading) — this verb does not resolve it
itself and does not trust an arbitrary caller-typed branch name any
differently than it already trusts a caller-typed `--target-pr` int: both
are values the INVOKING verb computed from its own already-verified state
(a real git ref for the branch, a real PR number from a real API/CLI flag
for the PR case), passed through this staging step as an opaque token, not
authenticated or re-derived here.
"""

from __future__ import annotations

import argparse
import sys

from clagentic_loadout._version import get_version
from clagentic_loadout.transport.body_env import (
    BODY_ENV_NOT_EPHEMERAL_NOTE,
    BodyEnvError,
    _resolve_caller_stamp_path,
    resolve_caller_body_path,
    stage_caller_body,
)
from clagentic_loadout.transport.git_host_api import (
    _SAFE_CALLER_RE,
    validate_body_stdin_content,
)
from clagentic_loadout.transport.git_host_api import GitHostApiError

# ---------------------------------------------------------------------------
# Exit codes -- a reserved range for this verb, distinct from every other
# verb's own table in this package (each verb owns its own 0-N range; see
# transport.git_host_api's and review.verb's own "Exit codes" comments for
# the precedent this follows).
# ---------------------------------------------------------------------------

#: Success -- the body and its identity-stamp sidecar were both written.
EXIT_OK = 0
#: Generic usage error (bad/missing --caller, --target-pr/--create-branch,
#: or --head-sha shape) -- fails BEFORE any filesystem write.
EXIT_USAGE = 1
#: stdin (or --body-stdin) content is empty / not valid JSON / has no
#: non-empty 'body' string field -- the SAME validation every other
#: body-ingesting verb in this package performs, applied here BEFORE the
#: body is ever staged to disk, so a caller never stages malformed content
#: that would only be caught later at --body-env read time.
EXIT_BODY_STDIN_EMPTY = 2
#: RETIRED (lr-e1e2fb): previously "--body-file names a path that does not
#: resolve to a readable regular file." --body-file is removed -- no verb in
#: this package accepts a caller-supplied filesystem path for PR-body
#: content. Kept as a named constant (unused, never raised) so the exit-code
#: NUMBER 3 stays reserved rather than being silently repurposed for
#: something else in this module's own range.
EXIT_BODY_FILE_UNREADABLE = 3
#: stage_caller_body's own post-write verify-after-write check (lr-765172)
#: raised BodyEnvError -- the body and/or its identity-stamp sidecar is
#: absent or empty immediately after the staging write. Distinct from every
#: other exit code here: this is a DURABILITY failure of the write itself,
#: never reported as EXIT_OK (the silent-producer-failure this code closes:
#: this verb must never print "staged" and exit 0 when the stamp sidecar is
#: not actually durable on disk).
EXIT_STAGE_VERIFY_FAILED = 4


class StageBodyVerbError(Exception):
    """Raised for any loadout-stage-body failure that should terminate the
    process with a specific exit code. Carries the intended exit code as
    `.code`, mirroring every other verb's own `*VerbError`/`*ApiError`
    convention in this package."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _fail(message: str, code: int) -> None:
    raise StageBodyVerbError(message, code)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-stage-body",
        description=(
            "loadout-stage-body -- the sanctioned WRITE side for --body-env. "
            "Stages a caller-namespaced body file AND its "
            "identity-stamp sidecar atomically, so a later "
            "`loadout-git-host-api --body-env` / `loadout-review-post "
            "--body-env` / `loadout-push` read (the read-and-consume + "
            "identity-stamp contract) has a stamp to verify against. "
            "Exactly one of --target-pr (existing PR, update/comment path) "
            "or --create-branch (the branch that will open a NEW PR, "
            "push's create path) is required. Reads the body from stdin "
            "(bare, or --body-stdin, same flag name every other "
            "body-ingesting verb in this package uses) -- no caller-"
            "supplied filesystem path is accepted for body content on any "
            "verb in this package; writes no network request, mints no "
            "credential -- this is a pure local filesystem staging step. "
            + BODY_ENV_NOT_EPHEMERAL_NOTE
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Example (existing-PR binding):\n"
            "  echo '{\"body\":\"LGTM, no issues found.\",\"review_status\":\"clean\"}' | \\\n"
            "    loadout-stage-body --caller some-reviewer --target-pr 42 \\\n"
            "      --head-sha abc123\n"
            "\n"
            "  loadout-git-host-api --caller some-reviewer --body-env \\\n"
            "    --verify-comment --pr-sha abc123 --expect-verdict-block some-reviewer \\\n"
            "    POST /api/v1/repos/some-owner/some-repo/issues/42/comments\n"
            "\n"
            "Example (PR-creation binding):\n"
            "  echo '{\"body\":\"plain PR description text\"}' | \\\n"
            "    loadout-stage-body --caller builder --create-branch feat/my-branch\n"
            "\n"
            "  loadout-push --caller builder --title 'feat: add x' --body-env\n"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"loadout-stage-body {get_version()}",
        help="Show the clagentic-loadout package version and exit.",
    )
    parser.add_argument(
        "--caller",
        required=True,
        help="Role/name this body is staged for -- MUST equal the "
        "--caller value the later --body-env read invocation supplies; "
        "the staged path is namespaced by this exact string "
        "(transport.body_env.resolve_caller_body_path).",
    )
    parser.add_argument(
        "--target-pr",
        default=None,
        type=int,
        help="The PR number this body is being staged for -- the existing-"
        "PR update/comment path. Bound into the identity-stamp sidecar's "
        "'target_pr' field -- the later --body-env read must be invoked "
        "against THIS SAME PR or it fails closed (stale-read "
        "guard). Mutually exclusive with --create-branch; exactly one is "
        "required.",
    )
    parser.add_argument(
        "--create-branch",
        default=None,
        dest="create_branch",
        help="The git branch that will open a NEW PR -- push's PR-creation "
        "path, which has no PR number yet at staging time. "
        "Bound into the identity-stamp sidecar's 'create_branch' field -- "
        "the later --body-env read must be invoked against THIS SAME "
        "branch or it fails closed, mirroring --target-pr's own stale-read "
        "guard. Mutually exclusive with --target-pr; exactly one is "
        "required.",
    )
    parser.add_argument(
        "--head-sha",
        default=None,
        help="Optional: the evaluated head SHA this body is staged for. "
        "Bound into the identity-stamp sidecar's 'head_sha' field -- a "
        "later --body-env read that supplies --pr-sha/--verdict-head-sha "
        "must match, or it fails closed. Omit for an ordinary, "
        "non-verdict comment with no SHA to bind against.",
    )
    parser.add_argument(
        "--body-stdin",
        action="store_true",
        help="Explicit marker that the body is read from stdin -- the "
        "default behavior when this flag is omitted too (bare stdin), "
        "kept as an explicit flag only to mirror every other body-"
        "ingesting verb's own --body-stdin flag name in this package.",
    )
    return parser


def _run(args: argparse.Namespace, *, body_bytes: bytes) -> int:
    if not _SAFE_CALLER_RE.match(args.caller):
        _fail(
            f"--caller {args.caller!r} contains invalid characters (only "
            f"alphanumeric, hyphen, underscore; no path separators or "
            f"traversal).",
            code=EXIT_USAGE,
        )

    # lr-e1e2fb: --target-pr and --create-branch are mutually exclusive --
    # exactly one binds the staged body's identity stamp. Checked BEFORE
    # any I/O, mirroring every other precondition in this module.
    if bool(args.target_pr is not None) == bool(args.create_branch is not None):
        _fail(
            f"exactly one of --target-pr / --create-branch is required "
            f"(got --target-pr={args.target_pr!r}, "
            f"--create-branch={args.create_branch!r}).",
            code=EXIT_USAGE,
        )
    if args.target_pr is not None and args.target_pr <= 0:
        _fail(
            f"--target-pr must be a positive integer, got: {args.target_pr!r}",
            code=EXIT_USAGE,
        )

    try:
        validate_body_stdin_content(body_bytes)
    except GitHostApiError as exc:
        _fail(str(exc), code=EXIT_BODY_STDIN_EMPTY)

    try:
        stage_caller_body(
            caller=args.caller,
            body_bytes=body_bytes,
            target_pr=args.target_pr,
            create_branch=args.create_branch,
            head_sha=args.head_sha,
        )
    except BodyEnvError as exc:
        # lr-765172: stage_caller_body's own verify-after-write raised --
        # the write did not durably land. Never report success here: a
        # verb-layer readback (below, on the happy path) is not the ONLY
        # backstop against a silent producer failure, but this catch is
        # what turns stage_caller_body's fail-closed signal into a non-zero
        # exit instead of an unhandled traceback.
        _fail(str(exc), code=EXIT_STAGE_VERIFY_FAILED)

    # lr-765172: verify-after-write, again, at the verb layer -- belt and
    # suspenders. stage_caller_body already stat's both files before
    # returning, but this verb is the boundary that actually prints
    # "success" and returns EXIT_OK; it must never trust a prior layer's
    # success return without its own stat of both artifacts, so a future
    # change to stage_caller_body cannot silently reopen the exact bug this
    # task fixes (success-exit decoupled from stamp-on-disk).
    #
    # lr-765172 review follow-up: existence is checked for BOTH files -- a
    # MISSING/unreadable file is always a real durability failure, staged
    # body or stamp alike. The zero-byte size check, though, is applied
    # ONLY to the stamp, matching transport.body_env.stage_caller_body's
    # own contract (see that function's verify-after-write comment): this
    # module always writes a non-empty JSON stamp, so a zero-byte stamp is
    # unconditionally a write failure, but a deliberately-empty body is
    # valid, stageable content -- the verb layer must not fail a caller
    # whose staged content legitimately names zero bytes (mirroring
    # test_stage_caller_body_accepts_deliberately_empty_body_content at the
    # transport layer; a zero-byte BODY is content this verb's own
    # validate_body_stdin_content call above has already accepted, not a
    # write-durability concern for this readback to re-litigate).
    body_path = resolve_caller_body_path(caller=args.caller)
    stamp_path = _resolve_caller_stamp_path(caller=args.caller)
    for staged_path, label in ((body_path, "body"), (stamp_path, "identity stamp")):
        try:
            size = staged_path.stat().st_size
        except OSError as exc:
            _fail(
                f"staged {label} at {staged_path!r} could not be verified "
                f"after stage_caller_body reported success: {exc}.",
                code=EXIT_STAGE_VERIFY_FAILED,
            )
        if label == "identity stamp" and size == 0:
            _fail(
                f"staged {label} at {staged_path!r} is empty after "
                f"stage_caller_body reported success -- refusing to exit 0.",
                code=EXIT_STAGE_VERIFY_FAILED,
            )

    binding_label = (
        f"target_pr={args.target_pr}" if args.target_pr is not None
        else f"create_branch={args.create_branch!r}"
    )
    print(
        f"loadout-stage-body: staged body+stamp for caller={args.caller!r} "
        f"{binding_label} at {body_path.parent} "
        f"(body.{args.caller}.json, body.{args.caller}.stamp.json).",
        file=sys.stderr,
    )
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code (does not call
    sys.exit itself so it stays testable) -- the `if __name__` guard below
    is the one place that translates the return value into a real exit.
    """
    if argv is None:
        argv = sys.argv[1:]

    if any(arg in ("--help", "-h") for arg in argv):
        _build_arg_parser().print_help()
        return EXIT_OK

    parser = _build_arg_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE

    # stdin is the SOLE content-input path (lr-e1e2fb: no caller-supplied
    # filesystem path is accepted anywhere in this package for PR-body
    # content) -- bare stdin or --body-stdin are the same shape.
    body_bytes = sys.stdin.buffer.read()

    try:
        return _run(args, body_bytes=body_bytes)
    except StageBodyVerbError as exc:
        print(f"loadout-stage-body: {exc}", file=sys.stderr)
        return exc.code


if __name__ == "__main__":
    sys.exit(main())
