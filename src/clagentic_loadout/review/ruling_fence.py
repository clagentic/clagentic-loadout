"""review.ruling_fence — the finding a ruling is for, read from a posted fence.

A ruling names one finding on one pull request. The finding is identified from
the reviewer's POSTED verdict comment, which is durable and which the host
attributes to a login, rather than from a file the caller supplies. The comment
is accepted only if every one of these holds:

  * it is a comment on the requested pull request of the requested repository;
  * its author's login is the login of a required reviewer role, resolved by the
    merge gate's own path (``merge.reviewer_login``), where the required roles are
    the ones the repository's tracked gate declares at the pull request's BASE
    commit (``merge.repo_gate_runtime``): content the pull request under review
    cannot change, read through the host API, never from a checkout the caller
    names;
  * it carries a well-formed verdict fence for that reviewer, read by the merge
    gate's own fence reader (``merge.verdict.read_reviewer_verdict``), for the
    requested pull request, at the pull request's current head (or the head the
    caller names);
  * the fence is an evaluated one that carries its ``evaluated_findings``, and one
    of them has the requested fingerprint.

The caller's own role credential reads the host, resolved through the same
credential provider the merge verb uses. Nothing here posts or changes anything.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.merge import forgejo_backend, github_backend
from clagentic_loadout.merge.errors import (
    GateFactUnavailableError,
    VerdictMalformedError,
    VerdictMissingError,
    VerdictRoleMismatchError,
    VerdictStaleError,
)
from clagentic_loadout.merge.repo_gate_runtime import (
    gate_from_tracked_text,
    with_resolvable_reviewer_roles,
)
from clagentic_loadout.merge.reviewer_login import (
    ReviewerLoginNotConfiguredError,
    resolve_reviewer_login,
)
from clagentic_loadout.merge.tree_sync import resolve_base_sha
from clagentic_loadout.merge.verdict import read_reviewer_verdict
from clagentic_loadout.platform_detect import PLATFORM_GITHUB
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.review.policy_reader import HostPolicyReader, PolicyReadError
from clagentic_loadout.sha import FULL_SHA_RE
from clagentic_loadout.transport.credential_provider import TokenProvider, resolve_token
from clagentic_loadout.transport.provider_config import resolve_platform_provider


class FenceRefusedError(Exception):
    """The comment is not a fence a ruling may be taken from; the message says
    which condition failed."""


class FindingNotInFenceError(FenceRefusedError):
    """The fence is acceptable but holds no finding with the requested fingerprint."""


@dataclass(frozen=True)
class FenceFinding:
    """The finding the fence holds for the requested fingerprint: every
    ``evaluated_findings`` entry carrying it, and where they came from."""

    matches: tuple[dict[str, Any], ...]
    comment_id: int
    reviewer: str
    head_sha: str


@dataclass(frozen=True)
class _Host:
    pr_info: Mapping[str, Any]
    comments: list[dict[str, Any]]
    policy: HostPolicyReader


def _read_host(
    platform: str,
    owner: str,
    repo: str,
    pr: int,
    *,
    token: str,
    git_host_base: str,
    opener,
) -> _Host:
    if platform == PLATFORM_GITHUB:
        pr_info = github_backend.get_pr_info(owner, repo, pr, token=token, opener=opener)
        comments = github_backend.fetch_comments(owner, repo, pr, token=token, opener=opener)
        files = GithubAcquireBackend(token, opener=opener)
    else:
        pr_info = forgejo_backend.get_pr_info(git_host_base, owner, repo, pr, token=token, opener=opener)
        comments = forgejo_backend.fetch_comments(
            git_host_base, owner, repo, pr, token=token, opener=opener
        )
        files = ForgejoAcquireBackend(token, git_host_base=git_host_base, opener=opener)
    return _Host(pr_info, comments, HostPolicyReader(files, owner=owner, repo=repo))


def _required_reviewer_logins(host: _Host, platform: str, owner: str, repo: str) -> dict[str, str]:
    """``{reviewer name: expected login}`` for the roles the repository's tracked
    gate requires at the pull request's base commit. Empty when none is declared
    or the declaration cannot be read: a fence is then not attributable."""
    base_sha = resolve_base_sha(dict(host.pr_info))
    if not base_sha:
        raise FenceRefusedError(
            f"the host reports no base commit for {owner}/{repo}'s pull request, so the "
            "required reviewer roles cannot be read from a revision the pull request cannot change"
        )
    try:
        text = (
            host.policy.read_text(base_sha, TRACKED_GATE_RELATIVE_PATH)
            if TRACKED_GATE_RELATIVE_PATH in host.policy.list_paths(base_sha)
            else None
        )
    except PolicyReadError as exc:
        raise GateFactUnavailableError(f"cannot read the repository gate: {exc}") from exc
    gate = with_resolvable_reviewer_roles(
        gate_from_tracked_text(text, source=f"{owner}/{repo}@{base_sha[:12]}:{TRACKED_GATE_RELATIVE_PATH}"),
        platform,
    )
    logins: dict[str, str] = {}
    for name in gate.reviewer_roles:
        try:
            logins[name] = resolve_reviewer_login(name, platform)
        except ReviewerLoginNotConfiguredError:
            continue
    if not logins:
        reasons = "; ".join(gate.warnings) or f"{TRACKED_GATE_RELATIVE_PATH} declares none"
        raise FenceRefusedError(
            f"no required reviewer role can be established for {owner}/{repo} at base "
            f"{base_sha[:12]} ({reasons}), so no comment can be shown to be a reviewer's verdict"
        )
    return logins


def read_fence_finding(
    *,
    platform: str,
    owner: str,
    repo: str,
    pr: int,
    comment_id: int,
    fingerprint: str,
    head: str | None,
    role: str,
    token_provider: TokenProvider | None,
    git_host_base: str,
    opener=None,
) -> FenceFinding:
    """The finding with *fingerprint* in the verdict comment *comment_id*.

    Raises PlatformMismatchError before any credential is resolved,
    CredentialProviderError when the caller's role has no credential,
    GateFactUnavailableError when the host cannot be read, FenceRefusedError
    when the comment is not an acceptable fence, FindingNotInFenceError when it
    holds no such finding."""
    if platform == PLATFORM_GITHUB:
        github_backend.assert_platform_is_github(owner, repo, explicit_platform=platform)
    else:
        forgejo_backend.assert_platform_is_forgejo(owner, repo, explicit_platform=platform)
    if head is not None and not FULL_SHA_RE.match(head):
        raise FenceRefusedError(f"--head must be a full 40-character lowercase hex SHA, got {head!r}")

    provider = token_provider if token_provider is not None else resolve_platform_provider(platform)
    token = resolve_token(role, provider, repo=f"{owner}/{repo}")
    host = _read_host(
        platform, owner, repo, pr, token=token, git_host_base=git_host_base, opener=opener
    )

    live_head = forgejo_backend.get_pr_head_sha(dict(host.pr_info))
    expected_head = head if head is not None else live_head
    if not expected_head:
        raise FenceRefusedError(
            f"the host reports no head commit for {owner}/{repo}#{pr}, so the fence cannot be "
            "shown to be for the current head"
        )

    logins = _required_reviewer_logins(host, platform, owner, repo)
    comment = next((c for c in host.comments if str(c.get("id")) == str(comment_id)), None)
    if comment is None:
        raise FenceRefusedError(f"{owner}/{repo}#{pr} has no comment with id {comment_id}")
    author = (comment.get("user") or {}).get("login", "")
    name = next((n for n, login in logins.items() if login == author), None)
    if name is None:
        raise FenceRefusedError(
            f"comment {comment_id} was posted by {author!r}, which is not the login of a required "
            f"reviewer role (required: {', '.join(sorted(logins.values()))})"
        )
    try:
        verdict = read_reviewer_verdict(
            [comment],
            expected_login=author,
            current_head_sha=expected_head,
            pr_number=pr,
            owner=owner,
            repo=repo,
            expected_reviewer_name=name,
            enforce_single_fence=True,
        )
    except (
        VerdictMissingError,
        VerdictMalformedError,
        VerdictStaleError,
        VerdictRoleMismatchError,
    ) as exc:
        raise FenceRefusedError(f"comment {comment_id} is not an acceptable verdict fence: {exc}") from exc
    if verdict.pr_number != pr:
        raise FenceRefusedError(
            f"comment {comment_id}'s fence is for pull request #{verdict.pr_number}, "
            f"but {owner}/{repo}#{pr} was requested"
        )
    findings = verdict.state.evaluated_findings
    if findings is None:
        raise FenceRefusedError(
            f"comment {comment_id}'s fence does not carry the findings its verdict was computed "
            "from (it is not an evaluated fence), so a finding cannot be identified from it"
        )
    matches = tuple(dict(f) for f in findings if f.get("fingerprint") == fingerprint)
    if not matches:
        raise FindingNotInFenceError(
            f"comment {comment_id}'s fence holds no finding with fingerprint {fingerprint!r}"
        )
    if len({(m["file"], m["rule_id"]) for m in matches}) > 1:
        raise FenceRefusedError(
            f"fingerprint {fingerprint!r} names findings on different rules or paths in comment "
            f"{comment_id}'s fence, so the ruling would be ambiguous"
        )
    return FenceFinding(matches, int(comment.get("id", comment_id)), verdict.reviewer, verdict.head_sha)


__all__ = [
    "FenceFinding",
    "FenceRefusedError",
    "FindingNotInFenceError",
    "read_fence_finding",
]
