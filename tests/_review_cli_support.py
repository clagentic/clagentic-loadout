"""Shared fixtures for the loadout-review tests: a combined GitHub fake HTTP
opener (acquire reads plus review-post writes), a synthetic diff builder, and
a stub carrier driven by a mode file so every retry/fallback path runs against
a real subprocess without any real model."""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import yaml

from clagentic_loadout.transport import attestation

BASE_SHA = "a" * 40
HEAD_SHA = "b" * 40

STUB_SCRIPT = textwrap.dedent(
    '''
    import json, os, re, sys, time

    root = os.environ["STUB_DIR"]
    name = NAME
    prompt = sys.stdin.read()
    logdir = os.path.join(root, name + "-prompts")
    os.makedirs(logdir, exist_ok=True)
    count = len(os.listdir(logdir))
    with open(os.path.join(logdir, "%04d.txt" % count), "w") as handle:
        handle.write(prompt)
    with open(os.path.join(root, name + ".mode")) as handle:
        mode = handle.read().strip()

    match = re.search(r"diff --git a/(\\S+) b/", prompt)
    path = match.group(1) if match else "unknown"
    array = json.dumps([{"file": path, "line": 1, "rule_id": "R1",
                         "severity": "nit", "message": "stub finding"}])

    if mode == "array":
        print(array)
    elif mode == "empty":
        print("[]")
    elif mode == "prose_then_array":
        print("Looks fine to me, nothing to add." if count == 0 else array)
    elif mode == "prose_then_exit127":
        if count == 0:
            print("Looks fine to me, nothing to add.")
        else:
            sys.exit(127)
    elif mode == "prose":
        print("I reviewed the change and it seems fine.")
    elif mode == "exit127":
        sys.exit(127)
    elif mode == "exit1":
        sys.stderr.write("carrier blew up\\n")
        sys.exit(1)
    elif mode == "exit2":
        sys.stderr.write(NAME + " blew up differently\\n")
        sys.exit(2)
    elif mode == "exit127_noisy":
        sys.stderr.write(NAME + " engine is missing\\n")
        sys.exit(127)
    elif mode == "usage_limit":
        sys.stderr.write("prompt echo\\nERROR: You've hit your usage limit.\\n")
        sys.exit(1)
    elif mode == "timeout_then_array":
        if count == 0:
            time.sleep(30)
        print(array)
    elif mode == "hang":
        sys.stdout.write("partial-reply-before-stall")
        sys.stdout.flush()
        sys.stderr.write("stall-diagnostic-on-stderr")
        sys.stderr.flush()
        time.sleep(30)
    elif mode == "stall_marker":
        stalls = 0
        for entry in os.listdir(logdir):
            with open(os.path.join(logdir, entry)) as logged:
                stalls += "STALL_ME" in logged.read()
        if "STALL_ME" in prompt and stalls <= 2:
            time.sleep(30)
        print(array)
    else:
        sys.exit(99)
    '''
)


def write_stub(directory: Path, name: str, mode: str) -> list[str]:
    """Write (or re-mode) a stub engine; returns its argv."""
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / f"stub_{name}.py"
    script.write_text(f"NAME = {name!r}\n" + STUB_SCRIPT, encoding="utf-8")
    (directory / f"{name}.mode").write_text(mode, encoding="utf-8")
    return [sys.executable, str(script)]


def set_mode(directory: Path, name: str, mode: str) -> None:
    (directory / f"{name}.mode").write_text(mode, encoding="utf-8")


def prompts(directory: Path, name: str) -> list[str]:
    logdir = directory / f"{name}-prompts"
    if not logdir.is_dir():
        return []
    return [p.read_text(encoding="utf-8") for p in sorted(logdir.iterdir())]


def write_profile_config(
    config_root: Path,
    *,
    role: str = "reviewer",
    carrier: list[str],
    fallback: list[str] | None = None,
    **extra,
) -> None:
    profile: dict = {"carrier": carrier, "parallel": 1, "timeout_seconds": 10}
    if fallback is not None:
        profile["fallback"] = fallback
    profile.update(extra)
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "config.yaml").write_text(
        yaml.safe_dump({"review": {"profiles": {role: profile}}}), encoding="utf-8"
    )


def make_diff(files: dict[str, int]) -> str:
    """A unified diff with one hunk per file of the given added-line count."""
    parts = []
    for name, added in files.items():
        parts.append(f"diff --git a/{name} b/{name}")
        parts.append("index 1111111..2222222 100644")
        parts.append(f"--- a/{name}")
        parts.append(f"+++ b/{name}")
        parts.append(f"@@ -0,0 +1,{added} @@")
        parts.extend(f"+line {i} of {name}" for i in range(1, added + 1))
    return "\n".join(parts) + "\n"


def identity_provider(subject: str = "reviewer"):
    identity = attestation.Identity(subject, attestation.SOURCE_CONFIGURED)
    return lambda: identity


class Env:
    """One synthetic deployment: user config root, stub engines, run root,
    an empty repo-path, and a fake host API serving `diff` for PR 42."""

    def __init__(self, root: Path) -> None:
        self.cfg = root / "cfg"
        self.stubs = root / "stubs"
        self.runs = root / "runs"
        self.repo = root / "repo"
        self.repo.mkdir()
        self.diff = make_diff({"a.py": 4})
        self.token_provider = RecordingTokenProvider()
        self.opener_state: dict = {}

    def configure(self, *, carrier_mode="array", fallback_mode=None, **extra) -> None:
        carrier = write_stub(self.stubs, "carrier", carrier_mode)
        fallback = None
        if fallback_mode is not None:
            fallback = write_stub(self.stubs, "fallback", fallback_mode)
        write_profile_config(self.cfg, carrier=carrier, fallback=fallback, **extra)

    def main(self, command: str, *extra: str, identity=None, **opener_kwargs) -> int:
        """One verb invocation against this deployment; returns the exit code.
        `identity` overrides the attested subject; the rest shape the fake host."""
        from clagentic_loadout.review import cli as review_cli

        return review_cli.main(
            [
                command, "--caller", "reviewer", "--repo", "some-owner/some-repo",
                "--pr", "42", "--platform", "github", "--repo-path", str(self.repo), *extra,
            ],
            token_provider=self.token_provider,
            opener=github_opener(diff=self.diff, state=self.opener_state, **opener_kwargs),
            identity_provider=identity_provider(identity or "reviewer"),
            config_root=self.cfg,
            run_root=self.runs,
        )

    def invoke(self, command: str, *extra: str, capsys, **kwargs) -> tuple[int, str, str]:
        """Run a verb and return (exit code, stdout, stderr) of that call only."""
        capsys.readouterr()
        code = self.main(command, *extra, **kwargs)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    def _result(self, command: str, extra, capsys, **kwargs) -> tuple[int, dict]:
        code, out, _ = self.invoke(command, *extra, capsys=capsys, **kwargs)
        out = out.strip()
        return code, json.loads(out.splitlines()[-1]) if out else {}

    def run(self, *extra: str, capsys, **kwargs) -> tuple[int, dict]:
        return self._result("run", extra, capsys, **kwargs)

    def post(self, *extra: str, capsys, **kwargs) -> tuple[int, dict]:
        return self._result("post", extra, capsys, **kwargs)


class RecordingTokenProvider:
    def __init__(self) -> None:
        self.resolved_for: list[str] = []

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.resolved_for.append(role)
        return "tok-123"


class _Response:
    def __init__(self, status: int, body: bytes, content_type: str = "application/json"):
        self.status = status
        self._body = body
        self.headers = {"Content-Type": content_type}

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json(payload) -> _Response:
    return _Response(200, json.dumps(payload).encode("utf-8"))


def github_opener(
    *,
    diff: str,
    pr_number: int = 42,
    base_sha: str = BASE_SHA,
    head_sha: str = HEAD_SHA,
    login: str = "reviewer",
    state: dict | None = None,
    compare: dict | None = None,
):
    """Serves the acquire reads and the review-post comment writes for one PR.
    `state` collects the posted body and every request URL. `compare`, when
    given, serves the commit-range endpoint: {"status": "ahead", "diff": "..."}
    (or "http_status" for a failing read); without it a compare request is an
    unexpected call."""
    recorded = state if state is not None else {}
    recorded.setdefault("posted_body", None)
    recorded.setdefault("requests", [])

    def opener(req, timeout=30):
        url = req.full_url
        method = req.get_method()
        accept = req.get_header("Accept", "")
        recorded["requests"].append((method, url))
        if method == "GET" and url.endswith(f"/pulls/{pr_number}"):
            if accept == "application/vnd.github.v3.diff":
                return _Response(200, diff.encode("utf-8"), "text/plain")
            return _json({"base": {"sha": base_sha}, "head": {"sha": head_sha}})
        if method == "GET" and url.endswith(f"/pulls/{pr_number}/files"):
            return _json([])
        if method == "GET" and "/compare/" in url and compare is not None:
            if compare.get("http_status", 200) != 200:
                return _Response(compare["http_status"], b"{}")
            if accept == "application/vnd.github.v3.diff":
                return _Response(200, compare["diff"].encode("utf-8"), "text/plain")
            return _json({"status": compare["status"]})
        if method == "POST" and url.endswith(f"/issues/{pr_number}/comments"):
            recorded["posted_body"] = json.loads(req.data.decode("utf-8"))["body"]
            return _json({"id": 5, "html_url": "http://post"})
        if method == "GET" and url.endswith("/user"):
            return _json({"login": login})
        if method == "GET" and url.endswith(f"/issues/{pr_number}/comments"):
            if recorded["posted_body"] is None:
                return _json([])
            return _json(
                [
                    {
                        "id": 5,
                        "user": {"login": login},
                        "body": recorded["posted_body"],
                        "created_at": "2099-01-01T00:00:10Z",
                        "html_url": "http://readback",
                    }
                ]
            )
        raise AssertionError(f"unexpected request: {method} {url} accept={accept!r}")

    return opener
