"""This repository's own declared interface matches the code.

`.clagentic/loadout/interface.yaml` lists what consumers may rely on: the console
scripts, each script's exit-code table, the shipped schema files and the
environment variable names. A change to one of them without the file fails here,
so the declaration cannot drift silently.

What is read from the code, and how:

  console scripts   the [project.scripts] table of pyproject.toml.
  exit codes        the module-level ``EXIT_*`` assignments of an integer literal in
                    the module each script runs. An alias of another constant is not
                    a code of its own and is not listed. The meaning is the constant
                    name without its prefix, in lower case.
  schemas           the ``*.json`` files in the package's schemas directory.
  env names         every ``CLAGENTIC_LOADOUT_*`` name written out in a source file;
                    a prefix that is completed at run time (it ends in an underscore)
                    is not a name.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from clagentic_loadout.interface.diff import diff_documents
from clagentic_loadout.interface.model import INTERFACE_PATH, InterfaceDoc, parse_interface_text

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "src" / "clagentic_loadout"
_ENV_NAME_RE = re.compile(r"CLAGENTIC_LOADOUT_[A-Z0-9_]*[A-Z0-9](?![A-Z0-9_])")
_SCRIPT_LINE_RE = re.compile(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"([^"]+)"\s*$')
_EXIT_PREFIX = "EXIT_"


def console_scripts() -> dict[str, str]:
    """Script name -> ``module:function`` from the [project.scripts] table."""
    scripts: dict[str, str] = {}
    in_table = False
    for line in (ROOT / "pyproject.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_table = stripped == "[project.scripts]"
            continue
        match = _SCRIPT_LINE_RE.match(line) if in_table else None
        if match:
            scripts[match.group(1)] = match.group(2)
    return scripts


def module_exit_codes(module_file: Path) -> dict[str, int]:
    """The integer-literal ``EXIT_*`` constants assigned at the top of a module."""
    codes: dict[str, int] = {}
    for node in ast.parse(module_file.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if (
            isinstance(target, ast.Name)
            and target.id.startswith(_EXIT_PREFIX)
            and isinstance(value, ast.Constant)
            and isinstance(value.value, int)
            and not isinstance(value.value, bool)
        ):
            codes[target.id[len(_EXIT_PREFIX) :].lower()] = value.value
    return codes


def code_exit_tables() -> dict[str, dict[str, int]]:
    tables: dict[str, dict[str, int]] = {}
    for script, target in console_scripts().items():
        module = target.split(":", 1)[0]
        module_file = ROOT / "src" / Path(*module.split(".")).with_suffix(".py")
        assert module_file.is_file(), f"{script} runs {module}, which is not a file under src/"
        tables[script] = module_exit_codes(module_file)
    return tables


def code_schemas() -> set[str]:
    return {p.name for p in (PACKAGE / "schemas").glob("*.json")}


def code_env_names() -> set[str]:
    names: set[str] = set()
    for source in PACKAGE.rglob("*.py"):
        names.update(_ENV_NAME_RE.findall(source.read_text(encoding="utf-8")))
    return names


def problems(declared: InterfaceDoc) -> list[str]:
    """Every way *declared* disagrees with the code, one sentence each."""
    found: list[str] = []
    tables = code_exit_tables()
    declared_verbs = {v.name: dict(v.exit_codes) for v in declared.verbs}
    for name in sorted(set(tables) - set(declared_verbs)):
        found.append(f"console script {name} is not declared")
    for name in sorted(set(declared_verbs) - set(tables)):
        found.append(f"declared verb {name} is not a console script")
    for name in sorted(set(tables) & set(declared_verbs)):
        actual, listed = tables[name], declared_verbs[name]
        if actual != listed:
            found.append(
                f"{name} exit codes: the code has {dict(sorted(actual.items()))}, "
                f"the file declares {dict(sorted(listed.items()))}"
            )
    schemas = code_schemas()
    for name in sorted(schemas - set(declared.schemas)):
        found.append(f"shipped schema {name} is not declared")
    for name in sorted(set(declared.schemas) - schemas):
        found.append(f"declared schema {name} is not shipped")
    env = code_env_names()
    for name in sorted(env - set(declared.env)):
        found.append(f"environment name {name} is not declared")
    for name in sorted(set(declared.env) - env):
        found.append(f"declared environment name {name} is not in the code")
    return found


def _own_interface() -> InterfaceDoc:
    return parse_interface_text(
        (ROOT / INTERFACE_PATH).read_text(encoding="utf-8"), INTERFACE_PATH
    )


def test_the_declared_interface_matches_the_code():
    assert problems(_own_interface()) == []


def test_the_check_notices_each_kind_of_drift():
    declared = parse_interface_text(
        "schema_version: 1\n"
        "verbs:\n"
        "  - name: loadout-spec\n"
        "    exit_codes: {ok: 0}\n"
        "  - name: not-a-script\n"
        "schemas: [not-shipped.json]\n"
        "env: [CLAGENTIC_LOADOUT_NOT_IN_THE_CODE]\n",
        "synthetic",
    )

    found = "\n".join(problems(declared))

    assert "console script loadout-merge is not declared" in found
    assert "declared verb not-a-script is not a console script" in found
    assert "loadout-spec exit codes" in found
    assert "shipped schema spec.schema.json is not declared" in found
    assert "declared schema not-shipped.json is not shipped" in found
    assert "environment name CLAGENTIC_LOADOUT_SPECS_DIR is not declared" in found
    assert "declared environment name CLAGENTIC_LOADOUT_NOT_IN_THE_CODE is not in the code" in found


def test_the_exit_code_reader_takes_literals_and_skips_aliases(tmp_path):
    module = tmp_path / "m.py"
    module.write_text(
        "EXIT_OK = 0\nEXIT_BAD_THING: int = 7\nEXIT_ALIAS = EXIT_OK\nEXIT_FLAG = True\nOTHER = 3\n",
        encoding="utf-8",
    )

    assert module_exit_codes(module) == {"ok": 0, "bad_thing": 7}


def test_a_file_compared_with_itself_is_not_a_breaking_change():
    own = _own_interface()

    assert not diff_documents(own, own).breaking
