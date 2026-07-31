"""guard.git_write_guard — git/PR write-operation hard-deny for a non-agent
session (lr-59dd37, port of the reference deployment's crew-git-guard.py;
lr-5a8d epic Wave C).

PORT PATTERN — mirrors `guard.write_scope`'s established convention:

1. STRIP THE HOOK SHELL. The reference file reads a PreToolUse/Bash JSON
   payload from stdin, resolves agent identity via the same four-path
   detection chain `guard.task_dispatch`/`guard.dispatch_discipline` already
   exclude (rule 6a), discovers a per-project builder-scope config file from
   a resolved cwd purely to TAILOR its deny message, and emits a
   `hookDecision`/`hookSpecificOutput` JSON block. None of that harness
   plumbing is ported.
   Every function here is a pure function over an explicit command string
   plus caller-supplied config; a caller's own harness adapter resolves
   whether the calling session is an attested agent and passes that boolean
   in, exactly as `guard.dispatch_discipline.check_dispatch_discipline`
   already establishes for the sibling warn-only guard.

2. NO AGENT-NAME OR SCRIPT-NAME HARDCODE. The reference hardcodes its own
   retired legacy push script, `loadout-merge`, `loadout-close-pr` as the
   sanctioned never-blocked scripts, and its own sanctioned-but-must-be-
   agent-invoked landing tool. `GitWriteGuardConfig.sanctioned_verb_patterns` /
   `restricted_verb_patterns` are caller-supplied instead — this module
   hardcodes no specific binary name (CLAUDE.md rule 1), mirroring
   `guard.role_allowlist.RoleAllowlistConfig.extra_verb_patterns`'s own
   "caller wires its own installed verb names in" convention.

3. NO HARDCODED FORGE HOSTNAME. The reference's PR-create/merge curl
   detection regexes embed a literal Forgejo hostname and IP-range pattern
   alongside the generic `api.github.com` GitHub pattern.
   `GitWriteGuardConfig.forge_host_patterns` takes a caller-supplied tuple of
   host-matching regex fragments instead (CLAUDE.md rule 1: no operator
   host hardcode) — mirrors `guard.director_mutation.LeadMutationConfig.
   forge_host_patterns`'s own precedent for the identical class of literal
   in a sibling module.

4. QUOTE-AWARE SCANNING REUSES `shell_parsing`, NEVER REIMPLEMENTED. The
   reference hand-rolls its own quote-masking scan target
   (`_scan_target` = `_cmd_head` + `_mask_quoted_spans`, both DYNAMICALLY
   RE-LOADED from guard-bash.py via a second copy-avoiding import shim) so a
   forbidden verb/token appearing only inside a QUOTED narrative argv span
   (e.g. a `lore task create --description "... git push --force was used
   ..."` narration) is treated as data, not a command to classify. This
   module calls `guard.shell_parsing.cmd_head` /
   `guard.shell_parsing.mask_quoted_spans` DIRECTLY — the real, single
   landed copy of both functions — rather than re-deriving or re-importing
   a second copy of either (module docstring's "reuse landed primitives;
   never duplicate a boundary" rule, applied here to the reference's own
   dynamic-reimport workaround, which existed only because the reference's
   two hook files had no shared importable package to begin with).

5. ANSI-C EVASION HARDENING (MANDATORY for every bare-verb / forbidden-
   substring classifier per docs/guard-policy.md's "Post-landing hardening"
   precedent, PR #115/#116): `classify_git_write_command` is a
   forbidden-SUBSTRING scan feeding an affirmative DENY (the mirror image of
   `check_forbidden_git_patterns`'s affirmative GRANT-adjacent deny list —
   same evasion class, opposite polarity: an ANSI-C-fragmented `git
   $'push' --force` must still classify as a write op, not silently pass
   through to "not a write operation" the way a fragmented forbidden-pattern
   scan would silently pass a mutation through to a bare-verb GRANT). This
   module therefore applies the SAME two-part fix `role_allowlist` already
   established: (a) `classify_git_write_command` scans
   `shell_parsing.normalize_shell_words(scan_target)` when normalization
   succeeds; (b) on normalization FAILURE, this now FAILS CLOSED — classifies
   the command as a write op outright — whenever the command contains any
   ANSI-C ($'...'/$"...") opener at all
   (`shell_parsing.has_unresolved_ansi_c_quote`), rather than falling back to
   a masked raw scan.

   COLLATERAL-MASKING BUG (pre-merge security-audit finding, follow-up to
   lr-59dd37 — the reason (b) above is a fail-closed CLASSIFY, not merely a
   raw-string fallback): the ORIGINAL version of this fix fell back to
   `_scan_target` (`cmd_head` + `shell_parsing.mask_quoted_spans`) on
   normalization failure — the SAME deny-leaning fallback posture every
   raw-fallback verb-matcher elsewhere in this package uses. That posture is
   safe everywhere else because those callers fall back to the UNMASKED raw
   string. This module's fallback was different: `mask_quoted_spans` has
   ZERO ANSI-C awareness — it treats a `$'...'`/`$"..."` span as an ordinary
   bare-quote span and BLANKS its content to spaces (the correct behavior
   for a genuinely isolated `'...'`/`"..."` narrative span, but WRONG for
   `$'...'`, which bash always evaluates as a live word). Meanwhile
   `normalize_shell_words` fails GLOBALLY (returns None) if ANY single
   ANSI-C span anywhere in the command is malformed (an unrecognized
   escape). The exploit: a command with ONE resolvable ANSI-C write-verb
   span (`git $'push' ...`) PLUS a SECOND, UNRELATED malformed ANSI-C span
   elsewhere (e.g. `$'\\c'`) makes `normalize_shell_words` return None for
   the WHOLE command — the fallback then masked the FIRST, perfectly
   resolvable `$'push'` span as if it were isolated narrative data, blanking
   the verb `classify_git_write_command` needed to see and returning
   `(False, "")` — an ALLOW, for a non-agent session, of a real `git push`.
   This is a bypass of the git-write deny, one level deeper than the
   fragmentation bug (a)/(b) above already closed: not "can a fragmented
   verb hide from a scan" but "can an UNRELATED malformed span anywhere in
   the command poison the scan of an otherwise-visible verb." Since this
   guard's only two outcomes are "flag as write op" (safe direction) or "not
   flagged" (the direction that must never be reached by ambiguity), the
   durable fix is FAIL-CLOSED ON AMBIGUITY: `_scan_target`'s masking
   fallback is retired from `classify_git_write_command`'s ambiguity path
   entirely — normalization failure plus ANY ANSI-C opener anywhere in the
   command now classifies as a write op outright, never attempts a masked
   scan that could blank a real verb. See
   `tests/test_guard_git_write_guard.py::TestAnsiCCollateralMaskingCannotEvadeClassification`.
   `_scan_target` itself is removed rather than left as dead, unreachable
   code (it was module-private and never exported, and this was its only
   call site).

WHAT THIS MODULE DELIBERATELY DOES NOT PORT:
  - Agent identity DETECTION (rule 6a, mirrors `guard.task_dispatch`/
    `guard.dispatch_discipline`).
  - Per-project builder-scope config discovery from cwd (message-tailoring
    only in the reference, never a gate) — a caller wanting a tailored
    redirect message supplies its own project-label string via
    `GitWriteGuardConfig`.
  - The reference's operator-override environment-variable read —
    this module never reads an environment variable itself (rule 6a); a
    caller resolves its own override signal and passes it as
    `override_active`, exactly mirroring
    `guard.dispatch_discipline.check_dispatch_discipline`'s own contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from clagentic_loadout.guard.shell_parsing import (
    cmd_head,
    has_unresolved_ansi_c_quote,
    normalize_shell_words,
)

#: git push (any form: bare, with a remote, with flags).
_GIT_PUSH_RE = re.compile(r"\bgit\s+push\b", re.IGNORECASE)

#: gh pr create / gh pr merge.
_GH_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b", re.IGNORECASE)
_GH_PR_MERGE_RE = re.compile(r"\bgh\s+pr\s+merge\b", re.IGNORECASE)

#: Write-intent marker for a curl-based PR-create classification: only a
#: POST/PUT curl is a write; a GET status check on the same endpoint shape
#: is not (reference: the `is_post` substring test in `_classify_command`).
_WRITE_METHOD_RE = re.compile(r"-X\s*POST|(?<!-)\bPOST\b|-X\s*PUT", re.IGNORECASE)


def _default_github_create_patterns() -> tuple[re.Pattern[str], ...]:
    create_patterns, _ = build_default_forge_patterns(())
    return create_patterns


def _default_github_merge_patterns() -> tuple[re.Pattern[str], ...]:
    _, merge_patterns = build_default_forge_patterns(())
    return merge_patterns


@dataclass(frozen=True)
class GitWriteGuardConfig:
    """Caller-supplied configuration for `classify_git_write_command`.

    push_verb_patterns: additional bare-verb regexes classified as a git
        write operation outright (reference: the reference deployment's own
        sanctioned landing-tool invocation pattern — a tool that must only
        ever be invoked BY an attested agent, never by an orchestrating
        session directly). No literal script name is hardcoded here; a
        caller supplies its own installed landing-tool verb pattern(s).
    forge_pr_create_patterns / forge_pr_merge_patterns: curl-shaped PR
        create/merge patterns. Default to the generic `api.github.com`
        pattern only (a public, non-operator-specific hostname — see
        `build_default_forge_patterns`); a caller with its own forge host(s)
        calls `build_default_forge_patterns(forge_host_patterns)` and passes
        the result here to extend the default GitHub coverage, or supplies
        an entirely independent pattern tuple of its own.
    forge_host_patterns: caller-supplied host-matching regex fragments
        (e.g. a forge FQDN or an internal-network CIDR fragment) with NO
        default (CLAUDE.md rule 1 — no operator host hardcode). This field
        is documentation-only convenience storage for a caller that wants to
        keep its host list alongside this config; `classify_git_write_command`
        itself never reads it directly — a caller must explicitly pass
        `build_default_forge_patterns(forge_host_patterns)`'s result into
        `forge_pr_create_patterns`/`forge_pr_merge_patterns` for it to take
        effect.
    sanctioned_verb_patterns: verb patterns NEVER classified as a write
        operation regardless of session — the caller's own attested-identity
        landing surface (reference: `crew_push.py`, `loadout-merge`,
        `loadout-close-pr`). Checked FIRST, before any classification regex,
        mirroring the reference's own `_classify_command` order.
    """

    push_verb_patterns: tuple[re.Pattern[str], ...] = ()
    forge_pr_create_patterns: tuple[re.Pattern[str], ...] = field(
        default_factory=_default_github_create_patterns
    )
    forge_pr_merge_patterns: tuple[re.Pattern[str], ...] = field(
        default_factory=_default_github_merge_patterns
    )
    forge_host_patterns: tuple[str, ...] = ()
    sanctioned_verb_patterns: tuple[re.Pattern[str], ...] = field(default_factory=tuple)


def build_default_forge_patterns(
    forge_host_patterns: tuple[str, ...],
) -> tuple[tuple[re.Pattern[str], ...], tuple[re.Pattern[str], ...]]:
    """Build the default (create_patterns, merge_patterns) pair for a set of
    caller-supplied forge-host regex fragments, plus the always-included
    generic `api.github.com` GitHub pattern (reference: `_GITHUB_PR_CREATE_RE`
    / `_GITHUB_MERGE_RE` — a public, non-operator-specific hostname, not an
    operator host hardcode, so it ships unconditionally).

    Each *forge_host_patterns* entry is inserted as a `(?:...)`
    non-capturing alternation member — a caller supplying a value containing
    unintended regex metacharacters is responsible for its own escaping
    (this helper is a convenience composer, not itself a security boundary;
    the classification these patterns feed is a DENY signal, so an
    overly-narrow caller pattern only under-classifies, never grants
    anything).
    """
    host_alt = "|".join(forge_host_patterns) if forge_host_patterns else ""
    create_patterns = [
        re.compile(
            r"api\.github\.com/repos/[^/]+/[^/]+/pulls[\"'\s]", re.IGNORECASE | re.DOTALL
        ),
    ]
    merge_patterns = [
        re.compile(
            r"api\.github\.com/repos/[^/]+/[^/]+/pulls/\d+/merge",
            re.IGNORECASE | re.DOTALL,
        ),
    ]
    if host_alt:
        create_patterns.append(
            re.compile(rf"curl\b.*(?:{host_alt}).*?/pulls[\"'\s]", re.IGNORECASE | re.DOTALL)
        )
        merge_patterns.append(
            re.compile(
                rf"curl\b.*(?:{host_alt}).*?/pulls/\d+/merge", re.IGNORECASE | re.DOTALL
            )
        )
    return tuple(create_patterns), tuple(merge_patterns)


def classify_git_write_command(
    command: str, config: GitWriteGuardConfig
) -> tuple[bool, str]:
    """Classify *command* as a git/PR write operation or not.

    Returns (is_write_op, reason). `reason` is empty when `is_write_op` is
    False. See module docstring point 5 for the ANSI-C-fragmentation
    hardening applied here: this scans
    `shell_parsing.normalize_shell_words(shell_parsing.cmd_head(command))`
    FIRST — `normalize_shell_words` already blanks isolated quoted argv
    spans as part of its own normalization pass (see that function's own
    docstring), so a narrative `--description "... git push ..."` span is
    excluded from the scan by the SAME mechanism that decodes a resolvable
    ANSI-C span (`git $'push'`) into its literal joined text, closing both
    concerns with one normalized scan target rather than layering an
    independent quote-mask BEFORE normalization ever runs (which would
    blank an ANSI-C span's contents before it could be decoded, silently
    hiding a fragmented write op — the exact evasion this hardening exists
    to close).

    On normalization FAILURE (unbalanced quoting, an unresolved ANSI-C
    opener, a quoted command-substitution opener), this FAILS CLOSED rather
    than falling back to a masked scan target: `sanctioned_verb_patterns` is
    still checked first against the RAW command (never masked — the
    sanctioned-tool escape hatch is a literal-name match on the caller's own
    attested landing tool, not narrative data a masking pass could ever
    legitimately need to hide), so `crew_push.py`'s own internal `git push`
    still self-exempts correctly even under ambiguity. Once past that check,
    ANY command whose quoting could not be confidently normalized AND which
    contains an ANSI-C ($'...'/$"...") opener anywhere at all
    (`shell_parsing.has_unresolved_ansi_c_quote`) is classified as a write
    op outright — see module docstring point 5's "COLLATERAL-MASKING BUG"
    section for why a masked-scan fallback (this module's PRIOR fix) is
    unsafe here: `mask_quoted_spans` blanks an ANSI-C span's content
    unconditionally (it has no ANSI-C awareness at all), so ONE malformed
    ANSI-C span anywhere in the command could poison `normalize_shell_words`
    globally and cause the masking fallback to blank a SECOND, perfectly
    resolvable ANSI-C write-verb span elsewhere in the same command — hiding
    a real `git $'push'` from this classifier entirely. Ambiguity on this
    deny-oriented classifier must resolve to "flag as write op," never to a
    scan that could blank the very verb it exists to find. A normalization
    failure with NO ANSI-C opener present at all (e.g. plain unbalanced
    bare-quote nesting) still falls back to the raw, unmasked `head` —
    identical in spirit to every other raw-fallback verb-matcher in this
    package: the raw string is never blanked, so it can only make this
    classifier MORE likely to correctly flag a write op, never less.
    """
    head = cmd_head(command)

    for pattern in config.sanctioned_verb_patterns:
        if pattern.search(command):
            return False, ""

    scan = normalize_shell_words(head)
    if scan is None:
        if has_unresolved_ansi_c_quote(head):
            return True, (
                "command contains an unresolved ANSI-C ($'...'/$\"...\") "
                "quote span that could not be confidently normalized -- "
                "deny-on-ambiguity applies to this write-operation "
                "classifier: an unrelated malformed ANSI-C span elsewhere "
                "in the command could otherwise poison a masked fallback "
                "scan and hide a real write verb"
            )
        scan = head  # deny-leaning fallback: raw, unmasked head.

    if _GIT_PUSH_RE.search(scan):
        return True, "git push"

    if _GH_PR_CREATE_RE.search(scan):
        return True, "gh pr create"

    if _GH_PR_MERGE_RE.search(scan):
        return True, "gh pr merge"

    for pattern in config.push_verb_patterns:
        if pattern.search(scan):
            return True, "sanctioned-landing-tool invocation from a non-agent session"

    is_write_method = bool(_WRITE_METHOD_RE.search(scan))

    if is_write_method:
        for pattern in config.forge_pr_create_patterns:
            if pattern.search(scan):
                return True, "PR creation via curl"

    for pattern in config.forge_pr_merge_patterns:
        if pattern.search(scan):
            return True, "PR merge via curl"

    return False, ""


@dataclass(frozen=True)
class GitWriteDenyContext:
    """Caller-supplied prose for the deny message. No agent name or script
    literal is baked into this module (CLAUDE.md rule 1) — a caller
    composing this into its own harness adapter supplies its own redirect
    guidance and project label.
    """

    project_label: str
    push_redirect_instructions: str
    merge_redirect_instructions: str
    override_env_var_name: str = ""


def check_git_write_call(
    command: str,
    is_named_agent: bool,
    config: GitWriteGuardConfig,
    deny_context: GitWriteDenyContext,
    override_active: bool = False,
) -> tuple[bool, str]:
    """Top-level entry point: return (ok, reason) for a Bash *command* under
    this guard.

    Resolution order (mirrors the reference's own `main()`):
      1. *override_active* -> allow (caller's own resolved override signal;
         this module reads no environment variable itself, rule 6a).
      2. `classify_git_write_command` says "not a write op" -> allow.
      3. *is_named_agent* -> allow (an attested agent IS the sanctioned
         path for a git/PR write operation, on any repo).
      4. Otherwise -> deny, with a redirect message tailored by whether the
         matched reason names a merge operation or a push/PR-create one.
    """
    if override_active:
        return True, ""

    is_write_op, op_reason = classify_git_write_command(command, config)
    if not is_write_op:
        return True, ""

    if is_named_agent:
        return True, ""

    redirect = (
        deny_context.merge_redirect_instructions
        if "merge" in op_reason.lower()
        else deny_context.push_redirect_instructions
    )
    override_line = (
        f"\n\nTo bypass this guard for a deliberate in-session git operation, "
        f"set: {deny_context.override_env_var_name}=1"
        if deny_context.override_env_var_name
        else ""
    )
    return False, (
        f"BLOCKED: {op_reason}.\n\n"
        f"This session is not a registered attested agent. Running git write "
        f"operations from an orchestrating session executes under the "
        f"session's own ambient credentials rather than an attested agent "
        f"identity, breaking identity attribution for the resulting "
        f"commit/PR.\n\n"
        f"  Project: {deny_context.project_label}\n\n"
        f"{redirect}"
        f"{override_line}"
    )


__all__ = [
    "GitWriteDenyContext",
    "GitWriteGuardConfig",
    "build_default_forge_patterns",
    "check_git_write_call",
    "classify_git_write_command",
]
