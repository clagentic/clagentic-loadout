"""test_merge_forgejo_backend.py — tests for
clagentic_loadout.merge.forgejo_backend (lr-885f, Wave B slice 4).

Coverage:
  - get_pr_info / fetch_changed_files / fetch_comments: happy path via an
    injected opener (NO real network call anywhere in this file); non-200 /
    non-list / network-error all raise GateFactUnavailableError (fail-closed
    -- an unreadable gate fact is never treated as a passing gate).
  - merge_pr: 200/204 success; 405 three-case disambiguation (empty-diff
    PR refused, already-merged-ancestor treated as idempotent success,
    genuine refusal raises); any other non-2xx raises MergeExecutionError.
  - Every call routes through transport.git_host_api.request (proven by the
    fact that a fake opener with NO real network access satisfies every
    test -- if this module rolled its own urllib call it would need a
    second, differently-shaped fake).
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from clagentic_loadout.merge import forgejo_backend
from clagentic_loadout.merge.errors import GateFactUnavailableError, MergeExecutionError

_API_BASE = "http://git-host.example.com"


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


def _opener_sequence(responses):
    """Return an opener callable that yields each entry in *responses* in
    order (one call per invocation), raising urllib.error.HTTPError for any
    entry that is itself an HTTPError instance."""
    it = iter(responses)

    def opener(req, timeout=15):
        item = next(it)
        if isinstance(item, Exception):
            raise item
        status, body = item
        return _FakeResponse(status, body)

    return opener


class TestGetPrInfo:
    def test_happy_path(self):
        opener = _opener_sequence([(200, b'{"head": {"sha": "abc"}, "title": "feat: x"}')])
        info = forgejo_backend.get_pr_info(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert info["head"]["sha"] == "abc"

    def test_non_200_raises_gate_fact_unavailable(self):
        opener = _opener_sequence([(404, b"{}")])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.get_pr_info(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)

    def test_network_error_raises_gate_fact_unavailable(self):
        def opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.get_pr_info(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)


class TestGetPrHeadShaAndTitle:
    def test_extracts_head_sha(self):
        assert forgejo_backend.get_pr_head_sha({"head": {"sha": "abc123"}}) == "abc123"

    def test_missing_head_sha_is_empty(self):
        assert forgejo_backend.get_pr_head_sha({}) == ""

    def test_extracts_title(self):
        assert forgejo_backend.get_pr_title({"title": "feat: x"}) == "feat: x"

    def test_missing_title_is_empty(self):
        assert forgejo_backend.get_pr_title({}) == ""


class TestFetchChangedFiles:
    def test_happy_path(self):
        body = json.dumps([{"filename": "a.py"}, {"filename": "b.py"}]).encode()
        opener = _opener_sequence([(200, body)])
        files = forgejo_backend.fetch_changed_files(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert files == ["a.py", "b.py"]

    def test_non_200_raises(self):
        opener = _opener_sequence([(500, b"{}")])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_changed_files(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)

    def test_non_list_body_raises(self):
        opener = _opener_sequence([(200, b'{"not": "a list"}')])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_changed_files(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)


class TestFetchComments:
    def test_happy_path(self):
        body = json.dumps([{"id": 1, "user": {"login": "x"}, "body": "hi"}]).encode()
        opener = _opener_sequence([(200, body)])
        comments = forgejo_backend.fetch_comments(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert comments[0]["id"] == 1

    def test_non_200_raises(self):
        opener = _opener_sequence([(403, b"{}")])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_comments(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)

    def test_non_list_body_raises(self):
        opener = _opener_sequence([(200, b'{"not": "a list"}')])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_comments(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)

    def test_empty_body_is_empty_list(self):
        opener = _opener_sequence([(200, b"")])
        assert forgejo_backend.fetch_comments(_API_BASE, "owner", "repo", 1, token="tok", opener=opener) == []


def _http_error(status: int, body: bytes) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("http://git-host.example.com/x", status, "err", {}, io.BytesIO(body))


class TestMergePr:
    def test_200_is_success(self):
        opener = _opener_sequence([(200, b"{}")])
        forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)  # no raise

    def test_204_is_success(self):
        opener = _opener_sequence([(204, b"")])
        forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)  # no raise

    def test_200_success_returns_none_sha(self):
        # lr-7c5540: Forgejo's merge endpoint has NO documented response
        # field carrying the merged commit SHA (unlike GitHub's) -- merge_pr
        # always returns None on success here. The caller
        # (merge.tree_sync.advance_repo_to_merged_sha) falls back to
        # resolving the merged tip via a base-branch fetch instead.
        opener = _opener_sequence([(200, b"{}")])
        result = forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert result is None

    def test_405_empty_diff_pr_refused(self):
        # POST raises 405; the PR-info re-read shows head SHA == base SHA
        # (empty-diff PR) -- must refuse, never treated as success.
        def opener(req, timeout=15):
            if req.get_method() == "POST":
                raise _http_error(405, b'{"message": "not mergeable"}')
            # GET pulls/{n} re-read.
            return _FakeResponse(
                200,
                json.dumps(
                    {"head": {"sha": "same-sha"}, "base": {"ref": "main", "sha": "same-sha"}}
                ).encode(),
            )

        with pytest.raises(MergeExecutionError) as exc_info:
            forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert "empty-diff" in str(exc_info.value)

    def test_405_already_merged_ancestor_is_idempotent_success(self):
        calls = {"n": 0}

        def opener(req, timeout=15):
            calls["n"] += 1
            if req.get_method() == "POST":
                raise _http_error(405, b'{"message": "not mergeable"}')
            if "/pulls/1" in req.full_url and "compare" not in req.full_url:
                return _FakeResponse(
                    200,
                    json.dumps(
                        {"head": {"sha": "head-sha"}, "base": {"ref": "main", "sha": "base-sha"}}
                    ).encode(),
                )
            # compare API: ahead_by == 0 means head is already merged into base.
            return _FakeResponse(200, json.dumps({"ahead_by": 0}).encode())

        forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)  # no raise
        assert calls["n"] == 3  # POST, GET pulls, GET compare

    def test_405_genuine_refusal_raises(self):
        def opener(req, timeout=15):
            if req.get_method() == "POST":
                raise _http_error(405, b'{"message": "not mergeable"}')
            if "/pulls/1" in req.full_url and "compare" not in req.full_url:
                return _FakeResponse(
                    200,
                    json.dumps(
                        {"head": {"sha": "head-sha"}, "base": {"ref": "main", "sha": "base-sha"}}
                    ).encode(),
                )
            # compare API: still ahead -- not an ancestor, genuine conflict.
            return _FakeResponse(200, json.dumps({"ahead_by": 3}).encode())

        with pytest.raises(MergeExecutionError) as exc_info:
            forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert "405" in str(exc_info.value)

    def test_other_non_2xx_raises(self):
        def opener(req, timeout=15):
            raise _http_error(500, b'{"message": "server error"}')

        with pytest.raises(MergeExecutionError) as exc_info:
            forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        assert "500" in str(exc_info.value)

    def test_network_error_raises(self):
        def opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(MergeExecutionError):
            forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)

    def test_merge_message_included_when_supplied(self):
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok", merge_message="custom note", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["merge_message_field"] == "custom note"

    def test_merge_title_included_when_supplied(self):
        # lr-1953a8: MergeTitleField composes the merge commit SUBJECT from
        # the caller-supplied title (merge.verb passes the PR's own title)
        # rather than Forgejo's own branch-ref-bearing default.
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok",
            merge_title="fix(merge): correct stale-SHA check order", opener=opener,
        )
        payload = json.loads(captured["data"].decode())
        assert payload["MergeTitleField"] == "fix(merge): correct stale-SHA check order"

    def test_merge_title_omitted_when_not_supplied(self):
        # Byte-identical to this parameter never existing: no MergeTitleField
        # key at all when the caller does not pass one, so Forgejo's own
        # default subject applies unchanged.
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        payload = json.loads(captured["data"].decode())
        assert "MergeTitleField" not in payload

    def test_default_merge_method_is_merge(self):
        # lr-14f704: the `Do` field default -- unchanged behavior for every
        # caller that never passes merge_method explicitly.
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(_API_BASE, "owner", "repo", 1, token="tok", opener=opener)
        payload = json.loads(captured["data"].decode())
        assert payload["Do"] == "merge"

    def test_explicit_merge_method_overrides_default(self):
        # lr-14f704 THE REGRESSION LOCK: before this fix, `Do` was hardcoded
        # to "merge" regardless of what merge_method was passed -- a caller
        # requesting squash silently got a real merge commit anyway.
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok", merge_method="squash", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["Do"] == "squash"

    def test_rebase_merge_method_reaches_do_field(self):
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok", merge_method="rebase", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["Do"] == "rebase"

    def test_rebase_merge_variant_reaches_do_field(self):
        # Forgejo-only shape with no GitHub equivalent -- proves this
        # backend's vocabulary is not artificially narrowed to the three
        # cross-platform tokens.
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok", merge_method="rebase-merge", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["Do"] == "rebase-merge"

    def test_manually_merged_reaches_do_field(self):
        captured = {}

        def opener(req, timeout=15):
            captured["data"] = req.data
            return _FakeResponse(200, b"{}")

        forgejo_backend.merge_pr(
            _API_BASE, "owner", "repo", 1, token="tok", merge_method="manually-merged", opener=opener
        )
        payload = json.loads(captured["data"].decode())
        assert payload["Do"] == "manually-merged"

    def test_unrecognized_merge_method_raises_before_any_http_call(self):
        def _unreachable_opener(req, timeout=15):
            raise AssertionError("no HTTP call should be made for an invalid merge_method")

        with pytest.raises(MergeExecutionError, match="not a recognized Forgejo"):
            forgejo_backend.merge_pr(
                _API_BASE, "owner", "repo", 1, token="tok",
                merge_method="not-a-real-do-value", opener=_unreachable_opener,
            )


class TestFetchBranchCommitSubjects:
    """lr-835c57: the branch commit-subject gate's fetch side -- GET
    .../compare/{base}...{head}, reusing the SAME endpoint
    _is_head_ancestor_of_base already calls on the 405-disambiguation path
    (no second implementation, see _fetch_compare)."""

    def test_happy_path_extracts_sha_and_first_line_subject(self):
        body = json.dumps(
            {
                "commits": [
                    {"sha": "sha1", "commit": {"message": "feat: first\n\nbody detail"}},
                    {"sha": "sha2", "commit": {"message": "fix(lr-1): second"}},
                ]
            }
        ).encode()
        opener = _opener_sequence([(200, body)])
        result = forgejo_backend.fetch_branch_commit_subjects(
            _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
        )
        assert result == [
            ("sha1", "feat: first"),
            ("sha2", "fix(lr-1): second"),
        ]

    def test_empty_commits_list(self):
        opener = _opener_sequence([(200, b'{"commits": []}')])
        result = forgejo_backend.fetch_branch_commit_subjects(
            _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
        )
        assert result == []

    def test_non_200_raises_gate_fact_unavailable(self):
        opener = _opener_sequence([(500, b"{}")])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_branch_commit_subjects(
                _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
            )

    def test_network_error_raises_gate_fact_unavailable(self):
        def opener(req, timeout=15):
            raise urllib.error.URLError("connection refused")

        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_branch_commit_subjects(
                _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
            )

    def test_non_list_commits_field_raises(self):
        opener = _opener_sequence([(200, b'{"commits": "not-a-list"}')])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_branch_commit_subjects(
                _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
            )

    def test_missing_commits_field_raises(self):
        opener = _opener_sequence([(200, b'{"ahead_by": 2}')])
        with pytest.raises(GateFactUnavailableError):
            forgejo_backend.fetch_branch_commit_subjects(
                _API_BASE, "owner", "repo", "main", "headsha", token="tok", opener=opener
            )
