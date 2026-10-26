"""No loadout-authored message may interpolate a remote value, in any form.

A branch's tracking remote (`branch.<name>.remote`) is whatever git config holds:
a URL carrying a credential in its userinfo, query or path, or a remote NAME that
was itself written as `<secret>@host/path`. Printing it is the defect, so the
messages name no remote at all and these cases assert the secret is absent from
the raised message, stdout and stderr of every site that used to print one.

Each remote value points at a closed local port, so every git call fails offline."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.merge.commit_files import CommitFileReadError, ensure_commit_present
from clagentic_loadout.merge.tree_sync import (
    TreeSyncError,
    advance_repo_to_merged_sha,
    fetch_merged_sha_object,
)
from clagentic_loadout.push import git_coords
from clagentic_loadout.push.git_hermeticity import check_repo_local_config_hazards
from clagentic_loadout.push.branch_commit_check import (
    CommitCheckUnavailableError,
    check_branch_for_stray_merge_commits,
    fetch_branch_commit_subjects,
)
from clagentic_loadout.push.errors import GitPushError, RemoteResolutionError
from clagentic_loadout.push.git_push import (
    GitFetchError,
    RepoLocalConfigHazardError,
    git_fetch_with_token,
    git_ls_remote_with_token,
    git_push_with_token,
)
from clagentic_loadout.push.remote_readback import RemoteReadbackError, read_remote_head
from clagentic_loadout.push.verb import _run_branch_commit_check
from tests._gate_repo import git, seed_base_commit

SECRET = "FAKETOKEN"
TOKEN = f"{SECRET}0123456789"
ALIAS_NAME = f"{TOKEN}@example.invalid/o/r"
UNREACHABLE = "http://127.0.0.1:1/o/r.git"
REMOTE_VALUES = {
    "userinfo": f"http://user:{TOKEN}@127.0.0.1:1/o/r.git",
    "query": f"{UNREACHABLE}?token={TOKEN}",
    "path": f"http://127.0.0.1:1/{TOKEN}/o/r.git",
    "alias-name": ALIAS_NAME,
}
MISSING_SHA = "f" * 40


@pytest.fixture(params=sorted(REMOTE_VALUES))
def tree_and_remote(request, tmp_path) -> tuple[Path, str]:
    """A repo on `main` whose tracking remote is one of the credential-bearing
    values. The alias form registers a real remote with that name."""
    path = tmp_path / "tree"
    seed_base_commit(path)
    value = REMOTE_VALUES[request.param]
    if request.param == "alias-name":
        git(path, "remote", "add", value, UNREACHABLE)
    git(path, "config", "branch.main.remote", value)
    return path, value


@pytest.fixture
def tree(tree_and_remote) -> Path:
    return tree_and_remote[0]


def _assert_clean(excinfo, capsys):
    out, err = capsys.readouterr()
    for text in (str(excinfo.value), repr(excinfo.value), out, err):
        assert SECRET.lower() not in text.lower()


def test_git_refuses_a_colon_in_a_remote_name(tmp_path):
    """Recorded answer: a remote NAME cannot carry the `user:secret@` userinfo
    form (git rejects the colon), so a colon-bearing value reaching
    `branch.<name>.remote` is a URL, covered by the URL-form cases above."""
    path = tmp_path / "tree"
    seed_base_commit(path)
    result = subprocess.run(
        ["git", "remote", "add", f"user:{TOKEN}@example.invalid/o/r", UNREACHABLE],
        cwd=str(path), capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "is not a valid remote name" in result.stderr


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_advance_repo_to_merged_sha_names_no_remote(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        advance_repo_to_merged_sha(tree, base_branch="main", known_merged_sha=known_sha)

    assert "exit" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


@pytest.mark.parametrize("known_sha", [None, MISSING_SHA])
def test_fetch_merged_sha_object_names_no_remote(tree, capsys, known_sha):
    with pytest.raises(TreeSyncError) as excinfo:
        fetch_merged_sha_object(tree, base_branch="main", known_merged_sha=known_sha)

    assert "exit" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_ensure_commit_present_names_no_remote(tree, capsys):
    with pytest.raises(CommitFileReadError) as excinfo:
        ensure_commit_present(tree, MISSING_SHA, base_branch="main")

    assert MISSING_SHA in str(excinfo.value) and "exit" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_remote_url_failure_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote
    try:
        git_coords.remote_url(git_coords.tracking_remote("main", tree), tree)
    except RemoteResolutionError as exc:
        assert SECRET not in str(exc)
    capsys.readouterr()


def test_git_fetch_with_token_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote

    with pytest.raises(GitFetchError) as excinfo:
        git_fetch_with_token(remote, "main", "tok", tree)

    assert "exited" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_git_ls_remote_readback_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote

    for token in ("tok", None):
        with pytest.raises(RemoteReadbackError) as excinfo:
            read_remote_head(remote, "main", tree, token=token)

        assert "exited" in str(excinfo.value)
        _assert_clean(excinfo, capsys)


def test_git_ls_remote_with_token_result_is_not_a_message(tree_and_remote):
    """The completed process is returned unexamined; it is the caller that
    builds a message from it, so the failing exit is all this asserts."""
    tree, remote = tree_and_remote

    assert git_ls_remote_with_token(remote, "main", "tok", tree).returncode != 0


def test_git_push_failure_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote

    with pytest.raises(GitPushError) as excinfo:
        git_push_with_token(remote, "main", "tok", tree)

    assert "exit=" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_git_push_platform_mismatch_hint_names_no_remote(tree_and_remote, tmp_path, monkeypatch, capsys):
    tree, remote = tree_and_remote
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    real_git = shutil.which("git")
    assert real_git, "git is required on PATH"
    (fake_bin / "git").write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "push" ]; then\n'
        '    echo "remote: Invalid username or token." 1>&2\n'
        "    exit 1\n"
        "  fi\n"
        "done\n"
        f'exec "{real_git}" "$@"\n'
    )
    (fake_bin / "git").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_bin}:{os.environ.get('PATH', '')}")

    with pytest.raises(GitPushError) as excinfo:
        git_push_with_token(
            remote, "main", "tok", tree, platform="github", other_platform_label="forgejo",
        )

    assert "PLATFORM MISMATCH" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_hermeticity_refusal_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote
    git(tree, "config", "credential.helper", "none")

    calls = {
        "fetch": lambda: git_fetch_with_token(remote, "main", "tok", tree),
        "ls-remote": lambda: git_ls_remote_with_token(remote, "main", "tok", tree),
        "push": lambda: git_push_with_token(remote, "main", "tok", tree),
    }
    for name, call in calls.items():
        with pytest.raises(RepoLocalConfigHazardError) as excinfo:
            call()
        assert "refusing to" in str(excinfo.value), name
        _assert_clean(excinfo, capsys)

    with pytest.raises(RemoteReadbackError) as readback:
        read_remote_head(remote, "main", tree, token="tok")
    _assert_clean(readback, capsys)


#: Repo-local config whose KEY embeds a URL segment carrying a credential, with
#: the category the refusal must still name.
HAZARD_KEYS = {
    "insteadof": (f"url.https://user:{TOKEN}@example.invalid/.insteadOf", "url.*.insteadOf"),
    "pushinsteadof": (f"url.https://user:{TOKEN}@example.invalid/.pushInsteadOf", "url.*.pushInsteadOf"),
    "extraheader": (f"http.https://example.invalid/?token={TOKEN}.extraheader", "http.*.extraheader"),
    "credential": (f"credential.https://{TOKEN}@example.invalid.helper", "credential.*"),
}


@pytest.mark.parametrize("name", sorted(HAZARD_KEYS))
def test_hermeticity_refusal_names_the_category_not_the_config_key(name, tmp_path, capsys):
    key, category = HAZARD_KEYS[name]
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    git(tree, "config", key, "https://example.invalid/")

    assert check_repo_local_config_hazards(tree) == (category,)
    calls = {
        "fetch": lambda: git_fetch_with_token("origin", "main", "tok", tree),
        "ls-remote": lambda: git_ls_remote_with_token("origin", "main", "tok", tree),
        "push": lambda: git_push_with_token("origin", "main", "tok", tree),
    }
    for call_name, call in calls.items():
        with pytest.raises(RepoLocalConfigHazardError) as excinfo:
            call()
        assert category in str(excinfo.value), call_name
        _assert_clean(excinfo, capsys)

    with pytest.raises(RemoteReadbackError) as readback:
        read_remote_head("origin", "main", tree, token="tok")
    _assert_clean(readback, capsys)


def test_hermeticity_refusal_names_no_includeif_condition(tmp_path, capsys):
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    git(tree, "config", f"includeIf.gitdir:/{TOKEN}/.path", "/dev/null")

    assert check_repo_local_config_hazards(tree) == ("includeIf.*",)
    with pytest.raises(RepoLocalConfigHazardError) as excinfo:
        git_fetch_with_token("origin", "main", "tok", tree)
    _assert_clean(excinfo, capsys)


def test_branch_commit_check_fetch_failure_names_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote

    with pytest.raises(CommitCheckUnavailableError) as excinfo:
        fetch_branch_commit_subjects(tree, "main", remote=remote)

    assert "exit" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_branch_commit_check_bypass_and_soft_fail_print_no_remote(tree_and_remote, capsys):
    tree, remote = tree_and_remote

    for skip in (True, False):
        _run_branch_commit_check(tree, base_branch="main", remote=remote, merge_method="merge", skip=skip)
        out, err = capsys.readouterr()
        assert err and SECRET not in out and SECRET not in err


#: Where the credential sits in a remote that is a local path (so the push
#: reaches the hook stage offline): a directory name, or a `?token=` suffix.
CREDENTIAL_PATH_FORMS = {
    "path": lambda base: base / SECRET / "o" / "r.git",
    "query": lambda base: base / "o" / f"r.git?token={SECRET}",
}


@pytest.fixture(params=sorted(CREDENTIAL_PATH_FORMS))
def tree_with_credential_path_remote(request, tmp_path) -> tuple[Path, Path]:
    """A repo with one unpushed commit and a bare remote whose path carries the
    credential."""
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    remote = CREDENTIAL_PATH_FORMS[request.param](tmp_path)
    remote.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(tree), str(remote))
    git(tree, "commit", "-q", "--allow-empty", "-m", "an unpushed commit")
    return tree, remote


def _install_hook(path: Path, body: str) -> None:
    path.write_text(f"#!/bin/sh\n{body}\nexit 1\n")
    path.chmod(0o755)


def test_pre_push_hook_echoing_the_remote_location_stays_out_of_the_message(
    tree_with_credential_path_remote, capsys
):
    tree, remote = tree_with_credential_path_remote
    _install_hook(tree / ".git" / "hooks" / "pre-push", 'echo "pushing to $2"')

    with pytest.raises(GitPushError) as excinfo:
        git_push_with_token(str(remote), "main", "tok", tree)

    assert excinfo.value.local_hook_lines
    assert "1 local pre-push hook line(s) withheld" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_pre_receive_hook_echoing_the_remote_path_stays_out_of_the_message(
    tree_with_credential_path_remote, capsys
):
    tree, remote = tree_with_credential_path_remote
    _install_hook(remote / "hooks" / "pre-receive", 'echo "received in $(pwd)" 1>&2')

    with pytest.raises(GitPushError) as excinfo:
        git_push_with_token(str(remote), "main", "tok", tree)

    assert excinfo.value.remote_lines
    assert "1 remote line(s)" in str(excinfo.value)
    _assert_clean(excinfo, capsys)


def test_withheld_hook_lines_print_under_verbose_only(tree_with_credential_path_remote, capsys):
    tree, remote = tree_with_credential_path_remote
    _install_hook(tree / ".git" / "hooks" / "pre-push", 'echo "pushing to $2"')

    with pytest.raises(GitPushError):
        git_push_with_token(str(remote), "main", "tok", tree)
    assert "pushing to" not in capsys.readouterr().err

    with pytest.raises(GitPushError):
        git_push_with_token(str(remote), "main", "tok", tree, verbose=True)
    assert "pushing to" in capsys.readouterr().err


def test_stray_merge_commit_error_names_no_remote(tmp_path, capsys):
    """A remote that is a local path carrying the secret in a directory name
    fetches fine, so the offender message itself is what is inspected."""
    tree = tmp_path / "tree"
    seed_base_commit(tree)
    remote = tmp_path / SECRET / "o" / "r.git"
    remote.parent.mkdir(parents=True)
    git(tmp_path, "clone", "-q", "--bare", str(tree), str(remote))
    git(tree, "commit", "-q", "--allow-empty", "-m", "a subject that is not conventional")

    from clagentic_loadout.push.branch_commit_check import StrayMergeCommitError

    with pytest.raises(StrayMergeCommitError) as excinfo:
        check_branch_for_stray_merge_commits(tree, "main", merge_method="merge", remote=str(remote))

    assert "a subject that is not conventional" in str(excinfo.value)
    _assert_clean(excinfo, capsys)
