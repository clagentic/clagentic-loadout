"""Temporary directories made by the merge and review verbs follow
`identity.staging_root` for an identity verified by an assertion provider.

One table drives the real entry points (`loadout-merge` with a sandboxed
pre_check, `loadout-review run`). Two callers, each bound by a verified assertion
with its own actor and session claims, run at the same time: each verb's own
subprocess records where it is running, and waits until the other caller's
subprocess has done the same, so both directories exist together. Nothing the
verbs call is stubbed; only the git host's HTTP surface is a canned double.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

import pytest

from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.push.git_push import _credentialed_git_env
from clagentic_loadout.review.run_pipeline import CHUNK_WORKDIR_PREFIX
from clagentic_loadout.transport.seam_binding import (
    SeamIdentity,
    staging_temp_parent,
    temp_parent_kwargs,
)
from tests._gate_repo import init_gate_repo
from tests._review_cli_support import Env, write_profile_config
from tests._support.gate_merge import run_gate_merge
from tests._support.identity_support import ISSUER, Issuer
from tests.test_identity_binding import DIR_VAR, _assertion_env, _use_config

_PY = sys.executable
_TEMPLATE = "{scratch}/{{actor_name}}/{{sid}}"
_CALLERS = (("alice", "sA"), ("bob", "sB"))

#: Run by the verb as its own subprocess: record the working directory and the
#: TMPDIR it was given, then wait for the other caller's subprocess to do the same.
_RENDEZVOUS = """
import json, os, sys, time
mine, other = sys.argv[1], sys.argv[2]
with open(mine + '.part', 'w') as handle:
    handle.write(json.dumps({'cwd': os.getcwd(), 'tmp': os.environ.get('TMPDIR', '')}))
os.rename(mine + '.part', mine)
deadline = time.time() + 8
while not os.path.exists(other) and time.time() < deadline:
    time.sleep(0.05)
if not os.path.exists(other):
    sys.exit(1)
"""

_CARRIER = "import sys\nsys.stdin.read()\n" + _RENDEZVOUS + "print('[]')\n"

_LAUNCHER = f"#!{_PY}\nimport os, sys\nrest = sys.argv[sys.argv.index('--') + 1:]\nos.execvp(rest[0], rest)\n"


def _marker(tmp_path: Path, name: str) -> Path:
    return tmp_path / f"{name}.json"


def _wait_for(path: Path) -> None:
    deadline = time.time() + 30
    while not path.exists() and time.time() < deadline:
        time.sleep(0.05)
    assert path.exists(), f"{path} was never written: the verb did not reach its subprocess"


def _run_concurrently(monkeypatch, tmp_path: Path, runs: dict) -> dict:
    """Start each caller under its own delivery directory, in order, and return
    each one's exit code. A caller is started only once the one before it is inside
    its subprocess, so the process environment is never read for two bindings at once."""
    codes: dict[str, object] = {}

    def work(name, run):
        try:
            codes[name] = run()
        except BaseException as exc:  # reported through the assertion below
            codes[name] = exc

    threads = []
    for name, (delivery, run) in runs.items():
        monkeypatch.setenv(DIR_VAR, delivery)
        thread = threading.Thread(target=work, args=(name, run))
        thread.start()
        threads.append(thread)
        _wait_for(_marker(tmp_path, name))
    for thread in threads:
        thread.join(60)
    return codes


def _recorded(tmp_path: Path) -> dict[str, dict[str, Path]]:
    out = {}
    for name, _sid in _CALLERS:
        data = json.loads(_marker(tmp_path, name).read_text())
        out[name] = {"cwd": Path(data["cwd"]).resolve(), "tmp": Path(data["tmp"])}
    return out


def _configure(monkeypatch, tmp_path: Path, mode: str, role: str, **sections) -> tuple[Path, dict[str, str]]:
    """Identity config for *mode*; returns the scratch root and each caller's delivery directory."""
    scratch = tmp_path / "scratch"
    deliveries: dict[str, str] = {}
    if mode == "unset-legacy":
        _use_config(monkeypatch, tmp_path, None, **sections)
        return scratch, {name: "" for name, _ in _CALLERS}
    issuer = Issuer()
    jwks = issuer.write_jwks(tmp_path / "jwks.json")
    identity = {"provider": "gatekeeper", "gatekeeper": {"jwks": jwks, "issuer": ISSUER}}
    if mode == "keyed":
        identity["staging_root"] = _TEMPLATE.format(scratch=scratch)
    _use_config(monkeypatch, tmp_path, identity, **sections)
    for name, sid in _CALLERS:
        deliveries[name] = _assertion_env(
            tmp_path, issuer, f"d-{name}", sub=f"agent:{name}", sid=sid, jti=f"jti-{name}", roles=[role]
        )[DIR_VAR]
    return scratch, deliveries


def _merge_runs(tmp_path: Path, deliveries: dict[str, str]) -> dict:
    runs = {}
    for name, _sid in _CALLERS:
        other = next(n for n, _ in _CALLERS if n != name)
        step = {
            "cmd": [_PY, "-c", _RENDEZVOUS, str(_marker(tmp_path, name)), str(_marker(tmp_path, other))],
            "on_failure": "fail",
        }
        repo = init_gate_repo(
            tmp_path / f"repo-{name}", tracked_gate={"required_reviewer_roles": [], "pre_checks": [step]}
        )
        runs[name] = (deliveries[name], lambda repo=repo: run_gate_merge(repo))
    return runs


def _review_runs(tmp_path: Path, deliveries: dict[str, str]) -> dict:
    runs = {}
    for name, _sid in _CALLERS:
        other = next(n for n, _ in _CALLERS if n != name)
        root = tmp_path / f"review-{name}"
        root.mkdir()
        env = Env(root)
        script = root / "carrier.py"
        script.write_text(_CARRIER, encoding="utf-8")
        write_profile_config(
            env.cfg,
            carrier=[_PY, str(script), str(_marker(tmp_path, name)), str(_marker(tmp_path, other))],
        )
        runs[name] = (deliveries[name], lambda env=env: env.main("run"))
    return runs


@pytest.mark.parametrize("verb", ["merge", "review"])
@pytest.mark.parametrize("mode", ["keyed", "unset-verified", "unset-legacy"])
def test_temporary_directories_follow_the_staging_root(
    verb, mode, monkeypatch, tmp_path, scratch_tmp, capsys
) -> None:
    monkeypatch.setenv("TMPDIR", str(scratch_tmp))
    if verb == "merge":
        launcher = tmp_path / "sandbox-launcher"
        launcher.write_text(_LAUNCHER, encoding="utf-8")
        launcher.chmod(0o755)
        scratch, deliveries = _configure(
            monkeypatch, tmp_path, mode, "merger", merge={"pre_checks_sandbox": [str(launcher), "--"]}
        )
        runs = _merge_runs(tmp_path, deliveries)
    else:
        scratch, deliveries = _configure(monkeypatch, tmp_path, mode, "reviewer")
        runs = _review_runs(tmp_path, deliveries)

    codes = _run_concurrently(monkeypatch, tmp_path, runs)

    assert codes == {name: merge_verb.EXIT_OK if verb == "merge" else 0 for name, _ in _CALLERS}, (
        codes,
        capsys.readouterr(),
    )
    found = _recorded(tmp_path)
    roots = {
        name: (scratch / name / sid).resolve() if mode == "keyed" else scratch_tmp.resolve()
        for name, sid in _CALLERS
    }
    for name, _sid in _CALLERS:
        work = found[name]["cwd"]
        if verb == "merge":
            # The merge-result clone sits one level below its `loadout-precheck-*` directory,
            # and the sandboxed step's private directory is beside it.
            parent, prefix, kept = work.parent.parent, "loadout-precheck-", work.parent
            private = found[name]["tmp"]
            assert private.parent.resolve() == roots[name] and private.name.startswith("pre-check-")
            assert not private.exists()
        else:
            parent, prefix, kept = work.parent, CHUNK_WORKDIR_PREFIX, work
        assert parent == roots[name] and kept.name.startswith(prefix), (name, work)
        assert not kept.exists()
        if deliveries[name]:
            assert not (Path(deliveries[name]) / "tmp").exists()
    a, b = (found[name]["cwd"] for name, _ in _CALLERS)
    assert a != b
    if mode == "keyed":
        assert roots["alice"] != roots["bob"]
        assert roots["alice"] not in roots["bob"].parents and roots["bob"] not in roots["alice"].parents


def _identity(provider: str, actor: str = "agent:alice") -> SeamIdentity:
    return SeamIdentity("builder", "assertion", provider=provider, self_declared=False, actor=actor, dir="/unused", sid="s")


def test_the_temp_parent_is_absent_unless_a_verified_identity_meets_the_key(monkeypatch, tmp_path) -> None:
    scratch = tmp_path / "scratch"
    cfg = {"provider": "static", "static": {"actor": "x", "roles": ["builder"]}}
    _use_config(monkeypatch, tmp_path, {**cfg, "staging_root": f"{scratch}/{{actor_name}}/{{sid}}"})
    assert staging_temp_parent(None) is None
    assert staging_temp_parent(_identity("declared")) is None
    assert not scratch.exists()


def test_an_unset_temp_parent_adds_no_keyword_to_a_call() -> None:
    assert temp_parent_kwargs(None) == {}
    assert temp_parent_kwargs("/a") == {"temp_parent": "/a"}


def test_the_credentialed_git_environment_is_made_under_the_given_parent(tmp_path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    with _credentialed_git_env("token", temp_parent=str(parent)) as env:
        home = Path(env["HOME"])
        assert home.parent.parent == parent and home.parent.name.startswith("loadout_push_")
    assert list(parent.iterdir()) == []
