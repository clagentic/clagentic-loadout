"""merge.model_attestation — enforce a reviewer verdict's OPTIONAL
`model_attested` field on a `clean` verdict (lr-95543d).

THE PROBLEM THIS CLOSES, AND THE ONE IT DOES NOT
--------------------------------------------------
A reviewer role backed by a cheap/local model can silently substitute for
the model a deployment actually intends to run review reasoning on (its
own crew's third recorded occurrence of this failure mode: two prior fixes
elsewhere guarded CONTRACT TEXT — a model-resolution default, a call-site
placement check — while the actual failure was RUNTIME BEHAVIOR neither
text-level fix could see). This module is the third attempt at a fix, and
it is written to be honest about which half of the problem it solves.

`assert_model_attested` below CLOSES the "nobody even declared what ran"
gap: an unattested (or garbage-shaped) `clean` verdict now refuses the
merge exactly like a `blocking` one — the same disposition
`merge.verdict.assert_clean_verdict` already gives a blocking status. That
is real, and it is the change this task ships.

It does NOT close the "the declaration is a lie" gap. `model_attested` is a
field the REVIEWED AGENT WRITES INTO ITS OWN FENCE, gated by shape/denylist
checks below — there is nothing at this seam that OBSERVES which model
actually produced the review judgment, the way `transport.attestation.
resolve_identity` observes the invoking OS/session identity (a sidecar file
an external harness writes, or the OS-reported user) rather than trusting a
caller's `--caller` string alone. A carrier that wants to lie can still
write a plausible, shape-valid model string having invoked nothing.

WHY THE EXISTING TOOL-CONSTRUCTED-FENCE PROPERTY DOES NOT EXTEND HERE
-----------------------------------------------------------------------
This package's real anti-forgery property for a verdict fence is that the
REVIEWER NEVER HAND-AUTHORS IT: `merge.verdict.build_verdict_block` /
`build_findings_verdict_body` construct the JSON/markdown, and the SAME
merge-gate parser (`read_reviewer_verdict`) re-reads the landed comment
body rather than trusting the locally-built string — closing a shell/
markdown-injection surface, and (via `review.verb`'s emit-and-verify
readback) confirming the fence that was POSTED is the fence that LANDED.
That property is real and this module inherits it faithfully for
`model_attested`'s JSON *shape*: `transport.git_host_api --expect-
verdict-block` and `review.verb --verdict-review-status` /
`--verdict-findings` (see both modules' docstrings) already build every
other field the SAME way, and `build_verdict_block`'s new `model_attested`
parameter (this task) slots into that identical machinery — the fence's
JSON representation of the CLAIM is tool-constructed, never a caller-typed
backtick.

What tool construction of the FENCE cannot give this specific field is
tool OBSERVATION of the CLAIM'S TRUTH. Contrast with what `reviewer`
itself already relies on: `read_reviewer_verdict` step 1 verifies
AUTHORSHIP independently, via the git host's own `user.login` on the
comment — a fact the platform, not the commenter, asserts. There is no
platform-level, or any other externally-verifiable, fact this seam can
read that says "this HTTP POST was preceded by an invocation of model X."
The fence-building verbs (`transport.git_host_api`, `review.verb`) are
pure git-host transport tools: they open a network connection, build a
JSON body, and POST/verify a comment. Neither one, nor anything upstream
of them in this package, wraps, launches, or has visibility into the
process that ran the review's actual reasoning — that process is owned by
the calling harness/crew entirely, on the other side of this repo's own
CLAUDE.md hard rule 2 boundary ("loadout does not own agent spawning...
that's the harness's or crew's job"). `transport.attestation.
resolve_identity`'s sidecar-file mechanism comes closest to a
tool-witnessed analog (a file an EXTERNAL harness writes, that THIS
process reads rather than trusting an argv claim) — but nothing in that
chain observes which model produced a piece of reasoning either; it
observes which OS-level identity is invoking the CLI, a different fact.
A deployment-specific harness COULD write an analogous
"which model actually ran the review" sidecar at review time and have a
future loadout release read it the same way — that is a real, buildable
extension, but it requires the DISPATCHING harness (whatever spawns and
invokes the reviewing agent) to emit a tool-witnessed fact that does not
exist today, and is out of scope for this task: this package does not own
agent spawning or the reviewing process's own runtime (CLAUDE.md hard rule
2), so it has no seam to observe that fact from even if a harness started
emitting it tomorrow.

Given that, shipping this module WITHOUT saying so — presenting a
denylist as "model attestation is now enforced" — would repeat exactly the
failure a pre-merge security-review finding on a producer-side attestation
attempt named: a control that READS as protection for a claim it cannot
actually verify.

THE RESIDUAL TRUST ASSUMPTION, STATED EXPLICITLY
----------------------------------------------------
After this module ships: a reviewer role invoking the model a deployment
intends is enforced to the extent that role HONESTLY reports what ran.
`assert_model_attested` DETECTS:
  - a `clean` verdict with NO `model_attested` field at all (an unattested
    carrier, or a producer that has not adopted the field);
  - an empty/whitespace-only value;
  - a bare TIER ALIAS (e.g. "gpt-flagship", "opus", "haiku") rather than a
    resolved model identifier — a deployment's tier-routing vocabulary is
    not itself evidence of which concrete model ran;
  - a value matching a configured DENYLIST substring (e.g. a known
    fallback/degraded model name) — CASE-INSENSITIVE, SUBSTRING-safe (see
    `_value_flags_denylist_term` below for the "opus" vs "not-opus-45"
    false-positive trap this specifically guards against).

It does NOT, and cannot, detect a reviewer that runs on the wrong model
and then writes a truthful-LOOKING but false model string. THAT is the
residual trust assumption this module ships with, stated here rather than
implied: `model_attested`, even after this module's checks, is a
SELF-REPORTED claim from the same process whose honesty is in question,
strengthened by shape/denylist validation, not converted into a
platform-verified fact. Parts A+B of this task (schema field + this
module's enforcement) are DETECTION OF HONEST DEGRADATION — a reviewer
role that is CORRECTLY WIRED to run model X but has drifted onto a cheaper
fallback and truthfully reports it gets caught. They are NOT PREVENTION OF
FORGERY — a reviewer role that wants to lie about what ran still can. Any
caller reading a `clean` verdict that passed this module's check should
read it as "no HONEST admission of a disallowed model was found," never as
"a disallowed model provably did not run this review."

reviewer_required: false INTERACTION (documented, not incidental)
---------------------------------------------------------------------
A project's merge-authority config file MAY set `reviewer_required: false`
(a dispatch-side convention honored by whatever executes the merge
choosing whether to pass `--required-reviewer` at all; it is not read or
enforced anywhere in this package's own code —
see lr-63595c, the sibling task scoping that separate gap). When a
deployment omits `--required-reviewer` for a given reviewer role,
`merge.verb` never calls `merge.verdict.read_reviewer_verdict` for that
role AT ALL — no comment is fetched, no fence is parsed, and consequently
this module's `assert_model_attested` never runs either, for the same
structural reason `assert_clean_verdict` never runs: THERE IS NO VERDICT
OBJECT TO CHECK. This config key therefore does not create a
model-attestation BYPASS distinct from the existing reviewer-gate bypass —
it is the SAME bypass, one layer up, and this module inherits it rather
than introducing a new one. A deployment that wants model attestation
enforced MUST already be requiring that reviewer's verdict at all
(`--required-reviewer <role>`); attestation enforcement can never be
stricter than the reviewer-presence gate it rides on top of.
"""

from __future__ import annotations

import re

from clagentic_loadout.merge.errors import VerdictBlockingError
from clagentic_loadout.merge.verdict import ReviewerVerdict

#: Bare deployment-facing TIER ALIASES rather than a resolved model
#: identifier. This is a vocabulary a *dispatcher* uses to pick a backend
#: (e.g. "route this role to the flagship tier") — it names an intent, not
#: a fact about what actually ran, so a fence carrying only this shape is
#: refused: it cannot even in principle distinguish "the flagship model
#: really ran" from "the fallback ran and something echoed the requested
#: tier name back." Matched CASE-INSENSITIVELY against the ENTIRE
#: stripped value (not a substring match — a bare alias means the whole
#: field IS the alias, not that the alias appears somewhere inside a
#: longer resolved-model string). Defense-in-depth alongside
#: _MODEL_STRING_SHAPE_RE below, which independently rejects any value
#: with no digit at all — an unenumerated alias not in this set (a new
#: tier name this module has not been updated for yet) is still caught by
#: the shape check.
_BARE_TIER_ALIASES = frozenset(
    {
        "gpt-flagship",
        "gpt-mini",
        "gpt-spark",
        "flagship",
        "mini",
        "spark",
        "opus",
        "sonnet",
        "haiku",
    }
)

#: A resolved model identifier is expected to look like a real model
#: string: a family/version token containing at least one digit or a
#: dated/versioned suffix (e.g. "claude-opus-4-1-20250805",
#: "claude-haiku-4-5-20251001", "gpt-5.1", "gpt-5-mini-2025-08-07") rather
#: than a bare word. This is intentionally PERMISSIVE in shape (it does
#: not enumerate every real model family a deployment might report — new
#: model names ship constantly and this module must not need a code change
#: every time one does) — it exists only to reject the DEGENERATE case (a
#: bare alias, or empty/whitespace) at the format layer, before the
#: substring/denylist checks below do the real discriminating work.
_MODEL_STRING_SHAPE_RE = re.compile(r"[0-9]")


class ModelAttestationMissingError(VerdictBlockingError):
    """Raised when a `clean` verdict carries no `model_attested` field at
    all (or an empty/whitespace-only one). Mirrors
    `VerdictBlockingError`'s disposition exactly — the merge refuses, named
    distinctly from a genuinely blocking review so a caller can tell
    "the reviewer never declared what it ran" apart from "the reviewer
    found a real issue." Subclasses VerdictBlockingError (not a bare
    Exception) so an existing caller catching that type still catches
    this new failure mode without a code change — see merge.verb's
    call site."""


class ModelAttestationInvalidError(VerdictBlockingError):
    """Raised when a `clean` verdict's `model_attested` field is present
    and non-empty but fails the shape/denylist check: a bare tier alias
    (e.g. "gpt-flagship") rather than a resolved model identifier, or a
    value matching a configured denylist term (e.g. a known degraded
    fallback model name). Same disposition as
    ModelAttestationMissingError — see that class's docstring for why this
    subclasses VerdictBlockingError rather than a new exception family."""


def _value_flags_denylist_term(value_casefold: str, term_casefold: str) -> bool:
    """Return True iff *term_casefold* appears in *value_casefold* as a
    DELIMITED token, not merely as a raw substring.

    THE FALSE-POSITIVE TRAP THIS EXISTS TO CLOSE (named explicitly in the
    task this module was written for): a naive `term in value` substring
    check on the denylist term "opus" would ALSO flag a genuine, allowed
    model string that happens to CONTAIN "opus" as a sub-word of something
    else — this function requires the match to be bounded by a non-
    alphanumeric character (or the string edge) on both sides, so "opus"
    matches the bare word "opus" and "claude-opus" but does NOT match
    inside a longer alphanumeric run that merely contains the same
    letters. Conversely, a term the denylist is written to catch as a
    KNOWN-BAD FULL MODEL STRING (e.g. "claude-haiku-4-5-20251001") is
    matched via the SAME delimited-substring rule, since a caller-declared
    fence value could embed it inside slightly different surrounding text
    (extra whitespace, a wrapping quote artifact) and the denylist term
    should still be recognized as long as it appears as its own token
    run, never truncated mid-alphanumeric.
    """
    idx = value_casefold.find(term_casefold)
    while idx != -1:
        before_ok = idx == 0 or not value_casefold[idx - 1].isalnum()
        after_idx = idx + len(term_casefold)
        after_ok = after_idx == len(value_casefold) or not value_casefold[after_idx].isalnum()
        if before_ok and after_ok:
            return True
        idx = value_casefold.find(term_casefold, idx + 1)
    return False


def assert_model_attested(
    verdict: ReviewerVerdict,
    reviewer_name: str,
    *,
    denylist: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Refuse a `clean` verdict that lacks a genuine `model_attested`
    declaration. A `blocking` verdict is UNAFFECTED — it is already
    refused by `merge.verdict.assert_clean_verdict`, and a reviewer that
    correctly found a blocking issue is not additionally punished for
    which model found it.

    See this module's own docstring for the FULL trust model — in
    particular, what this function DOES detect (an absent/malformed/
    denylisted declaration) versus what it CANNOT (a false-but-plausible
    declaration from a reviewer that wants to lie). Read that docstring
    before treating a pass here as proof of anything beyond "an honest
    admission of a disallowed model was not found."

    *denylist*: caller-supplied set of additional case-insensitive terms
    to reject (e.g. a deployment's own known degraded-fallback model
    names), checked via `_value_flags_denylist_term` (delimited-substring,
    not bare `in`) ON TOP OF the built-in bare-tier-alias check
    (`_BARE_TIER_ALIASES`, an exact-match-on-the-whole-value check, not a
    substring one — see that constant's own docstring for why the two
    checks use different match strategies). Defaults to empty — a caller
    that wants ONLY the shape/bare-alias check passes nothing.

    Raises:
        ModelAttestationMissingError — verdict.review_status == 'clean'
            and model_attested is None, empty, or whitespace-only.
        ModelAttestationInvalidError — verdict.review_status == 'clean'
            and model_attested is a bare tier alias (exact match against
            _BARE_TIER_ALIASES, case-insensitive, whole-value) or matches a
            denylist term (delimited-substring, case-insensitive).

    A `blocking` verdict, or a `clean` verdict with a non-empty
    model_attested that is neither a bare alias nor denylisted, is a no-op
    (returns None) — this function makes no claim about the value being
    TRUE, only that it clears these two specific, honest-degradation-
    shaped checks.
    """
    if verdict.review_status != "clean":
        return

    raw = verdict.model_attested
    if raw is None or not raw.strip():
        raise ModelAttestationMissingError(
            f"{reviewer_name.upper()} verdict comment #{verdict.comment_id} "
            f"on PR #{verdict.pr_number} reports review_status='clean' but "
            f"carries no 'model_attested' field (or an empty one). This "
            f"gate requires a clean verdict to declare the model backend "
            f"the reviewer reports having run as. NOTE: this is a "
            f"SELF-REPORTED field, not a tool-verified fact — see "
            f"merge.model_attestation's module docstring for the full "
            f"trust model. Re-run {reviewer_name.upper()} with its "
            f"producer wired to emit model_attested, then retry the merge "
            f"gate."
        )

    value = raw.strip()
    value_casefold = value.casefold()

    if value_casefold in {alias.casefold() for alias in _BARE_TIER_ALIASES} or (
        _MODEL_STRING_SHAPE_RE.search(value) is None
    ):
        raise ModelAttestationInvalidError(
            f"{reviewer_name.upper()} verdict comment #{verdict.comment_id} "
            f"on PR #{verdict.pr_number} declares model_attested={value!r}, "
            f"a bare deployment-routing TIER ALIAS (or alias-shaped word "
            f"with no version/date marker) rather than a resolved model "
            f"identifier. A tier alias names an intent (which backend a "
            f"dispatcher asked for), not a fact about what actually ran — "
            f"a resolved model string is expected to carry at least one "
            f"digit (a version or date marker, e.g. "
            f"'claude-haiku-4-5-20251001', 'gpt-5.1'). Re-run "
            f"{reviewer_name.upper()} with its producer emitting the "
            f"RESOLVED model string it actually invoked, then retry the "
            f"merge gate."
        )

    for term in denylist:
        if _value_flags_denylist_term(value_casefold, term.casefold()):
            raise ModelAttestationInvalidError(
                f"{reviewer_name.upper()} verdict comment #{verdict.comment_id} "
                f"on PR #{verdict.pr_number} declares model_attested={value!r}, "
                f"which matches configured denylist term {term!r}. Re-run "
                f"{reviewer_name.upper()} on an allowed model backend, then "
                f"retry the merge gate."
            )


__all__ = [
    "ModelAttestationInvalidError",
    "ModelAttestationMissingError",
    "assert_model_attested",
]
