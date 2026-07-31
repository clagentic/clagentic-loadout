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
from typing import Protocol, runtime_checkable


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

    @property
    def changed_filenames(self) -> list[str]:
        """Plain filename list — the same shape merge.diff_scope.
        check_diff_scope already expects, so a caller enforcing a
        changed-file-count cap does not need to re-derive it from
        ``changed_files``."""
        return [cf.filename for cf in self.changed_files]


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


__all__ = [
    "AcquireBackend",
    "AcquiredPr",
    "ChangedFile",
]
