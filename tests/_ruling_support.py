"""Shared builders for the ruling registry tests: rulings, registry files,
findings carrying a fingerprint, and the gate repository and verdict fence the
merge-gate tests read. Names are synthetic throughout."""

from __future__ import annotations

import base64
import datetime as dt
import json
import os
import urllib.parse
from pathlib import Path

import yaml

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import DISPOSITIONS_PATH
from clagentic_loadout.review.rulings import Ruling, build_ruling, rulings_document
from tests._gate_repo import commit_files, git, init_gate_repo
from tests._review_evaluate_support import facts, finding
from tests._support.fakes import FakeResponse
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

HEAD = "b" * 40
NAME = "some-reviewer"
REPO = "some-owner/some-repo"
FP = "fp2:0123456789abcdef"
OTHER_FP = "fp2:fedcba9876543210"
TODAY = dt.date(2026, 10, 1)

#: The day the merge gate reads as "today" in tests that pin it, ten days after
#: a default ruling's date: inside the 30-day accepted_risk window.
GATE_DAY = TODAY + dt.timedelta(days=10)


def pin_gate_clock(monkeypatch, day: dt.date = GATE_DAY) -> None:
    """Make every site the merge gate reads the UTC date from return *day*, so
    ruling expiry does not depend on the wall clock."""
    from clagentic_loadout.review import evaluate, evaluation

    for module in (evaluation, evaluate):
        monkeypatch.setattr(module, "today_utc", lambda: day)


#: Blocks on the worst-case stakes every unstated path resolves to, and is not a
#: security-floor category: a ruling may clear it.
NON_FLOOR = facts(confidentiality="full", integrity="full", availability="full")
#: A security-floor category: no ruling may clear it.
FLOOR = facts(category="injection", confidentiality="full", integrity="full", availability="full")


def ruling(**overrides) -> Ruling:
    fields = {
        "repo": REPO,
        "pr": 1,
        "fingerprint": FP,
        "rule_id": "R1",
        "path": "src/a.py",
        "kind": "accepted_risk",
        "rationale": "accepted for this change",
        "by": "some-role",
        "requested_by": "some-caller",
        "source_ref": "design note 4",
        "at": TODAY,
    }
    fields.update(overrides)
    return build_ruling(**fields)


def registry_dir(root: Path) -> Path:
    directory = root / "registry"
    directory.mkdir(exist_ok=True)
    os.chmod(directory, 0o755)
    return directory


def write_registry(root: Path, rulings: list[Ruling]) -> Path:
    """A trusted registry file under *root*: owner-writable, not world-writable."""
    path = registry_dir(root) / "rulings.yaml"
    path.write_text(yaml.safe_dump(rulings_document(rulings), sort_keys=False), encoding="utf-8")
    os.chmod(path, 0o644)
    return path


def ruled_finding(fact=NON_FLOOR, *, fingerprint=FP, **extra) -> dict:
    return finding(fact=fact, fingerprint=fingerprint, **extra)


def gate_repo(tmp_path: Path, *, dispositions: str | None = None, **merge_gate) -> Path:
    """A repo whose main tip carries the tracked gate (and dispositions)."""
    path = tmp_path / "repo"
    init_gate_repo(path, tracked_gate={"required_reviewer_roles": [], **merge_gate})
    if dispositions is not None:
        commit_files(path, {DISPOSITIONS_PATH: dispositions}, "chore: add dispositions")
    return path


def tip(repo: Path) -> str:
    return git(repo, "rev-parse", "main")


#: Default for fence(opened=...): derive the open entries from the findings.
DERIVE = object()


def open_entries(findings):
    """findings_open as the post path writes it: one entry per distinct id."""
    entries = {}
    for item in findings:
        state_id = finding_state_id(item["file"], item["line"], item["rule_id"])
        entries.setdefault(state_id, {"id": state_id, "rule_id": item["rule_id"], "head": HEAD})
    return list(entries.values())


def fence(
    outcome, *, findings=(), status=None, head=HEAD, reasons=None, base=None, keep_findings=True,
    opened=DERIVE, **more,
):
    """A fence of *outcome*. A BLOCKED fence records every finding as blocking
    unless *opened* names the entries (None leaves findings_open out)."""
    state = {
        "verdict_outcome": outcome,
        "verdict_reasons": reasons if reasons is not None else ([] if outcome == "CLEAN" else ["recorded"]),
        "evaluator_version": EVALUATOR_VERSION,
        **more,
    }
    if opened is DERIVE:
        opened = open_entries(findings) if outcome == "BLOCKED" else None
    if opened is not None:
        state["findings_open"] = opened
    if keep_findings:
        state["evaluated_findings"] = list(findings)
    if base:
        state["policy_base_sha"] = base
    status = status or ("clean" if outcome == "CLEAN" else "blocking")
    return build_verdict_block(NAME, status, head, 1, findings_state=state)


def evaluated_finding(fact=NON_FLOOR, *, fingerprint=FP, **extra) -> dict:
    """A finding as the evaluated_findings of a fence carries it."""
    item = {k: v for k, v in ruled_finding(fact, fingerprint=fingerprint, **extra).items()
            if k in ("file", "line", "rule_id", "message", "facts", "fingerprint")}
    if fingerprint is None:
        del item["fingerprint"]
    return item


LOGIN = "reviewer-login"


def comment(comment_id, body):
    """A host comment authored by the required reviewer's login."""
    return {"id": comment_id, "user": {"login": LOGIN}, "body": body}


class Run:
    def __init__(self, code, err, out, posted, requests):
        self.code, self.err, self.out, self.posted, self.requests = code, err, out, posted, requests


def _base_files(repo: Path) -> dict[str, str]:
    """The file tree at the repository's main tip, as the host would serve it."""
    names = git(repo, "ls-tree", "-r", "--name-only", tip(repo)).splitlines()
    return {name: git(repo, "show", f"{tip(repo)}:{name}") for name in names}


def _host_files_opener(inner, files: dict[str, str], requests: list[str], contents_http: int):
    """Serve the git-trees and contents endpoints from *files*, defer the rest."""

    def opener(req, timeout=15):
        url = req.full_url
        if req.get_method() == "GET" and "/git/trees/" in url:
            requests.append(url)
            entries = [{"path": name, "type": "blob"} for name in files]
            return FakeResponse(200, json.dumps({"tree": entries, "truncated": False}).encode())
        if req.get_method() == "GET" and "/contents/" in url:
            requests.append(url)
            if contents_http != 200:
                return FakeResponse(contents_http, b"{}")
            name = urllib.parse.unquote(url.split("/contents/", 1)[1].split("?")[0])
            if name not in files:
                return FakeResponse(404, b"{}")
            encoded = base64.b64encode(files[name].encode("utf-8")).decode("ascii")
            body = {"type": "file", "encoding": "base64", "content": encoded}
            return FakeResponse(200, json.dumps(body).encode())
        return inner(req, timeout)

    return opener


def merge(
    comments, repo: Path | None, capsys, *, head=HEAD, extra=None, pr_extra=None,
    local=True, contents_http=200,
):
    """Run the merge verb against *repo*'s main tip as the PR base. *local*
    False passes no --repo-path (the repository is then reachable only through
    the host's tree and contents endpoints)."""
    posted: list[str] = []
    policy_requests: list[str] = []
    info = {"head": {"sha": head}, "title": "feat: x"}
    if repo is not None:
        info["base"] = {"ref": "main", "sha": tip(repo), "repo": {"private": True}}
    info.update(pr_extra or {})
    inner = make_opener(pr_info=info, comments=comments)
    served = _host_files_opener(
        inner, _base_files(repo) if repo is not None else {}, policy_requests, contents_http
    )

    def opener(req, timeout=15):
        if req.get_method() == "POST" and "/comments" in req.full_url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return served(req, timeout)

    argv = base_args(
        **{"--required-reviewer": f"{NAME}:{LOGIN}", **({"--repo-path": str(repo)} if repo and local else {})}
    )
    capsys.readouterr()
    code = verb.main(
        argv + (extra or []),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return Run(code, captured.err, captured.out, posted, policy_requests)
