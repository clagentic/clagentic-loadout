"""merge.attestation — the lore-free merge-completion attestation comment.

Task lr-20e866 (parent lr-afba, RETIRE evidence). PROBLEM: a successful
loadout-merge left NO git-host-visible mark that IT executed a merge --
merge.verb printed a local JSON result and stopped (see that module's
docstring, "IDENTITY / SEAM STRIP FROM THE SOURCE MODULE" point 5). This
module ports ONLY the LORE-FREE half of the reference gate-note (an earlier
internal merge tool's post-merge comment) -- the task-signal half stays out
per CLAUDE.md rule 6a (lore never appears in product code); see merge.verb's
docstring point 5 for the original deferral rationale this task revisits.

CONTENT IS PURE GIT-HOST/PRODUCT DATA: tool identity + version, the gated HEAD
SHA and the SHA that actually landed, the required-reviewer logins whose
clean verdicts gated the merge, and the CI-status disposition already
computed by the gate chain. Zero lore references, zero LORE_* env vars, zero
crew vocabulary -- roles (reviewer logins), never agent names. This module's
own conformance is part of the package-wide "passes with no lore present, a
synthetic registry, invented agent names" gate (CLAUDE.md rule 6a) -- it has
no import of, or dependency on, any lore task-tracking client.

TWO WORK-ITEM LINES (lr-eb22f3): `task_id` and `issue_number` grow the body
with a generic, lore-blind pair of "both IDs on the PR" lines the earlier
lr-20e866 slice above intentionally left out (see that task's point 5,
never ported the reference module's LORE-COUPLED task-signal half). Both
are OPTIONAL and each renders independently:
  - `task_id`: the opaque work-item ref, using the SAME field name
    (`task_id`) the published envelope schema (schemas/common.json) already
    uses for this concept -- no invented "Task"/"Work-item" label, no lore
    coupling; the caller (merge.verb) passes through whatever opaque string
    its own envelope carried, or None when the invocation had none.
  - `issue_number`: the git-host issue this PR closes, parsed by the CALLER
    from the PR body's `Closes #NN` trailer (push.issue_link.
    parse_closes_issue_number) -- git-host-native, never a lore field. This
    function does not do that parsing itself (no I/O, no lore, no parsing of
    caller-supplied data beyond formatting) -- it only renders the already-
    resolved integer (or omits the line when None).

Each line is OMITTED CLEANLY (not rendered at all) when its value is
genuinely absent -- no `(not recorded)` placeholder text. This now includes
the reviewer-logins field too (lr-b6da32): earlier, an empty
`required_reviewer_logins` rendered a `(none required)` placeholder on the
theory that an empty list was a meaningful CONFIGURATION state worth
recording. In practice that framing misread as "this merge went out
unreviewed" even on merges that DID carry a clean review -- a caller-side
fix elsewhere now consistently passes real git-host logins whenever
--required-reviewer is supplied, so this module no longer needs to guess: it
reports
`Reviews: <login>, <login>` from whatever logins actually gated the merge,
and when the list is genuinely empty (no reviewer gate was configured for
this invocation) the line is omitted entirely, matching every other
optional field in this body.

FIELD/VALUE TABLE RENDERING (lr-0b77dd): the body renders as a markdown
table (`| Field | Value |`) rather than a bullet list, restoring the
presentation the retired reference module (the earlier internal merge
tool's own gate-note builder) used. This is a PURE RE-RENDER of the exact
same fields already documented
above -- no new data, no new parameter. The omit-clean behavior for
`task_id`/`issue_number`/`Reviews` is unchanged: an absent field is a
missing ROW, never a placeholdered value in the Value column.

CELL ESCAPING (pre-merge security-audit finding, lr-0b77dd): the table
format sharpens a pipe character or a newline in an interpolated value from
cosmetic (harmless on a bullet line) to STRUCTURAL -- either one breaks out
of the cell/row it was meant to render inside. `_escape_table_cell` runs on
every interpolated value (SHAs are structurally safe -- hex digests -- but
are escaped too, for one uniform rule rather than a per-field carve-out)
before it is placed inside a table cell: a pipe character is backslash-
escaped (the standard GFM/CommonMark table-cell escape, survives as literal
text rather than a second column boundary) and any run of CR/LF collapses
to a single space (a table cell cannot itself contain a real newline -- one
would either break the row or terminate the table). `required_reviewer_logins`
and `task_id` are the two fields this matters for in practice: both are
merger-role-trusted CLI input today (not attacker-reachable through this
call path), so this is a hardening measure against a future/looser caller,
not a fix for a live exploit.

NO "Authorize rationale" LINE (named trade-off, lr-0b77dd): the retired
reference gate-note's rationale line was sourced from `pre_checks_summary`,
a per-repo pre-checks config the reference module loaded and rendered a
human-readable digest of. merge.verb never loads or computes an equivalent
value -- doing so here would mean either (a) adding a new
`pre_checks_summary`-shaped parameter, reintroducing exactly the seam this
module's own docstring (and merge.verb's docstring point 6) already
deliberately left stripped, or (b) inventing a rationale string from
whichever fields happen to already be in hand, which would not be a
RATIONALE (a stated reason to authorize) so much as a restatement of the
CI-disposition/Reviews rows directly above it, adding words without adding
information. Ship the table without this line rather than take either path;
revisit only if merge.verb grows a lore-free, caller-identity-free
"why this passed" value of its own to pass through.

THIS MODULE ONLY BUILDS THE BODY STRING -- no I/O, no platform selection, no
fail-open handling. Posting (via the existing POST-and-verify comment
transport each backend already carries -- review.forgejo_backend.
post_and_verify_comment / review.github_backend.post_and_verify_review) and
the fail-open wrapping around that POST are merge.verb's responsibility
(the merge already succeeded by the time this fires; a failed attestation
POST must never fail the verb or change its exit code -- see that module's
docstring point 9 in the gate chain).
"""

from __future__ import annotations

from clagentic_loadout._version import get_version

#: Fence tag for the attestation body, mirroring merge.verdict's own fenced-
#: block convention (a stable, greppable marker for anything that wants to
#: parse this comment back out, without inventing a second markdown idiom).
ATTESTATION_HEADER = "Merged via clagentic-loadout"


def _escape_table_cell(value: str) -> str:
    """Escape a value for safe interpolation into a `| ... |` table cell.

    Pre-merge security-audit finding, lr-0b77dd: an unescaped pipe
    character splits the cell into an extra column; an unescaped newline
    breaks the row (or the table) outright. Applied uniformly to every
    interpolated value below -- SHAs are hex digests and structurally safe,
    but escaping them too keeps this a single rule rather than a per-field
    trust judgment call.
    """
    collapsed = " ".join(value.splitlines())
    return collapsed.replace("|", "\\|")


def build_attestation_body(
    *,
    gated_head_sha: str,
    merged_sha: str,
    required_reviewer_logins: "list[str] | tuple[str, ...]" = (),
    ci_disposition: str,
    task_id: str | None = None,
    issue_number: int | None = None,
    version: str | None = None,
) -> str:
    """Build the merge-completion attestation comment body.

    Pure function -- no I/O, no lore, no crew vocabulary. Every field here is
    git-host/product data already in hand by the time merge.verb reaches the
    post-merge step (see that module's `_run`, step 9):

      - `gated_head_sha`: the HEAD SHA the gate chain evaluated (merge.verb's
        `current_head_sha`, read once and reused by every gate above).
      - `merged_sha`: the SHA that actually landed. Today this is the same
        value as `gated_head_sha` (loadout-merge's merge_pr backends do not
        return a distinct post-merge commit SHA) -- kept as a SEPARATE
        parameter, not collapsed into one field, so a caller wired to a
        merge_pr response that DOES report a distinct merge-commit SHA in
        the future can pass it without this function's signature changing.
      - `required_reviewer_logins`: the resolved git-host logins (never agent
        names) of the reviewers whose clean verdicts gated this merge --
        merge.verb's own `required_reviewers.values()` at the point the
        verdict-fence gate passed. Rendered as a `Reviews:` line listing
        whatever logins actually reviewed this merge; empty when no
        reviewer-verdict gate was configured for this merge
        (--required-reviewer omitted entirely), in which case the line is
        omitted entirely rather than claiming reviews were "required".
      - `ci_disposition`: the CI-status gate's own disposition string,
        already computed by merge.ci_status / the platform CI-status
        fetchers -- e.g. "no-runner-by-design (0 commit-status entries)" or
        "combined_state='success' (N status(es), M run(s))". This function
        does not recompute or re-derive it; the caller passes through
        exactly what the CI-status gate already decided.
      - `task_id`: the opaque work-item ref this invocation's dispatch
        envelope carried (schemas/common.json's `task_id` fragment -- same
        field name, no invented label). None when the invocation had no
        task_id (an ad-hoc mode, for instance); the line is omitted
        entirely in that case, not rendered with a placeholder.
      - `issue_number`: the git-host issue number this PR closes, already
        resolved by the caller from the PR body's `Closes #NN` trailer
        (push.issue_link.parse_closes_issue_number) -- git-host-native,
        never a lore field. None when the PR body carries no such trailer;
        the line is omitted entirely in that case.
      - `version`: the clagentic-loadout package version. Defaults to
        `clagentic_loadout._version.get_version()` when omitted -- a caller
        overrides only for testing against a pinned version string.

    Returns the full comment body, ready to POST verbatim through either
    backend's existing post-and-verify comment transport. Rendered as a
    markdown field/value table (lr-0b77dd) -- see this module's docstring
    for why there is no "Authorize rationale" row. Every interpolated value
    is passed through `_escape_table_cell` first (pre-merge security-audit
    finding, lr-0b77dd) so a pipe character or a newline in a caller-
    supplied value (`required_reviewer_logins`, `task_id`) cannot break out
    of its table cell/row.
    """
    resolved_version = version if version is not None else get_version()
    rows = [
        ("Gated HEAD SHA", f"`{_escape_table_cell(gated_head_sha)}`"),
        ("Merged SHA", f"`{_escape_table_cell(merged_sha)}`"),
    ]
    if required_reviewer_logins:
        escaped_logins = [_escape_table_cell(login) for login in required_reviewer_logins]
        rows.append(("Reviews", ", ".join(escaped_logins)))
    rows.append(("CI status", _escape_table_cell(ci_disposition)))
    if task_id:
        rows.append(("task_id", _escape_table_cell(task_id)))
    if issue_number is not None:
        rows.append(("Issue", f"#{issue_number}"))

    lines = [
        f"{ATTESTATION_HEADER} v{resolved_version}",
        "",
        "| Field | Value |",
        "| --- | --- |",
    ]
    lines.extend(f"| {field} | {value} |" for field, value in rows)
    return "\n".join(lines) + "\n"


__all__ = ["ATTESTATION_HEADER", "build_attestation_body"]
