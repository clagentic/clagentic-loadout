"""Regression guard: no message in `src/` formats a remote value or git's own
transport diagnostics.

A remote value (a remote name, or the URL `branch.<name>.remote` can hold) can
carry a credential in its userinfo, query or path, and git quotes the remote URL
in the stderr of a failed fetch, push or ls-remote. Neither predicate "this
value is safe to print" holds, so the rule is structural: nothing that formats
text may interpolate

  - a name bound from `tracking_remote()`, or a variable named `remote`,
    `remote_name` or `shown_remote`; or
  - the `.stderr` of a completed git fetch, push or ls-remote process.

The check is AST-based, in the style of the anonymization guard, so a new
violation anywhere in `src/` is caught without a hand-kept file list."""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "clagentic_loadout"

REMOTE_VALUE_NAMES = frozenset({"remote", "remote_name", "shown_remote"})
REMOTE_BINDING_CALLS = frozenset({"tracking_remote"})
TRANSPORT_VERBS = frozenset({"fetch", "push", "ls-remote"})
TRANSPORT_CALL_MARKERS = ("ls_remote", "fetch_with_token", "push_with_token")

#: (path under src/clagentic_loadout, function, snippet of the offending
#: expression) -> why it is not a leak. Keep this empty unless the output is an
#: explicit, opt-in raw-transcript feature.
ALLOWLIST: dict[tuple[str, str, str], str] = {
    (
        "push/git_push.py",
        "git_push_with_token",
        "[dry-run exit=",
    ): (
        "`--dry-run`/`--verbose` exist to print git's raw push transcript; the "
        "output is explicit opt-in and redacted by the push redaction choke point"
    ),
}


def _is_text(node: ast.AST) -> bool:
    return isinstance(node, ast.JoinedStr) or (isinstance(node, ast.Constant) and isinstance(node.value, str))


def _formatted_parts(node: ast.AST) -> list[ast.AST]:
    """The sub-expressions *node* turns into text, when it builds a string."""
    if isinstance(node, ast.JoinedStr):
        return [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "format"
        and _is_text(node.func.value)
    ):
        return [*node.args, *(keyword.value for keyword in node.keywords)]
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Mod, ast.Add)) and (
        _is_text(node.left) or _is_text(node.right)
    ):
        return [node.left, node.right]
    return []


def _walk_outside_len(node: ast.AST):
    """ast.walk that skips `len(...)`: a byte count is not the text it counts."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len":
        return
    yield node
    for child in ast.iter_child_nodes(node):
        yield from _walk_outside_len(child)


def _call_name(call: ast.Call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _mentions_transport_verb(node: ast.AST) -> bool:
    return any(
        isinstance(child, ast.Constant) and child.value in TRANSPORT_VERBS for child in ast.walk(node)
    )


def _transport_lists(function: ast.AST) -> set[str]:
    """Names bound to an argv list holding a fetch, push or ls-remote verb."""
    return {
        target.id
        for node in ast.walk(function)
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.List) and _mentions_transport_verb(node.value)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _transport_results(function: ast.AST) -> set[str]:
    """Names bound to the result of running a git fetch, push or ls-remote."""
    argv_names = _transport_lists(function)
    results: set[str] = set()
    for node in ast.walk(function):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        call = node.value
        runs_transport = (
            any(marker in _call_name(call) for marker in TRANSPORT_CALL_MARKERS)
            or _mentions_transport_verb(call)
            or any(isinstance(child, ast.Name) and child.id in argv_names for child in ast.walk(call))
        )
        if runs_transport:
            results.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return results


def _remote_value_names(function: ast.AST) -> set[str]:
    names = set(REMOTE_VALUE_NAMES)
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _call_name(node.value) in REMOTE_BINDING_CALLS:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def find_violations(source: str, relpath: str) -> list[str]:
    """Every interpolation in *source* of a remote value or a transport stderr."""
    found: set[tuple[int, str]] = set()
    tree = ast.parse(source)
    for function in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        remote_names = _remote_value_names(function)
        transport_results = _transport_results(function)
        for node in ast.walk(function):
            for part in _formatted_parts(node):
                for inner in _walk_outside_len(part):
                    leaked = None
                    if isinstance(inner, ast.Name) and inner.id in remote_names:
                        leaked = f"remote value {inner.id!r}"
                    elif (
                        isinstance(inner, ast.Attribute)
                        and inner.attr == "stderr"
                        and isinstance(inner.value, ast.Name)
                        and inner.value.id in transport_results
                    ):
                        leaked = f"git transport stderr {inner.value.id}.stderr"
                    if leaked is None:
                        continue
                    snippet = ast.unparse(node)
                    if any(
                        path == relpath and name == function.name and marker in snippet
                        for path, name, marker in ALLOWLIST
                    ):
                        continue
                    found.add((node.lineno, f"{relpath}:{node.lineno} {function.name}(): {leaked} in {snippet[:80]!r}"))
    return [message for _line, message in sorted(found)]


def test_no_message_in_src_interpolates_a_remote_value_or_transport_stderr():
    violations: list[str] = []
    for path in sorted(SRC_ROOT.rglob("*.py")):
        violations += find_violations(path.read_text(encoding="utf-8"), path.relative_to(SRC_ROOT).as_posix())

    assert not violations, "interpolated remote value or git transport stderr:\n" + "\n".join(violations)


def test_the_guard_flags_each_shape_it_exists_to_catch():
    bad_name = "def f(branch):\n    r = tracking_remote(branch)\n    raise E(f'fetch {r} failed')\n"
    bad_param = "def f(remote):\n    print('to %s' % remote)\n"
    bad_format = "def f(remote_name):\n    raise E('x {}'.format(remote_name))\n"
    bad_stderr = (
        "def f(sha):\n"
        "    fetched = _git(['fetch', 'origin', sha])\n"
        "    raise E(f'failed: {fetched.stderr.strip()[:400]}')\n"
    )
    bad_pushed = (
        "def f(cmd):\n"
        "    push_cmd = ['git', 'push', 'x']\n"
        "    result = subprocess.run(push_cmd)\n"
        "    raise E('out: ' + result.stderr)\n"
    )
    bad_readback = (
        "def f(ref):\n"
        "    result = git_ls_remote_with_token('o', ref, 't')\n"
        "    raise E(f'{result.stderr}')\n"
    )

    for source in (bad_name, bad_param, bad_format, bad_stderr, bad_pushed, bad_readback):
        assert find_violations(source, "x.py"), source


def test_the_guard_passes_text_that_names_no_remote_and_ignores_unrelated_stderr():
    clean = (
        "def f(branch, sha):\n"
        "    remote = tracking_remote(branch)\n"
        "    fetched = _git(['fetch', remote, sha])\n"
        "    log = _git(['log', sha])\n"
        "    note = f'bytes={len(fetched.stderr)}'\n"
        "    raise E(f'fetch of {sha} exited {fetched.returncode}; log said {log.stderr}')\n"
    )

    assert find_violations(clean, "x.py") == []
