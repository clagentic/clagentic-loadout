"""acquire.scratch — stage an AcquiredPr's fetched content to a caller-usable
scratch directory, for a scanner that needs real files on disk.

lr-c17040 comment #1 ("SECOND FACET"): a security scanner (gitleaks/semgrep/
osv-scanner) does not operate on a diff blob or an in-memory string — it
walks a directory tree. Without a local checkout, a review agent had no
writable scratch path to stage fetched PR content into, and silently
dropped to manual-only review. This module is the missing half: given an
already-fetched AcquiredPr (acquire.contract), write its diff text and/or
each changed file's post-change content into a PER-SPAWN TMPDIR directory
(repo CLAUDE.md rule 7 — "no scratch files in the tree"; never a repo-local
path, never a hardcoded machine path) that a scanner can be pointed at
directly.

TMPDIR convention: mirrors transport.body_env's own `_TMPDIR_ENV_VAR` /
per-spawn-directory pattern exactly — reads the same TMPDIR env var a
harness already provides, under a dedicated subdirectory namespaced by
owner/repo/pr_number so two concurrent acquisitions (or two callers on one
shared TMPDIR) never collide on one physical path.

TRADE-OFF NAMED (write-once snapshot vs. a live/streamed handle): this
module writes complete files rather than exposing a stream, because every
scanner this task names (gitleaks, semgrep, osv-scanner) is a subprocess
invoked against a PATH, not a library call this process could hand an
open file handle to. A streaming API would still have to materialize a
temp file for the scanner subprocess to read regardless — there is no
scanner-side streaming contract to preserve here, so the simpler
write-then-return-the-directory-path shape is the whole story, not a
partial one falling back to something more complex later.

LIFECYCLE: this module does NOT delete the staged directory itself — the
caller (whatever invokes the scanner subprocess against the returned path)
owns cleanup, exactly like every other per-spawn TMPDIR consumer in this
package (transport.body_env's staged body/stamp files are the one
exception that self-consume, because THEIR contract is single-read-then-
gone; a scanner needs the files to persist for the scanner's own process
lifetime, which this module cannot see the end of). A caller that wants
guaranteed cleanup wraps its own scanner invocation in a context manager or
equivalent using the returned path.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.acquire.contract import AcquiredPr
from clagentic_loadout.acquire.errors import ScratchWriteError

#: Same TMPDIR-env convention transport.body_env uses (CLAUDE.md rule 7 —
#: per-spawn TMPDIR only, never a repo-local or hardcoded machine path).
_TMPDIR_ENV_VAR = "TMPDIR"

#: Subdirectory name (under the resolved TMPDIR root) this module stages
#: acquired PR content into — a dedicated subdirectory, distinct from
#: transport.body_env's own "clagentic-loadout" body-staging subdirectory,
#: so a scanner-staging write can never collide with a staged review body.
_SCRATCH_SUBDIR = "clagentic-loadout-acquire"

#: Filename for the whole-PR unified diff text, under the per-PR scratch
#: directory.
_DIFF_FILENAME = "pr.diff"

#: Subdirectory (under the per-PR scratch directory) holding each changed
#: file's post-change content, mirrored under its own repo-relative path —
#: this is the directory a scanner is pointed at for a tree-shaped scan.
_FILES_SUBDIR = "files"

#: A changed filename containing '..' or resolving outside the per-PR
#: scratch root would let a maliciously-named PR file (e.g. a path with a
#: traversal segment) escape the intended write directory. Rejected instead
#: of silently normalized — a caller staging a foreign repo's file list
#: should not have to trust every path in it is well-formed.
_PATH_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")


def _resolve_tmp_root(env: dict[str, str] | None) -> Path:
    import tempfile

    active_env = env if env is not None else os.environ
    return Path(active_env.get(_TMPDIR_ENV_VAR) or tempfile.gettempdir())


def resolve_scratch_dir(
    *,
    owner: str,
    repo: str,
    pr_number: int,
    env: dict[str, str] | None = None,
) -> Path:
    """Resolve the per-PR scratch directory a scanner-staging write uses:
    `<TMPDIR>/clagentic-loadout-acquire/<owner>__<repo>__<pr_number>/`.

    Namespaced by owner/repo/pr_number so acquisitions for different PRs
    (or the same PR re-fetched later) never collide on one physical
    directory within a shared TMPDIR. Does not create the directory —
    `write_scratch_content` does that as part of the write.
    """
    safe_owner = owner.replace("/", "_")
    safe_repo = repo.replace("/", "_")
    tmp_root = _resolve_tmp_root(env)
    return tmp_root / _SCRATCH_SUBDIR / f"{safe_owner}__{safe_repo}__{pr_number}"


@dataclass(frozen=True)
class StagedScratch:
    """Where an acquisition's content landed on disk, for a scanner caller
    to point its subprocess invocation at.

    `root` is the per-PR scratch directory itself. `diff_path` is the
    whole-PR unified diff file (present whenever the acquisition had
    non-empty diff text). `files_dir` is the directory holding each changed
    file's post-change content, mirrored under its own repo-relative
    path — the tree a file/secret/dependency scanner walks. `written_files`
    lists the repo-relative paths that were actually written under
    `files_dir` (a deleted-in-this-PR file, or one with no fetched content,
    is never written — see write_scratch_content's docstring).
    """

    root: Path
    diff_path: "Path | None"
    files_dir: Path
    written_files: tuple[str, ...]


def write_scratch_content(
    acquired: AcquiredPr,
    *,
    env: dict[str, str] | None = None,
) -> StagedScratch:
    """Stage *acquired*'s diff text and each changed file's post-change
    content into a per-PR TMPDIR scratch directory (see resolve_scratch_dir)
    a scanner can be pointed at without any local git checkout.

    Only changed files carrying non-empty `content` are written under
    `files_dir` — a deleted file, a binary file the backend could not
    decode, or an acquisition fetched with `include_file_contents=False`
    (every ChangedFile.content is "" in that case) contribute nothing here;
    this is a deliberate no-op for content this module was never given,
    not a silent partial write of a fetch failure. A caller that wants
    files staged must have called `fetch_pr_content(...,
    include_file_contents=True)` first.

    Raises acquire.errors.ScratchWriteError on any filesystem failure
    (unwritable TMPDIR, permission denied, disk full) — a caller that
    cannot stage a scannable artifact must treat that as its own degraded-
    posture signal, never as a silent "nothing to scan."
    """
    scratch_root = resolve_scratch_dir(
        owner=acquired.owner, repo=acquired.repo, pr_number=acquired.pr_number, env=env
    )
    files_dir = scratch_root / _FILES_SUBDIR

    try:
        scratch_root.mkdir(parents=True, exist_ok=True)

        diff_path: Path | None = None
        if acquired.diff_text:
            diff_path = scratch_root / _DIFF_FILENAME
            diff_path.write_text(acquired.diff_text, encoding="utf-8")

        written: list[str] = []
        for cf in acquired.changed_files:
            if not cf.content:
                continue
            if _PATH_TRAVERSAL_RE.search(cf.filename) or cf.filename.startswith("/"):
                raise ScratchWriteError(
                    f"refusing to stage changed file with an unsafe path "
                    f"{cf.filename!r} (path-traversal-shaped or absolute) "
                    f"for {acquired.owner}/{acquired.repo}#{acquired.pr_number}."
                )
            dest = files_dir / cf.filename
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(cf.content, encoding="utf-8")
            written.append(cf.filename)
    except OSError as exc:
        raise ScratchWriteError(
            f"failed to stage scratch content for "
            f"{acquired.owner}/{acquired.repo}#{acquired.pr_number} at "
            f"{scratch_root}: {exc}"
        ) from exc

    return StagedScratch(
        root=scratch_root,
        diff_path=diff_path,
        files_dir=files_dir,
        written_files=tuple(written),
    )


__all__ = [
    "StagedScratch",
    "resolve_scratch_dir",
    "write_scratch_content",
]
