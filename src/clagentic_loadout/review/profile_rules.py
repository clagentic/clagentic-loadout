"""review.profile_rules — the repo profile's class rules, applied as findings.

Given the profile read from the base revision (review.policy_source) and the
diff under review, this module states what the class rules say about the diff as
ordinary findings with FACTS, which the evaluator then scores like any other
(review.evaluate). It never names a severity and never decides a verdict.

What is checked mechanically:

  released-tool files  an added line that contains a denied name, host or
                       username (from the deployment's list,
                       review.profile_settings, plus the profile's own), or an
                       absolute host path under a home directory.

Nothing is checked mechanically for the internal class. Rules that need
judgement (other tools stay optional and pluggable; a released tool never
special-cases a consumer's tools or permissions; an internal tree uses a tool only through its
documented interface) are stated in repo_profile.CLASS_RULES. They reach the
reviewer in the chunk prompt: ``class_rules_section`` renders, per class present
in a chunk, that class's statements and the chunk files it governs, and
``rule_statements`` adds the profile's ``tool_packages`` list to the internal
statements. Both the prompt and ``loadout-profile check`` use that rendering.
Statements are context to judge against, never a finding source or a severity.

An invalid profile is itself reported, and so is a missing one, always. Each is
a finding that scores ``none``: it cannot block at any threshold, so it can
never hold a merge.

The facts these findings state say ``reachable: no``. That is true to the rule:
a shape violation is not an attack path, and the evaluator holds an unreachable
finding at ``low``. A deployment that wants these to block sets the evaluator
threshold to ``low``.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import Any

from clagentic_loadout.review.chunking import Chunk
from clagentic_loadout.review.finding_identity import with_fingerprints
from clagentic_loadout.review.policy_source import (
    PROFILE_INVALID,
    PROFILE_MISSING,
    PROFILE_OK,
    PROFILE_PATH,
    PolicySource,
)
from clagentic_loadout.review.profile_settings import ProfileSettings
from clagentic_loadout.review.repo_profile import (
    CLASS_INTERNAL,
    CLASS_RELEASED_TOOL,
    CLASSES,
    CLASS_RULES,
    DENY_HOSTS,
    DENY_NAMES,
    DENY_USERS,
    RepoProfile,
)

RULE_MISSING = "repo-profile.missing"
RULE_INVALID = "repo-profile.invalid"
RULE_DENIED = "repo-profile.denied-value"
RULE_HOST_PATH = "repo-profile.host-path"
RULE_MORE = "repo-profile.more"

#: Most rule findings reported for one diff; the rest are counted in one finding.
MAX_RULE_FINDINGS = 100

_MAX_SHOWN_CHARS = 60
_MAX_RULE_FILES = 20
_KIND_LABEL = {
    DENY_NAMES: "agent or deployment name",
    DENY_HOSTS: "host or domain",
    DENY_USERS: "username",
}

#: A home-directory path names a person's account on a machine. Only these
#: shapes are recognised on their own; any other host path a deployment wants
#: kept out is added to its ``hosts`` list as a literal prefix.
_HOME_PATH_RE = re.compile(
    r"(?<![\w.~$/-])(?:/(?:home|Users)/(?P<user>[A-Za-z0-9_.-]+)"
    r"|[A-Za-z]:\\Users\\(?P<winuser>[A-Za-z0-9_.-]+))"
)
#: Account names that stand for "some user" in documentation and fixtures, not
#: for anyone's identity.
PLACEHOLDER_USERS = frozenset(
    {
        "user", "username", "you", "yourname", "me", "name", "example", "someone",
        "somebody", "x", "foo", "bar", "baz", "alice", "bob", "test",
    }
)


def _facts(category: str, *, confidentiality: str = "none", integrity: str = "none") -> dict[str, Any]:
    return {
        "category": category,
        "reachable": "no",
        "attacker_precondition": "none",
        "impact": {
            "confidentiality": confidentiality,
            "integrity": integrity,
            "availability": "none",
        },
        "durability": "durable",
    }


def _finding(file: str, line: int, rule_id: str, message: str, facts: dict[str, Any]) -> dict[str, Any]:
    return {"file": file, "line": line, "rule_id": rule_id, "message": message, "facts": facts}


def _shown(text: str) -> str:
    return text if len(text) <= _MAX_SHOWN_CHARS else text[: _MAX_SHOWN_CHARS - 3] + "..."


def _missing_finding() -> dict[str, Any]:
    return _finding(
        PROFILE_PATH,
        1,
        RULE_MISSING,
        f"The base revision has no repo profile at {PROFILE_PATH}, so no class rule is applied. "
        "Run `loadout-profile init` to draft one.",
        _facts("maintainability"),
    )


def _invalid_finding(problem: str | None) -> dict[str, Any]:
    detail = _shown(problem or "it could not be read")
    return _finding(
        PROFILE_PATH,
        1,
        RULE_INVALID,
        f"The repo profile at {PROFILE_PATH} is invalid, so no class rule is applied: {detail}",
        _facts("maintainability"),
    )


def _token_regex(value: str) -> re.Pattern[str]:
    """*value* as a whole token, case-insensitive. ``_`` and ``-`` end a token, so
    a name inside an identifier or a hyphenated word is found, while a longer
    word that merely contains it is not. A boundary is required only at an edge
    that is a letter or digit, so a literal such as a path prefix matches
    wherever it starts a path."""
    before = r"(?<![A-Za-z0-9])" if value[:1].isalnum() else ""
    after = r"(?![A-Za-z0-9])" if value[-1:].isalnum() else ""
    return re.compile(before + re.escape(value) + after, re.IGNORECASE)


def _added_rows(chunk: Chunk, file: str) -> Iterator[tuple[int, str]]:
    for hunk in chunk.hunk_rows(file):
        for row in hunk.rows:
            if row.new_number is not None and row.old_number is None:
                yield row.new_number, row.text


def _released_tool_hits(
    text: str, denied: Sequence[tuple[str, re.Pattern[str], str]]
) -> Iterator[tuple[str, str, str]]:
    """``(rule_id, message, category)`` for each rule one added line of a
    released-tool file breaks."""
    for kind, pattern, value in denied:
        if pattern.search(text):
            yield (
                RULE_DENIED,
                f"{_KIND_LABEL[kind].capitalize()} {_shown(value)!r} appears in a released-tool "
                "file; the profile class forbids it in the shipped tree.",
                "data_exposure",
            )
    for match in _HOME_PATH_RE.finditer(text):
        user = match.group("user") or match.group("winuser")
        if user.lower() in PLACEHOLDER_USERS:
            continue
        yield (
            RULE_HOST_PATH,
            f"Absolute host path {_shown(match.group(0))!r} appears in a released-tool file; "
            "the profile class forbids host paths in the shipped tree.",
            "data_exposure",
        )


def _scan_chunk(
    chunk: Chunk,
    profile: RepoProfile,
    denied: Sequence[tuple[str, re.Pattern[str], str]],
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for file in chunk.files:
        if file == PROFILE_PATH:
            # The profile may list the very values it forbids.
            continue
        file_class = profile.class_for(file)
        for line, text in _added_rows(chunk, file):
            if file_class == CLASS_RELEASED_TOOL:
                for rule_id, message, category in _released_tool_hits(text, denied):
                    found.append(
                        _finding(
                            file, line, rule_id, message,
                            _facts(category, confidentiality="partial"),
                        )
                    )
    return found


def rule_statements(profile: RepoProfile, repo_class: str) -> list[str]:
    """The rules of *repo_class* as statements for the reviewer. For the
    internal class, when the profile lists ``tool_packages``, a statement naming
    them is added. Statements produce no finding and name no severity."""
    statements = list(CLASS_RULES[repo_class])
    if repo_class == CLASS_INTERNAL and profile.tool_packages:
        statements.append(
            "Tool packages this repository consumes: "
            + ", ".join(profile.tool_packages)
            + ". In internal paths, judge whether code reaches into their internal modules."
        )
    return statements


def classes_in_effect(profile: RepoProfile) -> list[str]:
    """The classes that govern at least one path of *profile*: its own class and
    each path override's, in the fixed order of ``CLASSES``."""
    present = {profile.repo_class, *(value for _, value in profile.paths)}
    return [name for name in CLASSES if name in present]


def class_rules_section(profile: RepoProfile, files: Sequence[str]) -> str:
    """The "Repository class rules" prompt section for a chunk touching *files*:
    per class present among them, the class's stated rules and the files it
    governs. Empty when *files* is empty. The statements are for the reviewer to
    judge against; this adds no check and no finding."""
    by_class: dict[str, list[str]] = {}
    for file in files:
        by_class.setdefault(profile.class_for(file), []).append(file)
    if not by_class:
        return ""
    parts = [
        "## Repository class rules\n\n"
        "The repository states these rules for the paths below. Judge the diff against "
        "them; report a breach as an ordinary finding with its facts.\n\n"
    ]
    for name in CLASSES:
        governed = by_class.get(name)
        if not governed:
            continue
        shown = governed[:_MAX_RULE_FILES]
        more = len(governed) - len(shown)
        parts.append(f"### {name} paths\n\nFiles in this chunk: {', '.join(shown)}")
        parts.append(f" (and {more} more)\n\n" if more > 0 else "\n\n")
        parts.extend(f"- {statement}\n" for statement in rule_statements(profile, name))
        parts.append("\n")
    return "".join(parts).rstrip("\n") + "\n"


def chunk_class_rules(policy: PolicySource, chunk: Chunk) -> str:
    """The class rules section for *chunk* under *policy*; empty unless the
    profile at the base revision was read and valid. A missing or invalid
    profile is already reported as a finding, so nothing is invented here."""
    if policy.profile_state != PROFILE_OK or policy.repo_profile is None:
        return ""
    return class_rules_section(policy.repo_profile, chunk.files)


def profile_findings(
    policy: PolicySource,
    chunks: Sequence[Chunk],
    settings: ProfileSettings | None = None,
) -> list[dict[str, Any]]:
    """The findings the repo profile at the base revision yields for the diff
    held in *chunks*. *settings* are the deployment's own (review.profile_settings):
    its denial list.
    Empty when the policy could not be read at all, which is reported elsewhere
    as a degraded input."""
    settings = settings or ProfileSettings()
    if policy.profile_state == PROFILE_MISSING:
        return [_missing_finding()]
    if policy.profile_state == PROFILE_INVALID:
        return [_invalid_finding(policy.profile_problem)]
    profile = policy.repo_profile
    if policy.profile_state != PROFILE_OK or profile is None:
        return []

    deny = profile.deny.merged(settings.deny)
    denied = [(kind, _token_regex(value), value) for kind, value in deny.entries()]

    findings: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    suppressed = 0
    for chunk in chunks:
        fresh = []
        for item in _scan_chunk(chunk, profile, denied):
            key = (item["file"], item["line"], item["rule_id"], item["message"])
            if key in seen:
                continue
            seen.add(key)
            if len(findings) + len(fresh) >= MAX_RULE_FINDINGS:
                suppressed += 1
                continue
            fresh.append(item)
        findings.extend(with_fingerprints(fresh, chunk))
    if suppressed and findings:
        first = findings[0]
        findings.append(
            _finding(
                first["file"], first["line"], RULE_MORE,
                f"{suppressed} further repo profile rule hits in this diff are not listed.",
                _facts("maintainability"),
            )
        )
    return findings


__all__ = [
    "MAX_RULE_FINDINGS",
    "PLACEHOLDER_USERS",
    "RULE_DENIED",
    "RULE_HOST_PATH",
    "RULE_INVALID",
    "RULE_MISSING",
    "RULE_MORE",
    "chunk_class_rules",
    "class_rules_section",
    "classes_in_effect",
    "profile_findings",
    "rule_statements",
]
