"""merge.verdict — the fenced ```review-result``` verdict-block contract.

Wave B slice 4 (lr-885f, tome #688). Ported from the reference PR-comment
gate transport (build_verdict_block / parse_verdict_block /
read_reviewer_verdict). The source module stays primary until its separate
CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

Protocol
--------
A reviewer appends a fenced verdict block to its PR comment body:

    ```review-result
    {"reviewer": "some-reviewer", "review_status": "clean", "head_sha": "<sha40>", "pr_number": 245}
    ```

Human prose may appear above or below the block; the block is the machine
contract. The fence language is ``review-result`` (exact string) and MUST be
on the same line as the opening triple-backticks.

SCOPE BOUNDARY (restated from review.contract, which this module is the
downstream counterpart of): review.contract's post_and_verify does NOT emit
or parse this fenced block — that shape is asserted HERE, by the merge gate,
not by the review-post verb. This is the one place in loadout that ASSERTS
the verdict-fence contract; a caller who wants to EMIT a conformant block
uses build_verdict_block() below and posts it via review.contract.

Verdict enforcement (read_reviewer_verdict)
--------------------------------------------
1. Among all comments whose ``user.login`` matches the reviewer's known
   bot/role login, select the most recent one that itself CONTAINS a
   parseable fenced ``review-result`` block — not merely the most recent
   comment by that login. AUTHORSHIP IS VERIFIED BY THE PLATFORM'S
   user.login FIELD — never by a claim inside the comment body text.
   "Most recent" is determined DETERMINISTICALLY by each candidate
   comment's own ``created_at`` timestamp (tie-break: comment ``id``, both
   monotonic per platform) — never by trusting the input list's order. The
   comments API response is NOT guaranteed to arrive in chronological order
   (pagination, platform-specific ordering); a resolver that just reverses
   the given list can pick an OLDER 'blocking' comment over a NEWER 'clean'
   one at the same SHA if the API happens to return them out of order
   (lr-c14a2d). A missing or unparseable ``created_at`` on any candidate
   comment (fenced or not) is a fail-closed VerdictMalformedError — never
   silently treated as "oldest" or skipped. A later comment from the same
   login that carries NO fence (a retraction, a clarification, an answer to
   a lead's question) is skipped over during selection rather than blanking
   out an earlier fenced verdict — fence-presence, not raw recency, is the
   selection key; only when NO comment from that login ever carried a fence
   does this refuse as genuinely missing.
1b. ``enforce_single_fence`` (lr-5260f9, default ON — see
   merge.post_merge_config.resolve_enforce_single_verdict_fence): refuse a
   selected comment body carrying MORE THAN ONE fenced ```review-result```
   block via assert_verdict_block_count_at_most_one rather than silently
   parsing the LAST one. A body with a 'blocking' fence followed by a
   'clean' fence resolves to 'clean' under the pre-existing last-fence-wins
   parse — a gate-bypass primitive, not a benign ambiguity — so this
   enforces by default; a repo with legacy multi-fence comments opts OUT
   explicitly (``merge: enforce_single_verdict_fence: false``) to restore
   the unconditional last-fence-wins parse.
2. Parse the fenced verdict block from that comment.
3. Enforce ``review_status != "blocking"``.
4. Enforce ``head_sha == current_pr_head_sha`` (the SHA-stamp freshness
   rule) via clagentic_loadout.sha.compare_sha_values.
5. Enforce that the block's own self-declared ``reviewer`` field matches the
   reviewer this slot is FOR (case-insensitive), via
   merge.errors.VerdictRoleMismatchError (lr-23fe19). This is
   DEFENSE-IN-DEPTH ON TOP OF step 1's user.login binding, never a
   replacement for it: the login check already establishes WHO posted;
   this check additionally establishes that the posted CONTENT is
   self-consistent with that authenticated identity. Concrete evidence this
   guards against (console PR #332, lr-f00c6f Fault 3): a security-audit
   verdict body was posted under the reviewer App's (correct) login after a
   shared body-staging path was clobbered — the login was right, but the
   fence's own 'reviewer' field still read the security auditor's name, a
   role/content mismatch a login-only check cannot see.
6. A missing comment, missing/malformed block, blocking status, stale SHA,
   or role/content mismatch all REFUSE (raise a distinct merge.errors
   exception) — never an implicit pass. Do not weaken this parse: a lenient
   parser that accepts a non-fenced, wrong-SHA, or mismatched-role verdict
   is a critical hole in the release gate.
7. A fenced verdict at a stale head never authorizes a merge, whatever else
   the reviewer has posted since. When later comments from the same login
   carry no verdict the refusal is VerdictStaleAfterCommentsError (a
   VerdictStaleError subclass, same exit family) so the operator sees that
   the reviewer commented without re-verdicting. Selection walks newest
   first, so a fresh fence is always chosen over an older stale one.
8. Findings state (merge.fence_state): the reviewer's EARLIER fences are
   walked in order, each classified by classify_earlier_fence; a finding one
   of the valid ones held open that the selected fence neither clears at its
   own head nor re-raises refuses via assert_prior_findings_resolved. An
   earlier fence that is unreadable AND may carry findings state refuses
   (VerdictMalformedError); one that is unreadable with no state, or is
   another reviewer's or PR's, is ignored with a warning.
   check_required_scanners refuses a clean verdict whose required scanner is
   reported failed, or that records no scanner outcomes while scanners are
   required. A fence with no state fields asserts nothing and is never an
   error.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from clagentic_loadout.envelope import validate_against_schema
from clagentic_loadout.merge.errors import (
    VerdictBlockingError,
    VerdictMalformedError,
    VerdictMissingError,
    VerdictPriorFindingsOpenError,
    VerdictRoleMismatchError,
    VerdictScannerFailedError,
    VerdictScannersMissingError,
    VerdictStaleAfterCommentsError,
    VerdictStaleError,
)
from clagentic_loadout.merge.fence_state import (
    KEY_CLEARED_CLAIMS,
    KEY_DROPPED,
    KEY_ENGINES,
    KEY_FINDINGS_OPEN,
    KEY_RANGE,
    SCANNER_FAILED,
    FindingsState,
    failure_sequences_of,
    has_findings_state,
    index_scanners,
    normalize_findings_state,
    raw_mentions_findings_state,
    render_engines,
    render_range,
    state_from_fence,
    unresolved_prior_findings,
    with_derived_failure_sequences,
)
from clagentic_loadout.merge.fence_syntax import find_fence_syntax
from clagentic_loadout.sha import FULL_SHA_RE, InvalidShaError, compare_sha_values, validate_sha

#: The fence language token that marks a machine-readable verdict block.
#: Emitters write: ```review-result\n{...}\n```
VERDICT_FENCE = "review-result"

#: Packaged schema for the verdict block's JSON payload (see
#: clagentic_loadout.envelope for the shared jsonschema/referencing wiring
#: this module reuses rather than hand-rolling a second field-set check).
_SCHEMAS_DIR = Path(__file__).resolve().parent.parent / "schemas"
_VERDICT_SCHEMA_PATH = _SCHEMAS_DIR / "review-result.schema.json"

#: Valid review_status values. Mirrored from the schema's enum for the
#: build-side guard in build_verdict_block() (fail fast before ever
#: constructing a block, rather than only catching it on parse).
_VALID_STATUSES = frozenset({"clean", "blocking"})

#: Regex that extracts the JSON payload from a fenced verdict block. The
#: fence LANGUAGE TAG must be on the SAME LINE as the opening triple-
#: backticks — a block whose tag is on its own line does not match and is
#: correctly treated as "no block found," not silently accepted.
_FENCE_RE = re.compile(r"```review-result\s*\n(.*?)\n```", re.DOTALL)


def _reject_fence_delimiters(value: str, finding_index: int, field_name: str) -> None:
    """Raise ValueError if *value* contains a fence-delimiter sequence.

    Fence-shaped means what merge.fence_syntax.find_fence_syntax says, the one
    definition shared with the findings-state validator; the plain word
    review-result is not fence syntax. Caller data that could open or close a
    block is rejected (fail pre-post), not escaped, because a tool-CONSTRUCTED
    body must never contain fence syntax it did not itself emit (security-audit
    finding, lr-c26110).

    Called on every findings[idx]['file'|'rule_id'|'message'] field before
    it is inserted into the body's prose/bullet lines — the only findings
    fields build_findings_verdict_body actually writes into free text
    (findings[idx]['line'] is coerced by the existing f-string but is
    caller-typed as an int per this function's documented contract, not a
    string a fence could hide inside).
    """
    offending = find_fence_syntax(value)
    if offending is not None:
        raise ValueError(
            f"findings[{finding_index}]['{field_name}'] contains the "
            f"fence-delimiter sequence {offending!r}: {value!r}. A "
            f"tool-constructed verdict body must never contain "
            f"fence-shaped syntax from caller-supplied data — this "
            f"field is rejected rather than escaped."
        )


def build_verdict_block(
    reviewer: str,
    review_status: str,
    head_sha: str,
    pr_number: int,
    model_attested: str | None = None,
    findings_state: dict[str, Any] | None = None,
) -> str:
    """Build the fenced verdict block to append to a reviewer's PR comment.

    Returns a multi-line string starting with a blank line (visual
    separation from human prose above), then the fenced block.

    *model_attested* (lr-95543d, OPTIONAL): a caller-supplied string naming
    the model backend the reviewer reports having run as, rendered on the
    'model_attested' field when given, OMITTED from the payload entirely
    when None (the pre-existing, model-attestation-unaware fence shape is
    unchanged for a caller that never passes this). THIS IS STILL A
    SELF-REPORTED CLAIM — building the fence through this tool constructs
    the JSON/markdown correctly and closes the additionalProperties:false
    rejection, but does not itself verify the string names the model that
    actually ran; see merge.model_attestation's module docstring for what
    downstream enforcement can and cannot prove about this value.

    *findings_state* (OPTIONAL): structured findings state (findings_open,
    supersedes, cleared_claims, scanners_run; see merge.fence_state). It is
    validated here and rendered INSIDE the fence with fence_schema_version 2;
    None or empty leaves the fence exactly as before. Callers pass the raw
    mapping, never a pre-rendered one.

    Raises ValueError if review_status is not 'clean' or 'blocking', or if
    *findings_state* is malformed or contradicts the status.
    """
    if review_status not in _VALID_STATUSES:
        raise ValueError(f"review_status must be 'clean' or 'blocking', got {review_status!r}")
    payload_fields: dict[str, Any] = {
        "reviewer": reviewer,
        "review_status": review_status,
        "head_sha": head_sha,
        "pr_number": pr_number,
    }
    if model_attested is not None:
        payload_fields["model_attested"] = model_attested
    payload_fields.update(
        normalize_findings_state(findings_state, head_sha=head_sha, review_status=review_status)
    )
    payload = json.dumps(payload_fields, separators=(", ", ": "))
    return f"\n```{VERDICT_FENCE}\n{payload}\n```\n"


def build_findings_verdict_body(
    reviewer: str,
    review_status: str,
    head_sha: str,
    pr_number: int,
    findings: list[dict[str, Any]],
    model_attested: str | None = None,
    findings_state: dict[str, Any] | None = None,
) -> str:
    """PRIMARY structured-body-construction mechanism (lr-c26110, operator
    reframe: 'enforce good behavior over blocking bad behavior'). Builds the
    ENTIRE PR review comment body — header, one bullet per finding, and the
    tool-owned fenced ```review-result``` block — from structured fields.
    THE REVIEWER NEVER SUPPLIES FREE-FORM PROSE to this function: there is
    no 'body'/narrative parameter here at all, by design. A foreign
    reviewer's narrative or fenced block cannot ride along in the output
    because nothing this function accepts could carry one — the good path
    is the only path (same shape as an earlier fix, lr-3b11ab: remove the
    ability to do the wrong thing rather than instruct against it).

    Extends the fence-construction build_verdict_block already owns (this
    function calls it, not a re-implementation) to own the WHOLE body, not
    just the fence appended after caller-supplied prose.

    Parameters
    ----------
    reviewer:       the fence's 'reviewer' field (also used as the header's
                     display name — uppercased for visual parity with the
                     conventional GATE-name-in-caps style existing verdict
                     messages already use, e.g. merge.verdict's own
                     assert_clean_verdict).
    review_status:  'clean' or 'blocking' — see build_verdict_block.
    head_sha:       the fence's 'head_sha' field — see build_verdict_block.
    pr_number:      the fence's 'pr_number' field — see build_verdict_block.
    findings:       a list of finding dicts, each with 'file', 'line',
                     'rule_id', and 'message' string keys. An empty list is
                     valid (a clean review with zero findings) and produces
                     a header-only body (plus the fence) with no bullets.
    model_attested: OPTIONAL — see build_verdict_block's own docstring for
                     the full contract; passed through unchanged.
    findings_state: OPTIONAL — see build_verdict_block; passed through
                     unchanged. Structured fields only, never prose. Its
                     evidence fields are also rendered into the body: a
                     "range:" and an "engine:" line under the header, and a
                     "Dropped candidates" section (counted in the header)
                     after the bullets. A finding's optional
                     'failure_sequence' string is rendered under its bullet;
                     the fence's copy of it comes from the caller's
                     findings_state ('failure_sequences').

    Returns the constructed body string: a header line, one bullet per
    finding (in the order given), then build_verdict_block's fence.

    Raises ValueError if review_status is not 'clean'/'blocking' (via
    build_verdict_block), if any finding dict is missing one of the
    required keys, or if any finding's 'file', 'rule_id', or 'message'
    field carries a fence-delimiter sequence (see
    _reject_fence_delimiters below) — a REJECT, not an escape, matching
    this module's existing fail-closed posture (security-audit finding,
    lr-c26110).
    """
    required_keys = ("file", "line", "rule_id", "message")
    # The reviewer name is written raw into the header line, so it goes through
    # the same predicate as the findings fields.
    offending_reviewer = find_fence_syntax(reviewer)
    if offending_reviewer is not None:
        raise ValueError(
            f"reviewer contains the fence-delimiter sequence {offending_reviewer!r}: "
            f"{reviewer!r}. A tool-constructed verdict body must never contain "
            f"fence-shaped syntax from caller-supplied data."
        )
    for idx, finding in enumerate(findings):
        missing = [k for k in required_keys if k not in finding]
        if missing:
            raise ValueError(
                f"findings[{idx}] is missing required field(s) {missing}: "
                f"{finding!r}. Each finding must have 'file', 'line', "
                f"'rule_id', and 'message'."
            )
        for field in ("file", "rule_id", "message"):
            _reject_fence_delimiters(finding[field], idx, field)

    for idx, finding in enumerate(findings):
        sequence = finding.get("failure_sequence")
        if sequence is None:
            continue
        if not isinstance(sequence, str):
            raise ValueError(
                f"findings[{idx}]['failure_sequence'] must be a string when present, "
                f"got {type(sequence).__name__}"
            )
        _reject_fence_delimiters(sequence, idx, "failure_sequence")

    # The fence's copy of each failure sequence is always derived from the
    # findings, replacing any staged copy, so the fence cannot contradict the
    # bullets it sits under.
    if findings_state is None or isinstance(findings_state, Mapping):
        findings_state = with_derived_failure_sequences(findings_state, findings)
    # Evidence is rendered from the same normalized fields the fence carries,
    # so the prose and the machine-readable copy are one source.
    evidence = normalize_findings_state(
        findings_state, head_sha=head_sha, review_status=review_status
    )
    dropped = evidence.get(KEY_DROPPED, [])

    status_label = "clean" if review_status == "clean" else "blocking"
    counts = f"{len(findings)} finding(s)"
    if dropped:
        counts += f", {len(dropped)} dropped"
    lines = [f"{reviewer.upper()} — {status_label} ({counts})"]
    if KEY_RANGE in evidence:
        lines.append(render_range(evidence[KEY_RANGE]))
    if KEY_ENGINES in evidence:
        lines.append(render_engines(evidence[KEY_ENGINES]))
    for finding in findings:
        lines.append(
            f"- {finding['file']}:{finding['line']} [{finding['rule_id']}] "
            f"{finding['message']}"
        )
        sequence = finding.get("failure_sequence")
        if isinstance(sequence, str) and sequence.strip():
            first, *rest = sequence.strip().splitlines()
            lines.append(f"  failure sequence: {first}")
            lines.extend(f"    {line}" for line in rest)
    if dropped:
        lines.append("")
        lines.append(f"Dropped candidates ({len(dropped)}):")
        for entry in dropped:
            lines.append(
                f"- {entry['file']}:{entry['line']} [{entry['rule_id']}] "
                f"{entry['message']} (reason: {entry['reason']})"
            )
    prose = "\n".join(lines)
    fence = build_verdict_block(
        reviewer, review_status, head_sha, pr_number, model_attested, findings_state
    )
    return f"{prose}\n{fence}"


def _parse_comment_timestamp(raw: Any) -> datetime | None:
    """Parse a git-host comment's ``created_at`` (RFC 3339 / ISO 8601) into
    an aware UTC datetime. Returns None if missing, not a string, or
    unparseable — callers must treat that as "cannot determine ordering",
    fail-closed, never as an implicit "oldest" sentinel.

    Deliberately self-contained rather than importing a sibling top-level
    package's private helper (review.github_backend._parse_github_timestamp,
    transport.git_host_api._parse_git_host_timestamp carry byte-identical
    logic already, lr-71f467 precedent) — merge, review, and transport are
    peer subpackages and this module does not reach into another layer's
    internals (rule 13).
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _extract_verdict_json(body: str) -> str | None:
    """Extract the raw JSON string from the fenced review-result block.

    Returns the JSON string, or None if no block is found. Only the LAST
    match is returned (most-recent invocation wins on retries)."""
    matches = _FENCE_RE.findall(body)
    if not matches:
        return None
    return matches[-1].strip()


def find_all_verdict_blocks(body: str) -> list[dict[str, Any]]:
    """Return every fenced ```review-result``` block found in *body*, parsed
    to a dict, in document order. Unlike parse_verdict_block (which returns
    only the LAST match — "most-recent invocation wins on retries", the
    single-caller-retry case), this is the FOREIGN-BLOCK DETECTION primitive
    (lr-c26110): a body carrying more than one block, or a block whose own
    'reviewer' field disagrees with the caller's identity, is evidence of
    another reviewer's content riding along in this comment — not a retry.

    A malformed individual block (invalid JSON, not an object, schema
    violation) is INCLUDED in the returned list as a dict with a single
    '_malformed' key holding the raw matched text, rather than raising —
    this function's job is enumeration for a caller's own closed-form
    'reject if more than one, or any foreign' check; a malformed block is
    still evidence of a second/foreign block being present even though it
    cannot itself be trusted as a well-formed verdict. Callers that need
    STRICT single-block parsing with malformed-JSON as a hard failure use
    parse_verdict_block instead.
    """
    blocks: list[dict[str, Any]] = []
    for raw in _FENCE_RE.findall(body):
        raw = raw.strip()
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            blocks.append({"_malformed": raw})
            continue
        if not isinstance(data, dict):
            blocks.append({"_malformed": raw})
            continue
        blocks.append(data)
    return blocks


def assert_verdict_block_count_at_most_one(body: str) -> None:
    """COUNT-ONLY backstop (lr-5260f9): raises VerdictMalformedError unless
    *body* contains AT MOST ONE fenced ```review-result``` block. Does not
    inspect reviewer identity or attempt to parse the surviving block —
    callers that also need the identity check use
    assert_single_own_verdict_block below (which calls this function first,
    then adds the reviewer-field assertion on top).

    Split out from assert_single_own_verdict_block so a caller that already
    has its OWN, more specific reviewer-identity check downstream (e.g.
    merge.verdict.read_reviewer_verdict's existing role-mismatch step, which
    raises the more precise merge.errors.VerdictRoleMismatchError rather than
    a generic VerdictMalformedError) can enforce the multi-fence refusal
    without a second, differently-worded identity check racing the first.

    THE DEFECT THIS CLOSES (lr-5260f9, observed against a Forgejo
    deployment): a comment body carrying a SECOND, IDENTICAL fence — e.g.
    because a caller pre-embedded its own fence in --body-stdin prose and
    transport.git_host_api's --expect-verdict-block (or review.verb's
    --verdict-review-status route) then appended a second one on top —
    validates cleanly against the pre-existing last-fence-wins parse
    (parse_verdict_block / _extract_verdict_json take matches[-1]). Both
    producer paths now REFUSE to construct that shape in the first place
    (see transport.git_host_api.build_expected_verdict_body and
    review.contract.validate_review_verdict_body_stdin_content) — this
    function is the CONSUMER-side backstop for a multi-fence body that
    reaches the gate by some other route.
    """
    blocks = find_all_verdict_blocks(body)
    if len(blocks) > 1:
        raise VerdictMalformedError(
            f"body contains {len(blocks)} fenced ```review-result``` blocks "
            f"— expected at most ONE. A body carrying more than one block "
            f"may contain a FOREIGN reviewer's content riding along "
            f"unnoticed, or a duplicate fence from a malformed producer. If "
            f"this repo carries LEGACY multi-fence comments predating this "
            f"refusal and cannot clean them up immediately, set "
            f"`merge: enforce_single_verdict_fence: false` in "
            f".clagentic/loadout/config.yaml to opt out — this restores "
            f"last-fence-wins parsing, under which a body carrying a "
            f"BLOCKING fence followed by a CLEAN fence resolves to CLEAN; "
            f"this is a legacy-comment escape hatch, not a general-purpose "
            f"way to silence this gate."
        )


def assert_single_own_verdict_block(body: str, expected_reviewer: str) -> None:
    """FOREIGN-BLOCK REJECTION BACKSTOP (lr-c26110, secondary/fail-closed
    guard beneath the primary structured-body-construction path —
    build_findings_verdict_body below). Raises VerdictMalformedError unless
    *body* contains EXACTLY ONE fenced ```review-result``` block, and that
    block's 'reviewer' field equals *expected_reviewer* exactly.

    Concrete evidence this guards against (observed against a Forgejo
    deployment, lr-f89f6f): a comment body carrying TWO fenced review-result
    blocks — one whose reviewer matched the poster's own identity, one that
    was a DIFFERENT reviewer's block riding along in the same body (a
    structural self-verify pass — 'a same-reviewer block is present' — while
    the body ALSO carried
    foreign content the self-verify never inspected). A per-field re-parse
    that only checks parse_verdict_block's LAST-match result cannot catch
    this: the last block can be correctly tagged while an earlier, foreign
    block is silently present.

    This is a BACKSTOP, not the primary mechanism: the primary fix (per
    lr-c26110's operator reframe, 'enforce good behavior over blocking bad
    behavior') is that the tool CONSTRUCTS the entire comment body from
    structured fields — a reviewer never hands this tool free-form prose,
    so there is nothing for a foreign narrative/block to hide inside. This
    check exists for any body-construction path that still accepts caller
    prose (e.g. the pre-existing --verdict-review-status route), and should
    shrink/retire as the structured route becomes the sole path.
    """
    assert_verdict_block_count_at_most_one(body)
    blocks = find_all_verdict_blocks(body)
    if len(blocks) == 0:
        raise VerdictMalformedError(
            "no fenced ```review-result``` block found in the body being "
            "verified — expected exactly one."
        )
    (block,) = blocks
    if "_malformed" in block:
        raise VerdictMalformedError(
            "the single fenced ```review-result``` block found could not "
            "be parsed as a well-formed JSON object."
        )
    block_reviewer = block.get("reviewer")
    if block_reviewer != expected_reviewer:
        raise VerdictMalformedError(
            f"the fenced ```review-result``` block's reviewer field "
            f"{block_reviewer!r} does not match the expected reviewer "
            f"{expected_reviewer!r} — a FOREIGN reviewer's block was found "
            f"in this body, riding alongside a same-reviewer block that "
            f"passed a naive structural self-verify check."
        )


@dataclass(frozen=True)
class ReviewerVerdict:
    """Parsed, enforced verdict from a reviewer's PR comment.

    model_attested (lr-95543d): the verdict block's OPTIONAL, caller-supplied
    'model_attested' field, passed through verbatim — None when the field was
    absent from the fence (an older producer, or a reviewer that has not
    adopted the field yet). This dataclass does NOT enforce anything about
    the value; enforcement (a reviewer requiring genuine attestation before a
    'clean' verdict counts) is merge.model_attestation.assert_model_attested,
    a SEPARATE, opt-in step merge.verb calls after assert_clean_verdict — see
    that module's docstring for the full trust model and its own explicit
    statement of what this field can and cannot prove.
    """

    reviewer: str
    review_status: str
    head_sha: str
    pr_number: int
    comment_id: int
    comment_author_login: str
    model_attested: str | None = None
    state: FindingsState = FindingsState()
    #: Findings an earlier fence held open that this verdict leaves
    #: unaccounted for. Enforced by assert_prior_findings_resolved.
    unresolved_prior_findings: tuple[dict[str, Any], ...] = ()
    #: The comment id this fence says it replaces, when no earlier verdict
    #: comment from the reviewer on the PR has that id. None when the fence
    #: supersedes nothing or the id resolves.
    unresolved_supersedes: int | None = None
    #: (comment id, reason) for each earlier fence the gate read past because it
    #: asserts nothing: unreadable with no findings state, or not this
    #: reviewer's or this PR's. The verb prints one warning per entry.
    ignored_earlier_fences: tuple[tuple[Any, str], ...] = ()


def parse_verdict_block(comment_body: str) -> dict[str, Any] | None:
    """Parse the fenced verdict block from a PR comment body.

    Returns the parsed dict on success, or None if no block is found.
    Raises VerdictMalformedError on malformed JSON or a schema violation
    (missing/extra field, wrong type, invalid review_status enum value).
    Caller is responsible for verifying comment authorship separately —
    this function only parses the block content.
    """
    raw = _extract_verdict_json(comment_body)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise VerdictMalformedError(f"verdict block contains invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise VerdictMalformedError(
            f"verdict block JSON is not an object: {type(data).__name__}"
        )

    errors = validate_against_schema(data, _VERDICT_SCHEMA_PATH)
    if errors:
        missing = sorted(
            f for f in ("reviewer", "review_status", "head_sha", "pr_number") if f not in data
        )
        if missing:
            raise VerdictMalformedError(f"verdict block missing required fields: {missing}")
        raise VerdictMalformedError(f"verdict block failed schema validation: {'; '.join(errors)}")
    try:
        index_scanners(data.get("scanners_run") or ())
    except ValueError as exc:
        raise VerdictMalformedError(f"verdict block failed validation: {exc}") from exc
    return data


def read_reviewer_verdict(
    comments: list[dict[str, Any]],
    expected_login: str,
    current_head_sha: str,
    pr_number: int,
    owner: str,
    repo: str,
    *,
    expected_reviewer_name: str | None = None,
    enforce_single_fence: bool = True,
) -> ReviewerVerdict:
    """Find the latest comment authored by *expected_login* and parse +
    enforce its verdict.

    Authorship is verified by ``comment["user"]["login"]`` — NEVER by the
    comment body text. An attacker who posts a comment body claiming to be
    the reviewer under a different platform account is rejected here.

    *expected_reviewer_name*, when given, is the TOOL-AUTHORITATIVE name of
    the reviewer this login slot is for (e.g. the --required-reviewer name a
    caller resolved *expected_login* from — see merge.reviewer_login). It is
    asserted (case-insensitively) against the verdict block's OWN
    self-declared 'reviewer' field as a DEFENSE-IN-DEPTH check layered ON
    TOP OF the user.login authorship binding above, never a replacement for
    it (lr-23fe19; see this module's docstring, step 5, and
    merge.errors.VerdictRoleMismatchError). Omitting this parameter (the
    default) skips the check entirely — callers that do not yet have a
    tool-authoritative reviewer name to check against are unaffected.

    *enforce_single_fence* (lr-5260f9, default True — ENFORCE-BY-DEFAULT,
    CONFIG-GATED OPT-OUT; see merge.post_merge_config.
    resolve_enforce_single_verdict_fence for the caller-side resolution and
    the full trade-off, including why this is the inverse shape of
    merge.merge_shape's warn-by-default precedent): the selected comment's
    body is checked via assert_verdict_block_count_at_most_one before
    parsing — a body carrying MORE THAN ONE fenced ```review-result``` block
    raises VerdictMalformedError rather than silently parsing the LAST match
    (parse_verdict_block's own, unconditional last-match behavior below).
    THE EVIDENCE FOR THE DEFAULT: a body carrying a 'blocking' fence
    followed by a 'clean' fence resolves to 'clean' under last-fence-wins —
    a gate-bypass primitive, not a benign ambiguity, so the default here is
    the SAFE one. Pass enforce_single_fence=False explicitly to restore the
    pre-existing, unconditional last-fence-wins parse (the documented
    per-repo opt-out is merge.post_merge_config's
    enforce_single_verdict_fence key, for a repo carrying legacy
    multi-fence comments it cannot immediately clean up). This closes the
    gap that made merge.verdict.find_all_verdict_blocks /
    assert_single_own_verdict_block dead code from the actual merge gate's
    point of view — review.verb's --verdict-findings/--verdict-review-status
    emit-and-verify routes already called assert_single_own_verdict_block on
    their OWN freshly-posted comment; this is the same count-check applied
    to the gate's READ side, covering a verdict comment however it reached
    the PR.

    Raises:
        VerdictMissingError     — no comment with matching user.login found,
                                   or NONE of that login's comments contain a
                                   fenced block (a later prose-only comment
                                   from the same login, e.g. a retraction or
                                   clarification, never triggers this on its
                                   own as long as an earlier comment from
                                   that login carries a fence — see the
                                   selection step below).
        VerdictMalformedError   — block found but JSON malformed, not an
                                   object, missing required fields, invalid
                                   review_status, a head_sha that fails
                                   40-char hex validation, or (when
                                   enforce_single_fence=True) more than one
                                   fenced block in the selected comment's body.
        VerdictStaleError       — verdict found and well-formed, but
                                   head_sha does not match current_head_sha.
        VerdictRoleMismatchError — expected_reviewer_name was given and the
                                   block's own 'reviewer' field does not
                                   match it — the authenticated App posted
                                   content belonging to a different
                                   reviewer/role.

    Returns a ReviewerVerdict on success (review_status may be 'clean' or
    'blocking' — callers enforce the blocking refusal separately via
    assert_clean_verdict, matching the reference module's step ordering).
    """
    matching_comments = [
        comment
        for comment in comments
        if comment.get("user", {}).get("login", "") == expected_login
    ]

    if not matching_comments:
        raise VerdictMissingError(
            f"No PR comment from reviewer login {expected_login!r} found on "
            f"PR #{pr_number} in {owner}/{repo}. The reviewer must post a "
            f"comment containing a fenced ```{VERDICT_FENCE}``` verdict "
            f"block."
        )

    # DETERMINISTIC "latest verdict supersedes" ORDERING (lr-c14a2d): sort
    # every same-reviewer comment by its own created_at, tie-broken by
    # comment id (both monotonic per platform) — never by trusting the
    # input list's position. The git-host comments API is not guaranteed to
    # return comments in chronological order; reversing the given list and
    # taking the first match (an earlier implementation) could land on an
    # OLDER 'blocking' comment instead of a NEWER 'clean' one at the same
    # SHA when the API returns them out of order. Multiple same-reviewer
    # comments are NORMAL supersede behavior here — a reviewer legitimately
    # re-posts a corrected verdict — never treated as an anomaly (contrast
    # assert_single_own_verdict_block, which is about multiple verdict
    # BLOCKS inside one comment BODY, a different concern this loop does not
    # touch).
    #
    # SELECTION is a SEPARATE step from ordering: pick the most recent
    # comment that itself CONTAINS a fenced block, not merely the most
    # recent comment by this login. A reviewer's later PROSE-ONLY comment
    # (a retraction, a clarification, an answer to a lead's question) does
    # not blank out an earlier, still-valid fenced verdict — the fence is
    # the gate's atomic unit of validation, not comment recency by itself.
    # created_at validation still runs over EVERY matching comment
    # (including fenceless ones) before selection, so a comment with an
    # unparseable timestamp still fails closed rather than being silently
    # skipped because it lacks a fence.
    parsed_timestamps: list[tuple[datetime, int, dict[str, Any]]] = []
    for comment in matching_comments:
        created_at = _parse_comment_timestamp(comment.get("created_at"))
        if created_at is None:
            raise VerdictMalformedError(
                f"PR comment id={comment.get('id')!r} from reviewer login "
                f"{expected_login!r} on PR #{pr_number} in {owner}/{repo} "
                f"has a missing or unparseable 'created_at' timestamp. "
                f"Deterministic latest-verdict selection requires a valid "
                f"created_at on every candidate comment — this fails "
                f"closed rather than guessing an order."
            )
        comment_id = comment.get("id", 0)
        parsed_timestamps.append((created_at, comment_id, comment))

    parsed_timestamps.sort(key=lambda entry: (entry[0], entry[1]))
    ordered_comments = [entry[2] for entry in reversed(parsed_timestamps)]

    # Walk newest-first and select the first candidate whose body carries at
    # least one fenced block. A candidate with ZERO fences (prose-only) is
    # skipped over rather than selected — it is not "the verdict," it is
    # silence, and silence does not supersede an earlier fenced verdict. A
    # candidate with fences goes through the existing multi-fence backstop
    # (lr-5260f9) and parse exactly as before; that per-comment enforcement
    # is unchanged by this loop (see lr-cb8db9's double-fence handling).
    matching_comment: dict[str, Any] | None = None
    selected_index = 0
    for position, candidate in enumerate(ordered_comments):
        candidate_body = candidate.get("body", "")
        if _FENCE_RE.search(candidate_body) is None:
            continue
        matching_comment = candidate
        selected_index = position
        break

    if matching_comment is None:
        raise VerdictMissingError(
            f"No PR comment from reviewer login {expected_login!r} on PR "
            f"#{pr_number} in {owner}/{repo} contains a fenced "
            f"```{VERDICT_FENCE}``` verdict block. The reviewer must embed "
            f"the verdict block in one of their PR comments."
        )

    body = matching_comment.get("body", "")
    comment_id = matching_comment.get("id", 0)
    author_login = matching_comment.get("user", {}).get("login", "")

    # lr-5260f9: multi-fence refusal, enforced by default, checked BEFORE
    # the ordinary last-fence-wins parse below so a caller never silently
    # accepts a malformed, duplicate-fence body unless it has explicitly
    # opted out. See this parameter's own docstring above and
    # merge.post_merge_config.resolve_enforce_single_verdict_fence for why
    # this defaults to on.
    if enforce_single_fence:
        assert_verdict_block_count_at_most_one(body)

    # parse_verdict_block cannot return None here: matching_comment was
    # selected above specifically because _FENCE_RE matched its body, and
    # parse_verdict_block's own extraction uses the same regex.
    verdict_data = parse_verdict_block(body)
    assert verdict_data is not None

    review_status = verdict_data.get("review_status", "")
    if review_status not in _VALID_STATUSES:
        raise VerdictMalformedError(
            f"Verdict from {expected_login!r} on PR #{pr_number} has "
            f"invalid review_status={review_status!r}. Expected 'clean' or "
            f"'blocking'."
        )

    # Validate the verdict's hand-carried SHA stamp BEFORE any compare: a
    # malformed or truncated SHA stamp is a caller (reviewer) bug, not a
    # stale-gate condition, and must fail closed as such rather than
    # silently entering compare_sha_values()'s abbreviated-prefix matching.
    verdict_sha = verdict_data.get("head_sha", "")
    try:
        validate_sha(verdict_sha, allow_abbreviated=False)
    except InvalidShaError as exc:
        raise VerdictMalformedError(
            f"Verdict from {expected_login!r} on PR #{pr_number} has a "
            f"malformed head_sha stamp: {exc}. This is a malformed-input "
            f"failure, not a stale-gate condition — the reviewer must "
            f"re-run and post a valid 40-character hex SHA stamp."
        ) from exc

    later_comments = ordered_comments[:selected_index]
    if not compare_sha_values(verdict_sha, current_head_sha) and later_comments:
        # Same refusal as a plain stale verdict, named precisely: the
        # selected fence is the newest one, so every later comment from this
        # login carries no verdict. Never relaxed: the older-head verdict
        # still does not authorize this head.
        raise VerdictStaleAfterCommentsError(
            f"STALE GATE DATA — the last verdict from {expected_login!r} on PR "
            f"#{pr_number} in {owner}/{repo} is at SHA {verdict_sha!r}, but "
            f"current PR HEAD is {current_head_sha!r}, and that reviewer has "
            f"posted {len(later_comments)} comment(s) since without a verdict "
            f"block. Commenting is not re-verdicting. The reviewer must post "
            f"a new verdict at the current HEAD, then retry the merge gate."
        )
    if not compare_sha_values(verdict_sha, current_head_sha):
        raise VerdictStaleError(
            f"STALE GATE DATA — verdict from {expected_login!r} on PR "
            f"#{pr_number} in {owner}/{repo} was evaluated at SHA "
            f"{verdict_sha!r} but current PR HEAD is {current_head_sha!r}. "
            f"The branch advanced after the reviewer ran. Re-run the "
            f"reviewer at the current HEAD, then retry the merge gate."
        )

    # DEFENSE-IN-DEPTH role/content consistency (lr-23fe19): the App-login
    # binding above already authenticated WHO posted this comment. This is a
    # SEPARATE assertion that WHAT they posted is self-consistent with that
    # identity — the block's own 'reviewer' field must name the reviewer
    # this slot is for. A mismatch means the authenticated App carried
    # another reviewer/role's content (console PR #332 shape: a
    # security-audit body posted under the reviewer App's correct login
    # after a shared body-staging path was clobbered). Case-insensitive:
    # build_findings_verdict_body's header uppercases the display name but
    # the fence's 'reviewer' field itself is caller-supplied free text, so
    # this check compares on identity, not on a specific casing convention.
    verdict_reviewer = verdict_data.get("reviewer", "")
    if (
        expected_reviewer_name is not None
        and verdict_reviewer.casefold() != expected_reviewer_name.casefold()
    ):
        raise VerdictRoleMismatchError(
            f"ROLE/CONTENT MISMATCH — the verdict comment on PR #{pr_number} "
            f"in {owner}/{repo} was posted by the authenticated login "
            f"{expected_login!r} (required reviewer {expected_reviewer_name!r}), "
            f"but its fenced ```{VERDICT_FENCE}``` block's own 'reviewer' "
            f"field is {verdict_reviewer!r} — a DIFFERENT reviewer's content "
            f"was posted under this App. This does not weaken the "
            f"user.login authorship check above (which already passed); it "
            f"is an additional gate-integrity failure on top of it. Re-run "
            f"{expected_reviewer_name!r} itself so its own verdict content "
            f"lands under its own App, then retry the merge gate."
        )

    current_state = state_from_fence(verdict_data)
    valid_earlier, ignored_earlier = _earlier_fence_states(
        ordered_comments[selected_index + 1 :][::-1],
        pr_number=pr_number,
        expected_reviewer_name=expected_reviewer_name or verdict_reviewer,
    )
    unresolved = unresolved_prior_findings(
        [(fence.head, fence.state) for fence in valid_earlier],
        current_state,
        verdict_sha,
    )

    # Supersession is provenance, not resolution, but a claim to replace a
    # comment this reviewer never posted a valid verdict in is worth
    # surfacing: it means the history the fence describes is not the history
    # on the PR. Only the VALID earlier fences are targets, the same list the
    # findings carry-forward above reads, so the two can never disagree.
    earlier_verdict_ids = {fence.comment_id for fence in valid_earlier}
    unresolved_supersedes = (
        current_state.supersedes
        if current_state.supersedes is not None
        and current_state.supersedes not in earlier_verdict_ids
        else None
    )

    return ReviewerVerdict(
        reviewer=verdict_reviewer,
        review_status=review_status,
        head_sha=verdict_sha,
        pr_number=int(verdict_data.get("pr_number", 0)),
        comment_id=comment_id,
        comment_author_login=author_login,
        model_attested=verdict_data.get("model_attested"),
        state=current_state,
        unresolved_prior_findings=tuple(unresolved),
        unresolved_supersedes=unresolved_supersedes,
        ignored_earlier_fences=tuple(
            (fence.comment_id, fence.reason) for fence in ignored_earlier
        ),
    )


#: classify_earlier_fence dispositions.
EARLIER_FENCE_VALID = "valid"
EARLIER_FENCE_IGNORED = "ignored"
EARLIER_FENCE_UNREADABLE_STATEFUL = "unreadable_stateful"


@dataclass(frozen=True)
class EarlierFence:
    """How the gate reads one earlier comment of the reviewer's that carries a
    fence. *head* and *state* are set only for a VALID fence; *reason* says why
    a fence was not VALID (empty for VALID)."""

    comment_id: Any
    kind: str
    head: str = ""
    state: FindingsState = FindingsState()
    reason: str = ""


def classify_earlier_fence(
    comment: dict[str, Any], *, pr_number: int, reviewer_name: str
) -> EarlierFence | None:
    """Decide what one EARLIER comment of the reviewer's contributes to the gate.
    The only place that does; _earlier_fence_states applies it to every earlier
    comment and nothing else filters them, so findings carry-forward and
    supersession targets cannot disagree about which fences are readable.

    Two axes decide the disposition. Readable or not: the fence parses, passes
    the schema, has no repeated scanner, is the only fence in its comment, has
    well-formed heads. Stateful or not: it carries findings state (see
    merge.fence_state.has_findings_state; for JSON that does not parse, a scan
    of the raw text for the state key names, which fails closed). An unreadable
    fence that may have held a finding open must refuse, because skipping it
    would silently drop the finding. An unreadable fence with no state asserts
    nothing and can clear nothing, so it is ignored with a warning rather than
    wedging every later merge on the PR.

    Checked in this order, first match wins:

      no fenced block in the comment            -> None (not a verdict)
      any fence's JSON does not parse into an
        object                                  -> raw scan: UNREADABLE_STATEFUL
                                                   if it names a state key,
                                                   else IGNORED
      every fence is another reviewer's or
        another PR's                            -> IGNORED
      more than one fence in the comment        -> UNREADABLE_STATEFUL if stateful,
                                                   else IGNORED
      schema error, repeated scanner, or a
        malformed findings_open / cleared_claims
        head                                    -> same split
      malformed head_sha                        -> same split
      otherwise                                 -> VALID

    The newest fence, the one being judged, is not read through here: it stays
    strict and any defect in it refuses.
    """
    comment_id = comment.get("id")
    raws = [raw.strip() for raw in _FENCE_RE.findall(comment.get("body") or "")]
    if not raws:
        return None

    parsed: list[dict[str, Any] | None] = []
    for raw in raws:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        parsed.append(data if isinstance(data, dict) else None)
    stateful = any(
        raw_mentions_findings_state(raw) if data is None else has_findings_state(data)
        for raw, data in zip(raws, parsed)
    )

    def unreadable(reason: str) -> EarlierFence:
        kind = EARLIER_FENCE_UNREADABLE_STATEFUL if stateful else EARLIER_FENCE_IGNORED
        return EarlierFence(comment_id, kind, reason=reason)

    if any(data is None for data in parsed):
        return unreadable("its fence is not a parseable JSON object")

    foreign_reasons = [
        _foreign_reason(data, pr_number, reviewer_name) for data in parsed if data is not None
    ]
    if all(foreign_reasons):
        return EarlierFence(comment_id, EARLIER_FENCE_IGNORED, reason=foreign_reasons[0])

    if len(raws) > 1:
        return unreadable(f"its comment carries {len(raws)} fences, expected one")
    (data,) = [d for d in parsed if d is not None]

    errors = validate_against_schema(data, _VERDICT_SCHEMA_PATH)
    if errors:
        return unreadable(f"its fence failed schema validation: {'; '.join(errors)}")
    try:
        index_scanners(data.get("scanners_run") or ())
    except ValueError as exc:
        return unreadable(f"its fence failed validation: {exc}")
    fence_head = data["head_sha"]
    try:
        validate_sha(fence_head, allow_abbreviated=False)
    except InvalidShaError as exc:
        # Its claims are matched against its head, so an unreadable head could
        # otherwise clear or supersede a finding by matching another malformed
        # value.
        return unreadable(f"it has a malformed head_sha stamp ({fence_head!r}): {exc}")
    for key in (KEY_FINDINGS_OPEN, KEY_CLEARED_CLAIMS):
        for entry in data.get(key) or ():
            if not FULL_SHA_RE.match(entry["head"]):
                return unreadable(
                    f"its {key} entry {entry.get('id')!r} has a malformed head ({entry['head']!r})"
                )
    return EarlierFence(
        comment_id, EARLIER_FENCE_VALID, head=fence_head, state=state_from_fence(data)
    )


def _foreign_reason(data: dict[str, Any], pr_number: int, reviewer_name: str) -> str:
    """Why *data* is not this reviewer's fence for this PR, or "" when it may
    be. A missing or mistyped reviewer / pr_number is a schema error, not a
    foreign fence, so it returns "" and is judged by the schema check."""
    reviewer = data.get("reviewer")
    if isinstance(reviewer, str) and reviewer.casefold() != reviewer_name.casefold():
        return f"its fence names another reviewer ({reviewer!r})"
    fence_pr = data.get("pr_number")
    if type(fence_pr) is int and fence_pr != pr_number:
        return f"its fence is for another PR (#{fence_pr})"
    return ""


def _earlier_fence_states(
    earlier_oldest_first: list[dict[str, Any]],
    *,
    pr_number: int,
    expected_reviewer_name: str,
) -> tuple[list[EarlierFence], list[EarlierFence]]:
    """Classify the reviewer's EARLIER comments, oldest first, returning
    (valid fences, ignored fences). The one filter over earlier comments; see
    classify_earlier_fence for the disposition table.

    Raises VerdictMalformedError naming every comment whose fence is
    unreadable and may have held findings open. Ignored fences are returned so
    the caller can surface each one; they are never dropped silently.
    """
    valid: list[EarlierFence] = []
    ignored: list[EarlierFence] = []
    unreadable: list[EarlierFence] = []
    for comment in earlier_oldest_first:
        fence = classify_earlier_fence(
            comment, pr_number=pr_number, reviewer_name=expected_reviewer_name
        )
        if fence is None:
            continue
        {
            EARLIER_FENCE_VALID: valid,
            EARLIER_FENCE_IGNORED: ignored,
            EARLIER_FENCE_UNREADABLE_STATEFUL: unreadable,
        }[fence.kind].append(fence)
    if unreadable:
        listed = "; ".join(f"comment #{f.comment_id!r}: {f.reason}" for f in unreadable)
        raise VerdictMalformedError(
            f"{len(unreadable)} earlier verdict comment(s) from {expected_reviewer_name!r} on "
            f"PR #{pr_number} cannot be read and may hold findings open ({listed}). An "
            f"unreadable fence cannot clear or supersede any finding, and the findings history "
            f"cannot be read past it; the reviewer must re-run and post a valid verdict."
        )
    return valid, ignored


def assert_clean_verdict(verdict: ReviewerVerdict, reviewer_name: str) -> None:
    """Refuse the merge if *verdict*'s review_status is 'blocking'.

    Split out from read_reviewer_verdict so a caller that wants the parsed
    verdict for logging/audit before deciding whether to enforce it can do
    so; the merge verb always calls this immediately after
    read_reviewer_verdict succeeds.

    Raises merge.errors.VerdictBlockingError on a blocking verdict.
    """
    if verdict.review_status == "blocking":
        raise VerdictBlockingError(
            f"{reviewer_name.upper()} BLOCKED merge — review_status is "
            f"'blocking' in verdict comment #{verdict.comment_id} from "
            f"{verdict.comment_author_login!r} on PR #{verdict.pr_number}. "
            f"The gate found a blocking issue that must be resolved. Fix "
            f"the issue, re-run {reviewer_name.upper()} (which will update "
            f"the verdict block), then retry the merge gate."
        )


def assert_prior_findings_resolved(verdict: ReviewerVerdict, reviewer_name: str) -> None:
    """Refuse the merge when an earlier fence's open finding has no
    resolution at this verdict's head, or when this clean verdict itself
    still lists findings open.

    A version 1 fence, or any history with no findings state, has nothing to
    carry forward and passes.

    Raises merge.errors.VerdictPriorFindingsOpenError naming each finding id.
    """
    if verdict.unresolved_prior_findings:
        listed = ", ".join(
            f"{f['id']} (rule {f['rule_id']}, raised at {f['head']})"
            for f in verdict.unresolved_prior_findings
        )
        raise VerdictPriorFindingsOpenError(
            f"{reviewer_name.upper()} left finding(s) open: {listed}. The verdict on PR "
            f"#{verdict.pr_number} at {verdict.head_sha!r} (comment #{verdict.comment_id}) "
            f"neither clears them with evidence at this head nor re-raises them. A "
            f"clean verdict at a later head does not resolve an earlier blocker. "
            f"Re-run {reviewer_name.upper()} so each is adjudicated, then retry the merge gate."
        )
    if verdict.review_status == "clean" and verdict.state.findings_open:
        ids = ", ".join(f["id"] for f in verdict.state.findings_open)
        raise VerdictPriorFindingsOpenError(
            f"{reviewer_name.upper()} posted a clean verdict (comment #{verdict.comment_id}) "
            f"that still lists open finding(s): {ids}. A verdict that holds findings "
            f"open is blocking."
        )


def check_required_scanners(
    verdict: ReviewerVerdict, reviewer_name: str, required_scanners: "tuple[str, ...]"
) -> list[str]:
    """Check a verdict's scanner outcomes against the scanners the
    deployment requires for this reviewer, and return warnings to surface.

    Refuses when the deployment declares required scanners and the verdict
    records no scanner outcomes at all (VerdictScannersMissingError), or when
    a required scanner is reported failed (VerdictScannerFailedError).
    not_applicable and not_invoked, with their reason, are legitimate. With no
    required scanners declared, absence only warns. A required scanner the
    verdict does not record, even beside others it does record, also refuses
    (VerdictScannersMissingError): a declared gate gates.
    A repeated scanner name is malformed (VerdictMalformedError), never
    last-write-wins.
    """
    try:
        reported = index_scanners(verdict.state.scanners_run)
    except ValueError as exc:
        raise VerdictMalformedError(
            f"{reviewer_name.upper()} verdict comment #{verdict.comment_id} is malformed: {exc}"
        ) from exc
    if required_scanners and not verdict.state.scanners_run:
        raise VerdictScannersMissingError(
            f"{reviewer_name.upper()} verdict (comment #{verdict.comment_id} on PR "
            f"#{verdict.pr_number}) records no scanner outcomes, but this deployment requires "
            f"scanner(s) {list(required_scanners)!r} for it. A clean verdict without a "
            f"scanners_run record is not coverage. Have the reviewer re-post it with each "
            f"scanner recorded (ran, or not_applicable/not_invoked with a reason), then retry "
            f"the merge gate."
        )
    for scanner in required_scanners:
        entry = reported.get(scanner)
        if entry is not None and entry["status"] == SCANNER_FAILED:
            raise VerdictScannerFailedError(
                f"{reviewer_name.upper()} reports required scanner {scanner!r} as failed "
                f"({entry.get('reason', 'no reason given')}) in verdict comment "
                f"#{verdict.comment_id} on PR #{verdict.pr_number}. A failed scanner is not "
                f"coverage. Re-run it, or have the reviewer record it as not_applicable or "
                f"not_invoked with a reason, then retry the merge gate."
            )
    unrecorded = [scanner for scanner in required_scanners if scanner not in reported]
    if unrecorded:
        raise VerdictScannersMissingError(
            f"{reviewer_name.upper()} verdict (comment #{verdict.comment_id} on PR "
            f"#{verdict.pr_number}) records no outcome for required scanner(s) "
            f"{unrecorded!r}. A declared scanner requirement is not met by a partial "
            f"record. Have the reviewer re-post it with each required scanner recorded "
            f"(ran, or not_applicable/not_invoked with a reason), then retry the merge gate."
        )
    warnings: list[str] = []
    if verdict.unresolved_supersedes is not None:
        warnings.append(
            f"{reviewer_name!r} verdict (comment #{verdict.comment_id}) says it supersedes "
            f"comment #{verdict.unresolved_supersedes}, which is not an earlier verdict "
            f"comment from that reviewer on this PR"
        )
    if not verdict.state.scanners_run:
        warnings.append(
            f"{reviewer_name!r} verdict (comment #{verdict.comment_id}) is clean but "
            f"records no scanner outcomes; it may rest on judgment alone"
        )
    return warnings


__all__ = [
    "VERDICT_FENCE",
    "assert_prior_findings_resolved",
    "check_required_scanners",
    "EARLIER_FENCE_IGNORED",
    "EARLIER_FENCE_UNREADABLE_STATEFUL",
    "EARLIER_FENCE_VALID",
    "EarlierFence",
    "ReviewerVerdict",
    "assert_clean_verdict",
    "assert_single_own_verdict_block",
    "assert_verdict_block_count_at_most_one",
    "build_findings_verdict_body",
    "build_verdict_block",
    "classify_earlier_fence",
    "find_all_verdict_blocks",
    "parse_verdict_block",
    "read_reviewer_verdict",
]
