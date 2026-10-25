"""A fake git host for `loadout-review run` and `post` on either platform, serving
one pull request built from a pull request object captured from a real host
(tests/real_payloads, sanitized). The diff, the changed-file list, the comments
and the posted verdicts are the test's; the pull request object, its endpoint
shapes and the comment paging follow the platform. Each platform's opener refuses
a URL shaped for the other, so a test that passes proves that platform's backend
was the one reached."""

from __future__ import annotations

import base64
import copy
import json
import urllib.parse
from pathlib import Path

from tests._support.fakes import FakeResponse, json_resp, paged
from tests._support.host_fakes import forgejo_tree_page

PAYLOADS = Path(__file__).resolve().parent.parent / "real_payloads"
FORGEJO_BASE_URL = "http://git-host.example.invalid:3000"
GITHUB_HOST = "api.github.com"
POSTED_AT = "2099-01-01T00:00:10Z"
#: The head an earlier review was stamped for, which the head fast-forwards from.
PRIOR_HEAD = "d" * 40


class ReviewHost:
    """One pull request on one platform. ``posted`` collects the verdict comments
    the verbs post; they are served back with any ``comments`` the test seeded."""

    def __init__(
        self,
        platform: str,
        *,
        diff: str,
        base_sha: str,
        files: list[str] | None = None,
        comments: list[dict] | None = None,
        login: str = "reviewer",
        base_files: dict[str, str] | None = None,
        comment_read_failures: int = 0,
        range_diff: str | None = None,
    ) -> None:
        self.platform = platform
        #: The net diff of the fast-forward from PRIOR_HEAD to the head, served by
        #: the compare endpoints for a delta review; None serves none.
        self.range_diff = range_diff
        #: The tree served by the tree and contents endpoints at any ref; None
        #: serves none, so a test that reads policy from the host must give it.
        self.base_files = base_files
        #: How many comment listings fail (HTTP 500) before they succeed.
        self.comment_read_failures = comment_read_failures
        pull = json.loads((PAYLOADS / f"{platform}_pull.json").read_text(encoding="utf-8"))
        pull["base"]["sha"] = base_sha
        self.pull = pull
        self.number: int = pull["number"]
        self.head_sha: str = pull["head"]["sha"]
        self.diff = diff
        self.files = files if files is not None else []
        self.seeded = list(comments or [])
        self.posted: list[dict] = []
        self.login = login
        self.requests: list[tuple[str, str]] = []

    @property
    def comments(self) -> list[dict]:
        return [*self.seeded, *self.posted]

    @property
    def last_body(self) -> str | None:
        return self.posted[-1]["body"] if self.posted else None

    def platform_args(self) -> list[str]:
        args = ["--platform", self.platform, "--pr", str(self.number)]
        if self.platform == "forgejo":
            args += ["--git-host-base-url", FORGEJO_BASE_URL]
        return args

    def _store(self, req) -> dict:
        body = json.loads(req.data.decode("utf-8"))["body"]
        comment = {
            "id": 9000 + len(self.posted),
            "user": {"login": self.login},
            "body": body,
            "created_at": POSTED_AT,
            "html_url": "http://comment.example.invalid/1",
        }
        self.posted.append(comment)
        return comment

    def _comment_read_fails(self) -> bool:
        if self.comment_read_failures > 0:
            self.comment_read_failures -= 1
            return True
        return False

    def _policy_file(self, method: str, url: str):
        """The tree and contents reads of the policy files, the same on both
        platforms apart from the tree's paging; None for any other URL."""
        if method != "GET" or self.base_files is None:
            return None
        if "/git/trees/" in url:
            entries = [{"path": name, "type": "blob"} for name in self.base_files]
            query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            if self.platform == "forgejo":
                return json_resp(
                    200,
                    forgejo_tree_page(
                        entries,
                        per_page=int(query["per_page"][0]),
                        page=int(query.get("page", ["1"])[0]),
                    ),
                )
            return json_resp(200, {"tree": entries, "truncated": False})
        if "/contents/" in url:
            name = urllib.parse.unquote(url.split("/contents/", 1)[1].split("?")[0])
            if name not in self.base_files:
                return FakeResponse(404, b"{}")
            encoded = base64.b64encode(self.base_files[name].encode("utf-8")).decode("ascii")
            return json_resp(200, {"type": "file", "encoding": "base64", "content": encoded})
        return None

    def opener(self, req, timeout=30):
        url = req.full_url
        method = req.get_method()
        self.requests.append((method, url))
        path = urllib.parse.urlparse(url).path
        served = self._policy_file(method, url)
        if served is not None:
            return served
        if self.platform == "github":
            assert GITHUB_HOST in url, f"expected a GitHub-shaped URL, got {url}"
            return self._github(req, method, url, path)
        assert url.startswith(f"{FORGEJO_BASE_URL}/api/v1/") or "/compare/" in url, (
            f"expected a Forgejo URL, got {url}"
        )
        return self._forgejo(req, method, url, path)

    def _github(self, req, method, url, path):
        n = self.number
        if method == "GET" and "/compare/" in path:
            assert self.range_diff is not None, f"unexpected compare request: {url}"
            if req.get_header("Accept", "") == "application/vnd.github.v3.diff":
                response = FakeResponse(200, self.range_diff.encode("utf-8"))
                response.headers = {"Content-Type": "text/plain"}
                return response
            return json_resp(200, {"status": "ahead"})
        if method == "GET" and path.endswith(f"/pulls/{n}"):
            if req.get_header("Accept", "") == "application/vnd.github.v3.diff":
                response = FakeResponse(200, self.diff.encode("utf-8"))
                response.headers = {"Content-Type": "text/plain"}
                return response
            return json_resp(200, copy.deepcopy(self.pull))
        if method == "GET" and path.endswith(f"/pulls/{n}/files"):
            return json_resp(200, [{"filename": f, "status": "modified"} for f in self.files])
        if method == "GET" and path.endswith(f"/issues/{n}/comments"):
            if self._comment_read_fails():
                return FakeResponse(500, b"{}")
            return json_resp(200, paged(url, self.comments))
        if method == "POST" and path.endswith(f"/issues/{n}/comments"):
            return json_resp(201, self._store(req))
        if method == "GET" and path.endswith("/user"):
            return json_resp(200, {"login": self.login})
        raise AssertionError(f"unexpected request: {method} {url}")

    def _forgejo(self, req, method, url, path):
        n = self.number
        if method == "GET" and "/compare/" in path:
            assert self.range_diff is not None, f"unexpected compare request: {url}"
            if path.endswith(".diff"):
                return FakeResponse(200, self.range_diff.encode("utf-8"))
            # The API route reports commits only: one commit forward from the prior
            # head to the head, none the other way.
            forward = path.endswith(f"/compare/{PRIOR_HEAD}...{self.head_sha}")
            return json_resp(200, {"total_commits": 1 if forward else 0})
        if method == "GET" and path.endswith(f"/pulls/{n}.diff"):
            return FakeResponse(200, self.diff.encode("utf-8"))
        if method == "GET" and path.endswith(f"/pulls/{n}/files"):
            return json_resp(200, [{"filename": f, "status": "modified"} for f in self.files])
        if method == "GET" and path.endswith(f"/pulls/{n}"):
            return json_resp(200, copy.deepcopy(self.pull))
        if method == "GET" and path.endswith(f"/issues/{n}/comments"):
            if self._comment_read_fails():
                return FakeResponse(500, b"{}")
            return json_resp(200, self.comments)
        if method == "POST" and path.endswith(f"/issues/{n}/comments"):
            return json_resp(201, self._store(req))
        if method == "GET" and path.endswith("/api/v1/user"):
            return json_resp(200, {"login": self.login})
        raise AssertionError(f"unexpected request: {method} {url}")
