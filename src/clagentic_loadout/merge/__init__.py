"""merge — the release-gate verb: the full gate chain, then merge.

Wave B slice 4 (lr-885f, tome #688) + slice 4b (lr-5375) + CLI platform
dispatch (lr-9c69). THIS IS THE LOAD-BEARING RELEASE GATE — the code that
decides whether anything lands on main. Ported from the reference merge gate;
the source module stays primary until its separate CUT OVER + RETIRE +
VERIFY-GONE task per the migration plan. See
merge.verb's module docstring for the full gate chain and the identity/seam-strip
inventory.

    authority       — the merge-authority provider seam (FAIL-CLOSED):
                       AuthorityProvider protocol, StaticRoleAuthorityProvider
                       (standalone reference), check_authority().
    verdict         — the fenced ```review-result``` verdict-block contract:
                       build/parse/enforce, authorship verified by platform
                       user.login, SHA-stamp freshness enforcement. Identical
                       for both platform backends below.
    stale_sha       — refuse a merge whose --expected-head-sha has gone
                       stale against the PR's live current head.
    diff_scope      — refuse a PR whose changed-file count exceeds the
                       configured cap.
    title_gate      — Conventional Commits PR title validation, with a
                       logged --skip-title-check bypass.
    forgejo_backend — Forgejo PR reads (info/files/comments) + merge
                       execution via the redirect-hardened
                       transport.git_host_api transport.
    github_backend  — GitHub PR reads (info/files/comments) + merge
                       execution (lr-5375), mirroring forgejo_backend's shape
                       exactly — same gate-fact contract, same fail-closed
                       posture, platform-specific request shaping only. See
                       review/forgejo_backend.py + review/github_backend.py
                       for the precedent this split mirrors.
    verb            — the CLI orchestrating the full gate chain, then merge.
                       Mandatory --platform dispatch (lr-9c69) selects
                       forgejo_backend or github_backend behind a fail-closed
                       platform guard that runs BEFORE any credential mint —
                       mirroring review.verb's platform-parameterized
                       dispatch shape exactly. The gate chain itself is
                       platform-agnostic; only fact-fetching and merge
                       execution route through the resolved backend.
    errors          — the shared exception vocabulary every gate module and
                       verb.py raises from.
    merge_readback  — (lr-361de3) post-merge/post-close authoritative
                       readback: verify_merge_landed re-reads the PR via
                       get_pr_info and confirms merged==true with a
                       resolvable merge_commit_sha; verify_pr_closed confirms
                       state=="closed". Both render into the SAME
                       transport.readback_envelope.Readback shape every
                       other remote-mutating verb's envelope carries.
    close_verb      — loadout-close-pr (lr-2ba5e1): closes a PR WITHOUT
                       merging it (abandon a superseded/dead PR). A smaller,
                       close-scoped analog of verb.py's platform-aware
                       backend dispatch -- namespace guard + merge-authority
                       + platform guard/credential resolution, then
                       forgejo_backend.close_pr / github_backend.close_pr.
                       Does NOT run the merge-gate chain (stale-SHA,
                       verdicts, diff-scope, title, CI) -- closing abandons
                       a PR rather than landing it. See that module's
                       docstring for why this is a dedicated verb rather
                       than a relaxed transport.git_host_api --body-stdin
                       validator.
"""

from __future__ import annotations
