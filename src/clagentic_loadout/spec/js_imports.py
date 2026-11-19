"""spec.js_imports -- the file-import graph of a set of JavaScript sources.

The JavaScript counterpart of spec.import_graph, producing the same
``ImportGraph``. It is pure: it takes the text of the sources and the list of
paths in the tree and returns the graph; it reads no file, runs no command and
makes no network call. Only the standard library is used.

A small tokenizer skips line and block comments, single- and double-quoted
strings, template literals (with nested ``${}`` expressions) and regular-expression
literals, so text inside them is never read as an import. An edge is made by:
``import ... from 'x'``, ``import 'x'``, ``export ... from 'x'`` (including
``export * from``), ``require('x')`` and ``import('x')``, each only when the
specifier is a plain string literal (a template literal with no substitution
counts; anything else does not).

Only relative specifiers (``./`` or ``../``) resolve, against the importing file's
own directory, in a fixed order: the exact path, then the path plus each covered
extension (``.js .jsx .mjs .cjs``), then ``<path>/index`` plus each; the first path
present in the tree wins. A bare specifier (a package name, ``node:``), an absolute
path, or a path that leaves the tree adds no edge. Package ``exports``/``imports``
maps, path aliases and ``node_modules`` are not consulted. Nothing is guessed.

A ``/`` after ``)``, ``]``, ``}`` or an operand is read as division, so a regular
expression literal right after a ``)`` (``if (x) /re/.test(y)``) is not recognised
as one. A ``/`` right after ``<`` is never a regular expression, so a JSX closing tag
(``</div>``) is skipped; JSX text holding an apostrophe is still read as an
unterminated string and the source is unparsed. A source whose string, comment, template or regular expression is not
terminated is unparsed, with the reason and line recorded.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Collection, Mapping
from typing import NamedTuple

from clagentic_loadout.spec.import_graph import ImportGraph

#: Covered extensions, in the order a specifier without one is tried.
JS_SUFFIXES = (".js", ".jsx", ".mjs", ".cjs")

_INDEX = "index"

# After these a "/" starts a regular expression; after any other identifier it divides.
_REGEX_KEYWORDS = frozenset(
    {"return", "typeof", "case", "do", "else", "in", "of", "instanceof", "new", "delete", "void",
     "throw", "yield", "await"}
)
_OPERAND_END = frozenset({")", "]", "}", "++", "--"})


class _Unparseable(Exception):
    def __init__(self, reason: str, pos: int, src: str) -> None:
        super().__init__(f"{reason} at line {src.count(chr(10), 0, pos) + 1}")


class _Token(NamedTuple):
    kind: str  # "id", "str", "num", "punct", "regex"
    value: str | None  # a string's text; None for a string that is not a plain literal


def _scan_string(src: str, i: int) -> tuple[int, str | None]:
    quote = src[i]
    j = i + 1
    plain = True
    while j < len(src):
        c = src[j]
        if c == quote:
            return j + 1, (src[i + 1 : j] if plain else None)
        if c == "\\":
            plain = False
            j += 2
            continue
        if c == "\n":
            break
        j += 1
    raise _Unparseable("unterminated string", i, src)


def _scan_template(src: str, i: int, start: int) -> tuple[int, bool, str | None]:
    """Scan template text from *i*. Returns the index after the closing backtick or
    after ``${``, whether a substitution was opened, and the text when plain."""
    j = i
    plain = True
    while j < len(src):
        c = src[j]
        if c == "`":
            return j + 1, False, (src[i:j] if plain else None)
        if c == "$" and src.startswith("${", j):
            return j + 2, True, None
        if c == "\\":
            plain = False
            j += 2
            continue
        j += 1
    raise _Unparseable("unterminated template literal", start, src)


def _scan_regex(src: str, i: int) -> int:
    j = i + 1
    in_class = False
    while j < len(src):
        c = src[j]
        if c == "\n":
            break
        if c == "\\":
            j += 2
            continue
        if c == "[":
            in_class = True
        elif c == "]":
            in_class = False
        elif c == "/" and not in_class:
            j += 1
            while j < len(src) and (src[j].isalpha()):
                j += 1
            return j
        j += 1
    raise _Unparseable("unterminated regular expression", i, src)


def _regex_allowed(prev: _Token | None) -> bool:
    if prev is None:
        return True
    if prev.kind == "punct":
        return prev.value not in _OPERAND_END
    if prev.kind == "id":
        return prev.value in _REGEX_KEYWORDS
    return False


def _is_id_start(c: str) -> bool:
    return c.isalpha() or c in "$_" or ord(c) > 127


def tokenize(src: str) -> list[_Token]:
    """The code tokens of *src*; raises ``_Unparseable`` when a string, comment,
    template or regular expression is not terminated."""
    tokens: list[_Token] = []
    stack: list[str] = []  # "b" for an open brace, "t" for an open ${ of a template
    n = len(src)
    i = 0
    if src.startswith("#!"):
        i = src.find("\n") if "\n" in src else n
    template_start = 0
    while i < n:
        c = src[i]
        if c in " \t\r\n\f\v﻿":
            i += 1
        elif src.startswith("//", i):
            end = src.find("\n", i)
            i = n if end == -1 else end
        elif src.startswith("/*", i):
            end = src.find("*/", i + 2)
            if end == -1:
                raise _Unparseable("unterminated comment", i, src)
            i = end + 2
        elif c in "'\"":
            i, value = _scan_string(src, i)
            tokens.append(_Token("str", value))
        elif c == "`":
            template_start = i
            i, opened, text = _scan_template(src, i + 1, i)
            if opened:
                stack.append("t")
                tokens.append(_Token("punct", "${"))
            else:
                tokens.append(_Token("str", text))
        elif c == "}":
            if stack and stack[-1] == "t":
                stack.pop()
                i, opened, text = _scan_template(src, i + 1, template_start)
                if opened:
                    stack.append("t")
                    tokens.append(_Token("punct", "${"))
                else:
                    tokens.append(_Token("str", None))
            else:
                if stack:
                    stack.pop()
                tokens.append(_Token("punct", "}"))
                i += 1
        elif c == "{":
            stack.append("b")
            tokens.append(_Token("punct", "{"))
            i += 1
        elif c == "/":
            prev = tokens[-1] if tokens else None
            # "</" is a JSX closing tag, never a regular expression.
            if _regex_allowed(prev) and not _is(prev, "punct", "<"):
                i = _scan_regex(src, i)
                tokens.append(_Token("regex", None))
            else:
                tokens.append(_Token("punct", "/"))
                i += 1
        elif _is_id_start(c):
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "$_" or ord(src[j]) > 127):
                j += 1
            tokens.append(_Token("id", src[i:j]))
            i = j
        elif c.isdigit():
            j = i + 1
            while j < n and (src[j].isalnum() or src[j] in "_."):
                j += 1
            tokens.append(_Token("num", None))
            i = j
        elif src.startswith(("++", "--"), i):
            tokens.append(_Token("punct", src[i : i + 2]))
            i += 2
        else:
            tokens.append(_Token("punct", c))
            i += 1
    if "t" in stack:
        raise _Unparseable("unterminated template literal", template_start, src)
    return tokens


def _is(token: _Token | None, kind: str, value: str | None = None) -> bool:
    return token is not None and token.kind == kind and (value is None or token.value == value)


def _skip_braces(tokens: list[_Token], j: int) -> int:
    """Index after the brace group opening at *j* (tokens[j] is ``{``)."""
    depth = 0
    while j < len(tokens):
        if _is(tokens[j], "punct", "{"):
            depth += 1
        elif _is(tokens[j], "punct", "}"):
            depth -= 1
            if depth == 0:
                return j + 1
        j += 1
    return j


def _at(tokens: list[_Token], j: int) -> _Token | None:
    return tokens[j] if 0 <= j < len(tokens) else None


def _is_from_clause(tokens: list[_Token], j: int) -> bool:
    return _is(_at(tokens, j), "id", "from") and _is(_at(tokens, j + 1), "str")


def _from_clause(tokens: list[_Token], j: int) -> str | None:
    """The specifier of a ``from 'x'`` at *j*, or None."""
    return tokens[j + 1].value if _is_from_clause(tokens, j) else None


def _static_import(tokens: list[_Token], j: int) -> str | None:
    while j < len(tokens):
        if _is_from_clause(tokens, j):
            return tokens[j + 1].value
        token = tokens[j]
        if _is(token, "punct", "{"):
            j = _skip_braces(tokens, j)
        elif token.kind == "id" or _is(token, "punct", ",") or _is(token, "punct", "*"):
            j += 1
        else:
            return None
    return None


def _export_from(tokens: list[_Token], j: int) -> str | None:
    if _is(_at(tokens, j), "punct", "*"):
        j += 1
        if _is(_at(tokens, j), "id", "as"):
            j += 2
    elif _is(_at(tokens, j), "punct", "{"):
        j = _skip_braces(tokens, j)
    else:
        return None
    return _from_clause(tokens, j)


def _call_argument(tokens: list[_Token], j: int, closers: tuple[str, ...]) -> str | None:
    """The plain string literal in ``( 'x' <closer>`` at *j* (tokens[j] is ``(``)."""
    if not _is(_at(tokens, j), "punct", "(") or not _is(_at(tokens, j + 1), "str"):
        return None
    after = _at(tokens, j + 2)
    if after is None or after.kind != "punct" or after.value not in closers:
        return None
    return tokens[j + 1].value


def extract_specifiers(src: str) -> list[str]:
    """Every plain-literal module specifier *src* imports; raises
    ``_Unparseable`` when *src* cannot be tokenized."""
    tokens = tokenize(src)
    found: list[str] = []
    for i, token in enumerate(tokens):
        if token.kind != "id" or _is(_at(tokens, i - 1), "punct", "."):
            continue
        spec: str | None = None
        if token.value == "import":
            nxt = _at(tokens, i + 1)
            if _is(nxt, "str"):
                spec = nxt.value if nxt else None
            elif _is(nxt, "punct", "("):
                spec = _call_argument(tokens, i + 1, (")", ","))
            else:
                spec = _static_import(tokens, i + 1)
        elif token.value == "export":
            spec = _export_from(tokens, i + 1)
        elif token.value == "require":
            spec = _call_argument(tokens, i + 1, (")",))
        if spec:
            found.append(spec)
    return found


def _resolve(specifier: str, importer: str, tree: frozenset[str]) -> str | None:
    if not specifier.startswith(("./", "../")):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))
    if base == ".." or base.startswith("../") or base.startswith("/"):
        return None
    # An exact path counts only when it is itself a covered JavaScript file, so
    # './foo.py' or './data.json' never makes an edge to a file of another language.
    exact = [base] if base.endswith(JS_SUFFIXES) and not specifier.endswith("/") else []
    extended = [base + ext for ext in JS_SUFFIXES] if not specifier.endswith("/") else []
    candidates = [*exact, *extended, *(f"{base}/{_INDEX}{ext}" for ext in JS_SUFFIXES)]
    return next((c for c in candidates if c in tree), None)


def build_js_import_graph(sources: Mapping[str, str], tree_paths: Collection[str]) -> ImportGraph:
    """The import graph of *sources* (path to JavaScript text), resolved against
    *tree_paths*, every path in the tree. *sources* must be a subset of
    *tree_paths*."""
    tree = frozenset(tree_paths)
    edges: dict[str, set[str]] = defaultdict(set)
    unparsed: dict[str, str] = {}
    for path in sorted(sources):
        try:
            specifiers = extract_specifiers(sources[path])
        except _Unparseable as exc:
            unparsed[path] = str(exc)
            continue
        for specifier in specifiers:
            target = _resolve(specifier, path, tree)
            if target is not None and target != path:
                edges[target].add(path)
    unread = sorted(p for p in tree if p.endswith(JS_SUFFIXES) and p not in sources)
    return ImportGraph(
        importers={target: frozenset(found) for target, found in edges.items()},
        unparsed=tuple(unparsed),
        unread=tuple(unread),
        unparsed_reasons=unparsed,
    )


__all__ = ["JS_SUFFIXES", "build_js_import_graph", "extract_specifiers"]
