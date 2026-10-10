"""End-anchor discipline for validator regexes.

A pattern ending in a bare ``$`` also matches before one trailing newline, so
``re.match`` on ``"value\\n"`` accepts it. Validators use ``\\A...\\Z`` (or
``fullmatch``) instead. This module pins each converted validator and an AST
guard that fails on a new ``re.compile`` literal ending in a bare ``$``.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path

import pytest

from clagentic_loadout import sha
from clagentic_loadout.guard import credential_paths, director_mutation, role_allowlist
from clagentic_loadout.merge import fence_state, post_merge_config
from clagentic_loadout.provisioning import model_routing, roles
from clagentic_loadout.push import verb as push_verb
from clagentic_loadout.push import verify_config
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import github_backend
from clagentic_loadout.review import verb as review_verb
from clagentic_loadout.transport import git_host_api

SRC = Path(__file__).resolve().parent.parent / "src" / "clagentic_loadout"

# (pattern, accepted value)
VALIDATORS = [
    (credential_paths._BAK_SUFFIX_RE, "abc-1"),
    (director_mutation._ROLE_TOKEN_RE, "role-1"),
    (role_allowlist._ROLE_TOKEN_RE, "role-1"),
    (fence_state._ID_RE, "id.1:x"),
    (post_merge_config._ENV_OVERRIDE_VAR_RE, "MY_VAR"),
    (model_routing._TOKEN_RE, "model-1"),
    (roles._TOKEN_RE, "role_1"),
    (verify_config._ENV_NAME_RE, "MY_VAR"),
    (github_backend._ISSUE_COMMENT_ID_RE, "12345"),
    (review_verb._DELETE_COMMENT_ID_RE, "12345"),
    (sha.FULL_SHA_RE, "a" * 40),
    (sha.ABBREVIATED_SHA_RE, "a" * 7),
    (push_verb._FULL_SHA_RE, "a" * 40),
    (push_verb._FULL_SHA_RE, "b" * 64),
    (git_host_api._SAFE_CALLER_TRACKING_ID_RE, "task-123"),
    (git_host_api._ISSUE_COMMENTS_RE, "/api/v1/repos/o/r/issues/3/comments"),
    (git_host_api._ISSUE_COMMENTS_RE, "/api/v1/repos/o/r/issues/3/comments/?page=2"),
    (git_host_api._ISSUE_COMMENT_ID_RE, "/api/v1/repos/o/r/issues/comments/9"),
]


@pytest.mark.parametrize(("pattern", "good"), VALIDATORS)
def test_validator_accepts_value_and_rejects_trailing_newline(pattern, good):
    assert pattern.match(good)
    assert not pattern.match(good + "\n")
    assert not pattern.search(good + "\n")


def test_review_cli_strips_head_sha_arguments():
    parser = review_cli._build_arg_parser()
    sha_value = "c" * 40
    post = parser.parse_args(
        ["post", "--repo", "o/r", "--pr", "1", "--findings", "f", "--status", "clean",
         "--head-sha", sha_value + "\n"]
    )
    assert post.head_sha == sha_value
    run = parser.parse_args(
        ["run", "--repo", "o/r", "--pr", "1", "--prior-head-sha", sha_value + "\n"]
    )
    assert run.prior_head_sha == sha_value


def test_delete_own_comment_is_stripped_at_the_argparse_boundary():
    parser = review_verb._build_arg_parser()
    args = parser.parse_args(["--platform", "github", "--delete-own-comment", "42\n", "o/r"])
    assert args.delete_own_comment == "42"


def test_caller_tracking_id_with_newline_is_refused():
    assert not git_host_api._SAFE_CALLER_TRACKING_ID_RE.match("id\n")
    assert not git_host_api._SAFE_CALLER_TRACKING_ID_RE.match("a\nb")


@pytest.mark.parametrize(
    "positionals",
    [
        ["/api/v1/repos/o/r/issues/3/comments\n"],
        ["POST", "/api/v1/repos/o/r/issues/3/comments\n"],
        ["GET", "/api/v1/repos/o/r/pulls\t/1"],
    ],
)
def test_control_character_in_path_is_a_usage_error(positionals):
    ns = argparse.Namespace(
        method_or_path=positionals[0],
        path_if_method=positionals[1] if len(positionals) > 1 else None,
    )
    with pytest.raises(git_host_api.GitHostApiError) as excinfo:
        git_host_api._split_method_and_path(ns)
    assert excinfo.value.code == git_host_api.EXIT_USAGE


def test_clean_path_still_splits():
    ns = argparse.Namespace(method_or_path="POST", path_if_method="/api/v1/x")
    assert git_host_api._split_method_and_path(ns) == ("POST", "/api/v1/x")
    ns = argparse.Namespace(method_or_path="/api/v1/x", path_if_method=None)
    assert git_host_api._split_method_and_path(ns) == ("GET", "/api/v1/x")


def test_detector_regexes_are_unchanged():
    from clagentic_loadout.push import git_hermeticity

    # Match-to-refuse patterns keep `$`: narrowing them would let a
    # newline-suffixed name slip past the refusal.
    assert git_hermeticity._GIT_CONFIG_INJECTION_RE.match("GIT_CONFIG_COUNT\n")


# Exempt sites: (relative path, pattern literal) -> reason.
ALLOWLIST = {
    ("push/git_hermeticity.py", r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$"):
        "detector: a match means refuse, narrowing would weaken the gate",
    ("push/git_hermeticity.py", r"^http\..*\.extraheader$"):
        "detector: a match means refuse",
    ("push/git_hermeticity.py", r"^url\..*\.(insteadof|pushinsteadof)$"):
        "detector: a match means refuse",
    ("merge/repo_path_consistency.py", r"^[^@]+@[^:/]+:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"):
        "parses a stripped git remote URL from tool output",
    ("merge/repo_path_consistency.py", r"^https?://(?:[^@/]+@)?[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"):
        "parses a stripped git remote URL from tool output",
    ("merge/repo_path_consistency.py", r"^ssh://[^@]+@[^/]+(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$"):
        "parses a stripped git remote URL from tool output",
    ("release/detector.py", r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$"):
        "parses a stripped git remote URL from tool output",
    ("push/git_push.py", r"^\s*!\s*\[(?:rejected|remote rejected)\]\s+\S+\s+->\s+\S+\s+\((?P<reason>[^)]+)\)\s*$"):
        "parses one line of git push output; trailing whitespace is intended",
    ("push/issue_link.py", r"(?im)^\s*task:\s*(\S+)\s*$"):
        "multiline trailer scan over a commit message, $ is a line end",
    ("release/detector.py", r"(?im)^\s*closes\s+#(\d+)\s*$"):
        "multiline trailer scan over a commit message, $ is a line end",
    ("release/detector.py", r"(?im)^\s*task:\s*(\S+)\s*$"):
        "multiline trailer scan over a commit message, $ is a line end",
    ("release/dispatch.py", r"(?im)^\s*closes\s+#(\d+)\s*$"):
        "multiline trailer scan over a commit message, $ is a line end",
    ("release/dispatch.py", r"(?im)^\s*task:\s*(\S+)\s*$"):
        "multiline trailer scan over a commit message, $ is a line end",
    ("review/chunking.py", r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$"):
        "parses stripped diff-tool output, single line",
    ("review/findings_contract.py", r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$"):
        "fence extraction; trailing whitespace is intended",
}


def _compile_literals(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "compile"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "re"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            yield node.args[0].value


def _bare_dollar_literals(source: str):
    for literal in _compile_literals(ast.parse(source)):
        if literal.endswith("$") and not literal.endswith("\\$"):
            yield literal


def _scan_src():
    found = set()
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        for literal in _bare_dollar_literals(path.read_text(encoding="utf-8")):
            found.add((rel, literal))
    return found


def test_no_unlisted_pattern_ends_in_a_bare_dollar():
    # Prefix matchers such as `^lore\s+search(\s|$)` end in `)`, so only a
    # literal whose final character is `$` is reported; those are validators
    # that must use \Z or fullmatch unless allowlisted.
    unlisted = {site for site in _scan_src() if site not in ALLOWLIST}
    assert not unlisted, f"re.compile literal ends in a bare $: {sorted(unlisted)}"


def test_allowlist_entries_still_exist():
    stale = set(ALLOWLIST) - _scan_src()
    assert not stale, f"stale allowlist entries: {sorted(stale)}"


def test_guard_fails_on_a_planted_pattern():
    assert list(_bare_dollar_literals('import re\nX = re.compile(r"^abc$")\n')) == ["^abc$"]
    assert not list(_bare_dollar_literals('import re\nX = re.compile(r"\\Aabc\\Z")\n'))
