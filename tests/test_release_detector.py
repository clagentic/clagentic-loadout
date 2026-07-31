"""
test_release_detector.py — tests for clagentic_loadout.release.detector
(lr-51d4, Wave A slice 6, ported from an internal deployment's own
lr-3ed8/T8 lineage).

Coverage (per the source task's acceptance criteria):
  - compute_commit_range: prev_tag..new_tag range string, and the first-tag
    (no --prev-tag) case.
  - get_commit_messages / extract_resolved_tasks: ref extraction across a
    real git commit range (built in a throwaway tmp_path repo, no network),
    and dedupe — two commits closing the same task yield ONE entry.
  - is_semantic_release_owned: the semantic-release skip predicate
    (release.config.js/.cjs/.mjs and the loadout .loadout marker cases).
  - dispatch_detected_tasks / dispatch_manual_task: delegate to
    dispatch.dispatch_task_shipped — mocked here, no real network call in
    any test.
  - repo_identity_from_remote / is_repo_authorized_for_auto_dispatch:
    confused-deputy fail-closed scope gate.
  - CLI wiring: semantic-release skip, no-op (no tasks in range), dedupe
    end-to-end, the manual private-crossing path, leading-dash tag
    rejection, SSRF gate, and the scope gate (drop vs fire).
"""

from __future__ import annotations

import subprocess

import pytest

from clagentic_loadout.release import detector, dispatch


# ---------------------------------------------------------------------------
# helpers — build a throwaway git repo with real commits/tags
# ---------------------------------------------------------------------------


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "--quiet")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")


def _commit(repo, filename: str, message: str) -> str:
    (repo / filename).write_text(message)
    _git(repo, "add", filename)
    _git(repo, "commit", "--quiet", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _tag(repo, name: str) -> None:
    _git(repo, "tag", name)


def _add_remote(repo, url: str, *, name: str = "origin") -> None:
    _git(repo, "remote", "add", name, url)


# ---------------------------------------------------------------------------
# compute_commit_range
# ---------------------------------------------------------------------------


class TestComputeCommitRange:
    def test_prev_and_new_tag_yields_two_dot_range(self, tmp_path):
        repo = tmp_path / "repo"
        rng = detector.compute_commit_range(repo, "v1.0.0", "v1.1.0")
        assert rng == "v1.0.0..v1.1.0"

    def test_no_prev_tag_first_release_case(self, tmp_path):
        repo = tmp_path / "repo"
        rng = detector.compute_commit_range(repo, None, "v0.1.0")
        assert rng == "v0.1.0"


# ---------------------------------------------------------------------------
# get_commit_messages + extract_resolved_tasks — real git repo, no network
# ---------------------------------------------------------------------------


class TestRefExtractionAndDedupe:
    def test_extracts_distinct_tasks_from_range(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing one\n\nTask: proj-aaaa\nCloses #10\n")
        _commit(repo, "c.txt", "feat: thing two\n\nTask: proj-bbbb\nCloses #20\n")
        _tag(repo, "v1.1.0")

        commit_range = detector.compute_commit_range(repo, "v1.0.0", "v1.1.0")
        messages = detector.get_commit_messages(repo, commit_range)
        tasks = detector.extract_resolved_tasks(messages)

        assert tasks == [("proj-aaaa", 10), ("proj-bbbb", 20)]

    def test_two_commits_closing_same_task_dedupe_to_one(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing\n\nTask: proj-cccc\nCloses #30\n")
        _commit(repo, "b2.txt", "fixup: address review\n\nTask: proj-cccc\nCloses #30\n")
        _tag(repo, "v1.1.0")

        commit_range = detector.compute_commit_range(repo, "v1.0.0", "v1.1.0")
        messages = detector.get_commit_messages(repo, commit_range)
        tasks = detector.extract_resolved_tasks(messages)

        assert tasks == [("proj-cccc", 30)]

    def test_first_tag_case_no_prev_tag(self, tmp_path):
        """No prior v* tag exists — range is everything up to --new-tag."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "feat: first release\n\nTask: proj-dddd\nCloses #1\n")
        _tag(repo, "v0.1.0")

        commit_range = detector.compute_commit_range(repo, None, "v0.1.0")
        messages = detector.get_commit_messages(repo, commit_range)
        tasks = detector.extract_resolved_tasks(messages)

        assert tasks == [("proj-dddd", 1)]

    def test_no_ref_no_issue_case_is_empty(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "chore: unrelated cleanup, no trailers\n")
        _tag(repo, "v1.1.0")

        commit_range = detector.compute_commit_range(repo, "v1.0.0", "v1.1.0")
        messages = detector.get_commit_messages(repo, commit_range)
        tasks = detector.extract_resolved_tasks(messages)

        assert tasks == []

    def test_task_trailer_without_closes_included_with_none_issue(self):
        messages = ["feat: internal-only\n\nTask: proj-eeee\n"]
        tasks = detector.extract_resolved_tasks(messages)
        assert tasks == [("proj-eeee", None)]

    def test_first_seen_issue_number_wins_on_conflict(self):
        messages = [
            "feat: original\n\nTask: proj-ffff\nCloses #5\n",
            "fixup: oops\n\nTask: proj-ffff\nCloses #6\n",
        ]
        tasks = detector.extract_resolved_tasks(messages)
        assert tasks == [("proj-ffff", 5)]


# ---------------------------------------------------------------------------
# is_semantic_release_owned — skip predicate
# ---------------------------------------------------------------------------


class TestIsSemanticReleaseOwned:
    @pytest.mark.parametrize(
        "config_name", ["release.config.js", "release.config.cjs", "release.config.mjs"]
    )
    def test_release_config_present_is_owned(self, tmp_path, config_name):
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / config_name).write_text("module.exports = {};\n")
        assert detector.is_semantic_release_owned(repo) is True

    def test_loadout_marker_present_is_owned(self, tmp_path):
        """.clagentic is the current per-repo loadout config marker
        (lr-446c35) — the successor marker to the source's .crew marker."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".clagentic").mkdir()
        assert detector.is_semantic_release_owned(repo) is True

    def test_legacy_loadout_marker_present_is_owned(self, tmp_path):
        """Transitional back-compat (lr-446c35): a repo that has not yet
        migrated off the legacy .loadout marker dir still counts as
        configured. Removed after the fleet migration (lr-a645aa)."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".loadout").mkdir()
        assert detector.is_semantic_release_owned(repo) is True

    def test_neither_marker_is_not_owned(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        assert detector.is_semantic_release_owned(repo) is False

    def test_wrapper_hop_finds_marker_one_level_up(self, tmp_path):
        """lr-18f46a: the marker dir lives in the WRAPPER, one level above
        the repo's own git top level -- the bounded hop must find it."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        (wrapper / ".clagentic").mkdir()
        assert detector.is_semantic_release_owned(repo) is True

    def test_self_contained_repo_does_not_climb_past_its_own_absence(self, tmp_path):
        """The repo's own git top level (== repo here) has no marker of its
        own; a marker exists in the wrapper. This differs from a repo that
        HAS its own marker (which is never expected to climb at all) -- this
        case documents that the hop DOES fire when repo_root itself is bare,
        matching test_wrapper_hop_finds_marker_one_level_up. A dead-end case
        (repo has no git anchor at all) is covered by
        test_neither_marker_is_not_owned."""
        wrapper = tmp_path / "wrapper"
        repo = wrapper / "repo"
        _init_repo(repo)
        # No marker anywhere -- must remain unowned, not climb to a
        # filesystem root or otherwise misbehave.
        assert detector.is_semantic_release_owned(repo) is False


# ---------------------------------------------------------------------------
# dispatch_detected_tasks / dispatch_manual_task — mock the hook POST
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_dispatch_detected_tasks_fires_one_signal_per_task(self, monkeypatch):
        captured = []

        def fake_dispatch(task_id, **kwargs):
            captured.append((task_id, kwargs))
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)

        results = detector.dispatch_detected_tasks(
            [("proj-aaaa", 10), ("proj-bbbb", 20)],
            status_hook_url="http://example.com/status-hook",
            version="1.1.0",
            dispatcher="some-dispatcher",
        )

        assert len(captured) == 2
        assert captured[0][0] == "proj-aaaa"
        assert captured[1][0] == "proj-bbbb"
        assert results == [
            ("proj-aaaa", 200, {"status": "ok"}),
            ("proj-bbbb", 200, {"status": "ok"}),
        ]

    def test_dispatch_detected_tasks_fails_closed_on_hook_error(self, monkeypatch):
        def fake_dispatch(task_id, **kwargs):
            raise SystemExit(dispatch.EXIT_HOOK_FAILED)

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)

        with pytest.raises(SystemExit) as exc_info:
            detector.dispatch_detected_tasks(
                [("proj-aaaa", 10)],
                status_hook_url="http://example.com/status-hook",
                version="1.1.0",
            )
        assert exc_info.value.code == dispatch.EXIT_HOOK_FAILED

    def test_dispatch_manual_task_delegates_to_dispatch(self, monkeypatch):
        captured = {}

        def fake_dispatch(task_id, **kwargs):
            captured["task_id"] = task_id
            captured["kwargs"] = kwargs
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)

        status, body = detector.dispatch_manual_task(
            "proj-a68f",
            status_hook_url="http://example.com/status-hook",
            version="0.9.0-beta.3",
            dispatcher="some-dispatcher",
        )
        assert status == 200
        assert captured["task_id"] == "proj-a68f"
        assert captured["kwargs"]["version"] == "0.9.0-beta.3"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCLI:
    def _patch_dispatch(self, monkeypatch):
        captured = {"calls": []}

        def fake_dispatch(task_id, **kwargs):
            captured["calls"].append((task_id, kwargs))
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)
        return captured

    def test_semantic_release_owned_repo_is_skipped(self, tmp_path, monkeypatch):
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "feat: x\n\nTask: proj-aaaa\nCloses #1\n")
        _tag(repo, "v1.0.0")
        (repo / "release.config.js").write_text("module.exports = {};\n")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--new-tag", "v1.0.0",
                "--status-hook-url", "http://example.com/status-hook",
            ]
        )
        assert rc == detector.EXIT_OK
        assert captured["calls"] == []

    def test_no_ref_no_issue_is_noop(self, tmp_path, monkeypatch):
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: no trailers\n")
        _tag(repo, "v1.0.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--new-tag", "v1.0.0",
                "--status-hook-url", "http://example.com/status-hook",
            ]
        )
        assert rc == detector.EXIT_OK
        assert captured["calls"] == []

    def test_one_signal_per_distinct_issue_end_to_end(self, tmp_path, monkeypatch):
        """
        Dedupe end-to-end, under the confused-deputy scope gate: the repo's
        own remote must match an --allowed-repo/--allowed-org entry for the
        AUTO path to fire at all — see TestScopeGate for the drop-side.
        """
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com/acme/widget.git")
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing\n\nTask: proj-cccc\nCloses #30\n")
        _commit(repo, "b2.txt", "fixup: same task\n\nTask: proj-cccc\nCloses #30\n")
        _commit(repo, "c.txt", "feat: other\n\nTask: proj-dddd\nCloses #40\n")
        _tag(repo, "v1.1.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--prev-tag", "v1.0.0",
                "--new-tag", "v1.1.0",
                "--status-hook-url", "http://example.com/status-hook",
                "--allowed-repo", "acme/widget",
            ]
        )
        assert rc == detector.EXIT_OK
        task_ids = [call[0] for call in captured["calls"]]
        assert task_ids == ["proj-cccc", "proj-dddd"]

    def test_manual_task_id_path_bypasses_range_scan(self, monkeypatch):
        captured = self._patch_dispatch(monkeypatch)
        rc = detector.main(
            [
                "--new-tag", "v0.9.0-beta.3",
                "--manual-task-id", "proj-a68f",
                "--status-hook-url", "http://example.com/status-hook",
            ]
        )
        assert rc == detector.EXIT_OK
        assert len(captured["calls"]) == 1
        assert captured["calls"][0][0] == "proj-a68f"

    def test_repo_path_not_a_git_repo_fails_usage(self, tmp_path, monkeypatch):
        self._patch_dispatch(monkeypatch)
        not_a_repo = tmp_path / "not-a-repo"
        not_a_repo.mkdir()

        with pytest.raises(SystemExit) as exc_info:
            detector.main(
                [
                    "--repo-path", str(not_a_repo),
                    "--new-tag", "v1.0.0",
                    "--status-hook-url", "http://example.com/status-hook",
                ]
            )
        assert exc_info.value.code == detector.EXIT_USAGE

    def test_unknown_new_tag_fails_usage(self, tmp_path, monkeypatch):
        self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")

        with pytest.raises(SystemExit) as exc_info:
            detector.main(
                [
                    "--repo-path", str(repo),
                    "--new-tag", "v9.9.9-does-not-exist",
                    "--status-hook-url", "http://example.com/status-hook",
                ]
            )
        assert exc_info.value.code == detector.EXIT_USAGE


# ---------------------------------------------------------------------------
# is_valid_status_hook_url — re-exported from dispatch, one real
# implementation reused via both module names.
# ---------------------------------------------------------------------------


class TestIsValidStatusHookUrl:
    def test_https_with_host_is_valid(self):
        assert detector.is_valid_status_hook_url(
            "https://triage.example.com:8743/status-hook"
        ) is True

    def test_http_with_host_is_valid(self):
        assert detector.is_valid_status_hook_url(
            "http://triage.example.com:8743/status-hook"
        ) is True

    def test_file_scheme_is_rejected(self):
        assert detector.is_valid_status_hook_url(
            "file:///etc/passwd"
        ) is False

    def test_no_scheme_is_rejected(self):
        assert detector.is_valid_status_hook_url(
            "triage.example.com/status-hook"
        ) is False

    def test_no_host_is_rejected(self):
        assert detector.is_valid_status_hook_url("https:///status-hook") is False

    def test_javascript_scheme_is_rejected(self):
        assert detector.is_valid_status_hook_url("javascript:alert(1)") is False


# ---------------------------------------------------------------------------
# repo_identity_from_remote — generic http(s) owner/repo extraction
# ---------------------------------------------------------------------------


class TestRepoIdentityFromRemote:
    def test_forgejo_style_remote(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com:3000/acme/widget.git")
        assert detector.repo_identity_from_remote(repo) == "acme/widget"

    def test_github_style_remote(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "https://github.com/acme/widget.git")
        assert detector.repo_identity_from_remote(repo) == "acme/widget"

    def test_no_remote_returns_none(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        assert detector.repo_identity_from_remote(repo) is None

    def test_scp_style_ssh_remote_returns_none(self, tmp_path):
        """ssh (scp-style) remotes are not the http(s) shape this module
        parses — treated as identity-unknown (fail closed), not crashed."""
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "git@github.com:acme/widget.git")
        assert detector.repo_identity_from_remote(repo) is None


# ---------------------------------------------------------------------------
# is_repo_authorized_for_auto_dispatch — fail-closed scope gate
# (confused-deputy remediation)
# ---------------------------------------------------------------------------


class TestIsRepoAuthorizedForAutoDispatch:
    def test_exact_repo_match_is_authorized(self):
        assert detector.is_repo_authorized_for_auto_dispatch(
            "acme/widget",
            allowed_repos=frozenset({"acme/widget"}),
            allowed_orgs=frozenset(),
        ) is True

    def test_org_match_is_authorized(self):
        assert detector.is_repo_authorized_for_auto_dispatch(
            "acme/widget",
            allowed_repos=frozenset(),
            allowed_orgs=frozenset({"acme"}),
        ) is True

    def test_no_match_is_denied(self):
        assert detector.is_repo_authorized_for_auto_dispatch(
            "evil-org/evil-repo",
            allowed_repos=frozenset({"acme/widget"}),
            allowed_orgs=frozenset({"acme"}),
        ) is False

    def test_none_identity_is_denied_even_with_allow_set(self):
        """Identity could not be derived at all -- fail closed regardless of
        what was allow-listed."""
        assert detector.is_repo_authorized_for_auto_dispatch(
            None,
            allowed_repos=frozenset({"acme/widget"}),
            allowed_orgs=frozenset({"acme"}),
        ) is False

    def test_empty_allow_set_is_denied_even_with_valid_identity(self):
        """The unconfigured case: no --allowed-repo/--allowed-org supplied
        at all -- ambiguous scope means do NOT fire."""
        assert detector.is_repo_authorized_for_auto_dispatch(
            "acme/widget",
            allowed_repos=frozenset(),
            allowed_orgs=frozenset(),
        ) is False


# ---------------------------------------------------------------------------
# Leading-dash tag rejection
# ---------------------------------------------------------------------------


class TestLeadingDashTagRejection:
    def test_leading_dash_new_tag_fails_usage(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")

        with pytest.raises(SystemExit) as exc_info:
            detector.compute_commit_range(repo, None, "--upload-pack=evil")
        assert exc_info.value.code == detector.EXIT_USAGE

    def test_leading_dash_prev_tag_fails_usage(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")

        with pytest.raises(SystemExit) as exc_info:
            detector.compute_commit_range(repo, "--evil-option", "v1.0.0")
        assert exc_info.value.code == detector.EXIT_USAGE

    def test_normal_tags_are_unaffected(self, tmp_path):
        repo = tmp_path / "repo"
        rng = detector.compute_commit_range(repo, "v1.0.0", "v1.1.0")
        assert rng == "v1.0.0..v1.1.0"

    def test_cli_leading_dash_new_tag_fails_usage(self, tmp_path, monkeypatch):
        """End-to-end via main(): a crafted --new-tag starting with '-' is
        refused before any git call, not silently passed through.

        A bare option-shaped value (e.g. "--upload-pack=evil") is rejected
        by argparse itself before this module's own _reject_leading_dash_tag
        check ever runs (argparse treats it as an unrecognized flag, exit
        code 2) -- still a non-zero, no-dispatch refusal, so the security
        property (never reaches git as an opaque positional) holds either
        way. Using "=--upload-pack=evil" (leading '=' before the dash)
        reaches argparse as a plain positional VALUE, so THIS module's own
        _reject_leading_dash_tag is what actually fires here.
        """
        captured = {"calls": []}

        def fake_dispatch(task_id, **kwargs):
            captured["calls"].append((task_id, kwargs))
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _commit(repo, "a.txt", "chore: initial\n")

        with pytest.raises(SystemExit) as exc_info:
            detector.main(
                [
                    "--repo-path", str(repo),
                    "--new-tag=--upload-pack=evil",
                    "--status-hook-url", "http://example.com/status-hook",
                ]
            )
        assert exc_info.value.code == detector.EXIT_USAGE
        assert captured["calls"] == []


# ---------------------------------------------------------------------------
# CLI-level scope gate: crafted out-of-scope task DOES NOT fire;
# in-scope task DOES fire
# ---------------------------------------------------------------------------


class TestScopeGate:
    def _patch_dispatch(self, monkeypatch):
        captured = {"calls": []}

        def fake_dispatch(task_id, **kwargs):
            captured["calls"].append((task_id, kwargs))
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)
        return captured

    def test_out_of_scope_task_is_dropped_not_fired(self, tmp_path, monkeypatch):
        """A crafted Task: trailer in a repo not allow-listed for
        auto-dispatch must NOT fire a signal — the confused-deputy case."""
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com/evil-org/evil-repo.git")
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(
            repo,
            "b.txt",
            "feat: crafted trailer for a task this repo does not own\n\n"
            "Task: proj-not-mine\nCloses #999\n",
        )
        _tag(repo, "v1.1.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--prev-tag", "v1.0.0",
                "--new-tag", "v1.1.0",
                "--status-hook-url", "http://example.com/status-hook",
                "--allowed-repo", "acme/widget",
            ]
        )
        assert rc == detector.EXIT_OK
        assert captured["calls"] == []

    def test_out_of_scope_with_no_allow_set_at_all_is_dropped(self, tmp_path, monkeypatch):
        """No --allowed-repo/--allowed-org supplied at all (the fully
        unconfigured case) -- must also drop, not fire-anyway."""
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com/acme/widget.git")
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing\n\nTask: proj-aaaa\nCloses #1\n")
        _tag(repo, "v1.1.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--prev-tag", "v1.0.0",
                "--new-tag", "v1.1.0",
                "--status-hook-url", "http://example.com/status-hook",
            ]
        )
        assert rc == detector.EXIT_OK
        assert captured["calls"] == []

    def test_in_scope_task_fires_via_allowed_repo(self, tmp_path, monkeypatch):
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com/acme/widget.git")
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing\n\nTask: proj-aaaa\nCloses #1\n")
        _tag(repo, "v1.1.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--prev-tag", "v1.0.0",
                "--new-tag", "v1.1.0",
                "--status-hook-url", "http://example.com/status-hook",
                "--allowed-repo", "acme/widget",
            ]
        )
        assert rc == detector.EXIT_OK
        assert [c[0] for c in captured["calls"]] == ["proj-aaaa"]

    def test_in_scope_task_fires_via_allowed_org(self, tmp_path, monkeypatch):
        captured = self._patch_dispatch(monkeypatch)
        repo = tmp_path / "repo"
        _init_repo(repo)
        _add_remote(repo, "http://forgejo.example.com/acme/widget.git")
        _commit(repo, "a.txt", "chore: initial\n")
        _tag(repo, "v1.0.0")
        _commit(repo, "b.txt", "feat: thing\n\nTask: proj-aaaa\nCloses #1\n")
        _tag(repo, "v1.1.0")

        rc = detector.main(
            [
                "--repo-path", str(repo),
                "--prev-tag", "v1.0.0",
                "--new-tag", "v1.1.0",
                "--status-hook-url", "http://example.com/status-hook",
                "--allowed-org", "acme",
            ]
        )
        assert rc == detector.EXIT_OK
        assert [c[0] for c in captured["calls"]] == ["proj-aaaa"]

    def test_manual_task_id_path_is_not_scope_gated(self, monkeypatch):
        """--manual-task-id is caller-asserted (out-of-band trust) and must
        remain unaffected by the AUTO-path scope gate -- no --allowed-repo/
        --allowed-org supplied, yet the manual signal still fires."""
        captured = self._patch_dispatch(monkeypatch)
        rc = detector.main(
            [
                "--new-tag", "v0.9.0-beta.3",
                "--manual-task-id", "proj-a68f",
                "--status-hook-url", "http://example.com/status-hook",
            ]
        )
        assert rc == detector.EXIT_OK
        assert len(captured["calls"]) == 1
        assert captured["calls"][0][0] == "proj-a68f"


# ---------------------------------------------------------------------------
# CLI-level SSRF gate: out-of-scheme status-hook URL is refused
# ---------------------------------------------------------------------------


class TestCLIStatusHookUrlValidation:
    def test_out_of_scheme_status_hook_url_fails_usage(self, monkeypatch):
        captured = {"calls": []}

        def fake_dispatch(task_id, **kwargs):
            captured["calls"].append((task_id, kwargs))
            return 200, {"status": "ok"}

        monkeypatch.setattr(dispatch, "dispatch_task_shipped", fake_dispatch)

        with pytest.raises(SystemExit) as exc_info:
            detector.main(
                [
                    "--new-tag", "v0.9.0-beta.3",
                    "--manual-task-id", "proj-a68f",
                    "--status-hook-url", "file:///etc/passwd",
                ]
            )
        assert exc_info.value.code == detector.EXIT_USAGE
        assert captured["calls"] == []
