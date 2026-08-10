"""push.push_failure_labels — the single source of truth for `git_push`'s
sub_cause classification vocabulary (lr-f57f13, D4 DECIDED).

BEFORE THIS MODULE: `_classify_push_failure` returned bare inline string
literals ("pre-receive-rejected", "local-hook-rejected", "non-fast-forward",
"transport", "unknown") with no enumerable set anywhere — a corpus test
asserting "every label has a covering fixture" had nothing to enumerate
against except re-reading the classifier's own source. Hoisting the labels
here is a HARD PRECONDITION for that self-policing test
(tests/test_push_git_push.py's corpus test walks `SUB_CAUSE_LABELS` and
fails if any label lacks a fixture) — the taxonomy's own growth becomes
self-policing rather than trusted to the next author's memory.

CLASSIFICATION IS ADDITIVE METADATA (see this task's own invariant,
lr-f57f13 seq 2): a caller must never branch on one of these labels alone
as the sole diagnostic signal — the raw transcript (`GitPushError.raw_stderr`)
and the parsed reject reason (`GitPushError.reject_reason`) are always
carried alongside it, and are the authoritative evidence. A label answers
"which coarse bucket did the classifier place this in," never "what did the
remote/local system actually say."
"""

from __future__ import annotations

#: A transport-level authentication failure where the credential is
#: WELL-FORMED but rejected -- dead/expired/revoked, wrong scope, wrong
#: password. git prints its own host-independent "fatal: Authentication
#: failed for '<url>'" line for this shape regardless of platform. Forgejo
#: (and other hosts) ALSO prefix their HTTP 401 response body with
#: "remote: " -- structurally indistinguishable from a genuine pre-receive/
#: policy rejection by the remote-line-presence check alone. Checked BEFORE
#: the remote-lines branch in _classify_push_failure so an auth failure is
#: never mislabeled as a branch-protection gate: the two labels have
#: disjoint remediations (rotate the credential vs. edit the branch/policy)
#: and disjoint bounce targets. Distinct from SUB_CAUSE_MALFORMED_TOKEN
#: below: both arrive via git's identical "Authentication failed" transport
#: shape (git does not distinguish WHY a 401 fired), but "the credential
#: itself is well-formed and just needs replacing" and "the credential-
#: minting/broker path is producing a structurally broken token" have
#: different remediations and different bounce targets (rotate a live
#: credential vs. fix the minting path that produced this one) --
#: _is_malformed_token_failure is checked FIRST specifically so a malformed
#: shape is never absorbed into this bucket.
SUB_CAUSE_AUTH_FAILED = "auth-failed"

#: A transport-level authentication failure where the SERVER-SIDE credential
#: validator repudiates the credential's SHAPE (malformed/garbled token,
#: wrong number of JWT segments, a parse failure) rather than a scope/
#: expiry/revocation denial of an otherwise well-formed credential. Arrives
#: via the SAME git-transport "fatal: Authentication failed for '<url>'"
#: shape as SUB_CAUSE_AUTH_FAILED (lr-91bac6, comment #1: git's own HTTP-401
#: handling does not distinguish WHY the server returned 401 -- confirmed
#: against a real Gitea/Forgejo-shaped server: malformed-token and
#: expired-token responses are BYTE-IDENTICAL at the git-transport level,
#: both HTTP 401 -> "fatal: Authentication failed"). No git-transport-level
#: or HTTP-status-level marker distinguishes the two causes; RFC 7235/9110
#: do not mandate a distinguishing status code either (both are 401). The
#: only observable signal is the server's OWN body text (relayed via
#: "remote: " lines), which is unavoidably vendor-specific in its EXACT
#: wording -- but "malformed"/"invalid" credential-shape vocabulary is
#: itself the RFC 7519 JSON Web Token structural-validation term of art
#: (three dot-separated segments), not a Forgejo-only coinage, so this is
#: anchored on that generic vocabulary rather than any single vendor's
#: literal phrasing. This is a NAMED TRADE-OFF, not a silent Forgejo
#: hardcode: a host whose malformed-token body text uses neither
#: "malformed" nor "invalid ... segment"/"invalid ... token" wording will
#: fall through to SUB_CAUSE_AUTH_FAILED rather than this label -- a
#: same-family miss (both still name "your credential is the problem, go
#: fix the credential"), never a false pre-receive-rejected/branch-
#: protection label, which is the harm this task exists to eliminate.
SUB_CAUSE_MALFORMED_TOKEN = "malformed-token"

#: Remote sent lines back (server-side hook/policy output, "remote: "
#: prefixed) — includes a pre-receive hook decline AND a "cannot lock ref"
#: race, both of which the remote reports via sideband.
SUB_CAUSE_PRE_RECEIVE_REJECTED = "pre-receive-rejected"

#: A LOCAL pre-push hook aborted the push before any network negotiation —
#: unprefixed content ahead of git's own summary line, with no "remote: "
#: lines at all.
SUB_CAUSE_LOCAL_HOOK_REJECTED = "local-hook-rejected"

#: Client-side, non-fast-forward-shaped rejection with a parsed per-ref
#: reject reason recognized as a known non-fast-forward variant — covers
#: git's "fetch first" AND "stale info" literals (the lr-f57f13 lease-
#: staleness bug: previously only "fetch first"/"non-fast-forward" matched a
#: substring, and "stale info" — a DISTINCT THIRD literal git emits for a
#: --force-with-lease rejection, with NO hint block at all — fell through to
#: "unknown").
SUB_CAUSE_NON_FAST_FORWARD = "non-fast-forward"

#: The local refspec itself does not resolve to a real ref ("src refspec ...
#: does not match any") — a usage/argument-shape defect (e.g. the branch
#: loadout resolved does not exist locally under that exact name), never a
#: remote or local-hook rejection. Previously misclassified as
#: local-hook-rejected (lr-f57f13 bug 3: WORSE than unknown, a confidently
#: FALSE label — this shape has no "To " line and no hook actually ran).
SUB_CAUSE_BAD_REFSPEC = "bad-refspec"

#: Connection-level failure — could not reach the host at all. No "To "
#: line, no sideband: the push never got far enough to negotiate with a
#: server-side hook.
SUB_CAUSE_TRANSPORT = "transport"

#: The reject-reason parser found a per-ref parenthetical reason git does
#: not (yet) have a named bucket for. Distinct from SUB_CAUSE_UNKNOWN: this
#: means classification succeeded at finding A reason, just not one this
#: taxonomy names yet -- the raw reason string is still carried verbatim in
#: `GitPushError.reject_reason`.
SUB_CAUSE_OTHER_REJECT_REASON = "other-reject-reason"

#: The classifier found no reject-reason parenthetical, no remote lines, no
#: local-hook lines, and no recognized transport-failure substring --
#: genuinely could not name a cause. ALWAYS carries the full raw transcript
#: (see push.errors.GitPushError) -- see this task's own AMENDMENT 1/
#: REJECTED-ALTERNATIVE decision for why this label reports observed FACTS
#: (exit code, byte count, "To <url>" presence) rather than a guessed
#: narrative about why the remote was silent.
SUB_CAUSE_UNKNOWN = "unknown"

#: The full enumerable set every classifier return value must be a member
#: of -- the self-policing corpus test in tests/test_push_git_push.py walks
#: this frozenset and fails if any label lacks a covering fixture case.
SUB_CAUSE_LABELS: frozenset[str] = frozenset(
    {
        SUB_CAUSE_AUTH_FAILED,
        SUB_CAUSE_MALFORMED_TOKEN,
        SUB_CAUSE_PRE_RECEIVE_REJECTED,
        SUB_CAUSE_LOCAL_HOOK_REJECTED,
        SUB_CAUSE_NON_FAST_FORWARD,
        SUB_CAUSE_BAD_REFSPEC,
        SUB_CAUSE_TRANSPORT,
        SUB_CAUSE_OTHER_REJECT_REASON,
        SUB_CAUSE_UNKNOWN,
    }
)

__all__ = [
    "SUB_CAUSE_AUTH_FAILED",
    "SUB_CAUSE_BAD_REFSPEC",
    "SUB_CAUSE_LABELS",
    "SUB_CAUSE_LOCAL_HOOK_REJECTED",
    "SUB_CAUSE_MALFORMED_TOKEN",
    "SUB_CAUSE_NON_FAST_FORWARD",
    "SUB_CAUSE_OTHER_REJECT_REASON",
    "SUB_CAUSE_PRE_RECEIVE_REJECTED",
    "SUB_CAUSE_TRANSPORT",
    "SUB_CAUSE_UNKNOWN",
]
