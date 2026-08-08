"""test_merge_github_backend.py — tests for
clagentic_loadout.merge.github_backend (lr-5375, Wave B slice 4b).

Coverage:
  - get_pr_info / fetch_changed_files / fetch_comments: happy path via an
    injected opener (NO real network call anywhere in this file); non-200 /
    non-list / network-error all raise GateFactUnavailableError (fail-closed
    -- an unreadable gate fact is never treated as a passing gate). Mirrors
    test_merge_forgejo_backend.py's coverage shape exactly.
  - merge_pr: 200+merged:true success; 200+merged:false refused (never trust
    status code alone); 405/409/404/other non-2xx all raise
    MergeExecutionError; network error raises MergeExecutionError.
  - Redirect hardening (same class of finding as review.github_backend,
    lr-412f): the DEFAULT opener (no `opener` injected) must be built via
    transport.redirect_guard.no_redirect_opener(), never bare
    urllib.request.urlopen -- every call here carries a live GitHub bearer/
    App-installation token in Authorization. A 3xx must surface as a failure
    (GateFactUnavailableError or MergeExecutionError), never a false
    success, and only ONE request is ever made -- no second request to a
    redirect target, so the token is never replayed to it.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from clagentic_loadout.merge import github_backend
from clagentic_loadout.merge.errors import GateFactUnavailableError, MergeExecutionError

_OWNER = "some-owner"
_REPO = "some-repo"


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _json_response(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _http_error(url: str, code: int, payload: dict) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, "err", {}, io.BytesIO(json.dumps(payload).encode("utf-8"))
    )


def _opener_sequence(responses):
    """Return an opener callable that yields each entry in *responses* in
    order (one call per invocation), raising any Exception entry."""
    it = iter(responses)

    def opener(req, timeout=30):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        return item

    return opener


class TestGetPrInfo:
    def test_happy_path(self):
        opener = _opener_sequence(
            [_json_response(200, {"head": {"sha": "abc"}, "title": "feat: x"})]
        )
        info = github_backend.get_pr_info(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert info["head"]["sha"] == "abc"

    def test_non_200_raises_gate_fact_unavailable(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 404, {"message": "Not Found"})

        with pytest.raises(GateFactUnavailableError):
            github_backend.get_pr_info(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_network_error_raises_gate_fact_unavailable(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.get_pr_info(_OWNER, _REPO, 1, token="tok", opener=opener)


class TestGetPrHeadShaAndTitle:
    def test_extracts_head_sha(self):
        assert github_backend.get_pr_head_sha({"head": {"sha": "abc123"}}) == "abc123"

    def test_missing_head_sha_is_empty(self):
        assert github_backend.get_pr_head_sha({}) == ""

    def test_extracts_title(self):
        assert github_backend.get_pr_title({"title": "feat: x"}) == "feat: x"

    def test_missing_title_is_empty(self):
        assert github_backend.get_pr_title({}) == ""


class TestFetchChangedFiles:
    def test_happy_path(self):
        opener = _opener_sequence(
            [_json_response(200, [{"filename": "a.py"}, {"filename": "b.py"}])]
        )
        files = github_backend.fetch_changed_files(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert files == ["a.py", "b.py"]

    def test_non_200_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 500, {})

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_changed_files(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_non_list_body_raises(self):
        opener = _opener_sequence([_json_response(200, {"not": "a list"})])
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_changed_files(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_network_error_raises(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_changed_files(_OWNER, _REPO, 1, token="tok", opener=opener)


class TestFetchComments:
    def test_happy_path(self):
        opener = _opener_sequence(
            [_json_response(200, [{"id": 1, "user": {"login": "x"}, "body": "hi"}])]
        )
        comments = github_backend.fetch_comments(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert comments[0]["id"] == 1

    def test_non_200_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 403, {})

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_comments(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_non_list_body_raises(self):
        opener = _opener_sequence([_json_response(200, {"not": "a list"})])
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_comments(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_empty_body_is_empty_dict_not_crash(self):
        # An empty 200 body decodes to {} via _github_request; fetch_comments
        # must still fail closed (not a list) rather than silently returning
        # an empty comment list that would be indistinguishable from "no
        # comments yet" -- distinct from Forgejo's endpoint which returns an
        # empty LIST body for "no comments" rather than an empty OBJECT.
        opener = _opener_sequence([_FakeResponse(200, b"")])
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_comments(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_network_error_raises(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_comments(_OWNER, _REPO, 1, token="tok", opener=opener)


class TestMergePr:
    def test_200_merged_true_is_success(self):
        opener = _opener_sequence([_json_response(200, {"merged": True, "sha": "deadbeef"})])
        github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)  # no raise

    def test_200_merged_true_returns_merged_sha(self):
        # lr-7c5540: the merged commit SHA (when GitHub's own response body
        # carries one) is returned to the caller -- merge.verb._run threads
        # this straight into merge.tree_sync.advance_repo_to_merged_sha
        # rather than re-deriving it via a base-branch fetch.
        opener = _opener_sequence([_json_response(200, {"merged": True, "sha": "deadbeef"})])
        result = github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert result == "deadbeef"

    def test_200_merged_true_with_no_sha_field_returns_none(self):
        # A malformed/absent "sha" in an otherwise-successful response must
        # not raise -- the merge already succeeded. The caller falls back to
        # the base-branch-fetch resolution path instead.
        opener = _opener_sequence([_json_response(200, {"merged": True})])
        result = github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert result is None

    def test_200_merged_false_is_refused(self):
        # GitHub's "processed, did not merge" shape -- a 200 alone must
        # never be trusted without checking the merged field.
        opener = _opener_sequence(
            [_json_response(200, {"merged": False, "message": "Required status check pending"})]
        )
        with pytest.raises(MergeExecutionError) as exc_info:
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert "merged=true" in str(exc_info.value)

    def test_405_not_mergeable_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 405, {"message": "Pull Request is not mergeable"})

        with pytest.raises(MergeExecutionError) as exc_info:
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert "405" in str(exc_info.value)
        assert "not mergeable" in str(exc_info.value)

    def test_409_sha_mismatch_race_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 409, {"message": "Head branch was modified"})

        with pytest.raises(MergeExecutionError) as exc_info:
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert "409" in str(exc_info.value)

    def test_404_not_found_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 404, {"message": "Not Found"})

        with pytest.raises(MergeExecutionError) as exc_info:
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert "404" in str(exc_info.value)

    def test_other_non_2xx_raises(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 500, {"message": "server error"})

        with pytest.raises(MergeExecutionError) as exc_info:
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert "500" in str(exc_info.value)

    def test_network_error_raises(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(MergeExecutionError):
            github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)

    def test_merge_message_included_when_supplied(self):
        captured = {}

        def opener(req, timeout=30):
            captured["data"] = req.data
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(
            _OWNER, _REPO, 1, token="tok", merge_message="custom note", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["commit_message"] == "custom note"

    def test_merge_title_included_when_supplied(self):
        # lr-1953a8: commit_title composes the merge commit SUBJECT from the
        # caller-supplied title (merge.verb passes the PR's own title)
        # rather than GitHub's own "Merge pull request #N from owner/branch"
        # default, which otherwise embeds the source branch ref (and any
        # task id in it) into the subject with nobody typing it.
        captured = {}

        def opener(req, timeout=30):
            captured["data"] = req.data
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(
            _OWNER, _REPO, 1, token="tok",
            merge_title="fix(merge): correct stale-SHA check order", opener=opener,
        )
        payload = json.loads(captured["data"].decode())
        assert payload["commit_title"] == "fix(merge): correct stale-SHA check order"

    def test_merge_title_omitted_when_not_supplied(self):
        # Byte-identical to this parameter never existing: no commit_title
        # key at all when the caller does not pass one, so GitHub's own
        # default subject applies unchanged.
        captured = {}

        def opener(req, timeout=30):
            captured["data"] = req.data
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        payload = json.loads(captured["data"].decode())
        assert "commit_title" not in payload

    def test_default_merge_method_is_merge(self):
        captured = {}

        def opener(req, timeout=30):
            captured["data"] = req.data
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        payload = json.loads(captured["data"].decode())
        assert payload["merge_method"] == "merge"

    def test_explicit_merge_method_overrides_default(self):
        captured = {}

        def opener(req, timeout=30):
            captured["data"] = req.data
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", merge_method="squash", opener=opener)
        payload = json.loads(captured["data"].decode())
        assert payload["merge_method"] == "squash"

    def test_merge_uses_put_method(self):
        captured = {}

        def opener(req, timeout=30):
            captured["method"] = req.get_method()
            return _json_response(200, {"merged": True})

        github_backend.merge_pr(_OWNER, _REPO, 1, token="tok", opener=opener)
        assert captured["method"] == "PUT"


# ---------------------------------------------------------------------------
# Redirect hardening (same class of finding as review.github_backend,
# lr-412f). Every call _github_request makes carries a live GitHub bearer/
# App-installation token in its Authorization header.
# ---------------------------------------------------------------------------


def _redirect_http_error(url: str, code: int = 302) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url, code, "Found", {"Location": "http://attacker.example.net/collect"},
        io.BytesIO(b""),
    )


class TestRedirectHardeningDefaultOpener:
    """No `opener` injected -- proves _github_request itself builds a
    no-redirect opener rather than falling back to bare urlopen. Verified by
    monkeypatching redirect_guard.no_redirect_opener (the SAME hardened
    builder review.github_backend and transport.git_host_api already use) and
    asserting it was actually called, with zero real network I/O."""

    def test_github_request_default_opener_is_redirect_guarded(self, monkeypatch):
        captured = {}

        class _FakeOpenerDirector:
            def open(self, req, timeout=30):
                captured["req"] = req
                return _json_response(200, {"head": {"sha": "abc"}})

        def fake_no_redirect_opener():
            captured["called"] = True
            return _FakeOpenerDirector()

        monkeypatch.setattr(
            "clagentic_loadout.merge.github_backend.no_redirect_opener",
            fake_no_redirect_opener,
        )

        status, body = github_backend._github_request(
            "GET", f"{github_backend.GITHUB_API_BASE}/repos/{_OWNER}/{_REPO}/pulls/1", "tok"
        )
        assert status == 200
        assert captured.get("called") is True


class TestRedirectHardeningEachCallShape:
    """A 3xx surfaces as a failure -- never a false success -- for every
    call shape this backend makes (PR read, files, comments, merge). Only
    ONE request is ever observed; the attacker-controlled Location target is
    never contacted, so the live token is never replayed to it."""

    def test_get_pr_info_redirect_raises_gate_fact_unavailable(self):
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(GateFactUnavailableError):
            github_backend.get_pr_info(_OWNER, _REPO, 1, token="super-secret-app-token", opener=opener)
        assert len(request_log) == 1
        assert "attacker.example.net" not in request_log[0]

    def test_fetch_changed_files_redirect_raises_gate_fact_unavailable(self):
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise _redirect_http_error(req.full_url, 301)

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_changed_files(
                _OWNER, _REPO, 1, token="super-secret-app-token", opener=opener
            )
        assert len(request_log) == 1

    def test_fetch_comments_redirect_raises_gate_fact_unavailable(self):
        request_log = []

        def opener(req, timeout=30):
            request_log.append(req.full_url)
            raise _redirect_http_error(req.full_url, 307)

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_comments(
                _OWNER, _REPO, 1, token="super-secret-app-token", opener=opener
            )
        assert len(request_log) == 1

    def test_merge_pr_redirect_raises_merge_execution_error_not_merged(self):
        request_log = []

        def opener(req, timeout=30):
            request_log.append((req.get_method(), req.full_url))
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(MergeExecutionError):
            github_backend.merge_pr(_OWNER, _REPO, 1, token="super-secret-app-token", opener=opener)
        # Exactly one request was made, and only to the originally
        # configured host -- the Location target was never contacted, so
        # the token was never sent there either.
        assert len(request_log) == 1
        assert "attacker.example.net" not in request_log[0][1]

    def test_authorization_header_never_sent_to_redirect_target(self):
        seen_hosts = []

        def opener(req, timeout=30):
            import urllib.parse

            seen_hosts.append(urllib.parse.urlsplit(req.full_url).hostname)
            assert req.get_header("Authorization") == "token super-secret-app-token"
            raise _redirect_http_error(req.full_url, 302)

        with pytest.raises(GateFactUnavailableError):
            github_backend.get_pr_info(_OWNER, _REPO, 1, token="super-secret-app-token", opener=opener)

        assert seen_hosts == ["api.github.com"]


class TestFetchBranchCommitSubjects:
    """lr-835c57: the branch commit-subject gate's fetch side -- GET
    /repos/{owner}/{repo}/compare/{base}...{head}, the GitHub analogue of
    forgejo_backend.fetch_branch_commit_subjects (identical {"commits": [...]}
    response shape)."""

    def test_happy_path_extracts_sha_and_first_line_subject(self):
        opener = _opener_sequence(
            [
                _json_response(
                    200,
                    {
                        "commits": [
                            {"sha": "sha1", "commit": {"message": "feat: first\n\nbody detail"}},
                            {"sha": "sha2", "commit": {"message": "fix(lr-1): second"}},
                        ]
                    },
                )
            ]
        )
        result = github_backend.fetch_branch_commit_subjects(
            _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
        )
        assert result == [
            ("sha1", "feat: first"),
            ("sha2", "fix(lr-1): second"),
        ]

    def test_empty_commits_list(self):
        opener = _opener_sequence([_json_response(200, {"commits": []})])
        result = github_backend.fetch_branch_commit_subjects(
            _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
        )
        assert result == []

    def test_non_200_raises_gate_fact_unavailable(self):
        def opener(req, timeout=30):
            raise _http_error(req.full_url, 404, {"message": "Not Found"})

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_branch_commit_subjects(
                _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
            )

    def test_network_error_raises_gate_fact_unavailable(self):
        def opener(req, timeout=30):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_branch_commit_subjects(
                _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
            )

    def test_non_list_commits_field_raises(self):
        opener = _opener_sequence([_json_response(200, {"commits": "not-a-list"})])
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_branch_commit_subjects(
                _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
            )

    def test_missing_commits_field_raises(self):
        opener = _opener_sequence([_json_response(200, {"ahead_by": 2})])
        with pytest.raises(GateFactUnavailableError):
            github_backend.fetch_branch_commit_subjects(
                _OWNER, _REPO, "main", "headsha", token="tok", opener=opener
            )
