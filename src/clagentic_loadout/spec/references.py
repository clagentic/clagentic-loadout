"""spec.references -- where a name is defined, exported and referenced in a source.

The primitive under the call-site finder (spec.call_sites) and the push report on
new public symbols (push.uncalled_report). It is pure: it takes the text of one
source and returns facts about it; it reads no file and runs no command. Only the
standard library is used.

Python is read with ``ast`` (spec.source_parse). A reference is a ``Name``, an
``Attribute`` attribute, or an import alias whose name is the symbol's (a dotted
module path such as ``import a.name`` is not one); a string, a
comment or a docstring that mentions it is not one, so a name that appears only
in ``__all__`` has no reference. A definition is a top-level ``def`` or ``class``.

JavaScript is read with the tokenizer of spec.js_imports, which skips comments,
strings, templates and regular-expression literals. A reference is any identifier
token with the name, a property access included. The definition and the export
statement of the symbol in its own file are not references. The export forms read
are ``export function|class|const|let|var NAME`` (only the first declarator of a
const, let or var statement is discovered, so ``export const a = 1, b = 2`` adds
``a`` alone; a destructuring pattern declares no name), ``export { a, b as c }`` (not a
re-export from another module), ``export default function|class NAME``,
``module.exports = { a, b: c }`` (the keys), ``module.exports.NAME = ...`` and
``exports.NAME = ...``. Nothing else is guessed: a dynamic or reflective use of a
name is not a reference.

Every function that parses raises ``SourceUnparsed`` with the reason when the
source cannot be parsed.
"""

from __future__ import annotations

import ast
from collections.abc import Collection, Sequence

from clagentic_loadout.spec.js_imports import Token, Unparseable, tokenize
from clagentic_loadout.spec.source_parse import parse_python

PYTHON = "python"
JAVASCRIPT = "javascript"

_DECLARATIONS = frozenset({"function", "class", "const", "let", "var"})
#: Words that start an expression value, never the name of a local binding.
_NOT_A_LOCAL = frozenset(
    {"function", "async", "class", "new", "await", "require", "null", "true", "false", "undefined", "this"}
)
_OPENERS = frozenset({"(", "[", "{"})
_CLOSERS = frozenset({")", "]", "}"})


class SourceUnparsed(Exception):
    """The source cannot be parsed; the message says why."""


def _is(token: Token | None, kind: str, value: str | None = None) -> bool:
    return token is not None and token.kind == kind and (value is None or token.value == value)


class JsModule:
    """The declarations and exports of one JavaScript source, by token index.

    ``declared`` maps a name to the index of the token that declares it
    (``function``, ``class``, ``const``, ``let`` or ``var``); ``exports`` maps an
    exported name to the indices of the tokens that make up its export and its own
    declaration, which are not references to it."""

    def __init__(self, source: str) -> None:
        try:
            self.tokens = tokenize(source)
        except Unparseable as exc:
            raise SourceUnparsed(str(exc)) from exc
        self.source = source
        self.declared: dict[str, int] = {}
        self.exports: dict[str, set[int]] = {}
        self._read_declarations()
        self._read_exports()

    def line_of(self, index: int) -> int:
        return self.source.count("\n", 0, self.tokens[index].pos) + 1

    def own_indices(self, name: str) -> set[int]:
        """The tokens that declare or export *name* in this source."""
        own = set(self.exports.get(name, ()))
        if name in self.declared:
            own.add(self.declared[name])
        return own

    def _at(self, index: int) -> Token | None:
        return self.tokens[index] if 0 <= index < len(self.tokens) else None

    def _after_dot(self, index: int) -> bool:
        return _is(self._at(index - 1), "punct", ".")

    def _declared_name(self, keyword_index: int) -> int | None:
        """The index of the name a declaration keyword at *keyword_index* declares."""
        j = keyword_index + 1
        if _is(self._at(keyword_index), "id", "function") and _is(self._at(j), "punct", "*"):
            j += 1
        return j if _is(self._at(j), "id") else None

    def _read_declarations(self) -> None:
        for i, token in enumerate(self.tokens):
            if token.kind == "id" and token.value in _DECLARATIONS and not self._after_dot(i):
                name_index = self._declared_name(i)
                if name_index is not None:
                    self.declared.setdefault(str(self.tokens[name_index].value), name_index)

    def _add_export(self, name: str, own: set[int], local: str | None) -> None:
        if name == "default":
            return
        held = self.exports.setdefault(name, set())
        held.update(own)
        if local is not None and local in self.declared:
            held.add(self.declared[local])
        if name in self.declared:
            held.add(self.declared[name])

    def _group_end(self, start: int) -> int:
        """Index after the bracket group opening at *start*."""
        depth = 0
        j = start
        while j < len(self.tokens):
            token = self.tokens[j]
            if token.kind == "punct":
                if token.value in _OPENERS:
                    depth += 1
                elif token.value in _CLOSERS:
                    depth -= 1
                    if depth == 0:
                        return j + 1
            j += 1
        return j

    def _entries(self, open_index: int) -> list[list[int]]:
        """The comma-separated entries, as token indices, inside the brace group
        opening at *open_index*, split at depth zero."""
        end = self._group_end(open_index) - 1
        entries: list[list[int]] = [[]]
        depth = 0
        for j in range(open_index + 1, end):
            token = self.tokens[j]
            if token.kind == "punct":
                if token.value in _OPENERS:
                    depth += 1
                elif token.value in _CLOSERS:
                    depth -= 1
                elif token.value == "," and depth == 0:
                    entries.append([])
                    continue
            entries[-1].append(j)
        return [entry for entry in entries if entry]

    def _read_clause(self, open_index: int) -> None:
        """``export { a, b as c }``: not a re-export, so each name is this file's own."""
        if _is(self._at(self._group_end(open_index)), "id", "from"):
            return
        for entry in self._entries(open_index):
            tokens = [self.tokens[j] for j in entry]
            if len(tokens) == 1 and tokens[0].kind == "id":
                self._add_export(str(tokens[0].value), {entry[0]}, str(tokens[0].value))
            elif len(tokens) == 3 and tokens[0].kind == "id" and _is(tokens[1], "id", "as") and tokens[2].kind == "id":
                self._add_export(str(tokens[2].value), {entry[0], entry[2]}, str(tokens[0].value))

    def _read_object(self, open_index: int) -> None:
        """``module.exports = { a, b: c }``: the keys are the exported names."""
        for entry in self._entries(open_index):
            tokens = [self.tokens[j] for j in entry]
            key = tokens[0]
            if key.kind not in ("id", "str") or not key.value:
                continue
            if len(tokens) == 1 and key.kind == "id":
                self._add_export(str(key.value), {entry[0]}, str(key.value))
            elif len(tokens) >= 2 and _is(tokens[1], "punct", ":"):
                local = None
                own = {entry[0]}
                if len(tokens) == 3 and tokens[2].kind == "id" and tokens[2].value not in _NOT_A_LOCAL:
                    local = str(tokens[2].value)
                    own.add(entry[2])
                self._add_export(str(key.value), own, local)

    def _read_assignment(self, name_index: int) -> None:
        """``exports.NAME = value`` with the name token at *name_index*."""
        name = self._at(name_index)
        if name is None or name.kind != "id" or not _is(self._at(name_index + 1), "punct", "="):
            return
        if _is(self._at(name_index + 2), "punct", "="):
            return
        own = {name_index}
        local = None
        value = self._at(name_index + 2)
        after = self._at(name_index + 3)
        if (
            value is not None
            and value.kind == "id"
            and value.value not in _NOT_A_LOCAL
            and not (after is not None and after.kind == "punct" and after.value in ("(", "."))
        ):
            local = str(value.value)
            own.add(name_index + 2)
        self._add_export(str(name.value), own, local)

    def _read_export_statement(self, i: int) -> None:
        j = i + 1
        default = _is(self._at(j), "id", "default")
        if default:
            j += 1
        if _is(self._at(j), "id", "async"):
            j += 1
        token = self._at(j)
        if token is None:
            return
        if token.kind == "id" and token.value in _DECLARATIONS:
            if default and token.value not in ("function", "class"):
                return
            name_index = self._declared_name(j)
            if name_index is not None:
                self._add_export(str(self.tokens[name_index].value), {name_index}, None)
        elif _is(token, "punct", "{") and not default:
            self._read_clause(j)

    def _read_exports(self) -> None:
        for i, token in enumerate(self.tokens):
            if token.kind != "id" or self._after_dot(i):
                continue
            if token.value == "export":
                self._read_export_statement(i)
            elif token.value == "module" and _is(self._at(i + 1), "punct", ".") and _is(self._at(i + 2), "id", "exports"):
                after = i + 3
                if _is(self._at(after), "punct", "=") and not _is(self._at(after + 1), "punct", "="):
                    if _is(self._at(after + 1), "punct", "{"):
                        self._read_object(after + 1)
                elif _is(self._at(after), "punct", "."):
                    self._read_assignment(after + 1)
            elif token.value == "exports" and _is(self._at(i + 1), "punct", "."):
                self._read_assignment(i + 2)


def _python_tree(source: str) -> ast.Module:
    try:
        return parse_python(source)
    except SyntaxError as exc:
        raise SourceUnparsed(f"{exc.msg} at line {exc.lineno}") from exc
    except (ValueError, RecursionError) as exc:
        raise SourceUnparsed(str(exc) or "nested too deeply to parse") from exc


def python_top_level(source: str) -> dict[str, int]:
    """The top-level ``def`` and ``class`` names of *source*, each with its line."""
    found: dict[str, int] = {}
    for node in _python_tree(source).body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.setdefault(node.name, node.lineno)
    return found


def js_exported(source: str) -> set[str]:
    """The names *source* exports, in the forms the module docstring lists."""
    return set(JsModule(source).exports)


def defines(language: str, source: str, name: str) -> bool:
    """Whether *source* defines *name*: a top-level ``def`` or ``class`` in Python;
    an exported or declared name in JavaScript."""
    if language == PYTHON:
        return name in python_top_level(source)
    module = JsModule(source)
    return name in module.exports or name in module.declared


def _python_lines(source: str, names: Collection[str]) -> dict[str, set[int]]:
    found: dict[str, set[int]] = {}
    for node in ast.walk(_python_tree(source)):
        hits: list[str] = []
        if isinstance(node, ast.Name):
            hits = [node.id]
        elif isinstance(node, ast.Attribute):
            hits = [node.attr]
        elif isinstance(node, ast.alias):
            # `from m import name [as other]` names the symbol; `import a.name` is a
            # module path, which is not a reference to a symbol called name.
            hits = [node.name]
        for hit in hits:
            if hit in names:
                found.setdefault(hit, set()).add(getattr(node, "lineno", 0))
    return found


def _js_lines(source: str, names: Collection[str], defining: Collection[str]) -> dict[str, set[int]]:
    module = JsModule(source)
    # Per name: a token that declares or exports one symbol can be a plain reference
    # to another (the local on the right of ``{ a: b }``), so exclusions never pool.
    skipped = {name: module.own_indices(name) for name in defining}
    found: dict[str, set[int]] = {}
    for index, token in enumerate(module.tokens):
        if token.kind == "id" and token.value in names and index not in skipped.get(str(token.value), ()):
            found.setdefault(str(token.value), set()).add(module.line_of(index))
    return found


def reference_lines(
    language: str, source: str, names: Collection[str], *, defining: Sequence[str] = ()
) -> dict[str, list[int]]:
    """For each of *names* that *source* references, the sorted lines of its
    references. *defining* names the symbols *source* itself defines: in
    JavaScript, the tokens that declare or export them are not references. A name
    that does not occur in the text at all is not looked for."""
    wanted = {name for name in names if name in source}
    if not wanted:
        return {}
    found = _python_lines(source, wanted) if language == PYTHON else _js_lines(source, wanted, defining)
    return {name: sorted(lines) for name, lines in found.items()}


__all__ = [
    "JAVASCRIPT",
    "PYTHON",
    "JsModule",
    "SourceUnparsed",
    "defines",
    "js_exported",
    "python_top_level",
    "reference_lines",
]
