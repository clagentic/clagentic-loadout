"""test_acquire_scratch.py — tests for clagentic_loadout.acquire.scratch
(lr-c17040 comment #1, "SECOND FACET").

Coverage:
  - resolve_scratch_dir is namespaced by owner/repo/pr_number under TMPDIR
    (never a repo-local or hardcoded path) -- different PRs never collide.
  - write_scratch_content writes the whole-PR diff text and each changed
    file's non-empty content under files_dir, mirrored at its own
    repo-relative path; a file with no content (deleted, binary, or
    fetched without include_file_contents) is never written.
  - Path-traversal-shaped or absolute changed-file names are refused
    (ScratchWriteError) rather than silently escaping the scratch root.
  - An unwritable scratch root surfaces as ScratchWriteError, never a bare
    OSError leaking out of this module.
  - Nothing is ever written outside TMPDIR (repo CLAUDE.md rule 7) -- every
    test here uses an injected `env={"TMPDIR": str(tmp_path)}` and asserts
    all output paths are rooted under tmp_path.
"""

from __future__ import annotations

import pytest

from clagentic_loadout.acquire.contract import AcquiredPr, ChangedFile
from clagentic_loadout.acquire.errors import ScratchWriteError
from clagentic_loadout.acquire.scratch import resolve_scratch_dir, write_scratch_content


class TestResolveScratchDir:
    def test_namespaced_by_owner_repo_pr(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        a = resolve_scratch_dir(owner="o1", repo="r1", pr_number=1, env=env)
        b = resolve_scratch_dir(owner="o1", repo="r1", pr_number=2, env=env)
        c = resolve_scratch_dir(owner="o2", repo="r1", pr_number=1, env=env)
        assert a != b != c
        assert str(tmp_path) in str(a)

    def test_rooted_under_tmpdir(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        scratch_dir = resolve_scratch_dir(owner="o", repo="r", pr_number=1, env=env)
        assert tmp_path in scratch_dir.parents


class TestWriteScratchContent:
    def test_writes_diff_text(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a" * 40, head_sha="b" * 40,
            diff_text="diff --git a/x b/x\n+hi\n",
        )
        staged = write_scratch_content(acquired, env=env)
        assert staged.diff_path is not None
        assert staged.diff_path.read_text() == "diff --git a/x b/x\n+hi\n"
        assert tmp_path in staged.diff_path.parents

    def test_no_diff_path_when_diff_text_empty(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b")
        staged = write_scratch_content(acquired, env=env)
        assert staged.diff_path is None

    def test_writes_changed_file_content(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b",
            changed_files=(
                ChangedFile(filename="pkg/mod.py", status="modified", content="print(1)\n"),
            ),
        )
        staged = write_scratch_content(acquired, env=env)
        assert staged.written_files == ("pkg/mod.py",)
        written_path = staged.files_dir / "pkg" / "mod.py"
        assert written_path.read_text() == "print(1)\n"
        assert tmp_path in written_path.parents

    def test_skips_files_with_no_content(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b",
            changed_files=(
                ChangedFile(filename="deleted.py", status="deleted", content=""),
                ChangedFile(filename="kept.py", status="modified", content="x = 1\n"),
            ),
        )
        staged = write_scratch_content(acquired, env=env)
        assert staged.written_files == ("kept.py",)
        assert not (staged.files_dir / "deleted.py").exists()

    def test_refuses_path_traversal_filename(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b",
            changed_files=(
                ChangedFile(filename="../../etc/passwd", status="modified", content="evil"),
            ),
        )
        with pytest.raises(ScratchWriteError):
            write_scratch_content(acquired, env=env)

    def test_refuses_absolute_filename(self, tmp_path):
        env = {"TMPDIR": str(tmp_path)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b",
            changed_files=(
                ChangedFile(filename="/etc/passwd", status="modified", content="evil"),
            ),
        )
        with pytest.raises(ScratchWriteError):
            write_scratch_content(acquired, env=env)

    def test_unwritable_root_raises_scratch_write_error(self, tmp_path):
        # Point TMPDIR at a path that is itself a FILE, not a directory --
        # mkdir(parents=True) underneath it must fail with OSError, which
        # this module translates to ScratchWriteError rather than leaking.
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x")
        env = {"TMPDIR": str(blocker)}
        acquired = AcquiredPr(
            owner="o", repo="r", pr_number=1, base_sha="a", head_sha="b",
            diff_text="diff\n",
        )
        with pytest.raises(ScratchWriteError):
            write_scratch_content(acquired, env=env)
