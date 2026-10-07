"""Shared fixtures for the review-post verb's tests: a GitHub fake that echoes
back the body actually posted (so the readback reflects the tool-built fence),
and the verb runner that feeds a body on stdin."""

from __future__ import annotations

import io
import json

from clagentic_loadout.review import verb
from tests._support.fakes import (
    FakeResponse,
    RecordingTokenProvider,
    RefusingTokenProvider,
    json_resp,
)

__all__ = [
    "FakeResponse",
    "RecordingTokenProvider",
    "RefusingTokenProvider",
    "github_verdict_opener",
    "json_resp",
    "run_main",
]


def github_verdict_opener(*, pr_number=42, posted_id=5, landed_body=None, capture_into=None):
    """Echoes back whatever body was actually posted -- the readback must
    reflect the REAL posted body (including the tool-constructed fence), not
    a hand-crafted fixture string, so the mismatch-detection tests can
    override `landed_body` to simulate a mangled-in-transit fence.
    `landed_body`, when given a callable, is invoked with the real posted
    body and must return the (possibly mangled) body the ordinary substring
    readback will see, letting a mismatch test pass the ordinary
    post-and-verify check while corrupting only the fence. `capture_into`,
    when given a dict, is populated with {"posted_body": ...} for a caller
    that wants to inspect exactly what the verb constructed and posted."""
    state: dict = capture_into if capture_into is not None else {}
    state.setdefault("posted_body", None)

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET" and url.endswith(f"/issues/{pr_number}/comments") and state["posted_body"] is None:
            # Pre-POST dedupe readback (the GitHub backend's idempotency
            # check): nothing has been posted yet, so no existing-own-comment
            # match is possible.
            return json_resp(200, [])
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            state["posted_body"] = json.loads(req.data.decode("utf-8"))["body"]
            return json_resp(200, {"id": posted_id, "html_url": "http://post"})
        if url.endswith("/user"):
            return json_resp(200, {"login": "reviewer"})
        if url.endswith(f"/issues/{pr_number}/comments"):
            if callable(landed_body):
                body = landed_body(state["posted_body"])
            else:
                body = landed_body if landed_body is not None else state["posted_body"]
            return json_resp(
                200,
                [
                    {
                        "id": posted_id,
                        "user": {"login": "reviewer"},
                        "body": body,
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://readback",
                    }
                ],
            )
        raise AssertionError(f"unexpected: {method} {url}")

    return opener


def run_main(argv, *, stdin_bytes, token_provider, opener, monkeypatch):
    """Drive verb.main() with *stdin_bytes* on the (monkeypatched) stdin
    buffer. --body-env is the DEFAULT body-ingestion route when neither
    --body-env nor --body-stdin is passed, so --body-stdin is injected here
    unless *argv* already names a body-ingestion flag, driving stdin content
    through the invocation without a per-test edit."""
    monkeypatch.setattr("sys.stdin", type("_S", (), {"buffer": io.BytesIO(stdin_bytes)})())
    if "--body-stdin" not in argv and "--body-env" not in argv:
        argv = [*argv, "--body-stdin"]
    return verb.main(argv, token_provider=token_provider, opener=opener)
