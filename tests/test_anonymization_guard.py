"""Anonymization grep-guard (lr-5bf2 Slice 0) — enforces the internal-identity
strip on every line of product code and public-facing docs from day one.

This is deliberately a TEST, not a CI-only grep step: it runs in the normal
suite so any local `pytest` invocation catches a hardcode before it is ever
pushed, matching the "make the class unrepresentable" posture used
elsewhere in this platform (an internal deployment's own lr-a943).

Rules (task lr-5bf2 comment, tome #687 §12):

  Product code (src/):
    - deny akuehner.com / private-host literals
    - deny the agent-role identifiers amos|naomi|peaches|bobbie|holden|
      miller|drummer|prax|ashford|roci|tiamut|avasarala|expanse
      (crew-manifest internal cast names — role vocabulary like
      "builder"/"merger"/"reviewer" is fine, the personal names are not),
      including when embedded inside a larger identifier
      (e.g. AMOS_BUILD_TEST_LINT, amos_helper_fn) — a bare `\b` word
      boundary alone does not catch this because `_` is itself a word
      character in regex, so `AMOS_FOO` has no boundary between `AMOS` and
      `_FOO` (lr-2f7a review finding)
    - deny `clagentic` used as an owner-namespace VALUE (e.g. a hardcoded
      `owner == "clagentic"` check or a literal `clagentic/<repo>` git
      remote) — `clagentic` as a brand string (package name, CLI prefix,
      docs prose) is fine and exempt from this specific check
    - deny LORE_* environment variable names
    - deny ~/.lore paths
    - deny the internal component names Sentinel|Archivist|Scribe in prose
      (comments, docstrings, schema descriptions) — these are external
      collector/consumer names, not loadout vocabulary (lr-61b9 review
      finding: the guard did not previously pattern bare component names,
      only paths/env vars/hosts, so a prose mention could slip past it)

  REVERSED (lr-1659 pre-seed scrub, operator-overruled): a bare-repo-name
  pattern for "crew-manifest" in product-code prose was evaluated and
  REJECTED just above at the time of the lr-51d4 review — see the paragraph
  this replaces in earlier revisions of this file — on the reasoning that
  Wave-A slices' "Ported from crew-manifest scripts/..." migration-provenance
  lines were established convention. The operator has since overruled that
  decision, VERBATIM AND EMPHATIC: "NO. FUCKING. CREW. REFERENCES. other
  then the allowed crew config." Every prior "crew-manifest"/crew-tool
  citation in product code and docs was rewritten (lr-1659) to state the
  underlying ENGINEERING FACT without the internal repo/PR pointer — e.g.
  "observed against a Forgejo deployment" rather than "observed live
  crew-manifest PR #486" — and CREW_REFERENCE_PATTERNS below now enforces
  the reversal mechanically so it cannot regress. "gatekeeper" remains
  EXEMPT: it is the name of a real, separately-shipped clagentic product
  (README.md's integration list), not an internal reference, and a bare-word
  pattern on it would produce false positives on every legitimate mention.
  The ONLY crew-related content still permitted in the shipped tree is the
  `.crew/*.yaml` CONFIG FILES THEMSELVES (see "NOTE on .crew/" below) — their
  own internal cast/agent identifiers are exactly what those files exist to
  carry, and are a different question from a crew-manifest PROSE CITATION
  landing in product code or docs.

  Public-facing files (README.md, docs/**, CLAUDE.md) additionally deny:
    - internal repo references: clagentic-brand/, crew-manifest, /workspace/
    - bare internal task ids matching `lr-[0-9a-zA-Z]+` (operator decision,
      lr-1659 review follow-up: internal task ids are pervasive prose
      context in this package's own docs and are NOT guarded in src/ —
      docstrings there keep their ids by explicit operator decision — but a
      public-facing doc citing one is exactly the kind of internal-process
      leakage this guard exists to catch). This pattern is deliberately
      PUBLIC-FACING-ONLY: adding it to PRODUCT_CODE_PATTERNS would fail the
      whole src/ tree by construction, which is not what was decided.

  CLAUDE.md ships publicly (it is the contributor-facing rules document for
  this repo) and is guarded the same as README.md/docs/** — no exemption.

  NOTE on .crew/ (operator decision, recorded here so a future reader does
  not "fix" this): .crew/*.yaml ships as disaster-recovery deployment
  config and is DELIBERATELY excluded from the guarded surface. Those files
  are named after, and contain, real cast/agent identifiers by design — that
  is their entire content. Adding .crew/ to the guard would fail it by
  construction, because the guard's whole purpose is to deny exactly the
  identifiers .crew/ is supposed to carry. The guard governs product code
  and public prose (what ships as the *tool*), not a deployment's own cast
  config (what ships as *disaster-recovery data* alongside it). Do not add
  .crew/'s bare directory to the PUBLICATION-SCOPE sweep below either — but
  note that sweep DOES walk into `.crew/*.yaml`'s own PROSE (comments/
  docstring-equivalent yaml `#` lines) for the narrower "andy"/crew-manifest-
  citation patterns, since those are a leaked-identity/reversed-decision
  question about specific STRINGS, not the "this file legitimately carries
  cast names" question this note is about — see PUBLICATION_SCOPE_EXEMPT_FILES
  below for the precise (narrow) boundary: `.crew/*.yaml` files are swept for
  CREW_REFERENCE_PATTERNS/andy but never for AGENT_ROLE_IDENTIFIERS (their
  own cast/role names, which are exactly what they exist to carry).

  PUBLICATION-SCOPE COVERAGE (lr-1659 durable fix, task requirement 7): every
  guard above this point is an INCLUSION list (src/, README.md, CLAUDE.md,
  docs/**) that was hand-assembled slice by slice and drifted out of step
  with the full tracked-file set — every file tracked in this repo ships the
  moment the repo itself is published, because everything tracked ships when
  this repo is published (there is no separate publication manifest or
  exclusion list to reconcile against; the shipped set IS the tracked set).
  133 of 279 tracked files at one point had NO guard coverage at all
  (lr-1659 seq 20/21 discovery: the guard's hand-assembled inclusion list and
  the repo's actual tracked-file set were never reconciled against each
  other). The fix is `PUBLICATION_SCOPE_FILES` below: it calls
  `git ls-tree -r --name-only HEAD` directly — the same primitive any
  publication of this repo's tracked tree would use to enumerate what ships
  — so the guarded set can never again drift behind the shipped set — a new
  tracked file is automatically in scope the moment it is committed, with NO
  guard-file edit required. The complement is a documented EXEMPTION list
  (`PUBLICATION_SCOPE_EXEMPT_FILES`/`PUBLICATION_SCOPE_EXEMPT_SUFFIXES`), not
  an inclusion list — per this module's own "WHY MECHANICAL AND NOT REVIEW"
  argument and the DOCSTRING-EXCLUSION precedent already set by the AST
  guard below: an inclusion list drifts silently (a new file is invisible
  until someone remembers to add it), an exemption list fails loudly (a new
  file is swept by default; only a reviewed, named exemption opts it out).
  Binary media assets (`PUBLICATION_SCOPE_EXEMPT_SUFFIXES` — `.png`, `.svg`)
  cannot be read as UTF-8 text at all and are excluded on that structural
  basis, not a policy judgment about their content. LICENSE is exempt by
  name (task requirement 1: the copyright notice naming the licensor is the
  POINT of that file, verified against the sibling clagentic-gatekeeper
  repo's byte-identical header — never add an "andy" pattern that would trip
  on it). PUBLICATION_SCOPE_PATTERNS applies CREW_REFERENCE_PATTERNS (the
  item-3 reversal above) and the "andy" operator-name pattern to this
  mechanically-derived file set; it deliberately does NOT re-apply
  AGENT_ROLE_IDENTIFIERS (`.crew/*.yaml`'s own legitimate cast content would
  fail it by construction, same reasoning as the pre-existing .crew/
  exemption above) or the task-id pattern (CLAUDE.md rule 8's docstring/
  hash-comment boundary, unchanged and still correct per lr-1659 seq 21 item
  7 — task ids in comments/docstrings across the ~2400-instance/220-file
  surface remain policy-permitted and OUT OF SCOPE for this sweep, except
  where item 6's propagating templates are concerned, which get their own
  dedicated test below).

  USER-FACING-STRING AST GUARD (task lr-3160c0): src/ docstrings and hash-
  comments keep their internal task ids by explicit operator decision (see
  "Product code (src/)" above and PRODUCT_CODE_PATTERNS's exemption) — but
  that exemption was never meant to cover a string an external CLI user
  actually reads. A bare line-based regex over src/ cannot tell a docstring
  from a raised/printed string apart (both are just lines of text); this is
  exactly why 84 internal-id/repo-name leaks into argparse help text and
  raised/printed runtime strings survived FIVE review rounds on PR #142
  before task lr-3160c0's dedicated AST-based inventory pass finally caught
  them all in one shot (see lr-3160c0 and lr-1659 seq 12-15 for the
  discovery narrative).

  USER_FACING_AST_PATTERNS below is `PRODUCT_CODE_PATTERNS` (unmodified,
  same dict) plus the task-id pattern already declared once in
  `PUBLIC_FACING_EXTRA_PATTERNS` — reused by reference, never redeclared, so
  there is exactly one place that defines what an "internal identifier"
  looks like.

  `_walk_user_facing_strings` is keyed on exactly ONE structural exclusion,
  not an inclusion list of call/return shapes: it walks EVERY string/
  f-string `Constant` node in the module's AST, MINUS every node that is a
  DOCSTRING (the leading `Expr(Constant(str))` statement of the Module, or
  of a `ClassDef`/`FunctionDef`/`AsyncFunctionDef` body — the exact set
  `ast.get_docstring` recognizes). An inclusion-list approach (name every
  shape a user-facing string can appear in — raise, print, argparse help,
  this package's own `_fail(...)`-wrapped-raise convention, the pervasive
  `(ok, reason)` tuple-return checker-function convention, a helper
  function's bare `return f"..."` whose caller wraps it in `raise(...)`, a
  summary string built once and later passed to `CheckResult(summary=...)`
  or `errors.append(...)`) was tried first and repeatedly under-caught: this
  codebase alone uses at least six materially different shapes to carry a
  user-facing string to its eventual raise/print/CheckResult/argparse sink,
  and a naming-convention-agnostic project cannot enumerate that list
  exhaustively without it becoming exactly the kind of hand-maintained,
  driftable inventory this task's own "WHY MECHANICAL AND NOT REVIEW"
  argument (module docstring above) says a guard must never be. The
  DOCSTRING EXCLUSION, by contrast, is a single, exhaustively-defined
  Python-grammar shape (`ast.get_docstring`'s own recognized position) that
  can never miss a new sink shape a future contributor invents, because it
  is not enumerating sinks at all — it is naming the ONE position a string
  is exempt from, and treating every other string constant as in scope.
  This is the same "deny unless provably safe" posture PRODUCT_CODE_PATTERNS
  and PUBLIC_FACING_EXTRA_PATTERNS already apply at the line level; here it
  is applied at the AST-node level instead. The identifier-pattern regex
  (USER_FACING_AST_PATTERNS itself) is what keeps this from being noisy: a
  benign internal string (a regex pattern, a config dict key, a log label)
  is swept into the walk but produces no violation because it matches none
  of the identifier patterns — only a string that ACTUALLY carries an
  internal identifier or repo name is ever reported. A false positive here
  is a nuisance (an internal-only string that happens to embed something
  matching an identifier pattern, requiring one added exemption); a false
  negative is the exact bug this check exists to prevent — the module
  docstring's own stated trade-off, taken to its logical conclusion.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

AGENT_ROLE_IDENTIFIERS = (
    "amos",
    "naomi",
    "peaches",
    "bobbie",
    "holden",
    "miller",
    "drummer",
    "prax",
    "ashford",
    "roci",
    "tiamut",
    "avasarala",
    "expanse",
)

# Product-code-only patterns (src/).
PRODUCT_CODE_PATTERNS: dict[str, re.Pattern[str]] = {
    "operator host literal": re.compile(r"akuehner\.com|akuehner-", re.IGNORECASE),
    # Two forms are needed because `_` is a word character in regex, so a
    # bare `\b` boundary never fires between a cast name and an adjacent
    # underscore (e.g. AMOS_BUILD_TEST_LINT would slip past `\bamos\b`):
    #   1. the plain word-boundary form, for a name standing alone or
    #      adjacent to non-word characters/punctuation.
    #   2. an underscore-adjacent form, for a name embedded in a
    #      SCREAMING_SNAKE_CASE or snake_case identifier (name preceded
    #      and/or followed by `_`).
    "internal agent-role identifier": re.compile(
        r"\b(" + "|".join(AGENT_ROLE_IDENTIFIERS) + r")\b"
        r"|(?:^|_)(" + "|".join(AGENT_ROLE_IDENTIFIERS) + r")(?:_|$)",
        re.IGNORECASE,
    ),
    "clagentic as a hardcoded owner-namespace value": re.compile(
        r'["\']clagentic["\']\s*(==|in\s+\()|clagentic/[a-zA-Z0-9_.-]+\.git'
    ),
    "LORE_* environment variable": re.compile(r"\bLORE_[A-Z_]+\b"),
    "~/.lore path": re.compile(r"~/\.lore\b"),
    "internal component name (Sentinel/Archivist/Scribe)": re.compile(
        r"\b(Sentinel|Archivist|Scribe)\b"
    ),
}

# Additional patterns for public-facing files (README, docs/**), layered on
# top of the product-code patterns above.
PUBLIC_FACING_EXTRA_PATTERNS: dict[str, re.Pattern[str]] = {
    "internal repo reference (clagentic-brand/)": re.compile(r"clagentic-brand/"),
    "internal repo reference (crew-manifest)": re.compile(r"crew-manifest"),
    "internal path reference (/workspace/)": re.compile(r"/workspace/"),
    # Public-facing-only (operator decision): src/ docstrings keep their
    # internal task ids deliberately and are NOT covered by this pattern —
    # it lives here, never in PRODUCT_CODE_PATTERNS.
    "internal task id (lr-NNNNNN)": re.compile(r"\blr-[0-9a-zA-Z]+\b"),
}

# Crew-reference deny patterns (lr-1659 item 3, REVERSAL of the lr-51d4
# rejection recorded and superseded in the module docstring above). Every
# name here is an internal repo/script reference that must never appear in
# a shipped file OUTSIDE the `.crew/*.yaml` config files themselves (see
# PUBLICATION_SCOPE_EXEMPT_FILES below for that narrow carve-out).
CREW_REFERENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "internal repo reference (crew-manifest)": re.compile(r"crew-manifest"),
    "internal script reference (crew_merge.py)": re.compile(r"crew_merge\.py"),
    "internal script reference (crew_run.py)": re.compile(r"crew_run\.py"),
    "internal script reference (ship.py)": re.compile(r"\bship\.py\b"),
}

# Operator-name deny pattern (lr-1659 item 2): "andy" as an operator name in
# shipped prose/comments/fixtures. Word-boundary only — never matches inside
# an unrelated longer word. LICENSE is the sole exemption (task requirement
# 1; see PUBLICATION_SCOPE_EXEMPT_FILES).
ANDY_OPERATOR_NAME_PATTERN: dict[str, re.Pattern[str]] = {
    "operator name (andy)": re.compile(r"\bandy\b", re.IGNORECASE),
}

# Combined PUBLICATION-SCOPE sweep patterns (lr-1659 item 7):
# CREW_REFERENCE_PATTERNS + the andy pattern, applied to the mechanically-
# derived tracked-file set below. Deliberately EXCLUDES AGENT_ROLE_IDENTIFIERS
# (would fail .crew/*.yaml by construction — those files' cast names are
# their entire legitimate content) and the task-id pattern (CLAUDE.md rule
# 8's docstring/hash-comment boundary is unchanged; see module docstring).
PUBLICATION_SCOPE_PATTERNS: dict[str, re.Pattern[str]] = {
    **CREW_REFERENCE_PATTERNS,
    **ANDY_OPERATOR_NAME_PATTERN,
}

# Files exempt from the PUBLICATION-SCOPE sweep by name (lr-1659 item 7).
# Every entry is REVIEWED and NAMED — never a silent inclusion-list gap:
#   - LICENSE: task requirement 1, "andy" is Andy Kuehner's copyright notice,
#     the POINT of the file (verified against clagentic-gatekeeper's
#     byte-identical FSL-1.1-MIT header) — never scrubbed.
#   - .crew/*.yaml: NOT exempt from PUBLICATION_SCOPE_PATTERNS (their own
#     crew-manifest-citation/andy prose IS in scope and was scrubbed by this
#     same task — see .crew/naomi.yaml's own post-scrub content) — but they
#     ARE structurally exempt from ever being treated as "a crew reference
#     that must be removed" for their OWN existence/filename, which is a
#     different question the module docstring's ".crew/" note already
#     answers. No entry needed here for that: PUBLICATION_SCOPE_PATTERNS
#     never matches a bare filename, only file CONTENT, so `.crew/*.yaml`
#     files are swept like any other tracked file and simply pass now that
#     their content has been scrubbed.
#   - tests/test_anonymization_guard.py (this file): the guard that DEFINES
#     CREW_REFERENCE_PATTERNS/ANDY_OPERATOR_NAME_PATTERN necessarily NAMES
#     every forbidden string in its own pattern definitions, module
#     docstring, and regression tests (test_publication_scope_guard_catches_*)
#     — the same self-reference exemption every pattern table in this module
#     already implicitly needs (PRODUCT_CODE_PATTERNS's own module is never
#     swept by PRODUCT_CODE_PATTERNS either, since it lives in tests/, not
#     src/). Explicit here because the PUBLICATION-SCOPE sweep is the first
#     check in this file broad enough to reach its own source.
PUBLICATION_SCOPE_EXEMPT_FILES: frozenset[str] = frozenset(
    {"LICENSE", "tests/test_anonymization_guard.py"}
)

# Suffixes that cannot be read as UTF-8 text at all (binary media assets) —
# a structural exemption, not a policy judgment about content.
PUBLICATION_SCOPE_EXEMPT_SUFFIXES: frozenset[str] = frozenset(
    {".png", ".svg", ".ico", ".jpg", ".jpeg"}
)


def _publication_scope_tracked_files() -> list[Path]:
    """Return every file `git ls-tree -r --name-only HEAD` reports at
    REPO_ROOT, minus PUBLICATION_SCOPE_EXEMPT_FILES/
    PUBLICATION_SCOPE_EXEMPT_SUFFIXES — every file tracked in this repo,
    because everything tracked ships when this repo is published (see module
    docstring "PUBLICATION-SCOPE COVERAGE"). A git-unavailable or non-repo
    environment (e.g. a source tarball with no `.git`) degrades to an empty
    list rather than failing collection — this sweep is additive coverage on
    top of the pre-existing inclusion-list checks above, which still run
    regardless."""
    import subprocess

    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", "HEAD"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    for line in result.stdout.splitlines():
        rel = line.strip()
        if not rel:
            continue
        candidate = REPO_ROOT / rel
        if rel in PUBLICATION_SCOPE_EXEMPT_FILES:
            continue
        if candidate.suffix in PUBLICATION_SCOPE_EXEMPT_SUFFIXES:
            continue
        if not candidate.is_file():
            # A tracked path that is not currently a regular file on disk
            # (e.g. this test running against a worktree mid-edit) is
            # skipped rather than erroring collection.
            continue
        paths.append(candidate)
    return paths


def _tracked_files(subdir: str, suffixes: tuple[str, ...] | None = None) -> list[Path]:
    base = REPO_ROOT / subdir
    if not base.exists():
        return []
    files = [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    if suffixes is not None:
        files = [p for p in files if p.suffix in suffixes]
    return files


def _product_config_files() -> list[Path]:
    """Repo-root config files that ship as part of the product surface
    (dependency list, console-script entry points) and so are just as
    subject to the identity guard as src/ modules — not just docs."""
    candidates = [REPO_ROOT / "pyproject.toml"]
    return [p for p in candidates if p.exists()]


def _public_facing_files() -> list[Path]:
    # CLAUDE.md ships as the public contributor rules document (see module
    # docstring) and is guarded the same as README.md/docs/**; .crew/ is
    # deliberately never included here (see module docstring).
    candidates = [REPO_ROOT / "README.md", REPO_ROOT / "CLAUDE.md"]
    candidates += _tracked_files("docs", suffixes=(".md",))
    return [p for p in candidates if p.exists()]


def _check_patterns(path: Path, patterns: dict[str, re.Pattern[str]]) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        display_path = path.relative_to(REPO_ROOT)
    except ValueError:
        display_path = path
    violations = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for label, pattern in patterns.items():
            if pattern.search(line):
                violations.append(f"{display_path}:{line_no}: [{label}] {line.strip()}")
    return violations


@pytest.mark.parametrize(
    "path",
    _tracked_files("src", suffixes=(".py", ".json")) + _product_config_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_product_code_has_no_internal_identity_hardcodes(path: Path) -> None:
    """Covers .py modules AND packaged product artifacts such as the
    published JSON schemas under src/clagentic_loadout/schemas/ (lr-8edc) —
    those ship as part of the contract and are just as subject to this
    guard as source code. Also covers pyproject.toml (lr-2f7a) — the
    console-script entry points and dependency list are as much a public
    contract surface as anything under src/."""
    violations = _check_patterns(path, PRODUCT_CODE_PATTERNS)
    assert not violations, "\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "path",
    _public_facing_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_public_facing_docs_have_no_internal_identity_or_repo_hardcodes(path: Path) -> None:
    combined = {**PRODUCT_CODE_PATTERNS, **PUBLIC_FACING_EXTRA_PATTERNS}
    violations = _check_patterns(path, combined)
    assert not violations, "\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "path",
    _publication_scope_tracked_files(),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_publication_scope_files_have_no_crew_reference_or_operator_name(path: Path) -> None:
    """The lr-1659 durable fix (task requirement 7): every tracked file in
    this repo (`git ls-tree -r --name-only HEAD`, minus the narrow named/
    structural exemptions in PUBLICATION_SCOPE_EXEMPT_FILES/
    PUBLICATION_SCOPE_EXEMPT_SUFFIXES — see module docstring
    "PUBLICATION-SCOPE COVERAGE") must carry no crew-manifest/crew-tool
    reference and no "andy" operator name. This is DELIBERATELY broader than
    the src/README/docs/CLAUDE.md inclusion lists above (tests/,
    .crew/*.yaml, .clagentic/, .claude/, scripts/, .lore, media/*.svg text
    content, etc. are all in scope here even though several of those
    directories have no other guard coverage at all) — see this test's own
    file-set fixture for how that coverage is derived mechanically rather
    than hand-maintained."""
    violations = _check_patterns(path, PUBLICATION_SCOPE_PATTERNS)
    assert not violations, "\n" + "\n".join(violations)


def test_publication_scope_file_set_matches_tracked_file_count() -> None:
    """Sanity check that `_publication_scope_tracked_files` is actually
    deriving from git (not silently degrading to an empty list, which would
    make `test_publication_scope_files_have_no_crew_reference_or_operator_name`
    above parametrize to zero cases and pass vacuously). Asserts a floor
    rather than an exact count so this does not need editing every time a
    file is added to the tree."""
    files = _publication_scope_tracked_files()
    assert len(files) > 200, (
        f"expected the publication-scope sweep to discover the full "
        f"tracked-file set via `git ls-tree`, got only {len(files)} files "
        f"-- this usually means git was unavailable and the fixture "
        f"silently degraded to an empty/partial list rather than genuinely "
        f"sweeping the tree."
    )


def test_publication_scope_exempt_suffixes_are_never_readable_as_text() -> None:
    """Regression guard for PUBLICATION_SCOPE_EXEMPT_SUFFIXES itself: every
    tracked file with an exempt suffix must actually be binary (or at least
    not something the sweep silently mis-skipped for a text file). This does
    not assert non-UTF8-decodability (a truly empty or ASCII-only .png
    fixture is unlikely but not impossible) -- it asserts the exempted files
    are the ones this repo tracks with those suffixes today, so a future
    contributor adding a NEW, genuinely textual file under one of these
    suffixes notices the gap rather than it silently going unswept."""
    import subprocess

    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "HEAD"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.skip("git ls-tree unavailable in this environment")
    exempt_hits = [
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip() and Path(line.strip()).suffix in PUBLICATION_SCOPE_EXEMPT_SUFFIXES
    ]
    for rel in exempt_hits:
        assert (REPO_ROOT / rel).suffix in (".png", ".svg", ".ico", ".jpg", ".jpeg")


def test_guard_actually_finds_something_when_present(tmp_path: Path) -> None:
    """Regression test for the guard itself: a synthetic file containing a
    known-bad literal must be caught, so a future refactor of the pattern
    table can't silently produce a guard that always passes."""
    bad_file = tmp_path / "synthetic_bad.py"
    bad_file.write_text('HOST = "runner.akuehner.com"\n')
    violations = _check_patterns(bad_file, PRODUCT_CODE_PATTERNS)
    assert violations


@pytest.mark.parametrize(
    "literal",
    [
        "crew-manifest",
        "crew_merge.py",
        "crew_run.py",
        "ship.py",
        "/workspace/crew-manifest/scripts/",
        "crew-manifest PR #486",
    ],
)
def test_publication_scope_guard_catches_crew_reference(literal: str, tmp_path: Path) -> None:
    """Regression test for the lr-1659 item-3 reversal: every crew-reference
    shape the operator named (crew-manifest, crew_merge.py, crew_run.py,
    ship.py, and a /workspace/crew-manifest/scripts/ path) must be caught by
    CREW_REFERENCE_PATTERNS/PUBLICATION_SCOPE_PATTERNS."""
    bad_file = tmp_path / "synthetic_crew_ref.py"
    bad_file.write_text(f"# {literal}\n")
    violations = _check_patterns(bad_file, PUBLICATION_SCOPE_PATTERNS)
    assert violations, f"expected {literal!r} to be caught by PUBLICATION_SCOPE_PATTERNS"


def test_publication_scope_guard_does_not_flag_gatekeeper() -> None:
    """clagentic-gatekeeper is a REAL, separately-shipped public product
    (task requirement 3, explicit exemption) -- CREW_REFERENCE_PATTERNS must
    never match it, even though it shares the "clagentic-" prefix crew
    references also happen to share here."""
    text = "See clagentic-gatekeeper for the sibling product.\n"
    for label, pattern in CREW_REFERENCE_PATTERNS.items():
        assert not pattern.search(text), (
            f"CREW_REFERENCE_PATTERNS[{label!r}] unexpectedly matched a "
            f"legitimate clagentic-gatekeeper mention"
        )


def test_publication_scope_guard_catches_andy_operator_name(tmp_path: Path) -> None:
    """Regression test for the lr-1659 item-2 andy deny pattern."""
    bad_file = tmp_path / "synthetic_andy.py"
    bad_file.write_text("# operator decision, andy, 2026-07-30\n")
    violations = _check_patterns(bad_file, PUBLICATION_SCOPE_PATTERNS)
    assert violations, "expected a bare 'andy' operator-name mention to be caught"


def test_publication_scope_guard_license_is_exempt() -> None:
    """Task requirement 1: LICENSE's 'Copyright 2026 Andy Kuehner' notice is
    the correct, intended content and must be excluded from the sweep by
    name, not by weakening ANDY_OPERATOR_NAME_PATTERN itself (which must
    stay able to catch 'andy' everywhere else)."""
    swept = {p.name for p in _publication_scope_tracked_files() if p.parent == REPO_ROOT}
    assert "LICENSE" not in swept, (
        "LICENSE must be excluded from the publication-scope sweep via "
        "PUBLICATION_SCOPE_EXEMPT_FILES, not left to accidentally pass the "
        "andy pattern"
    )


#: Task requirement 6: files verbatim-COPIED into every downstream repo by
#: /loadout-init or an integrator's own copy step. A bare internal task id
#: landing in one of these is the highest-propagation leak class in the
#: tree — it does not just sit in this repo's own history, it gets carried
#: into every repo that runs /loadout-init or copies the .example file.
PROPAGATING_TEMPLATE_FILES: tuple[Path, ...] = (
    REPO_ROOT / "src" / "clagentic_loadout" / "loadout_init" / "starter_config.yaml",
    REPO_ROOT / ".clagentic" / "loadout" / "config.yaml.example",
)


@pytest.mark.parametrize(
    "path",
    [p for p in PROPAGATING_TEMPLATE_FILES if p.exists()],
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_propagating_templates_carry_no_internal_task_id(path: Path) -> None:
    """Task requirement 6: starter_config.yaml (copied verbatim into every
    downstream repo by /loadout-init) and .clagentic/loadout/config.yaml.example
    (the template external integrators copy) must carry NO bare internal
    task id — even though CLAUDE.md rule 8 otherwise permits task ids in
    comments/docstrings elsewhere in the tree. These two files are the
    exception: a task id here does not stay in THIS repo's provenance, it
    propagates into every repo that copies it. This is a narrower,
    dedicated pattern application (not folded into PUBLICATION_SCOPE_PATTERNS,
    which deliberately excludes the task-id pattern for the general case
    per CLAUDE.md rule 8)."""
    violations = _check_patterns(
        path, {"internal task id (lr-NNNNNN)": PUBLIC_FACING_EXTRA_PATTERNS["internal task id (lr-NNNNNN)"]}
    )
    assert not violations, "\n" + "\n".join(violations)


@pytest.mark.parametrize(
    "literal",
    [
        "AMOS_BUILD_TEST_LINT",
        "amos_build_test_lint",
        "BUILD_AMOS_LINT",
        "_amos_helper",
        "naomi_merge_gate",
        "PEACHES_REVIEW_TIMEOUT",
        "ASHFORD_APPROVAL_ACKED_BY",
        "ashford_install_binary",
        "ROCI_TRANSPORT_TIMEOUT",
        "tiamut_harvest_run",
        "AVASARALA_PLAN_SCOPE",
        "EXPANSE_CAST_REGISTRY",
    ],
)
def test_guard_catches_cast_name_embedded_in_identifier(literal: str, tmp_path: Path) -> None:
    """Regression test for the lr-2f7a blind spot: a bare `\\b(amos|...)\\b`
    word-boundary pattern does NOT fire inside a SCREAMING_SNAKE_CASE or
    snake_case identifier, because `_` is itself a word character (no
    boundary exists between `AMOS` and `_BUILD`). Each of these synthetic
    identifiers embeds a cast name and must still be caught."""
    bad_file = tmp_path / "synthetic_underscore_bad.py"
    bad_file.write_text(f"{literal} = 1\n")
    violations = _check_patterns(bad_file, PRODUCT_CODE_PATTERNS)
    assert violations, f"expected {literal!r} to be caught by the identity guard"


def test_public_facing_guard_catches_bare_internal_task_id(tmp_path: Path) -> None:
    """Regression test for the public-facing-only task-id pattern (operator
    decision, lr-1659 review follow-up): a bare `lr-NNNNNN` citation in a
    public-facing doc must be caught by PUBLIC_FACING_EXTRA_PATTERNS."""
    bad_file = tmp_path / "synthetic_public_doc.md"
    bad_file.write_text("This behavior was fixed in lr-abc123.\n")
    violations = _check_patterns(bad_file, PUBLIC_FACING_EXTRA_PATTERNS)
    assert violations, "expected a bare lr-NNNNNN task id to be caught"


def test_product_code_patterns_do_not_include_task_id_pattern() -> None:
    """The task-id pattern is deliberately PUBLIC-FACING-ONLY (operator
    decision: src/ docstrings keep their internal task ids) — this asserts
    PRODUCT_CODE_PATTERNS never gains it by an accidental future merge of
    the two pattern tables."""
    bad_file_text = "This behavior was fixed in lr-abc123.\n"
    for label, pattern in PRODUCT_CODE_PATTERNS.items():
        assert not pattern.search(bad_file_text), (
            f"PRODUCT_CODE_PATTERNS[{label!r}] unexpectedly matches a bare "
            f"task id -- the task-id guard must stay public-facing-only "
            f"(PUBLIC_FACING_EXTRA_PATTERNS), never applied to src/."
        )


# ---------------------------------------------------------------------------
# USER-FACING-STRING AST GUARD (task lr-3160c0) — see module docstring
# "USER-FACING-STRING AST GUARD" section for the full design rationale.
#
# This is a SEPARATE, structural dimension beside the line-based sweep
# above: PRODUCT_CODE_PATTERNS/PUBLIC_FACING_EXTRA_PATTERNS operate on raw
# lines and are deliberately blind to whether a line sits inside a
# docstring (sanctioned) or a raised/printed/argparse-help string (a leak an
# external CLI user actually reads). Reusing PRODUCT_CODE_PATTERNS by
# reference (not copying it) means the identifier definitions never drift
# into two disagreeing sources of truth; the task-id pattern is reused the
# same way from PUBLIC_FACING_EXTRA_PATTERNS, the ONE place it is declared.
# ---------------------------------------------------------------------------

#: The identifier patterns this AST guard checks a user-facing string
#: against. `PRODUCT_CODE_PATTERNS` already covers operator hosts, agent-role
#: identifiers, the clagentic-owner-value shape, LORE_* env vars, ~/.lore
#: paths, and the Sentinel/Archivist/Scribe component names — none of those
#: belong in a string a CLI user reads either, so they apply here unchanged.
#: The task-id pattern is pulled from PUBLIC_FACING_EXTRA_PATTERNS (the one
#: place it is declared) rather than redeclared, per this guard's own
#: reuse-not-duplicate design note above: a bare `lr-NNNNNN` in a raised
#: string or a --help line is exactly the same class of leak as one in a
#: public-facing doc.
USER_FACING_AST_PATTERNS: dict[str, re.Pattern[str]] = {
    **PRODUCT_CODE_PATTERNS,
    "internal task id (lr-NNNNNN)": PUBLIC_FACING_EXTRA_PATTERNS["internal task id (lr-NNNNNN)"],
}


#: Node types that can carry a docstring as their FIRST body statement,
#: exactly the set `ast.get_docstring` recognizes (Module, ClassDef,
#: FunctionDef, AsyncFunctionDef) — the single structural exclusion
#: `_walk_user_facing_strings` applies (see module docstring).
_DOCSTRING_HOLDER_TYPES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_constant_ids(tree: ast.AST) -> set[int]:
    """Return the `id()` of every `Constant` node in *tree* that IS a
    docstring — the value of the leading `Expr(Constant(str))` statement of
    a Module/ClassDef/FunctionDef/AsyncFunctionDef body. `id()` (object
    identity within this single freshly-parsed tree, never reused or
    garbage-collected before `_walk_user_facing_strings` finishes with it)
    is exact where a `(lineno, col_offset)` tuple is merely usually-unique."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, _DOCSTRING_HOLDER_TYPES) and node.body:
            first = node.body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                ids.add(id(first.value))
    return ids


def _walk_user_facing_strings(tree: ast.AST) -> list[ast.Constant]:
    """Return every string/f-string `Constant` node in *tree* that is NOT a
    docstring (module docstring "USER-FACING-STRING AST GUARD": a single
    structural EXCLUSION — the leading `Expr(Constant(str))` statement of a
    Module/ClassDef/FunctionDef/AsyncFunctionDef body — rather than an
    inclusion list of raise/print/argparse/tuple-return/etc. shapes, because
    this codebase alone uses at least six materially different shapes to
    carry a user-facing string to its eventual sink and an inclusion list
    cannot be proven exhaustive; a docstring exclusion can, since it names
    the one Python-grammar position a string is exempt from rather than
    enumerating where a string might end up)."""
    docstring_ids = _docstring_constant_ids(tree)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstring_ids
    ]


def _check_user_facing_ast(path: Path) -> list[str]:
    """Parse *path* and return one violation string per user-facing string
    literal that matches USER_FACING_AST_PATTERNS, formatted the same way as
    `_check_patterns` (file:line: [label] text) for consistent failure
    output between the line-based and AST-based checks."""
    text = path.read_text(encoding="utf-8")
    try:
        display_path = path.relative_to(REPO_ROOT)
    except ValueError:
        display_path = path
    tree = ast.parse(text, filename=str(path))
    violations = []
    for const in _walk_user_facing_strings(tree):
        for label, pattern in USER_FACING_AST_PATTERNS.items():
            if pattern.search(const.value):
                snippet = const.value.strip().replace("\n", " ")[:120]
                violations.append(f"{display_path}:{const.lineno}: [{label}] {snippet}")
    return violations


@pytest.mark.parametrize(
    "path",
    _tracked_files("src", suffixes=(".py",)),
    ids=lambda p: str(p.relative_to(REPO_ROOT)),
)
def test_user_facing_strings_have_no_internal_identity_hardcodes(path: Path) -> None:
    """The mechanical-enforcement half of task lr-3160c0: a raised
    exception message, a print() argument, or an argparse help/description/
    epilog/usage string must never contain an internal identifier or a bare
    internal task id — those are read by an external CLI user, unlike the
    module/function docstrings this guard structurally never walks into
    (see module docstring "USER-FACING-STRING AST GUARD"). Keyed on AST
    node type (Raise / print-Call / add_argument-and-ArgumentParser-Call),
    never a hand-maintained file or line list, so this cannot drift the way
    a manually curated inventory would."""
    violations = _check_user_facing_ast(path)
    assert not violations, "\n" + "\n".join(violations)


def test_user_facing_ast_guard_catches_raised_task_id(tmp_path: Path) -> None:
    """Regression test for the AST guard itself: a bare internal task id
    inside a raised exception message must be caught, even though the same
    id in a docstring two lines above is exempt."""
    bad_file = tmp_path / "synthetic_raise_bad.py"
    bad_file.write_text(
        '"""Module docstring citing lr-abc123 -- exempt, never checked here."""\n'
        "def f():\n"
        '    raise ValueError("boom (lr-abc123)")\n'
    )
    tree = ast.parse(bad_file.read_text())
    hits = _walk_user_facing_strings(tree)
    assert any("lr-abc123" in c.value for c in hits), (
        "expected the raised string's task id to be walked by "
        "_walk_user_facing_strings"
    )
    violations = _check_user_facing_ast(bad_file)
    assert violations, "expected the AST guard to flag the raised task id"


def test_user_facing_ast_guard_catches_print_and_argparse_help(tmp_path: Path) -> None:
    """Regression test: a print() argument and an argparse help= string
    each carrying an internal identifier must both be caught."""
    bad_file = tmp_path / "synthetic_print_argparse_bad.py"
    bad_file.write_text(
        "import argparse\n"
        "print('routed via amos for this build (lr-def456)')\n"
        "p = argparse.ArgumentParser(description='ported from lr-ghi789')\n"
        "p.add_argument('--x', help='see lr-jkl012 for background')\n"
    )
    violations = _check_user_facing_ast(bad_file)
    assert len(violations) >= 3, (
        f"expected the print(), ArgumentParser(description=...), and "
        f"add_argument(help=...) strings all to be caught, got: {violations}"
    )


def test_user_facing_ast_guard_ignores_docstrings(tmp_path: Path) -> None:
    """Regression test for the guard's own docstring exemption (module
    docstring: 'no exemption logic is needed or added' -- a docstring
    structurally cannot appear inside a Raise/print/add_argument-or-
    ArgumentParser-Call shape). A module, class, and function docstring each
    citing an internal id must produce ZERO violations."""
    clean_file = tmp_path / "synthetic_docstrings_only.py"
    clean_file.write_text(
        '"""Module docstring, ported from lr-mod111."""\n'
        "\n"
        "class Foo:\n"
        '    """Class docstring, ported from lr-cls222."""\n'
        "\n"
        "    def bar(self):\n"
        '        """Function docstring, ported from lr-fn333."""\n'
        "        return 1\n"
    )
    violations = _check_user_facing_ast(clean_file)
    assert not violations, f"docstrings must never be flagged, got: {violations}"


def test_user_facing_ast_guard_derives_task_id_pattern_from_public_facing_table() -> None:
    """Assert USER_FACING_AST_PATTERNS reuses PUBLIC_FACING_EXTRA_PATTERNS's
    task-id pattern object by identity, never a second, independently
    compiled copy — the module docstring's 'derive the identifier pattern
    from the guard's existing pattern definition rather than declaring a
    second source of truth' requirement, made mechanically checkable."""
    assert (
        USER_FACING_AST_PATTERNS["internal task id (lr-NNNNNN)"]
        is PUBLIC_FACING_EXTRA_PATTERNS["internal task id (lr-NNNNNN)"]
    )


# ---------------------------------------------------------------------------
# COMMIT-SUBJECT GUARD — a commit subject's trailing `(#123)` PR reference is
# parsed and rendered as a structured link in the generated changelog (see
# this repo's own CLAUDE.md "Commit convention" section); a bare internal
# task id in a subject can therefore land in a PUBLISHED CHANGELOG on a
# public product, where it resolves to nothing for an external reader — the
# exact boundary CLAUDE.md hard rule 8 draws. PR #6 originally shipped with
# internal task ids in commit SUBJECTS (caught and bounced by hand); this is
# the mechanical guard against a recurrence.
#
# SUBJECTS ONLY, DELIBERATELY: a commit BODY is the sanctioned home for a
# `Task: lr-XXXXXX` provenance trailer (this repo's own established
# convention, followed throughout this branch's own commits) — the trailer
# never reaches a changelog entry, only the subject line does. Widening this
# guard to bodies would break the very convention it exists to protect.
#
# REUSES the task-id pattern already declared once in
# PUBLIC_FACING_EXTRA_PATTERNS (the same object PRODUCT_CODE_PATTERNS/
# USER_FACING_AST_PATTERNS above already reuse by reference) — no second,
# independently-maintained pattern list.
# ---------------------------------------------------------------------------

_TASK_ID_SUBJECT_PATTERN: re.Pattern[str] = PUBLIC_FACING_EXTRA_PATTERNS["internal task id (lr-NNNNNN)"]


def _commit_subjects_ahead_of_merge_base() -> tuple[list[str], str | None]:
    """Return (subjects, skip_reason) for every commit strictly ahead of this
    branch's merge-base with `origin/main`.

    DEGRADES GRACEFULLY, never fails closed, on any condition that makes the
    merge-base unresolvable: no git binary, not a git repository, no `origin`
    remote configured, `origin/main` not present locally (a shallow clone or
    a CI checkout without a full fetch), or HEAD detached with nothing to
    diff against. Each such condition returns `([], "<reason>")` rather than
    raising — CLAUDE.md hard rule 6 requires this suite to pass with
    synthetic identity and no real deployment context, and a repo checked
    out without full history is exactly such a context, not a violation to
    fail on.

    A resolvable-but-EMPTY range (HEAD already equals the merge-base, e.g.
    running this suite directly on main) also returns `([], None)` — no
    skip reason, since that is not a degraded-environment condition, just an
    empty result the caller's own test correctly treats as vacuously
    passing (there is nothing to check, not something the check failed to
    check).
    """
    import subprocess

    try:
        head_check = subprocess.run(
            ["git", "rev-parse", "--verify", "HEAD"],
            cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
        )
    except OSError:
        return [], "git binary unavailable"
    if head_check.returncode != 0:
        return [], "not a git repository, or HEAD unresolvable"

    merge_base = subprocess.run(
        ["git", "merge-base", "origin/main", "HEAD"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
    )
    if merge_base.returncode != 0:
        return [], (
            "origin/main merge-base unresolvable (shallow clone, missing "
            "origin remote, or a checkout without full history) -- "
            "skipping rather than failing closed on a missing-history "
            "condition"
        )
    base_sha = merge_base.stdout.strip()
    if not base_sha:
        return [], "merge-base returned no SHA"

    log = subprocess.run(
        ["git", "log", "--format=%s", f"{base_sha}..HEAD"],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=False,
    )
    if log.returncode != 0:
        return [], "git log over the merge-base range failed"
    subjects = [line for line in log.stdout.splitlines() if line.strip()]
    return subjects, None


def test_commit_subjects_ahead_of_main_carry_no_internal_task_id() -> None:
    """Commit SUBJECTS (not bodies — see section docstring above) on the
    commits ahead of this branch's merge-base with `origin/main` must carry
    no bare internal task id: a subject's trailing `(#123)` PR reference is
    parsed into the generated changelog, so a task id there leaks into a
    published release note that resolves to nothing for an external reader
    (CLAUDE.md hard rule 8's exact boundary). SKIPS (never fails) when the
    merge-base cannot be resolved -- see `_commit_subjects_ahead_of_merge_base`
    for the full list of environments this degrades gracefully in."""
    subjects, skip_reason = _commit_subjects_ahead_of_merge_base()
    if skip_reason is not None:
        pytest.skip(skip_reason)
    violations = [
        f"commit subject: [internal task id (lr-NNNNNN)] {subject}"
        for subject in subjects
        if _TASK_ID_SUBJECT_PATTERN.search(subject)
    ]
    assert not violations, "\n" + "\n".join(violations)


def test_commit_subject_guard_reuses_public_facing_task_id_pattern_by_identity() -> None:
    """Assert the commit-subject guard's pattern is the SAME object as
    PUBLIC_FACING_EXTRA_PATTERNS's task-id entry, never a second,
    independently-compiled copy -- mirrors
    test_user_facing_ast_guard_derives_task_id_pattern_from_public_facing_table's
    own identity assertion for the AST guard, applied to this guard too."""
    assert _TASK_ID_SUBJECT_PATTERN is PUBLIC_FACING_EXTRA_PATTERNS["internal task id (lr-NNNNNN)"]


def test_commit_subject_guard_catches_a_synthetic_bad_subject() -> None:
    """Regression test for the guard itself: a synthetic subject carrying a
    bare internal task id must be caught by the SAME pattern the real check
    uses against real git history."""
    bad_subject = "fix(push): resolve lr-abc123 (#42)"
    assert _TASK_ID_SUBJECT_PATTERN.search(bad_subject), (
        "expected the task-id pattern to catch a bare lr-NNNNNN in a "
        "synthetic commit subject"
    )


def test_commit_subject_guard_does_not_flag_a_task_trailer_in_the_body() -> None:
    """The convention this guard must NOT break: a `Task: lr-XXXXXX` trailer
    lives in the commit BODY, never the subject, and this guard only ever
    inspects subjects (`git log --format=%s`, which is the subject line
    alone -- it does not include the body at all). A synthetic subject with
    no task id, paired with a body that does carry one, must pass."""
    clean_subject = "fix(push): resolve a real defect (#42)"
    assert not _TASK_ID_SUBJECT_PATTERN.search(clean_subject), (
        "a clean subject with no task id must not be flagged by the "
        "commit-subject guard, regardless of what its (unchecked) body "
        "contains"
    )


def test_commit_subject_guard_degrades_gracefully_when_merge_base_unresolvable(
    monkeypatch, tmp_path
) -> None:
    """Regression test for the graceful-degradation contract: point this
    guard's git invocation at an EMPTY, freshly-initialized repo (a git repo
    that structurally cannot resolve `origin/main` -- no remote configured
    at all) and assert it returns a skip reason rather than raising or
    fabricating an empty-but-successful result. Proves the degrade path is
    real code, not merely documented intent."""
    import subprocess

    empty_repo = tmp_path / "empty_repo"
    empty_repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=str(empty_repo), check=True)
    subprocess.run(
        ["git", "config", "user.email", "a@example.invalid"], cwd=str(empty_repo), check=True,
    )
    subprocess.run(["git", "config", "user.name", "A"], cwd=str(empty_repo), check=True)
    (empty_repo / "f.txt").write_text("x\n")
    subprocess.run(["git", "add", "f.txt"], cwd=str(empty_repo), check=True)
    subprocess.run(["git", "commit", "-q", "-m", "chore: seed"], cwd=str(empty_repo), check=True)

    # REPO_ROOT is a module-level constant _commit_subjects_ahead_of_merge_base
    # reads via the enclosing module's global -- monkeypatch it directly on
    # this module's own globals (sys.modules[__name__]) rather than importing
    # a package path, since this test file's own dotted import path is not
    # stable across every collection mode pytest supports.
    import sys

    this_module = sys.modules[__name__]
    monkeypatch.setattr(this_module, "REPO_ROOT", empty_repo)
    subjects, skip_reason = _commit_subjects_ahead_of_merge_base()

    assert subjects == []
    assert skip_reason is not None
    assert "origin" in skip_reason or "merge-base" in skip_reason
