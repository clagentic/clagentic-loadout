"""merge.title_gate — Conventional Commits PR title validation (lr-0115
class).

Wave B slice 4 (lr-885f, tome #688). Ported from the reference merge gate's
title check (_assert_pr_title_valid). Extended (lr-6067) so the same grammar
check is callable at PR-open time (push.verb), before a PR number exists —
see `is_conventional_title` / the `pr_number: int | None` signature below.

The PR title is promoted verbatim into the merge commit message on a real
(non-squash) merge. An invalid title produces permanent, un-fixable history
that breaks downstream changelog parsers and audit tooling. Ideally it is
caught even earlier, at PR-open time (push.verb, lr-6067) — but is ALWAYS
validated again here, immediately BEFORE the merge executes, so a bad title
is caught fail-closed at the exact step that writes the commit, regardless
of whether the PR-open-time check ran, was bypassed, or the title was
changed after open.

Grammar:
    ^(feat|fix|docs|refactor|perf|test|build|ci|chore)(\\([^)]+\\))?!?: .+
  - type  in feat|fix|docs|refactor|perf|test|build|ci|chore
  - scope = optional parenthesised subsystem, e.g. (lr-1234)
  - !     = optional breaking-change marker (may appear before the colon)
  - ': '  = required colon + space separator
  - description = at least one non-empty character

Opt-out: the caller passes skip=True (--skip-title-check at the CLI layer)
only when the title cannot be changed (e.g. a platform-produced title for an
automation PR). Use of the bypass is the CALLER's job to log for audit; this
module only enforces or no-ops.
"""

from __future__ import annotations

import re

from clagentic_loadout.merge.errors import TitleInvalidError

#: Compiled Conventional Commits grammar regex.
CONVENTIONAL_COMMITS_RE = re.compile(
    r"^(feat|fix|docs|refactor|perf|test|build|ci|chore)(\([^)]+\))?!?: .+",
    re.DOTALL,
)


def is_conventional_title(title: str) -> bool:
    """Pure grammar predicate: True iff *title* matches the Conventional
    Commits grammar (CONVENTIONAL_COMMITS_RE). No PR/owner/repo context, no
    exceptions — the single source of truth for "does this string conform,"
    reused by both check_pr_title (merge, PR-number-aware) and any caller
    that needs the check before a PR number exists (e.g. push.verb at
    PR-open time, lr-6067)."""
    return bool(CONVENTIONAL_COMMITS_RE.match(title))


def _format_title_error(title: str, *, pr_number: int | None, owner: str, repo: str) -> str:
    """Build the TitleInvalidError message. Reports the resolved offending
    title and the full expected grammar (CLI hygiene rule 4) whether or not
    a PR number is available yet — the PR-number clause is simply omitted
    when absent (PR-open time, before the platform has assigned one), never
    filled with a placeholder."""
    where = f"PR #{pr_number} in {owner}/{repo}" if pr_number is not None else f"the PR being opened in {owner}/{repo}"
    return (
        f"PR title gate FAILED — {where} has a "
        f"non-conformant title: {title!r}\n"
        f"Expected Conventional Commits grammar:\n"
        f"  <type>(<scope>)!?: <description>\n"
        f"  type in feat|fix|docs|refactor|perf|test|build|ci|chore\n"
        f"  scope = optional parenthesised subsystem (e.g. '(auth)')\n"
        f"  !     = optional breaking-change marker before the colon\n"
        f"  description = at least one character after ': '\n"
        f"Example valid titles:\n"
        f"  feat(auth): add PR title gate to the merge verb\n"
        f"  fix!: correct stale-SHA check order\n"
        f"RESOLUTION: update the PR title to match the grammar, then retry. "
        f"To bypass this gate (e.g. an automation PR with a platform-set "
        f"title), pass --skip-title-check."
    )


def check_pr_title(
    title: str,
    pr_number: int | None,
    owner: str,
    repo: str,
    *,
    skip: bool = False,
) -> None:
    """Assert *title* conforms to Conventional Commits grammar.

    *pr_number* is Optional: pass the real PR number when one exists (the
    merge gate, always) or None when validating a title before a PR number
    has been assigned yet (PR-open time — see push.verb). The error message
    adapts to omit the PR-number clause when absent; it never fabricates a
    placeholder number.

    No-op when *skip* is True — the caller is responsible for logging that
    the bypass was used, since only the caller knows the invocation context
    (e.g. --skip-title-check on the CLI).

    Raises merge.errors.TitleInvalidError when the title does not match,
    naming the offending title and the required grammar.
    """
    if skip:
        return
    if is_conventional_title(title):
        return
    raise TitleInvalidError(_format_title_error(title, pr_number=pr_number, owner=owner, repo=repo))


__all__ = ["CONVENTIONAL_COMMITS_RE", "check_pr_title", "is_conventional_title"]
