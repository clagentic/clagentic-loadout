"""acquire — platform-agnostic PR diff/content-acquisition entrypoint.

lr-c17040 (tome #687 EPIC E). Given a PR ref, fetches the correct
merge-base..head diff and/or changed-file list from the HOST API — never a
local working tree, so wrapper-cwd and stale-local-main are non-issues by
construction. Mirrors review's "one contract, two transports" shape:

    contract         — AcquireBackend Protocol + AcquiredPr/ChangedFile
                        result shapes.
    errors           — PlatformMismatchError / AcquireFetchError /
                        ScratchWriteError, shared by both backends.
    forgejo_backend  — Forgejo/Gitea REST transport (pulls/{n},
                        pulls/{n}/files, pulls/{n}.diff, contents/{path}).
    github_backend   — GitHub REST transport (pulls/{n} in both JSON and
                        .v3.diff media-type forms, pulls/{n}/files,
                        contents/{path}).
    scratch          — stage a fetched PR's diff + changed-file content to
                        a per-spawn TMPDIR scratch directory a security
                        scanner can be pointed at without a local checkout
                        (lr-c17040 comment #1).
    verb             — the acquire CLI: role-parameterized (--caller),
                        platform-parameterized (--platform, mandatory).

SCOPE BOUNDARY: this package is READ-ONLY — it never posts a comment/review
(that is review.verb's job) and never touches the merge gate's verdict-block
contract (merge.verdict).
"""

from __future__ import annotations

from clagentic_loadout.acquire.contract import AcquireBackend, AcquiredPr, ChangedFile
from clagentic_loadout.acquire.errors import (
    AcquireFetchError,
    PlatformMismatchError,
    ScratchWriteError,
)
from clagentic_loadout.acquire.forgejo_backend import ForgejoAcquireBackend
from clagentic_loadout.acquire.github_backend import GithubAcquireBackend
from clagentic_loadout.acquire.scratch import StagedScratch, resolve_scratch_dir, write_scratch_content
from clagentic_loadout.acquire.verb import (
    EXIT_FETCH_FAILED,
    EXIT_OK,
    EXIT_SCRATCH_WRITE_FAILED,
    EXIT_TOKEN_FETCH_FAILED,
    EXIT_USAGE,
    EXIT_WRONG_PLATFORM,
    assert_platform_is_forgejo,
    assert_platform_is_github,
    build_backend,
    main,
)

__all__ = [
    "EXIT_FETCH_FAILED",
    "EXIT_OK",
    "EXIT_SCRATCH_WRITE_FAILED",
    "EXIT_TOKEN_FETCH_FAILED",
    "EXIT_USAGE",
    "EXIT_WRONG_PLATFORM",
    "AcquireBackend",
    "AcquireFetchError",
    "AcquiredPr",
    "ChangedFile",
    "ForgejoAcquireBackend",
    "GithubAcquireBackend",
    "PlatformMismatchError",
    "ScratchWriteError",
    "StagedScratch",
    "assert_platform_is_forgejo",
    "assert_platform_is_github",
    "build_backend",
    "main",
    "resolve_scratch_dir",
    "write_scratch_content",
]
