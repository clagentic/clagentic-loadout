"""acquire.contract — the shared PR diff/content-acquisition seam.

lr-c17040 (tome #687 EPIC E). Mirrors review.contract's "one contract, two
transports" shape: ``AcquireBackend`` is the seam both the Forgejo transport
(acquire.forgejo_backend) and the GitHub transport (acquire.github_backend)
satisfy. acquire.verb — the CLI entry point — depends only on this Protocol;
it never branches on which platform it is talking to beyond selecting which
backend instance to call.

WHY THIS EXISTS (lr-c17040 task thread): the two read-only review agents that
gate every PR each had to source a PR's diff THEMSELVES before reviewing,
and loadout provided no acquisition entrypoint — only the review-POST verb
(review.verb). Two independent failure modes resulted: a stale LOCAL git ref
producing a bogus diff range, and a wrapper-cwd sandbox making the actual
checkout unreachable. Both are symptoms of one gap: diff ACQUISITION has no
first-class, platform-agnostic entrypoint, so each caller improvises.

NEVER A LOCAL WORKING TREE: every ``fetch_pr_content`` implementation
resolves the diff/changed-file list from the HOST API alone — never `git
diff`, never a local checkout, never a cwd-relative path. This is what makes
wrapper-cwd and stale-local-main non-issues BY CONSTRUCTION, not by
convention: there is no code path here that could reach a local git ref even
if one happened to be present and stale. The correct base is always the
PR's own ``base.sha`` / merge-base as read from the platform API at fetch
time, never a caller-supplied or locally-resolved ref.

SCANNABLE ARTIFACTS (lr-c17040 comment #1, "SECOND FACET"): acquisition must
yield content usable by BOTH a human/model reviewer (the unified diff text)
AND a security scanner that needs actual files on disk (gitleaks/semgrep/
osv-scanner do not operate on a diff blob — they need a directory tree).
``AcquiredPr.changed_file_contents`` carries the POST-CHANGE file contents,
one entry per changed file, fetched from the API's own contents endpoint —
a scanner-backed caller stages these via ``write_scratch_content`` into a
per-spawn TMPDIR directory (repo CLAUDE.md rule 7: no scratch files in the
tree) and points its scanner at that directory. This never requires a git
clone/checkout of any kind.

TRADE-OFF NAMED (fetched snapshot vs. real checkout): a scanner normally
walks a full checkout, not a synthetic directory of only the changed files.
Scanning only the changed files misses cross-file context a real checkout
would have (e.g. a secret pattern that only resolves against an untouched
config file elsewhere in the tree). This is accepted here because: (a) the
alternative — no scannable artifact at all when local repo access is
blocked — is strictly worse (the observed security-review-agent degradation
this task fixes), and (b) the changed files ARE the fetched diff's own universe by
definition; a scanner gains real, new coverage over "diff eyeballing only"
even though it is narrower than a full-checkout scan. A caller that needs
full-tree scanning uses the SECONDARY local-repo-root resolution
(repo_config.find_git_top_level) when a local checkout is genuinely
reachable; this module's job stops at "reviewable AND scannable without a
checkout."
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, TypedDict, runtime_checkable

from clagentic_loadout.acquire.errors import AcquireFetchError


@dataclass(frozen=True)
class ChangedFile:
    """One changed file in a PR's diff, as read from the host API.

    `patch` is the per-file unified-diff hunk text when the platform's own
    changed-files endpoint provides one (both GitHub and Forgejo/Gitea do,
    for text files under a per-file size cap) — empty string when absent
    (binary file, or the platform omitted it for a very large file). This is
    NOT the same as ``AcquiredPr.diff_text`` (the whole-PR unified diff);
    both are exposed because a caller reviewing file-by-file wants the
    former, while a caller staging a scratch artifact wants the latter.

    `content` is the POST-CHANGE full file text, fetched from the contents
    endpoint at the PR's head SHA — empty string when the file was deleted
    in this PR, or when the platform reports it as binary (never guessed;
    see the backend's own docstring for how binary is detected per
    platform). This is what a scanner needs: `patch` alone is a diff hunk,
    not a scannable file.
    """

    filename: str
    status: str = ""
    patch: str = ""
    content: str = ""


@dataclass(frozen=True)
class AcquiredPr:
    """The result of one PR content-acquisition call.

    `base_sha` / `head_sha` are sourced from the PR's OWN metadata as read
    from the host API at fetch time — never a caller-supplied guess, never a
    locally-resolved git ref. `base_sha` is the PR's merge-base/base
    commit — the correct "other end" of the diff range, resolved by the
    platform itself (GitHub's `base.sha`; Forgejo/Gitea's `base.sha`), so a
    caller never has to compute or trust a local `git merge-base` result.

    `diff_text` is the whole-PR unified diff (base_sha..head_sha) as a
    single string, suitable for a reviewer to read directly. `changed_files`
    carries the per-file breakdown (see ChangedFile) — both a plain
    filename list (for a diff-scope-style cap check) and, when requested,
    each file's post-change content (for scanner staging).
    """

    owner: str
    repo: str
    pr_number: int
    base_sha: str
    head_sha: str
    diff_text: str = ""
    changed_files: tuple[ChangedFile, ...] = field(default_factory=tuple)
    #: Title and description as the host reported them in the same metadata
    #: read that produced head_sha; "" when the host sent none.
    title: str = ""
    body: str = ""
    #: Visibility of the repository the PR merges into, as the host's repository
    #: record in the same metadata read reports it (VISIBILITY_*). None when the
    #: record is absent, unreadable or says something this module does not
    #: recognise: callers treat None as the worst case, never as private.
    repo_visibility: str | None = None

    @property
    def changed_filenames(self) -> list[str]:
        """Plain filename list — the same shape merge.diff_scope.
        check_diff_scope already expects, so a caller enforcing a
        changed-file-count cap does not need to re-derive it from
        ``changed_files``."""
        return [cf.filename for cf in self.changed_files]


def pr_text(pr_info: dict, key: str) -> str:
    """A PR metadata text field (``title``/``body``) as a string: a null,
    absent or non-string host value is ``""``."""
    value = pr_info.get(key)
    return value if isinstance(value, str) else ""


def tree_file_paths(entries: list, what: str) -> list[str]:
    """File paths from a host tree listing, directories left out. Every entry
    must be an object with a string ``path`` and a string ``type``: an entry
    that is not is a failure, never skipped, because a dropped entry could be
    the very policy file the caller is looking for."""
    paths: list[str] = []
    for entry in entries:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("path"), str)
            or not isinstance(entry.get("type"), str)
        ):
            raise AcquireFetchError(f"{what} contained a malformed entry")
        if entry["type"] != "tree":
            paths.append(entry["path"])
    return paths


VISIBILITY_PUBLIC = "public"
VISIBILITY_PRIVATE = "private"
VISIBILITY_INTERNAL = "internal"
_NON_PUBLIC = (VISIBILITY_PRIVATE, VISIBILITY_INTERNAL)


def repository_visibility(pr_info: dict) -> str | None:
    """Visibility of the repository the PR merges into, from the repository
    record under ``base.repo`` in the PR metadata (GitHub reports ``visibility``
    and ``private``; Forgejo/Gitea report ``private`` and ``internal``).

    Non-public is a claim that can lower a verdict, so it is returned only when
    every signal the record carries says non-public and at least one is present.
    Any signal that says public wins, and a record with no recognisable signal,
    or no record at all, returns None. A signal that is present but unrecognised
    (an unknown ``visibility`` value, or a ``private``/``internal`` field that
    is not a Boolean) makes the whole record untrustworthy: None, whatever the
    other signals say."""
    base = pr_info.get("base")
    repo = base.get("repo") if isinstance(base, dict) else None
    if not isinstance(repo, dict):
        return None
    signals: list[str] = []
    if "visibility" in repo:
        named = repo["visibility"]
        if not (isinstance(named, str) and named in (VISIBILITY_PUBLIC, *_NON_PUBLIC)):
            return None
        signals.append(named)
    if "internal" in repo and not isinstance(repo["internal"], bool):
        return None
    if "private" in repo:
        private = repo["private"]
        if not isinstance(private, bool):
            return None
        if private:
            signals.append(
                VISIBILITY_INTERNAL if repo.get("internal") is True else VISIBILITY_PRIVATE
            )
        else:
            signals.append(VISIBILITY_PUBLIC)
    if not signals:
        return None
    if VISIBILITY_PUBLIC in signals:
        return VISIBILITY_PUBLIC
    return VISIBILITY_INTERNAL if VISIBILITY_INTERNAL in signals else VISIBILITY_PRIVATE


@runtime_checkable
class AcquireBackend(Protocol):
    """The one contract both transports satisfy. acquire.verb depends only
    on this signature — never on a transport-specific exception type,
    request shape, or pagination mechanism."""

    def fetch_pr_content(
        self,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        include_file_contents: bool = False,
    ) -> AcquiredPr:
        """Fetch one PR's diff/content from the HOST API — never a local
        working tree.

        Args:
            owner, repo, pr_number: the PR to fetch.
            include_file_contents: when True, also fetches each changed
                file's post-change content (ChangedFile.content) via the
                platform's contents endpoint, one call per changed file —
                needed for scanner staging (see contract.py's module
                docstring, "SCANNABLE ARTIFACTS"). False (the default) skips
                those calls entirely — a caller that only needs the diff
                text/file list for reviewing does not pay for them.

        Raises:
            acquire.errors.AcquireFetchError: the PR metadata, diff, or
                changed-file list could not be read (non-2xx, network
                failure, or unexpected response shape).
        """
        ...


@runtime_checkable
class PolicyFileBackend(Protocol):
    """Optional capability of a transport: read a repository's tree and file
    contents at a commit from the host API, with no local checkout. Separate
    from AcquireBackend for the same reason as RangeDiffBackend."""

    def fetch_tree_paths(self, *, owner: str, repo: str, ref: str) -> list[str]:
        """Every file path in the tree at *ref*.

        Raises:
            acquire.errors.AcquireFetchError: the tree could not be read in
                full (non-2xx, network failure, a truncated listing, or an
                unexpected response shape).
        """
        ...

    def fetch_file_bytes(self, *, owner: str, repo: str, path: str, ref: str) -> bytes:
        """The bytes of *path* at *ref*.

        Raises:
            acquire.errors.AcquireFetchError: the file could not be read in
                full, including when it does not exist at *ref*.
        """
        ...


class IssueComment(TypedDict, total=False):
    """The keys of a PR comment that callers of ``CommentBackend`` read. The host
    returns more; only these are part of the contract."""

    id: int
    body: str


@runtime_checkable
class CommentBackend(Protocol):
    """Optional capability of a transport: read every comment on a PR from the
    host API. Separate from AcquireBackend for the same reason as
    RangeDiffBackend."""

    def fetch_issue_comments(
        self, *, owner: str, repo: str, pr_number: int
    ) -> list[IssueComment]:
        """Every comment on the PR, in full.

        Raises:
            acquire.errors.AcquireFetchError: the list could not be read in
                full (non-2xx, network failure, a page that proves the list
                incomplete, or an unexpected response shape).
        """
        ...


@dataclass(frozen=True)
class RangeFile:
    """One file of a range as the host reports it in structured fields: its
    path after the change and its added and removed line counts."""

    path: str
    added: int
    removed: int


@dataclass(frozen=True)
class RangeDiff:
    """The change between two commits of one repository, from the host API.

    `fast_forward` is True only when `head_sha` is a strict descendant of
    `base_sha`: the one shape in which "what changed since base" is a
    meaningful, linear delta. Any other relation (identical, behind,
    diverged after a force-push or rebase) is False and `diff_text` is empty.

    `files` is the host's structured per-file list when the host gives a
    complete one, else None: a caller then has only `diff_text` to read.
    """

    base_sha: str
    head_sha: str
    fast_forward: bool
    diff_text: str = ""
    files: tuple[RangeFile, ...] | None = None


@runtime_checkable
class RangeDiffBackend(Protocol):
    """Optional capability of a transport: the net diff between two commits.

    Separate from AcquireBackend so a transport that cannot answer it stays a
    valid AcquireBackend; callers that need it check for this Protocol.
    """

    def fetch_range_diff(
        self, *, owner: str, repo: str, base_sha: str, head_sha: str
    ) -> RangeDiff:
        """Read the base..head relation and, for a fast-forward, its diff.

        Raises:
            acquire.errors.AcquireFetchError: the range could not be read
                (an unknown commit, a non-2xx response, a network failure).
        """
        ...


@runtime_checkable
class BaseMergeBackend(Protocol):
    """Optional capability of a transport: whether the PR's base was merged into
    the branch between two of its heads. Separate from RangeDiffBackend for the
    same reason: a transport that cannot answer stays a valid RangeDiffBackend.
    """

    def fetch_base_merged(
        self, *, owner: str, repo: str, base_sha: str, since_head: str, head_sha: str
    ) -> bool:
        """True when the merge base of *base_sha* and *head_sha* differs from
        the merge base of *base_sha* and *since_head*, that is, when the range
        since_head..head_sha carries commits of the base and not only the PR's
        own. *head_sha* must descend from *since_head*.

        Raises:
            acquire.errors.AcquireFetchError: the answer could not be read (an
                unknown commit, a non-2xx response, a network failure, or a
                response that does not carry the needed fields).
        """
        ...


__all__ = [
    "VISIBILITY_INTERNAL",
    "VISIBILITY_PRIVATE",
    "VISIBILITY_PUBLIC",
    "AcquireBackend",
    "AcquiredPr",
    "BaseMergeBackend",
    "ChangedFile",
    "CommentBackend",
    "IssueComment",
    "PolicyFileBackend",
    "RangeDiff",
    "RangeDiffBackend",
    "RangeFile",
    "pr_text",
    "repository_visibility",
    "tree_file_paths",
]
