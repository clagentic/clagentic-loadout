"""guard.bash_admission — containment/scratch/scope admission layer for a
Bash-command guard (lr-288fad, port of the reference deployment's
guard-bash.py containment/scope layer, reference ll.1400-2560; lr-5a8d epic,
guard-bash decomposition slice 2 of 3).

Builds on `guard.shell_parsing` (slice 1, lr-7ff55e) for every structural
parse and reuses `guard.scratch_policy` (already-landed PR #110) for the
$TMPDIR containment boundary (lr-f8649f: narrowed from $TMPDIR/$HOME to
$TMPDIR-only — see that module's own docstring "TMPDIR-ONLY NARROWING")
rather than forking a second copy — see "RECONCILIATION" below, an explicit
acceptance criterion for this slice (lr-5a8d comment #7 item 2, task
lr-288fad CRITICAL RECONCILE).

WHAT THIS MODULE PORTS (reference guard-bash.py ll.1400-2560):

  - `detect_project_tree_write_targets` / `is_fd_safe_target` — enumerate
    every non-staging write-redirect target in a command, so a caller's
    project-tree scope check (`guard.write_scope.check_write_scope`) can be
    applied to a Bash command's redirect targets exactly as it already
    applies to a Write/Edit call's `file_path` (reference
    `_detect_project_tree_write_targets` / `_is_fd_safe_target`,
    ll.1706-1755 / ll.3935-3941).
  - `is_admitted_loadout_family_readonly` — the generic `loadout-<verb>`
    read/validate family grant, mutating-verb-name excluded (reference
    `_is_admitted_loadout_family_readonly`, ll.2420-2494).
  - `requires_admission_flag` — the generic "POST/DELETE to a specific API
    path shape requires a specific flag present as a genuine shell word"
    belt-and-suspenders check the reference duplicates per verb pair
    (`--verify-comment` on a comments-path POST, `--delete-own-comment` on a
    single-comment DELETE — reference
    `_forgejo_curl_comments_post_requires_verify_comment` /
    `_loadout_git_host_api_comments_post_requires_verify_comment` /
    `_loadout_git_host_api_delete_own_comment_requires_flag`). Parameterized
    once here instead of three near-identical copies.
  - `is_admitted_body_stdin_pipe` / `classify_body_stdin_pipe_ambiguity` —
    the loadout-verb `--body-stdin` PIPE CARVE-OUT (INHERITED ITEM 1 below).

INHERITED ITEM 1 — the loadout-verb `--body-stdin` pipe carve-out (task
lr-288fad comment seq 2, deferred from lr-7ff55e): the reference
`_compound_check` admits exactly one narrow pipe shape —
`echo|printf|cat | loadout-<verb> [--body-stdin]` for three specific
transport verbs (`loadout-git-host-api`, `loadout-review-post`,
`loadout-stage-body`) — via `_is_admitted_loadout_body_stdin_pipe` /
`_classify_body_stdin_pipe_ambiguity`. `guard.shell_parsing.compound_check`
deliberately excludes this (parsing core stays policy-free — see that
module's docstring point 2). THIS is the transport-admission slice the
carve-out belongs in: `is_admitted_body_stdin_pipe` is built on
`shell_parsing.split_segments` / `normalize_shell_words` / `cmd_head`
exactly as the reference's own version is, generalized to a caller-supplied
`BodyStdinVerb` registry instead of three hardcoded verb names/regexes — a
caller wires its own installed-verb names and install paths in, this module
never hardcodes a specific binary name or absolute path (CLAUDE.md rule 1).
A caller's own `compound_check`-wrapping admission function checks
`is_admitted_body_stdin_pipe` FIRST (mirroring the reference's own
`_compound_check` composition order) and only falls through to
`shell_parsing.compound_check` when it returns False.

INHERITED ITEM 2 — the security-reviewer forward nit (PR #113 comment
15824): `shell_parsing.has_unresolved_ansi_c_quote` must be INVOKED
wherever this slice builds a verb-matcher that falls back to scanning a RAW
(non-normalized) string on ambiguity — an unresolvable ANSI-C ($'...') span
still hides its wrapped verb from a raw-text scan exactly as it would from a
normalized one, so a bare "fall back to raw" without also checking this
function is a live evasion gap, not merely a missed convenience. Every
verb-matching function in this module that has a raw-fallback branch
(`is_admitted_loadout_family_readonly`, `requires_admission_flag`,
`is_admitted_body_stdin_pipe`, `classify_body_stdin_pipe_ambiguity`) checks
`has_unresolved_ansi_c_quote` on that branch and hard-denies rather than
scanning through an unresolved ANSI-C wrapper. See each function's
docstring for its specific wiring.

RECONCILIATION with `guard.scratch_policy` (PR #110, task's CRITICAL
RECONCILE): the reference's containment layer (ll.1492-1704) implements TWO
narrow, verb-specific mkdir/redirect containment bridges
(`_is_contained_scratch_mkdir_target` / `_is_admitted_scratch_mkdir`, and a
literal `/tmp/clagentic-loadout` fallback pair) that its OWN module comment
(reference ll.1396-1423) documents as a temporary pre-port BRIDGE that "must
be deleted, not kept as a second, divergent allowlist" once "the loadout
guard port lands." `guard.scratch_policy.is_scratch_contained` (already
landed, PR #110) IS that port: a general, VERB-CATEGORY (not mkdir-only)
containment grant with the same realpath-canonicalization /
symlink-and-`..`-escape-safe boundary comparison the reference's bridge
uses, generalized across `SCRATCH_SAFE_VERBS` instead of `mkdir` alone. This
module deliberately does NOT reimplement `_is_contained_scratch_mkdir_target`
or the literal `/tmp/clagentic-loadout` fallback pair — a caller wanting
scratch-mkdir/redirect containment calls `guard.scratch_policy.
is_scratch_contained` directly (already superior to the reference's
narrower bridge: it covers `touch`/`mv`/`cp`/`rm`/`mktemp`/`rmdir`/`ln`/
`chmod` too, not only `mkdir`). Nothing in the reference's ll.1400-2560
containment surface needed EXTENDING beyond what `scratch_policy` already
provides; the only genuinely new capability this module adds beyond
`scratch_policy`'s existing boundary is the FIXED-uid-home-fallback edge
case (reference `_uid_home_fallback`/lr-b3a7bf) — already ported into
`scratch_policy.resolve_scratch_boundary`'s own fallback branch (lr-f8649f:
repointed from `HOME`'s unset-fallback to `TMPDIR`'s, since `HOME` is no
longer a scratch root at all — see that function's docstring) — so no gap
remains to backfill here.

NOT PORTED HERE (per-role allow-checkers, lr-19ae42):
  - The eleven `_is_allowed_<agent>` per-agent functions and the
    director/lead mutation checker (reference ll.2564-5554) — the bulk of
    the file, role-keyed per CLAUDE.md rule 1, tracked as lr-19ae42. This
    module supplies the containment/scope PRIMITIVES those checkers call
    into (`detect_project_tree_write_targets`,
    `is_admitted_loadout_family_readonly`, `requires_admission_flag`,
    `is_admitted_body_stdin_pipe`) — it does not itself decide which role
    may invoke which verb.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from clagentic_loadout.guard.shell_parsing import (
    cmd_head,
    has_unresolved_ansi_c_quote,
    is_staging_redirect_target,
    normalize_shell_words,
    split_segments,
)

#: A write-redirect target token, quote-normalized-scan input (mirrors the
#: reference's `_WRITE_REDIRECT_TARGET_RE`, reference l.1374).
_WRITE_REDIRECT_TARGET_RE = re.compile(r">>?\s*(\S+)")


def is_fd_safe_target(target: str) -> bool:
    """True when *target* is `/dev/null` or an fd-duplication redirect
    (`&<digit>`) — no file is actually written, so it is never a candidate
    project-tree mutation (reference `_is_fd_safe_target`, ll.3935-3941).
    """
    if target == "/dev/null":
        return True
    if target.startswith("&") and len(target) >= 2 and target[1:].isdigit():
        return True
    return False


def detect_project_tree_write_targets(command: str) -> list[str]:
    """Return every unquoted write-redirect target in *command* that is
    NOT `$TMPDIR` staging (`shell_parsing.is_staging_redirect_target` —
    lr-f8649f: `$HOME` dropped, `$TMPDIR`-only as of that narrowing)
    and not an fd-safe no-op form (`is_fd_safe_target`) — every OTHER
    target is a candidate project-tree mutation a caller should run through
    its own scope check (`guard.write_scope.check_write_scope`).

    Checks ALL redirect targets, not just the first (reference
    `_detect_project_tree_write_targets` docstring, a security-review sast
    finding): bash's actual last-redirect-wins semantics mean any one of
    multiple `>`/`>>` targets could be where a write actually lands, so a
    caller must deny on ANY out-of-scope target, not just the first one
    found. Order is preserved (left-to-right as they appear in the
    command).

    Quote-aware: uses `shell_parsing.normalize_shell_words` so a
    redirect-looking substring inside an isolated quoted argv span (data,
    not a real redirect) is never reported as one, while a quote-glued
    redirect (`>""/workspace/evil`) is still correctly recognized. On
    ambiguity (unparseable quoting), falls back to scanning the raw head —
    deny-on-ambiguity is the CALLER's responsibility once it sees a
    project-tree-shaped target returned here; this function's own job is
    only enumeration, not the allow/deny decision.
    """
    head = cmd_head(command)
    scan_target = normalize_shell_words(head)
    if scan_target is None:
        scan_target = head  # deny-on-ambiguity: unparseable quoting, scan raw
    targets: list[str] = []
    for match in _WRITE_REDIRECT_TARGET_RE.finditer(scan_target):
        target = match.group(1)
        if is_staging_redirect_target(target):
            continue
        if is_fd_safe_target(target):
            continue
        targets.append(target)
    return targets


# ---------------------------------------------------------------------------
# Loadout-family readonly admission (reference `_is_admitted_loadout_family_
# readonly`, ll.2420-2494) — generalized: a caller supplies its own
# sanctioned bin dir and mutating-verb exclusion set instead of a hardcoded
# `/root/.local/bin` + fixed verb-name list (CLAUDE.md rule 1: no
# operator-path hardcodes in product code).
# ---------------------------------------------------------------------------


def _family_bare_re(mutating_verb_names: frozenset[str]) -> re.Pattern[str]:
    excluded_alt = "|".join(re.escape(name) for name in sorted(mutating_verb_names))
    return re.compile(
        r"^(?!(?:" + excluded_alt + r")(?:\s|$))"
        r"loadout-[a-z0-9][a-z0-9-]*(\s|$)"
    )


def _family_install_path_re(
    sanctioned_bin_dir: str, mutating_verb_names: frozenset[str]
) -> re.Pattern[str]:
    excluded_alt = "|".join(re.escape(name) for name in sorted(mutating_verb_names))
    return re.compile(
        r"^(?!" + re.escape(sanctioned_bin_dir) + r"/(?:"
        + excluded_alt + r")(?:\s|$))"
        + re.escape(sanctioned_bin_dir) + r"/loadout-[a-z0-9][a-z0-9-]*(\s|$)"
    )


_FAMILY_MODULE_RE = re.compile(
    r"^python3\s+-m\s+clagentic_[a-z0-9_]+(?:\.[A-Za-z0-9_]+)*(\s|$)"
)


def is_admitted_loadout_family_readonly(
    command: str,
    *,
    mutating_verb_names: frozenset[str],
    sanctioned_bin_dir: str | None = None,
) -> bool:
    """Return True iff *command* is a clagentic-brand READ+VALIDATE tool
    invocation: the bare basename or (if *sanctioned_bin_dir* is given) fixed
    absolute install-path form of a non-mutating `loadout-<name>` tool, OR
    the `python3 -m clagentic_*` module-namespace form (reference
    `_is_admitted_loadout_family_readonly`, ll.2420-2494).

    *mutating_verb_names* is a caller-supplied exclusion set (e.g.
    `{"loadout-merge", "loadout-push", ...}`) — a verb in this set is NEVER
    admitted through this family grant regardless of spelling, only through
    its own explicit gate. This is a NEGATIVE-lookahead exclusion, so a new
    read/validate `loadout-<name>` tool is admitted automatically without an
    edit here; only a NEW MUTATING verb requires updating the caller's
    exclusion set.

    *sanctioned_bin_dir* is optional (no hardcoded default — CLAUDE.md rule
    1): a caller that only ever sees bare-basename invocations (PATH-based
    dispatch) can omit it, in which case only the bare-basename and
    `python3 -m` forms are recognized.

    ANSI-C evasion (INHERITED ITEM 2, security-reviewer PR #113 comment
    15824): this is a verb-matcher with an implicit raw-fallback posture
    (the regexes above scan `cmd_head(command)` directly, with no
    `normalize_shell_words` pre-pass of their own, mirroring the
    reference's own un-normalized bare-basename matchers) — an
    unresolvable ANSI-C span
    (`$'\\x6coadout-doctor'`-shaped) could otherwise hide a mutating verb's
    TRUE identity from the negative-lookahead exclusion while still reading
    as a plausible `loadout-<name>` bare token to a naive downstream
    matcher. `has_unresolved_ansi_c_quote` is checked FIRST and hard-denies
    before any of the family regexes run, closing that gap.
    """
    head = cmd_head(command)
    if normalize_shell_words(head) is None and has_unresolved_ansi_c_quote(head):
        return False
    if _family_bare_re(mutating_verb_names).match(head):
        return True
    if sanctioned_bin_dir is not None and _family_install_path_re(
        sanctioned_bin_dir, mutating_verb_names
    ).match(head):
        return True
    return bool(_FAMILY_MODULE_RE.match(head))


# ---------------------------------------------------------------------------
# Generic "METHOD to PATH requires FLAG" admission (reference's three
# near-identical --verify-comment / --delete-own-comment belt-and-suspenders
# checks: `_forgejo_curl_comments_post_requires_verify_comment`,
# `_loadout_git_host_api_comments_post_requires_verify_comment`,
# `_loadout_git_host_api_delete_own_comment_requires_flag`) — the reference's
# "comment-post verify-id" checks referenced in this task's scope are this
# shape (there is no distinct concept named "verify-id" in the reference;
# the fixed-flag-on-comments-endpoint belt-and-suspenders IS what that scope
# phrase names). Parameterized once instead of three copies.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MethodPathFlagRule:
    """One "METHOD to a PATH shape requires FLAG present" admission rule.

    *method* is matched case-insensitively as a standalone shell word
    (mirrors the reference's case-insensitive POST/DELETE match, a
    security-review nit: a tool's own argv-parsing upper-cases the method
    token before comparing, so the guard must agree). *path_pattern*
    is matched anywhere in the normalized command (a compiled regex, e.g.
    an API path shape like `/issues/\\d+/comments\\b`). *required_flag* is
    the exact flag token that must be present as a standalone shell word for
    the invocation to be admitted.
    """

    method: str
    path_pattern: re.Pattern[str]
    required_flag: str
    deny_reason: str


def requires_admission_flag(
    command: str,
    *,
    verb_pattern: re.Pattern[str],
    rule: MethodPathFlagRule,
) -> tuple[bool, str]:
    """Return (ok, reason) for *rule* applied to *command*.

    ok=True (no denial) when:
      - `command` does not match *verb_pattern* at all (this check has no
        opinion on a non-matching command; a caller's other allowlist rules
        decide those);
      - the invocation is not `rule.method` to a `rule.path_pattern` path
        (rule.required_flag remains optional for every other method/path
        combination, unchanged);
      - the invocation IS that method/path shape AND `rule.required_flag`
        is present as a genuine shell word.

    ok=False when the invocation is that method/path shape and the flag is
    NOT present — belt-and-suspenders with a tool-level hard-refuse a
    caller's own verb implementation applies independently (mirrors the
    reference's `--verify-comment`/`--delete-own-comment` pairing with
    `scripts/forgejo-curl`'s own `EXIT_VERIFY_COMMENT_REQUIRED`-style
    refusal).

    Quote-aware and fail-CLOSED on ambiguity (reference lr-8916 posture):
    scans `shell_parsing.normalize_shell_words(command)`; if normalization
    cannot confidently resolve the command, this DENIES rather than falling
    back to a raw-string scan a quote-obfuscated command could evade —
    stricter than a generic compound-check's own deny-on-ambiguity fallback,
    because this is itself a security-boundary admission check (resolving
    ambiguity permissively here could hide a smuggled flag/method inside a
    quoted span, or miss a genuine one).

    ANSI-C evasion (INHERITED ITEM 2): when normalization fails, this also
    checks `has_unresolved_ansi_c_quote` purely to produce a more specific
    deny reason (an unresolved ANSI-C span is one CAUSE of normalization
    failure) — the deny verdict itself is identical either way, since ANY
    normalization failure already denies unconditionally on this
    security-boundary check.
    """
    if not verb_pattern.match(command):
        return True, ""

    scan_target = normalize_shell_words(command)
    if scan_target is None:
        ambiguity_note = (
            " (an unresolved ANSI-C ($'...') quote span is present)"
            if has_unresolved_ansi_c_quote(command)
            else ""
        )
        return False, (
            "invocation could not be confidently normalized (unbalanced "
            "quoting, unresolved escape, or quoted command substitution)"
            f"{ambiguity_note} -- deny-on-ambiguity applies to this "
            f"admission check; issue a plainly-quoted command instead"
        )

    method_re = re.compile(rf"(?:^|\s){re.escape(rule.method)}(?:\s|$)", re.IGNORECASE)
    if not method_re.search(scan_target):
        return True, ""
    if not rule.path_pattern.search(scan_target):
        return True, ""

    flag_re = re.compile(rf"(?:^|\s){re.escape(rule.required_flag)}(?:\s|$)")
    if flag_re.search(scan_target):
        return True, ""

    return False, rule.deny_reason


# ---------------------------------------------------------------------------
# INHERITED ITEM 1 — loadout-verb `--body-stdin` pipe carve-out (reference
# `_is_admitted_loadout_body_stdin_pipe` / `_classify_body_stdin_pipe_
# ambiguity`, ll.598-767). Generalized to a caller-supplied verb registry.
# ---------------------------------------------------------------------------

_BODY_STDIN_PRODUCER_RE = re.compile(r"^(?:echo|printf|cat)(?:\s|$)")


@dataclass(frozen=True)
class BodyStdinVerb:
    """One loadout-verb admitted as the RHS of the `--body-stdin` pipe
    carve-out.

    *verb_pattern* matches the verb invocation itself (bare basename and/or
    absolute install path — a caller composes this the same way
    `guard.credential_paths`/`guard.write_scope` callers compose their own
    verb patterns, no hardcoded path baked in here). *requires_body_stdin_flag*
    is True for a verb whose body-input path is flag-gated (e.g.
    `loadout-git-host-api --body-stdin`, which ALSO supports other body
    sources) and False for a verb whose stdin IS its unconditional, sole
    body path (e.g. `loadout-review-post`/`loadout-stage-body`, reference
    lr-ad4b54/lr-e768a1) — no flag is required on that RHS at all.
    """

    verb_pattern: re.Pattern[str]
    requires_body_stdin_flag: bool = False


_BODY_STDIN_FLAG_RE = re.compile(r"(?:^|\s)--body-stdin(?:\s|$)")

#: Bare verb-name recognizer used ONLY to select an ambiguity-classification
#: MESSAGE (never to admit a command) — see `classify_body_stdin_pipe_ambiguity`.
_BODY_STDIN_VERB_NAME_HINT_RE = re.compile(r"(?:^|\s)loadout-[a-z0-9][a-z0-9-]*(?:\s|$)")


def is_admitted_body_stdin_pipe(
    command: str, *, verbs: tuple[BodyStdinVerb, ...]
) -> bool:
    """Return True iff *command* is EXACTLY the documented
    `echo|printf|cat | loadout-<verb> [--body-stdin]` pipe shape for one of
    *verbs* (reference `_is_admitted_loadout_body_stdin_pipe`, ll.615-661).

    Exactly two segments, joined by exactly one top-level `|`, and no other
    operator anywhere in the split (a caller composes this check BEFORE its
    own `shell_parsing.compound_check` call and short-circuits on a match —
    mirroring the reference's own `_compound_check` composition order,
    `guard.shell_parsing.compound_check`'s module docstring point 2).

    *command* is expected to already be `shell_parsing.cmd_head` output
    (heredoc body already stripped by the caller) — this function only
    splits on the remaining top-level shell operators.

    ANSI-C evasion (INHERITED ITEM 2): if EITHER segment fails to normalize
    AND contains an unresolved ANSI-C opener, this denies outright rather
    than falling through to the segment-shape checks below — an ANSI-C
    wrapper that hides a mutating verb's true name from
    `verb.verb_pattern.match` on a normalization failure must never be
    treated as "not this pipe shape, fall through to the generic allowlist"
    (which could then admit the raw un-recognized command via some OTHER,
    less specific rule); it must be recognized as ambiguous-and-denied at
    this narrow layer too, so the caller's overall admission decision for
    the whole pipe is never silently permissive.
    """
    try:
        pairs = split_segments(command)
    except ValueError:
        return False  # deny-on-ambiguity: unparseable quoting

    if len(pairs) != 2:
        return False
    (op0, lhs), (op1, rhs) = pairs
    if op0 != "" or op1 != "|":
        return False

    lhs_normalized = normalize_shell_words(lhs)
    if lhs_normalized is None:
        return False  # deny-on-ambiguity (has_unresolved_ansi_c_quote adds no further denial here)
    if not _BODY_STDIN_PRODUCER_RE.match(lhs_normalized.strip()):
        return False

    rhs_normalized = normalize_shell_words(rhs)
    if rhs_normalized is None:
        return False  # deny-on-ambiguity
    rhs_stripped = rhs_normalized.strip()

    for verb in verbs:
        if verb.verb_pattern.match(rhs_stripped):
            if not verb.requires_body_stdin_flag:
                return True
            return bool(_BODY_STDIN_FLAG_RE.search(rhs_stripped))
    return False


def classify_body_stdin_pipe_ambiguity(command: str) -> str | None:
    """Return a dedicated body-AMBIGUITY rejection message iff *command* is a
    NEAR-MISS of the admitted loadout-verb `--body-stdin` pipe shape — one
    producer segment (echo/printf/cat) piped into what LOOKS like a
    `loadout-<verb>` invocation, but `is_admitted_body_stdin_pipe` denied it
    ONLY because of quoting ambiguity on either side of the pipe (typically:
    quoted body prose containing a backtick or `$(` command-substitution
    opener) — never because the verb genuinely isn't a loadout verb, or
    because there is a second top-level operator (reference
    `_classify_body_stdin_pipe_ambiguity`, ll.678-767).

    Returns None for every other shape (a genuine unrelated compound/pipe,
    or a pipe not recognizably involving a `loadout-*` verb at all) — those
    fall through to a caller's own generic compound-shape message unchanged.

    This is a MESSAGING classifier only — it never changes whether the
    command is admitted (that verdict is `is_admitted_body_stdin_pipe`'s
    alone); a false-positive match here only changes which deny message is
    shown.

    ANSI-C evasion (INHERITED ITEM 2): the RHS raw-fallback scan below
    (`_BODY_STDIN_VERB_NAME_HINT_RE` against the un-normalized `rhs`) is
    exactly the "raw-fallback verb-matcher" shape this item warns about. If
    normalization failed BECAUSE of an unresolved ANSI-C span, that span's
    intact `$'...'` wrapper could hide a verb name from the raw-text hint
    regex either way — but since this function only ever *downgrades* a
    denial's message (never upgrades a deny to an allow), a missed hint here
    has no security consequence: the worst outcome is a generic
    compound-shape message instead of this more specific one. No ANSI-C
    check is added to this function's raw-fallback branch for that reason —
    unlike `is_admitted_body_stdin_pipe`'s and `requires_admission_flag`'s
    admission-DECIDING raw-fallback branches, which do check it.
    """
    try:
        pairs = split_segments(command)
    except ValueError:
        return None  # not a recognizable pipe shape at all; not this classifier's concern

    if len(pairs) != 2:
        return None
    (op0, lhs), (op1, rhs) = pairs
    if op0 != "" or op1 != "|":
        return None

    lhs_normalized = normalize_shell_words(lhs)
    rhs_normalized = normalize_shell_words(rhs)

    if lhs_normalized is not None and rhs_normalized is not None:
        # Both sides parsed cleanly — is_admitted_body_stdin_pipe already had
        # every chance to admit (or correctly deny for a genuinely different
        # reason, e.g. a missing required flag). Not this ambiguity class.
        return None

    lhs_producer_like = bool(
        _BODY_STDIN_PRODUCER_RE.match(lhs_normalized.strip())
        if lhs_normalized is not None
        else re.match(r"^\s*(?:echo|printf|cat)(?:\s|$)", lhs)
    )
    if not lhs_producer_like:
        return None  # producer segment isn't echo/printf/cat -- unrelated shape

    rhs_verb_like = bool(
        _BODY_STDIN_VERB_NAME_HINT_RE.search(rhs_normalized)
        if rhs_normalized is not None
        else _BODY_STDIN_VERB_NAME_HINT_RE.search(rhs)
    )
    if not rhs_verb_like:
        return None  # RHS doesn't recognizably name a loadout verb

    return (
        "loadout body-stdin pipe DENIED -- body-AMBIGUITY, not a "
        "compound-shape deny: this command's body content could not be "
        "confidently parsed (typically a backtick or $(...) "
        "command-substitution opener inside the quoted body prose -- "
        "callers naturally quote such text when citing error strings or "
        "action-tag names). This is NOT 'pipes are banned' and NOT 'verb "
        "not in allowlist' -- the verb IS allowlisted, and other pipe "
        "shapes ARE permitted; ONLY this body's content is unparseable on "
        "the stdin-pipe path. FIX: stage the body to a file first via an "
        "already-admitted redirect (no pipe, so ambiguity inside the "
        "quoted content is irrelevant), then invoke the verb's bare, "
        "no-pipe file-input form instead."
    )


__all__ = [
    "BodyStdinVerb",
    "MethodPathFlagRule",
    "classify_body_stdin_pipe_ambiguity",
    "detect_project_tree_write_targets",
    "is_admitted_body_stdin_pipe",
    "is_admitted_loadout_family_readonly",
    "is_fd_safe_target",
    "requires_admission_flag",
]
