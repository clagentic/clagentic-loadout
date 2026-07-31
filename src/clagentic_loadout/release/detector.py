"""detector.py — generic v*-tag release detector.

Release-signal tag-scan verb (lr-51d4, Wave A slice 6, tome #688). Ported
from the reference implementation; the source copy stays primary until its
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

Invoked by a caller (CI job, release-authorizing caller, or the operator) on
a new `v*` semver git tag. On a tag push it (1) computes the
prev-tag..new-tag commit range, (2) extracts every distinct resolved work
item from that range's commit messages, and (3) fires ONE "task shipped"
signal per distinct item via dispatch.dispatch_task_shipped() — the exact
same HMAC-signed status-hook call dispatch's own CLI makes. No trailer
parsing, HMAC signing, or hook-firing logic is duplicated here.

Resolution sources (same grammar as dispatch.parse_trailers, applied
per-commit-message across a range):
  - Same-repo GitHub case: `Task: <id>` / `Closes #NN` trailers are read
    directly off each commit message in the prev..new range.
  - Cross-git-host / manual crossing: still manual, not auto-resolved. This
    module does NOT invent an id -> public-issue map or any persistent
    store. A repo whose releases need that crossing supplies it out-of-band
    via dispatch_manual_task() (thin wrapper matching dispatch's --task-id
    CLI path) — never auto-resolved from the commit range.

Idempotency (semantic-release skip): a repo already self-releasing via
semantic-release (presence of release.config.js/.cjs/.mjs, OR a per-repo
loadout config marker file/dir) fires its OWN release signal — this
detector must not double-post. is_semantic_release_owned() is the skip
predicate; the CLI checks it before doing any range/ref work and no-ops
(exit 0) when true.

SSRF hardening: is_valid_status_hook_url() lives in dispatch.py, where
fire_status_hook() itself enforces it for every caller. This module
re-exports the same name so its own CLI check and existing callers are
unaffected; there is exactly one validator, not two.

Scope binding (confused-deputy remediation): the AUTO path (commit-trailer
scanning) fires a real HMAC-signed "shipped" signal from whatever repo
--repo-path happens to point at, for whatever task_id a commit trailer
names. Without a binding between the detected task_id and the repo actually
being scanned, ANY repo running this detector could fire a false "shipped"
signal for a task_id it does not own, via nothing more than a crafted/
mistaken `Task: <id>` commit-message trailer.

The fix:

  - repo_identity_from_remote() derives THIS repo's own "owner/repo" identity
    from its own `git remote get-url` — a generic http(s) two-path-segment
    parser, not tied to any one git host, since this detector is documented
    to run against arbitrary repos.
  - is_repo_authorized_for_auto_dispatch() is the fail-closed scope gate: the
    operator supplies an explicit allow-set at invocation time via
    --allowed-repo (repeatable, exact "owner/repo") and/or --allowed-org
    (repeatable). A scanned repo whose derived identity does not match ANY
    supplied entry is OUT of scope. No entries supplied at all -> OUT of
    scope, unconditionally (fail closed on the unconfigured case).
  - The AUTO path (extract_resolved_tasks -> dispatch_detected_tasks) is
    gated: main() computes the scanned repo's identity, checks it against
    the allow-set, and DROPS every auto-detected task (no signal fired) when
    out of scope — a drop is silent-but-logged (stderr), never a crash,
    mirroring dispatch's no-op philosophy for genuine no-signal cases.
  - The --manual-task-id path is explicitly UNCHANGED and NOT gated: the
    caller supplies task_id out-of-band and asserts it themselves
    (dispatch_manual_task's existing contract) — that is caller-asserted
    trust, not a scraped, unauthenticated commit trailer, so the scope gate
    does not apply there.

Exit codes (mirrors dispatch.py's contract):
    0   OK — zero or more signals fired successfully, OR the repo was
        skipped as semantic-release-owned, OR no distinct task was found
        in the range, OR every auto-detected task was dropped by the scope
        gate (all are successful no-op-capable outcomes).
    1   Usage error (bad arguments, --repo-path not a git repo, --new-tag
        not found in the repo).
    2   Secret resolution failed (propagated from dispatch).
    3   A status-hook call failed (propagated from dispatch) — this module
        fails closed on ANY signal failure rather than silently dropping a
        distinct task.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.release import dispatch
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_MARKER,
    resolve_repo_config_root,
)

# is_valid_status_hook_url is owned by dispatch.py -- fire_status_hook()
# itself guards on it, so every caller (this detector's AUTO path included)
# gets the same check. Re-exported here so existing callers of
# detector.is_valid_status_hook_url keep working without a second copy of
# the validator.
is_valid_status_hook_url = dispatch.is_valid_status_hook_url

# ---------------------------------------------------------------------------
# Exit codes — reuse dispatch's numbering so a caller scripting both
# entrypoints sees one consistent contract.
# ---------------------------------------------------------------------------

EXIT_OK = dispatch.EXIT_OK
EXIT_USAGE = dispatch.EXIT_USAGE
EXIT_SECRET_FAILED = dispatch.EXIT_SECRET_FAILED
EXIT_HOOK_FAILED = dispatch.EXIT_HOOK_FAILED

# ---------------------------------------------------------------------------
# Trailer parsing — same keyword grammar as dispatch.parse_trailers, but
# applied per-commit-message across a range rather than to a single PR body,
# so multiple distinct commits/tasks in one release are all found.
# ---------------------------------------------------------------------------

_TASK_TRAILER_RE = re.compile(r"(?im)^\s*task:\s*(\S+)\s*$")
_CLOSES_TRAILER_RE = re.compile(r"(?im)^\s*closes\s+#(\d+)\s*$")

# semantic-release ownership markers (idempotency skip predicate).
_SEMANTIC_RELEASE_CONFIG_NAMES = (
    "release.config.js",
    "release.config.cjs",
    "release.config.mjs",
)

#: Per-repo loadout config marker dir — its presence doubles as "this repo
#: already has its own release wiring". Checks both the current
#: `.clagentic` home (see repo_config.py, lr-446c35) and the legacy
#: `.loadout` home, so a not-yet-migrated repo is still recognized during
#: the transitional period (removed after the fleet migration, lr-a645aa).
#: Kept as a bare marker-DIRECTORY check (not the specific config FILE) —
#: lr-18f46a adds the bounded wrapper-hop to this lookup but deliberately
#: does NOT change this check's own file-vs-dir semantics, only WHERE it
#: looks (repo_path itself, or one git-anchored hop above it).
_LOADOUT_CONFIG_MARKER = Path(DEFAULT_CONFIG_RELATIVE_PATH).parts[0]
_LEGACY_LOADOUT_CONFIG_MARKER = LEGACY_CONFIG_MARKER

# Generic http(s) git-host "owner/repo" extractor — deliberately not tied to
# any one git host: this detector's own docstring documents it running
# against arbitrary repos. Accepts:
#   http(s)://<host>[:<port>]/<owner>/<repo>[.git]
_REMOTE_OWNER_REPO_RE = re.compile(
    r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$"
)


def repo_identity_from_remote(repo_path: Path, *, remote: str = "origin") -> str | None:
    """
    Derive *repo_path*'s own "owner/repo" identity from its git remote URL.

    Returns None (never raises) when the repo has no such remote, the URL
    does not match the generic http(s) two-path-segment shape, or any git
    command fails -- callers treat None as "identity unknown," which the
    fail-closed scope gate below always treats as OUT of scope.
    """
    result = subprocess.run(
        ["git", "remote", "get-url", remote],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    match = _REMOTE_OWNER_REPO_RE.match(result.stdout.strip())
    if not match:
        return None
    owner, repo_name = match.group(1), match.group(2)
    return f"{owner}/{repo_name}"


def is_repo_authorized_for_auto_dispatch(
    repo_identity: str | None,
    *,
    allowed_repos: frozenset[str],
    allowed_orgs: frozenset[str],
) -> bool:
    """
    Fail-closed scope gate for the AUTO-detected task path (confused-deputy
    remediation).

    A repo is authorized to auto-fire a "shipped" signal only if its OWN
    identity (derived from its git remote, never from a commit trailer)
    appears in an explicit caller-supplied allow-set.

    Fail-closed cases (all return False -- no signal fires):
      - repo_identity is None (identity could not be derived at all).
      - allowed_repos is empty AND allowed_orgs is empty (the unconfigured
        case -- no allow-set was supplied at all). Ambiguous/unconfigured
        scope means do NOT fire, not fire-anyway.

    Authorized cases (return True):
      - repo_identity is an exact member of allowed_repos ("owner/repo").
      - repo_identity's owner segment is a member of allowed_orgs.
    """
    if repo_identity is None:
        return False
    if not allowed_repos and not allowed_orgs:
        return False
    if repo_identity in allowed_repos:
        return True
    owner = repo_identity.split("/", 1)[0]
    return owner in allowed_orgs


def is_semantic_release_owned(repo_path: Path) -> bool:
    """
    True if *repo_path* already self-releases via semantic-release and this
    detector must skip it entirely (no signal at all) to avoid double-posting.

    Ownership markers (any is sufficient): a release.config.{js,cjs,mjs} at
    repo root, or a per-repo loadout config marker DIRECTORY reachable via
    the bounded wrapper-hop (lr-18f46a: `repo_path` itself, its own git top
    level, or that top level's immediate parent/wrapper — see
    `repo_config.resolve_repo_config_root`) at either the CURRENT
    `.clagentic` home (see repo_config.py, lr-446c35) or the LEGACY
    `.loadout` home (transitional — a not-yet-migrated repo still counts as
    configured; removed after the fleet migration, lr-a645aa). Unchanged
    from the pre-lr-18f46a contract: this checks bare marker-DIRECTORY
    presence, not the specific config file — only WHERE it looks gained the
    wrapper hop, not the presence predicate itself.
    """
    for name in _SEMANTIC_RELEASE_CONFIG_NAMES:
        if (repo_path / name).exists():
            return True
    config_root = resolve_repo_config_root(
        repo_path,
        _LOADOUT_CONFIG_MARKER,
        _LEGACY_LOADOUT_CONFIG_MARKER,
        exists_check=Path.exists,
    )
    if (config_root / _LOADOUT_CONFIG_MARKER).exists():
        return True
    if (config_root / _LEGACY_LOADOUT_CONFIG_MARKER).exists():
        return True
    return False


def _run_git(repo_path: Path, args: list[str]) -> str:
    """Run a git subcommand in *repo_path*, returning stripped stdout.

    Raises SystemExit(EXIT_USAGE) on any non-zero exit — every caller site
    here treats a git failure as a usage error (bad repo path, unknown tag,
    not a git repo), never a signal-worthy condition.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=repo_path,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _die(
            EXIT_USAGE,
            f"git {' '.join(args)} failed in {repo_path}: {result.stderr.strip()}",
        )
    return result.stdout.strip()


def _reject_leading_dash_tag(tag: str, *, flag_name: str) -> None:
    """
    Refuse a tag name that begins with '-' before it ever reaches git as an
    opaque positional argument.

    --new-tag/--prev-tag flow into `git log <range>` and `git tag -l <tag>`
    as bare positionals. A tag name beginning with '-' (e.g. "--upload-pack=...")
    could be parsed by git as an OPTION rather than a revision/refname.
    subprocess.run's list-form argv (no shell=True) already rules out shell
    injection; this closes the narrower git-option-injection vector, in
    addition to the `--` end-of-options separator considerations in
    get_commit_messages() below (belt-and-suspenders: reject here so the bad
    input never reaches ANY git call site, not just one).

    Raises SystemExit(EXIT_USAGE) on a leading-dash tag name.
    """
    if tag.startswith("-"):
        _die(
            EXIT_USAGE,
            f"{flag_name} {tag!r} begins with '-' — refusing to pass a "
            f"tag name that could be parsed as a git option rather than a "
            f"revision/refname.",
        )


def compute_commit_range(repo_path: Path, prev_tag: str | None, new_tag: str) -> str:
    """
    Return the git revision range for the release, as a string suitable for
    `git log <range>`.

    - prev_tag given: "prev_tag..new_tag" (standard two-dot range).
    - prev_tag absent (first-tag case, no prior v* tag exists yet): the
      range is every commit reachable from new_tag, i.e. just "new_tag" —
      git log's single-revision form already means "everything up to and
      including this commit," which is exactly the first-release range.

    Refuses (SystemExit(EXIT_USAGE)) a prev_tag/new_tag beginning with '-'
    before building the range string — see _reject_leading_dash_tag.
    """
    _reject_leading_dash_tag(new_tag, flag_name="--new-tag")
    if prev_tag:
        _reject_leading_dash_tag(prev_tag, flag_name="--prev-tag")
        return f"{prev_tag}..{new_tag}"
    return new_tag


def get_commit_messages(repo_path: Path, commit_range: str) -> list[str]:
    """
    Return the full commit message body (subject + body) of every commit in
    *commit_range*, oldest-first, using a NUL-delimited format so multi-line
    commit bodies are never misparsed as multiple commits.

    Git-option-injection hardening: a `--` end-of-options separator here is
    NOT used, deliberately -- `git log --format=... --reverse -- <range>`
    makes git parse <range> as a PATHSPEC, not a revision range (verified:
    it silently returns zero commits instead of the intended range). The
    leading-dash rejection therefore lives entirely in compute_commit_range()'s
    _reject_leading_dash_tag() call, which runs BEFORE commit_range is ever
    built from --new-tag/--prev-tag -- the only two untrusted inputs that
    can reach this function's *commit_range* argument via this module's own
    call graph.
    """
    raw = _run_git(
        repo_path,
        ["log", "--format=%B%x00", "--reverse", commit_range],
    )
    if not raw:
        return []
    # Trailing NUL from the last commit leaves one empty element; drop it.
    return [msg for msg in raw.split("\x00") if msg.strip()]


def extract_resolved_tasks(commit_messages: list[str]) -> list[tuple[str, int | None]]:
    """
    Extract DISTINCT (task_id, issue_number) pairs from a list of commit
    messages, in first-seen order.

    Dedupe key is task_id alone: two commits in the same range closing the
    same task (e.g. a fixup commit + the original) must yield exactly ONE
    signal. If the same task_id appears with conflicting issue numbers
    across commits, the FIRST-seen issue number wins — commits are
    processed oldest-first so this is the original PR's trailer, not a
    later fixup's.

    Commits with a `Task:` trailer but no `Closes #NN` are included with
    issue_number=None (genuine no-linked-issue case — dispatch's own CLI
    already treats this as "still fire using task_id alone," so the
    detector inherits that behavior rather than dropping the task).

    Commits with neither trailer contribute nothing.
    """
    seen: dict[str, int | None] = {}
    order: list[str] = []
    for message in commit_messages:
        task_match = _TASK_TRAILER_RE.search(message)
        if not task_match:
            continue
        task_id = task_match.group(1)
        closes_match = _CLOSES_TRAILER_RE.search(message)
        issue_number = int(closes_match.group(1)) if closes_match else None
        if task_id not in seen:
            seen[task_id] = issue_number
            order.append(task_id)
    return [(task_id, seen[task_id]) for task_id in order]


# ---------------------------------------------------------------------------
# Dispatch — thin wrappers over dispatch.dispatch_task_shipped so no
# HMAC/payload/secret logic is duplicated here.
# ---------------------------------------------------------------------------


def dispatch_detected_tasks(
    tasks: list[tuple[str, int | None]],
    *,
    status_hook_url: str,
    version: str,
    dispatcher: str | None = None,
    secret_env_caller: str | None = None,
    secret_env_var: str | None = None,
) -> list[tuple[str, int, dict]]:
    """
    Fire one "task shipped" signal per distinct (task_id, issue_number) in
    *tasks*, via dispatch.dispatch_task_shipped(). Returns a list of
    (task_id, status_code, response_body) for every successful call.

    Fails closed (propagates SystemExit) on the FIRST hook failure — a
    partial-success silent swallow would drop a real signal, which this
    module's contract (fail closed on ANY signal failure) forbids.
    """
    results: list[tuple[str, int, dict]] = []
    for task_id, _issue_number in tasks:
        status, body = dispatch.dispatch_task_shipped(
            task_id,
            status_hook_url=status_hook_url,
            dispatcher=dispatcher,
            version=version,
            secret_env_caller=secret_env_caller,
            secret_env_var=secret_env_var,
        )
        results.append((task_id, status, body))
    return results


def dispatch_manual_task(
    task_id: str,
    *,
    status_hook_url: str,
    version: str | None = None,
    dispatcher: str | None = None,
    secret_env_caller: str | None = None,
    secret_env_var: str | None = None,
) -> tuple[int, dict]:
    """
    Fire a single "task shipped" signal for an explicitly-supplied *task_id*
    — the cross-git-host / manual crossing path. The caller supplies the
    already-resolved task_id out-of-band; this function does not derive it
    from any commit range, map, or lookup. Identical in effect to calling
    dispatch.py --task-id directly — provided here so both the
    auto-detected and manual paths go through this module's one entrypoint.
    """
    return dispatch.dispatch_task_shipped(
        task_id,
        status_hook_url=status_hook_url,
        dispatcher=dispatcher,
        version=version,
        secret_env_caller=secret_env_caller,
        secret_env_var=secret_env_var,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _die(exit_code: int, msg: str) -> None:
    print(f"release-detector: error: {msg}", file=sys.stderr)
    sys.exit(exit_code)


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "release-detector — generic v*-tag release detector. Feeds "
            "dispatch's existing 'task shipped' hook once per distinct "
            "resolved task in the tag's commit range."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  clagentic-loadout release detect \\\n"
            "      --repo-path /path/to/repo \\\n"
            "      --new-tag v1.2.3 \\\n"
            "      --status-hook-url https://triage.example.com/status-hook \\\n"
            "      --dispatcher some-dispatcher \\\n"
            "      --allowed-repo some-org/some-repo\n"
            "\n"
            "  # First-tag case (no prior v* tag exists) -- omit --prev-tag.\n"
            "  clagentic-loadout release detect \\\n"
            "      --repo-path /path/to/repo \\\n"
            "      --new-tag v0.1.0 \\\n"
            "      --status-hook-url https://triage.example.com/status-hook \\\n"
            "      --allowed-org some-org\n"
        ),
    )
    parser.add_argument(
        "--cli-version",
        action="version",
        version=f"release-detect {get_version()}",
        help="Show the clagentic-loadout package version and exit. Named "
        "--cli-version, not --version, because this verb's own --version "
        "flag is a release-version-string BUSINESS argument (see below) "
        "that predates the CLI conformance rule and cannot be renamed "
        "without breaking existing callers.",
    )
    parser.add_argument(
        "--repo-path",
        default=".",
        help="Path to the git repository to scan (default: cwd).",
    )
    parser.add_argument(
        "--new-tag",
        required=True,
        help="The new v* tag that was just pushed (e.g. v1.2.3).",
    )
    parser.add_argument(
        "--prev-tag",
        default=None,
        help="The previous v* tag, if any. Omit for the first-tag case "
        "(no prior v* tag exists) — the range becomes 'everything up to "
        "and including --new-tag'.",
    )
    manual_group = parser.add_mutually_exclusive_group()
    manual_group.add_argument(
        "--manual-task-id",
        default=None,
        help="Cross-git-host / manual crossing: fire a signal for this "
        "explicit task_id instead of scanning the commit range. Mutually "
        "exclusive with range-based detection.",
    )
    parser.add_argument(
        "--dispatcher",
        default=None,
        help="Dispatcher name to narrow the release-signal service's "
        "lookup (caller-supplied; no default is assumed).",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="Release version string. Defaults to --new-tag when omitted.",
    )
    parser.add_argument(
        "--status-hook-url",
        required=True,
        help="Full URL of the release-signal service's inbound status-hook endpoint.",
    )
    parser.add_argument(
        "--allowed-repo",
        action="append",
        default=[],
        dest="allowed_repos",
        metavar="OWNER/REPO",
        help="Exact 'owner/repo' authorized to auto-fire a signal from the "
        "AUTO-detected (commit-trailer) path. Repeatable. Confused-deputy "
        "scope gate: the repo being scanned (derived from its OWN git "
        "remote, never from a commit trailer) must match an --allowed-repo "
        "or --allowed-org entry, or every auto-detected task is DROPPED "
        "with no signal fired. Does not apply to --manual-task-id "
        "(caller-asserted, out of scope for this gate).",
    )
    parser.add_argument(
        "--allowed-org",
        action="append",
        default=[],
        dest="allowed_orgs",
        metavar="ORG",
        help="Org/owner authorized to auto-fire a signal from the "
        "AUTO-detected path — any repo owned by ORG is in scope. "
        "Repeatable. See --allowed-repo for the fail-closed scope-gate "
        "contract.",
    )
    parser.add_argument(
        "--secret-env-caller",
        default=None,
        help="Role/name whose secret-env file holds STATUS_HOOK_SECRET "
        "(default: dispatch.DEFAULT_ROLE).",
    )
    parser.add_argument(
        "--secret-env-var",
        default=None,
        help="Read the HMAC secret directly from this already-set env var "
        "instead of self-fetching from a role-scoped .env file.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_arg_parser()
    args = parser.parse_args(argv)
    version = args.version or args.new_tag

    # SSRF hardening: reject an obviously-invalid --status-hook-url (bad
    # scheme / no host) before any dispatch path -- manual or auto -- ever
    # reaches urllib. See is_valid_status_hook_url's docstring for what
    # this does and does not cover.
    if not is_valid_status_hook_url(args.status_hook_url):
        _die(
            EXIT_USAGE,
            f"--status-hook-url {args.status_hook_url!r} is not a valid "
            f"http(s) URL with a host.",
        )

    if args.manual_task_id:
        status, body = dispatch_manual_task(
            args.manual_task_id,
            status_hook_url=args.status_hook_url,
            version=version,
            dispatcher=args.dispatcher,
            secret_env_caller=args.secret_env_caller,
            secret_env_var=args.secret_env_var,
        )
        print(
            f"release-detector: manual task {args.manual_task_id} — "
            f"status-hook responded HTTP {status}: {body}",
            file=sys.stderr,
        )
        return EXIT_OK

    repo_path = Path(args.repo_path).resolve()
    if not (repo_path / ".git").exists():
        _die(EXIT_USAGE, f"--repo-path is not a git repository: {repo_path}")

    if is_semantic_release_owned(repo_path):
        print(
            f"release-detector: {repo_path} is semantic-release-owned "
            "(release.config.js/.cjs/.mjs or a .clagentic/.loadout marker "
            "present) — skipping entirely (idempotency, no signal).",
            file=sys.stderr,
        )
        return EXIT_OK

    commit_range = compute_commit_range(repo_path, args.prev_tag, args.new_tag)
    commit_messages = get_commit_messages(repo_path, commit_range)
    tasks = extract_resolved_tasks(commit_messages)

    if not tasks:
        print(
            f"release-detector: no 'Task: <id>' trailer found in "
            f"range {commit_range!r} — nothing to dispatch (no-op).",
            file=sys.stderr,
        )
        return EXIT_OK

    # Confused-deputy scope gate: the AUTO-detected path must never fire a
    # signal for a task_id whose ownership is only asserted by an
    # unauthenticated commit trailer in the scanned repo. Derive the
    # scanned repo's OWN identity from its git remote and check it against
    # the caller-supplied allow-set BEFORE any dispatch call. Fails closed:
    # no --allowed-repo/--allowed-org supplied, or the derived identity
    # matches neither, drops every auto-detected task with no signal fired
    # (never a crash — same no-op philosophy as the no-task/
    # semantic-release-owned cases above).
    repo_identity = repo_identity_from_remote(repo_path)
    allowed_repos = frozenset(args.allowed_repos)
    allowed_orgs = frozenset(args.allowed_orgs)
    if not is_repo_authorized_for_auto_dispatch(
        repo_identity, allowed_repos=allowed_repos, allowed_orgs=allowed_orgs
    ):
        dropped = [task_id for task_id, _issue_number in tasks]
        print(
            f"release-detector: scope gate DROPPED {len(dropped)} "
            f"auto-detected task(s) {dropped} — scanned repo identity "
            f"{repo_identity!r} is not authorized by any --allowed-repo/"
            f"--allowed-org (fail-closed confused-deputy gate). No signal fired.",
            file=sys.stderr,
        )
        return EXIT_OK

    results = dispatch_detected_tasks(
        tasks,
        status_hook_url=args.status_hook_url,
        version=version,
        dispatcher=args.dispatcher,
        secret_env_caller=args.secret_env_caller,
        secret_env_var=args.secret_env_var,
    )
    for task_id, status, body in results:
        print(
            f"release-detector: {task_id} — status-hook responded "
            f"HTTP {status}: {body}",
            file=sys.stderr,
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
