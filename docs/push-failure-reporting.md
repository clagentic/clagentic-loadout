# Push-failure reporting contract

Back to [README.md](README.md).

This documents `loadout-push`'s (`clagentic_loadout.push.git_push` +
`clagentic_loadout.push.errors.GitPushError`) failure-reporting contract: what is always
carried on a failed push, what the coarse `sub_cause` classification does and does not
mean, and the redaction guarantee.

## The always-carried-transcript guarantee

Every `GitPushError` this package raises carries the **full raw `git push` stderr**
(`GitPushError.raw_stderr`), independent of whether classification succeeded. A caller
that only reads `str(exc)` still gets the transcript folded into the message (bounded to a
generous character limit for display; the full text is always on `raw_stderr` itself).

**Classification is ADDITIVE METADATA, never a replacement for the transcript.**
`GitPushError.sub_cause` states which coarse bucket the classifier placed a failure in —
it is a claim about what the *classifier* could determine, not a claim about what the
*remote* did. A caller (a build agent, a dispatcher, a human reading a bounced task)
**must not branch on `sub_cause` alone** as the sole diagnostic signal; the fields below
are the authoritative evidence, and `sub_cause` is a coarse index into them.

## The `sub_cause` label set

Defined once, in `clagentic_loadout.push.push_failure_labels.SUB_CAUSE_LABELS` (a frozen
enum-like set — the classifier's return value is always a member of this set, and a test
in this package's own suite fails if a new label is added without a covering fixture):

| Label | Meaning |
|---|---|
| `pre-receive-rejected` | The remote sent lines back (`remote: `-prefixed sideband) — a server-side hook/policy rejection, or a server-side ref-lock race that surfaced over sideband. |
| `local-hook-rejected` | A **local** `.git/hooks/pre-push` script aborted the push before any network negotiation. |
| `non-fast-forward` | A client-side conflict against remote state the local repo does not yet have — covers git's `non-fast-forward`, `fetch first`, **and** `stale info` (a `--force-with-lease` rejection against a stale local remote-tracking ref) literals; all three are the same coarse shape from an operator's perspective. |
| `bad-refspec` | The local refspec itself does not resolve to a real ref (`error: src refspec ... does not match any`) — a usage/argument-shape defect, never a remote or local-hook rejection. |
| `transport` | A connection-level failure — the push never reached a remote to negotiate with at all. |
| `other-reject-reason` | The parser found a per-ref reject-reason parenthetical this taxonomy does not (yet) name a bucket for — the raw reason string is still carried verbatim on `GitPushError.reject_reason`. |
| `unknown` | The classifier found no reject-reason parenthetical, no remote lines, no local-hook lines, and no recognized transport substring. States "the classifier could not name a cause" — **never** "the remote said nothing," which would be an unproven (and sometimes flatly false) narrative. |

## Reject-reason parsing, not substring guessing

Git states a client-observed per-ref rejection's reason in a fixed, parseable position:
the parenthetical in

```
! [rejected]        <refspec> (<reason>)
! [remote rejected] <refspec> (<reason>)
```

The classifier lifts that reason **verbatim** (`GitPushError.reject_reason`) rather than
substring-matching the whole stderr blob for a hand-enumerated list of known phrases. This
is exhaustive by construction over every reason git's porcelain output can produce — a new
git version adding a new reason literal is still parsed correctly, because the parser
reads the *position*, not an enumerated string list. An earlier substring-matching
classifier shipped two "fixes" that both missed real failure shapes (a lease-staleness
rejection classified as `unknown`, and a bad-refspec failure confidently mislabeled as
`local-hook-rejected`) — see [push.git_push's module
docstring](../src/clagentic_loadout/push/git_push.py) for the full account. Adding a
fourth or fifth substring branch to that construction would have preserved the exact
failure mode; the reject-reason parser replaces the construction itself.

## Other fields on `GitPushError`

- `exit_code` — the `git push` subprocess's exit code.
- `remote_lines` / `local_hook_lines` — the extracted remote-sideband / local-pre-push-hook
  content, when present.
- `reached_transport` — whether git printed its client-side destination line (`To
  <remote>`) — the real did-we-reach-transport signal (absence of remote sideband on a
  locally-detected non-fast-forward or lease failure is *expected* and carries zero
  information; this field is what actually answers "did the push get far enough to talk
  to a remote at all").
- `remote` / `refspec` — the resolved remote name and refspec this push attempted.
- `lease_forced` / `lease_origin` — whether `--force-with-lease` was in effect for this
  push and where that decision came from (see `loadout-push`'s own `--force-with-lease`/
  `--no-force-with-lease` reference in [verbs.md](verbs.md)) — never silently inferred.

## The redaction guarantee

Redaction is **structural**, not conventional: `GitPushError.__init__` itself calls
`clagentic_loadout.push.push_redaction.redact_push_secrets` on every string-bearing field —
the message, `raw_stderr`, `reject_reason`, and every entry of `remote_lines`/
`local_hook_lines` — before any of them is ever stored on `self`. A caller constructing a
`GitPushError` passes `known_secrets=(token,)` (the literal token value(s) it holds) and
gets an already-redacted object back; there is no separate "remember to redact before
constructing" step, and a second construction site anywhere in this package inherits the
same guarantee automatically rather than needing to reimplement it. This function masks:
the minted token by exact value, URL userinfo (`scheme://user:secret@host`),
`Authorization:`/`Credential:` header values, `Bearer`/`token=`-shaped literals as defense
in depth, and (unconditionally, regardless of any secret-shaped match) ANSI escape
sequences plus every C0 control character, `DEL` (0x7F), and the full C1 range
(U+0080–U+009F) other than `\t`/`\n`/`\r` (kept, since this module's own multi-line
messages rely on them for formatting) — remote-controlled text (a `remote: `-prefixed line,
a parsed reject-reason string) reaches operator-visible stderr, and a malicious remote must
not be able to inject terminal escapes or other control sequences into it. This operates on
already-decoded `str` text, never raw bytes: stripping the C1 codepoint range is safe only
post-decode (each `str` character in that range is an unambiguous control codepoint, never a
byte fragment) — at the byte level, `0x80`–`0x9F` are continuation bytes inside legitimate
multi-byte UTF-8 sequences, and a naive byte-level strip would corrupt genuine non-ASCII
content in a remote message. This matters especially for the opt-in `GIT_TRACE` passthrough
— reachable via `loadout-push`'s discoverable `--verbose`/`--trace` flag, or via the
`CLAGENTIC_LOADOUT_PUSH_GIT_TRACE` env var it keeps working as a compat alias for (see
[verbs.md](verbs.md#loadout-push--bot-attributed-commit-push--pr-openupdate)) — which dumps
git's own packet/hook/transport trace: verbose output that can otherwise surface a
credential-helper invocation's headers. The SAME redaction pass documented on this page
applies to that output before it can reach a raised message, stdout, or stderr — there is no
second, unredacted path for trace output to reach a caller.

**`--dry-run`'s transcript is redacted through this SAME choke point, not a second one.** A
`--dry-run` push (see [verbs.md](verbs.md#loadout-push--bot-attributed-commit-push--pr-openupdate))
prints its stdout+stderr transcript via `push.push_redaction.redact_push_secrets` with the
resolved token passed as a known secret — the identical function every other field on this
page already goes through, called at the identical two sites (`GitPushError.__init__` for a
failing push, and `git_push.git_push_with_token`'s own dry-run branch for a successful one)
rather than a new, divergently-implemented redaction pass for this one surface.

**The pre-lease fetch is credentialed, not ambient.** `push.lease_control.resolve_lease`
fetches the target branch's remote-tracking ref (see below) via
`push.git_push.git_fetch_with_token` — the SAME `GIT_ASKPASS` + isolated-`HOME` envelope
`git_push_with_token` itself uses (`push.git_push._credentialed_git_env`, a single shared
primitive both functions build their subprocess call on top of) — never a bare ambient
`git fetch` that would authenticate via whatever credential helper happens to be configured
on the host. A fetch failure's message is redacted before it is ever raised
(`GitFetchError`), so the caller (`resolve_lease`) folds it directly into a printed warning
with no second redaction step.

## The CLI failure envelope

Everything above documents `GitPushError`'s own fields for a caller that imports this
package and catches the exception directly. A caller that instead shells out to the
`loadout-push` CLI (`clagentic_loadout.push.verb`) receives the identical evidence over two
stderr channels on `EXIT_PUSH_FAILED`, never only the coarse `sub_cause` label:

1. The human-readable line `push: git push failed (exit N, <sub_cause>): ...` — this already
   folds in the extracted `REMOTE MESSAGE`/`LOCAL PRE-PUSH HOOK MESSAGE` blocks verbatim (see
   the label table above), so a caller reading plain stderr text sees the hook's or remote's
   own message, not merely the classification.
2. Immediately following it, one JSON line on stderr (never stdout — stdout carries JSON only
   on a successful push) with these keys:

   | Key | Meaning |
   |---|---|
   | `sub_cause` | Same value as `GitPushError.sub_cause`. |
   | `exit_code` | Same value as `GitPushError.exit_code`. |
   | `reached_transport` | Same value as `GitPushError.reached_transport`. |
   | `reject_reason` | Same value as `GitPushError.reject_reason` (may be `null`). |
   | `remote_lines` | Same content as `GitPushError.remote_lines`, as a JSON array. |
   | `local_hook_lines` | Same content as `GitPushError.local_hook_lines`, as a JSON array. |

   Every value here is already redacted through the same choke point described above
   (`GitPushError.__init__` redacts before this envelope is ever built) — a caller consuming
   this JSON gets no less redaction than a caller reading the plain-text line.

**Why this exists:** prior to this fix, the CLI's structured output was success-only —
`_run_create_pr`/`_run_update_pr` print a JSON envelope on `EXIT_OK`, but a push failure
produced no JSON at all, only the plain-text line above. A caller that parsed stdout/stderr
as JSON (rather than scraping text) received nothing on failure beyond the exit code — which
is exactly the gap that sent one investigation into a wrong subsystem (see the module
docstring in `push.git_push`, "REJECT-REASON PARSER, NOT A SUBSTRING CLASSIFIER", for the
sibling defect class this belongs to).

## Integrator guidance

- Read `raw_stderr` (or the formatted `str(exc)`, which always contains it) when a caller
  needs to actually understand a failure — `sub_cause` is a search key, not the answer.
- Do not build automation that treats `sub_cause == "unknown"` as "no information was
  available" — the transcript is always there; `unknown` states only that the classifier
  could not name a bucket.
- See [verbs.md](verbs.md#loadout-push--bot-attributed-commit-push--pr-openupdate) for the
  `--force-with-lease`/`--no-force-with-lease` CLI control and the printed
  resolved-lease-state contract, and the troubleshooting note on `(stale info)` below.
- When a failure's classified message still isn't enough: re-run with `--dry-run` (a
  read-only push attempt through the same minted credential path, no ref updated on the
  remote) and/or `--verbose`/`--trace` (git's own verbose output plus a GIT_TRACE
  passthrough) — see [verbs.md](verbs.md#loadout-push--bot-attributed-commit-push--pr-openupdate)
  for both flags. This is the sanctioned substitute for shelling out to raw git under an
  ambient credential.

## Troubleshooting: `(stale info)`

A `--force-with-lease` push rejected with `! [rejected] <ref> (stale info)` means the
lease was evaluated against a **local remote-tracking ref that is out of date** relative
to the actual remote — the remote has moved since this process last saw it. **Git itself
prints no `hint:` block for this case** (unlike a plain `fetch first` rejection, which gets
five explanatory `hint:` lines) — from the raw git output alone, this is the single
least-explained rejection shape git produces. Classified as `non-fast-forward` (see the
label table above); `loadout-push` fetches the remote-tracking ref before evaluating a
forced lease by default (see `push.lease_control`), so this should now be rare — if it
still occurs, it means the remote moved again in the narrow window between that fetch and
the push itself, or lease-forcing was requested via `--force-with-lease` on a repo state
this process never refreshed. Re-run after a fresh `git fetch` of the target branch.
