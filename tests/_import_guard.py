"""tests/_import_guard.py — source-level "module X must never import module
Y" static guarantee (lr-3f1851).

WHY THIS EXISTS: `tests/test_doctor_checks.py`'s
`test_merge_verb_and_push_verb_never_import_gate_config` (the bootstrap-
safety guarantee behind lr-638945's hardened merge-gate config -- see
`merge/gate_config.py`'s own "BLAST RADIUS" docstring section) previously
inspected `vars(module)` for the imported module's exported names. That
catches

    from clagentic_loadout.merge import gate_config
    from clagentic_loadout.merge.gate_config import load_required_reviewer_roles

(both bind a name in the checked module's namespace) but NOT a
qualified-submodule import:

    import clagentic_loadout.merge.gate_config
    ... clagentic_loadout.merge.gate_config.load_required_reviewer_roles(...)

which binds only the TOP-LEVEL package name (`clagentic_loadout`) in the
checked module's namespace -- `vars(module)` has no `gate_config`-shaped
symbol to find, so a future refactor wiring the forbidden module in via this
shape would pass the old test while silently recreating the exact bootstrap
trap the test exists to prevent (an unsatisfiable diagnostic-only gate
config becoming reachable from a write/merge path, bricking the repo it is
meant to help fix).

THE FIX: a SOURCE-level check, not a runtime-introspection one. Parses the
checked module's own `.py` file with `ast` and walks every `Import` /
`ImportFrom` node for a dotted path that resolves to (or imports FROM) the
forbidden module -- this catches every import SHAPE (plain, `from`,
qualified-submodule, aliased) uniformly, because all of them show up as an
`Import`/`ImportFrom` node regardless of what name they end up binding at
runtime. AST-based rather than a substring/grep scan of the source text
specifically to avoid a false positive on `merge/verb.py`'s own PROSE, which
legitimately names `gate_config` in a comment/docstring today (see that
module's docstring) -- an AST walk only ever inspects import STATEMENTS,
never comment or string-literal text.

GENERALIZED (per lr-3f1851's own suggestion): `assert_module_never_imports`
takes the checked module and the forbidden dotted path as parameters, so the
same technique can lock any other "module X must never import module Y"
invariant this package accumulates, rather than being one bespoke,
copy-pasted check per pair.
"""

from __future__ import annotations

import ast
import inspect
from types import ModuleType


class ForbiddenImportFoundError(AssertionError):
    """Raised by `assert_module_never_imports` when the checked module's own
    source imports the forbidden module, in any shape. Carries a message
    that explains WHY the import is forbidden (bootstrap safety: a
    diagnostic must never block its own remediation) -- not merely that an
    assertion failed -- so a future engineer hitting this in CI understands
    the invariant, not just its violation."""


def _dotted_path_starts_with(dotted: str, prefix: str) -> bool:
    """True iff *dotted* IS *prefix*, or *prefix* followed by a '.' (i.e.
    *dotted* names the forbidden module itself or something inside/under
    it) -- never a same-prefix-different-module false positive (e.g.
    forbidding "clagentic_loadout.merge.gate_config" must not also flag
    "clagentic_loadout.merge.gate_config_helpers", a distinct module that
    merely shares a string prefix)."""
    return dotted == prefix or dotted.startswith(prefix + ".")


def _find_forbidden_import(tree: ast.Module, forbidden_module: str) -> ast.AST | None:
    """Walk every `Import`/`ImportFrom` node in *tree* looking for a dotted
    path resolving to *forbidden_module*. Covers every import shape:

      - `import a.b.c`                 (Import, alias.name == the dotted path)
      - `import a.b.c as x`            (Import, alias.name unaffected by `as`)
      - `from a.b import c`            (ImportFrom, module='a.b', names=['c'])
      - `from a.b.c import d`          (ImportFrom, module='a.b.c')
      - `from a.b.c import *`         (ImportFrom, module='a.b.c')

    A qualified-submodule import (`import a.b.c`, referenced later via the
    dotted attribute chain `a.b.c.whatever`) is caught here because the
    `Import` node's `alias.name` is the FULL dotted path the statement
    itself named ("a.b.c"), regardless of what name Python ends up binding
    in the importing module's namespace (just the top-level package "a") --
    this is exactly the gap `vars(module)` inspection cannot see, since it
    only ever looks at bound names, never at the import statement itself.

    Returns the first offending AST node found, or None.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _dotted_path_starts_with(alias.name, forbidden_module):
                    return node
        elif isinstance(node, ast.ImportFrom):
            # A relative import (`from . import x`, node.module possibly
            # None) can never resolve to an absolute forbidden_module dotted
            # path this checker is given -- skip rather than mis-flag.
            if node.module and _dotted_path_starts_with(node.module, forbidden_module):
                return node
            # `from a.b.c import gate_config` -- the imported NAME itself
            # equals the forbidden module's own final component AND the
            # module path is the forbidden module's parent package. This
            # covers `from clagentic_loadout.merge import gate_config`
            # specifically (module='clagentic_loadout.merge',
            # names=['gate_config']), which the prefix check above alone
            # would miss (the forbidden path's PARENT, not the forbidden
            # path itself, is what `node.module` equals here).
            if node.module:
                forbidden_parent, _, forbidden_leaf = forbidden_module.rpartition(".")
                if forbidden_parent and node.module == forbidden_parent:
                    for alias in node.names:
                        if alias.name == forbidden_leaf:
                            return node
    return None


def assert_module_never_imports(module: ModuleType, forbidden_module: str) -> None:
    """Assert that *module*'s own source file contains no import of
    *forbidden_module*, in any shape (plain, `from`, qualified-submodule,
    aliased) -- an AST-level check, never a `vars(module)`/substring scan.

    Raises `ForbiddenImportFoundError` (a self-remediating message naming
    both modules and WHY the invariant exists) on any match.
    """
    source_path = inspect.getsourcefile(module)
    if source_path is None:
        raise ForbiddenImportFoundError(
            f"could not resolve a source file for {module.__name__!r} -- "
            f"this checker requires a real .py file to parse, and cannot "
            f"verify the 'never imports {forbidden_module}' invariant "
            f"without one."
        )
    with open(source_path, "r", encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=source_path)

    offending_node = _find_forbidden_import(tree, forbidden_module)
    if offending_node is not None:
        raise ForbiddenImportFoundError(
            f"{module.__name__} ({source_path}) imports {forbidden_module} "
            f"at line {getattr(offending_node, 'lineno', '?')} -- this is "
            f"forbidden for BOOTSTRAP SAFETY: {forbidden_module} is "
            f"diagnostic-only (see its own module docstring's 'BLAST "
            f"RADIUS' section), and a hard failure it can raise must never "
            f"become reachable from a write/merge path, or an unsatisfiable "
            f"config would brick the exact operation (push a corrected "
            f"config, land it) needed to fix what the diagnostic is "
            f"complaining about. Remove this import; call the forbidden "
            f"module only from a read-only diagnostic path (e.g. "
            f"doctor.checks), never from {module.__name__}."
        )


__all__ = [
    "ForbiddenImportFoundError",
    "assert_module_never_imports",
]
