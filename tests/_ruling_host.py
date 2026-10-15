"""A fake git host for the ruling verb's fence read: the pull request, its
comments, and the base tree that carries the repository gate. Names are
synthetic throughout. Both platforms' endpoint shapes are served by one
URL-routing opener, so a test names the scenario, not the plumbing."""

from __future__ import annotations

import base64
import io
import json
import urllib.error
import urllib.parse

import yaml

from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.repo_config import TRACKED_GATE_RELATIVE_PATH
from clagentic_loadout.review.evaluate import EVALUATOR_VERSION
from clagentic_loadout.review.evaluation import evaluated_findings_for_fence
from tests._support.fakes import FakeResponse
from tests._support.merge_verb import make_opener

HEAD = "b" * 40
BASE = "d" * 40
REVIEWER = "some-reviewer"
FENCE_COMMENT = 7


def fence_body(
    findings,
    *,
    reviewer: str = REVIEWER,
    head: str = HEAD,
    pr: int = 1,
    evaluated: bool = True,
) -> str:
    """The body of a reviewer's verdict comment, built with the product's own
    fence builder. *evaluated* False builds a fence that carries no computed
    verdict and no findings."""
    state = None
    if evaluated:
        state = {
            "verdict_outcome": "BLOCKED",
            "verdict_reasons": ["recorded"],
            "evaluator_version": EVALUATOR_VERSION,
            "evaluated_findings": evaluated_findings_for_fence(list(findings), with_fingerprint=True),
        }
    return build_verdict_block(reviewer, "blocking", head, pr, findings_state=state)


def comment(comment_id: int, body: str, *, login: str = REVIEWER) -> dict:
    return {"id": comment_id, "user": {"login": login}, "body": body}


def gate_text(roles) -> str:
    return yaml.safe_dump({"merge": {"required_reviewer_roles": list(roles)}})


def host_opener(
    comments,
    *,
    head: str = HEAD,
    gate_roles=(REVIEWER,),
    gate: str | None = None,
    with_gate: bool = True,
    pr_http: int = 200,
    requests: list[str] | None = None,
):
    """Serve the pull request at *head* with base *BASE*, *comments*, and (when
    *with_gate*) a tracked gate file at the base commit requiring *gate_roles*
    (or the literal *gate* text)."""
    files = {}
    if with_gate:
        files[TRACKED_GATE_RELATIVE_PATH] = gate if gate is not None else gate_text(gate_roles)
    pr_info = {"head": {"sha": head}, "base": {"ref": "main", "sha": BASE}, "title": "feat: x"}
    inner = make_opener(pr_info=pr_info, comments=list(comments))

    def opener(req, timeout=15):
        url = req.full_url
        if requests is not None:
            requests.append(f"{req.get_method()} {url}")
        path = urllib.parse.urlparse(url).path
        if req.get_method() == "GET" and path.endswith(("/pulls/1", "/pulls/2")) and pr_http != 200:
            raise urllib.error.HTTPError(url, pr_http, "err", {}, io.BytesIO(b"{}"))
        if req.get_method() == "GET" and "/git/trees/" in url:
            entries = [{"path": name, "type": "blob"} for name in files]
            return FakeResponse(200, json.dumps({"tree": entries, "truncated": False}).encode())
        if req.get_method() == "GET" and "/contents/" in url:
            name = urllib.parse.unquote(url.split("/contents/", 1)[1].split("?")[0])
            if name not in files:
                return FakeResponse(404, b"{}")
            encoded = base64.b64encode(files[name].encode("utf-8")).decode("ascii")
            return FakeResponse(
                200, json.dumps({"type": "file", "encoding": "base64", "content": encoded}).encode()
            )
        return inner(req, timeout)

    return opener
