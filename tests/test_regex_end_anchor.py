"""A whole-string validator must not accept a trailing newline.

`$` matches before a final newline, so `re.compile("^...$").match("NAME\\n")`
succeeds: a value that passes validation and then never equals the identifier it
was meant to name. Validators anchor with `\\Z` (or use `fullmatch`). This guard
walks every `re.compile` of a literal pattern under `src/` so a new `^...$`
validator is caught without a hand-kept list of call sites."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from clagentic_loadout.sha import ABBREVIATED_SHA_RE, FULL_SHA_RE

_SRC = Path(__file__).resolve().parent.parent / "src"

#: Patterns that parse text a tool printed or a request path built by loadout
#: itself (git config keys, remote URLs, push output, a diff hunk header, a
#: fenced block, an API path), where `$` is deliberate and the input is not a
#: caller-supplied identifier being validated.
_LINE_ORIENTED = frozenset(
    {
        r"^[^@]+@[^:/]+:(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^ssh://[^@]+@[^/]+(?::\d+)?/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^https?://(?:[^@/]+@)?[^/]+/(?P<owner>[^/]+)/(?P<repo>[^/]+?)(?:\.git)?/?$",
        r"^https?://[^/]+/([^/]+)/([^/]+?)(?:\.git)?/?$",
        r"^\s*!\s*\[(?:rejected|remote rejected)\]\s+\S+\s+->\s+\S+\s+\((?P<reason>[^)]+)\)\s*$",
        r"^```[A-Za-z0-9_-]*\s*\n(.*?)\n```\s*$",
        r"^/api/v1/repos/([^/]+)/([^/]+)/issues/(\d+)/comments/?(?:\?.*)?$",
        r"^http\..*\.extraheader$",
        r"^url\..*\.(insteadof|pushinsteadof)$",
        r"^GIT_CONFIG_(COUNT|KEY_\d+|VALUE_\d+)$",
        r"^/api/v1/repos/([^/]+)/([^/]+)/issues/comments/(\d+)/?(?:\?.*)?$",
        r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$",
    }
)


def _literal_compiles() -> list[tuple[str, int, str]]:
    found = []
    for path in sorted(_SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
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
                found.append((str(path.relative_to(_SRC)), node.lineno, node.args[0].value))
    return found


def test_no_whole_string_validator_ends_with_a_dollar_anchor():
    offenders = [
        f"{where}:{line}: {pattern!r}"
        for where, line, pattern in _literal_compiles()
        if pattern.startswith("^")
        and pattern.endswith("$")
        and not pattern.endswith("\\$")
        and pattern not in _LINE_ORIENTED
    ]
    assert offenders == [], "anchor whole-string validators with \\Z, not $:\n" + "\n".join(offenders)


@pytest.mark.parametrize("pattern", [FULL_SHA_RE, ABBREVIATED_SHA_RE])
def test_a_sha_with_a_trailing_newline_is_rejected(pattern):
    assert pattern.match("a" * 40)
    assert pattern.match("a" * 40 + "\n") is None
