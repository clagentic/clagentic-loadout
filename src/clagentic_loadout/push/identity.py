"""push.identity — bot-attributed commit re-authoring + HEAD-author gate.

Wave B slice 3 (lr-09ca, tome #688). Ported from the reference push
transport's identity-pinning path (pin_commits_to_builder_identity /
_reauthor_commits / _verify_head_author, lr-66c2/lr-2422/lr-9f50). This is
LOAD-BEARING BEHAVIOR the task requires preserved, not identity to strip: a
push authored under the wrong identity is unrecoverable once merged, so the
caller's own bot identity is verified on HEAD before every push.

WHAT MOVED / WHAT DIDN'T:
  - The re-authoring mechanism (git filter-branch --env-filter over the
    caller's own commits, excluded against the caller-resolved base ref)
    is unchanged.
  - The IDENTITY SOURCE is no longer read from a reference-implementation-
    specific per-agent config section or credential-provider config file.
    Both are caller inputs to this module: `bot_name` / `bot_email` are
    plain parameters. Config-file lookup (if any) is the caller's concern
    (e.g. a loadout config schema in a later slice), never baked in here.
  - There is no reference-implementation gatekeeper "org_default" fallback
    tier — that was GitHub-App-install-token-specific plumbing this module
    does not own. A caller integrating a credential-minting provider that
    also exposes a bot identity resolves it itself and passes the result in.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from clagentic_loadout.push.errors import AuthorMismatchError


def _run(
    cmd: list[str],
    *,
    check: bool = False,
    cwd: Path | None = None,
    env: dict | None = None,
) -> subprocess.CompletedProcess:
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd is not None else None,
        env=env,
    )
    if check and result.returncode != 0:
        raise subprocess.CalledProcessError(result.returncode, cmd[0], result.stdout, result.stderr)
    return result


def get_head_author_email(git_cwd: Path | None = None) -> str:
    """Return the author email of the current HEAD commit, or "" on any
    failure (callers treat empty as unverifiable/mismatch)."""
    r = _run(["git", "log", "-1", "--format=%ae", "HEAD"], cwd=git_cwd)
    if r.returncode != 0:
        return ""
    return r.stdout.strip()


def verify_head_author(expected_email: str, git_cwd: Path | None = None) -> bool:
    """True iff HEAD's author email matches *expected_email* exactly."""
    actual = get_head_author_email(git_cwd)
    if not actual:
        return False
    return actual == expected_email


def resolve_exclusion_ref(
    base_branch: str,
    git_cwd: Path | None = None,
) -> tuple[str, str] | tuple[None, None]:
    """Resolve the exclusion ref filter-branch uses as the rewrite floor.

    Prefers `origin/<base_branch>` (remote tracking ref) over the local
    `<base_branch>` ref — a lagging local base ref would otherwise place
    already-landed commits inside the rewrite range and corrupt their SHAs.
    Returns (ref, label) on success, (None, None) if neither ref resolves.
    """
    remote_ref = f"origin/{base_branch}"
    r = _run(["git", "rev-parse", "--verify", remote_ref], cwd=git_cwd)
    if r.returncode == 0:
        return remote_ref, f"remote tracking ref {remote_ref!r}"

    r2 = _run(["git", "rev-parse", "--verify", base_branch], cwd=git_cwd)
    if r2.returncode == 0:
        return base_branch, f"local ref {base_branch!r} (remote unavailable)"

    return None, None


def reauthor_commits(
    base_branch: str,
    bot_name: str,
    bot_email: str,
    git_cwd: Path | None = None,
) -> bool:
    """Rewrite every commit on the current branch (not reachable from the
    resolved exclusion ref) to *bot_name*/*bot_email* as both author and
    committer. Returns True on success (including the no-op case where
    there is nothing to rewrite), False on any failure — callers must fail
    closed on False.
    """
    quick_check = _run(["git", "rev-list", "--count", f"{base_branch}..HEAD"], cwd=git_cwd)
    if quick_check.returncode != 0 or quick_check.stdout.strip() == "0":
        return True

    exclusion_ref, _label = resolve_exclusion_ref(base_branch, git_cwd)
    if exclusion_ref is None:
        return False

    range_check = _run(["git", "rev-list", "--count", "HEAD", f"^{exclusion_ref}"], cwd=git_cwd)
    if range_check.returncode != 0 or range_check.stdout.strip() == "0":
        return True

    env_filter = (
        'GIT_AUTHOR_NAME="$_BOT_NAME" '
        'GIT_AUTHOR_EMAIL="$_BOT_EMAIL" '
        'GIT_COMMITTER_NAME="$_BOT_NAME" '
        'GIT_COMMITTER_EMAIL="$_BOT_EMAIL" '
        "export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL GIT_COMMITTER_NAME GIT_COMMITTER_EMAIL"
    )
    filter_env = os.environ.copy()
    filter_env["_BOT_NAME"] = bot_name
    filter_env["_BOT_EMAIL"] = bot_email
    filter_env["FILTER_BRANCH_SQUELCH_WARNING"] = "1"

    result = subprocess.run(
        ["git", "filter-branch", "-f", "--env-filter", env_filter, "HEAD", f"^{exclusion_ref}"],
        capture_output=True,
        text=True,
        env=filter_env,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )

    # Clean up filter-branch's backup refs regardless of outcome.
    subprocess.run(
        ["git", "update-ref", "-d", "refs/filter-branch/backup/refs/heads/HEAD"],
        capture_output=True,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )
    subprocess.run(
        ["git", "update-ref", "-d", "ORIG_HEAD"],
        capture_output=True,
        cwd=str(git_cwd) if git_cwd is not None else None,
    )

    return result.returncode == 0


def pin_commits_to_bot_identity(
    bot_name: str | None,
    bot_email: str | None,
    base_branch: str,
    git_cwd: Path | None = None,
    *,
    fail_closed_on_missing: bool = False,
) -> bool:
    """Re-author branch commits to (*bot_name*, *bot_email*), then verify
    HEAD carries that identity.

    Identity resolution is the CALLER's responsibility — this function takes
    the resolved name/email directly, never reads a config file itself. When
    either is missing:
      - fail_closed_on_missing=True: raise AuthorMismatchError (a caller
        that requires bot attribution, e.g. a namespace-restricted push,
        must not silently skip it).
      - fail_closed_on_missing=False: return False (re-authoring skipped;
        the caller decides whether that is acceptable for its deployment).

    Returns True when re-authoring was performed (the caller must then pass
    force_with_lease=True to the subsequent push, since history changed).

    Raises AuthorMismatchError when re-authoring fails, or when HEAD's
    author does not match the expected identity afterward — a
    mis-attributed push is unrecoverable, so this never returns a silent
    partial success.
    """
    if not bot_name or not bot_email:
        if fail_closed_on_missing:
            raise AuthorMismatchError(
                "bot identity (name + email) is required for this push but "
                "was not supplied. Refusing to push — a mis-attributed push "
                "is unrecoverable. Resolve the bot identity (config or "
                "credential-provider-derived) and retry."
            )
        return False

    ok = reauthor_commits(base_branch, bot_name, bot_email, git_cwd)
    if not ok:
        raise AuthorMismatchError(
            f"commit re-authoring failed — cannot pin commits to bot identity "
            f"{bot_email!r}. A mis-attributed push is unrecoverable; fix the "
            f"filter-branch failure and retry."
        )

    if not verify_head_author(bot_email, git_cwd):
        actual = get_head_author_email(git_cwd)
        raise AuthorMismatchError(
            f"HEAD commit author email {actual!r} does not match expected "
            f"bot identity {bot_email!r} after re-authoring. A mis-"
            f"attributed push is unrecoverable; refusing to push."
        )

    return True


__all__ = [
    "get_head_author_email",
    "pin_commits_to_bot_identity",
    "reauthor_commits",
    "resolve_exclusion_ref",
    "verify_head_author",
]
