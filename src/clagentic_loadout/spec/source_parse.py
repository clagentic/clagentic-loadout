"""spec.source_parse -- the one place Python source is parsed for the import graph,
the call-site finder and the push report on new public symbols.

Reading a tree file is not running it, so the warnings the compiler prints for
text it merely reads (an invalid escape sequence in a string, for one) are
suppressed here. A source that is not valid Python still raises, as ``ast.parse``
does: ``SyntaxError`` or ``ValueError`` (a null byte).
"""

from __future__ import annotations

import ast
import warnings


def parse_python(source: str) -> ast.Module:
    """The syntax tree of *source*, with no ``SyntaxWarning`` printed."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        return ast.parse(source)


__all__ = ["parse_python"]
