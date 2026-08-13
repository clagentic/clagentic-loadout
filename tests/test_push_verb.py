"""test_push_verb.py — tests for clagentic_loadout.push.verb (lr-09ca, Wave
B slice 3).

Coverage:
  - Role-parameterization: an arbitrary --caller value resolves a token via
    an injected TokenProvider that records exactly which role it was asked
    for -- no hardcoded caller name anywhere in the dispatch path.
  - Namespace guard: fires BEFORE token resolution for both platforms
    (proven via a refusing TokenProvider).
  - Bot-identity gate: --bot-name/--bot-email re-authors HEAD; a mismatch
    after re-authoring fails closed with EXIT_AUTHOR_MISMATCH; omitted
    identity is a no-op unless --require-bot-identity.
  - Issue-link enforcement: --issue auto-injects/asserts the 'Closes #NN'
    trailer; a conflicting existing trailer fails closed.
  - PR open (create) end-to-end for both platforms (mocked git via a real
    local bare-repo remote, mocked HTTP via an injected opener).
  - --update-pr short-circuits to a PATCH-only path -- no push performed
    (proven by asserting the branch never lands on the fake remote).
  - --body-stdin: empty/malformed stdin fails closed before any token
    resolution or git/network call. --body-env (lr-e1e2fb): reads a body
    already staged via transport.body_env's identity-stamped API -- no
    caller-supplied filesystem path anywhere.
  - Exit-code coverage.
  - PR-title gate (lr-6067): a conformant --title is accepted at PR-open; a
    multi-scope title (the PR #35 repro, 'feat(lr-273d)(lr-7a6e): ...') is
    REJECTED with EXIT_PR_TITLE_INVALID before any push or PR call;
    --skip-title-check bypasses the gate (logged); --update-pr with an
    invalid title is also rejected; a push with no --title (not reachable
    in practice -- --title is required to create a PR -- but --update-pr
    with only --body-stdin) is unaffected by the gate.
"""

from __future__ import annotations

import io
import json
import subprocess

import pytest

from clagentic_loadout.push import verb
from clagentic_loadout.push.github_backend import GITHUB_API_BASE
from clagentic_loadout.sha import validate_sha
from clagentic_loadout.transport import body_env, stage_body_verb
from clagentic_loadout.transport.credential_provider import CredentialProviderError


def _git(args: list[str], cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True
    )


class _RecordingTokenProvider:
    def __init__(self, token: str = "tok-123"):
        self.resolved_for: list[str] = []
        self._token = token

    def resolve_token(self, role: str) -> str:
        self.resolved_for.append(role)
        return self._token


class _RefusingTokenProvider:
    def resolve_token(self, role: str) -> str:
        raise AssertionError(f"token provider must not be called (role={role!r})")


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


def _json_resp(status: int, payload) -> _FakeResponse:
    return _FakeResponse(status, json.dumps(payload).encode("utf-8"))


def _forgejo_create_opener(*, pr_number=42):
    def opener(req, timeout=15):
        if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
            return _json_resp(201, {"number": pr_number})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


def _github_create_opener(*, pr_number=42):
    def opener(req, timeout=30):
        if req.get_method() == "POST" and req.full_url == f"{GITHUB_API_BASE}/repos/some-owner/some-repo/pulls":
            return _json_resp(201, {"number": pr_number})
        raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

    return opener


@pytest.fixture
def repo_with_remote(tmp_path):
    """A local repo with a bare-repo 'origin' remote (real git, no
    network), a base main + feature branch with one commit ahead."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    seed = tmp_path / "seed"
    seed.mkdir()
    _git(["init", "-b", "main"], seed)
    _git(["config", "user.email", "base@example.com"], seed)
    _git(["config", "user.name", "Base"], seed)
    (seed / "README.md").write_text("hello\n")
    _git(["add", "README.md"], seed)
    _git(["commit", "-m", "initial"], seed)
    _git(["remote", "add", "origin", str(remote)], seed)
    _git(["push", "origin", "main"], seed)

    repo = tmp_path / "repo"
    _git(["clone", str(remote), str(repo)], tmp_path)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    _git(["checkout", "-b", "feature"], repo)
    (repo / "feature.txt").write_text("work\n")
    _git(["add", "feature.txt"], repo)
    # Conventional-Commits-shaped (lr-dd1742, push.branch_commit_check):
    # this fixture's commit is a stand-in for ordinary feature work, and
    # every push-time gate in this module -- including the new branch
    # commit-subject check -- validates real commit subjects, so the
    # fixture's own content must conform rather than accidentally tripping
    # a gate that isn't the one under test.
    _git(["commit", "-m", "feat: add feature work"], repo)
    _git(
        ["remote", "set-url", "origin", "http://git-host.example.com/some-owner/some-repo.git"],
        repo,
    )
    # git push's actual network target must be the real local bare repo (no
    # real network access anywhere in this test file), while `git remote
    # get-url origin` (used for owner/repo/api_base coordinate parsing) keeps
    # returning the neutral placeholder Forgejo-shaped URL above unchanged.
    #
    # PREVIOUSLY: a repo-local `url.<remote>.pushInsteadOf` directive
    # achieved this split. That directive is now correctly refused by
    # push.git_hermeticity.check_repo_local_config_hazards (pre-merge
    # security review finding, repo-local-hazard-coverage-gap): a
    # url.*.insteadOf/pushInsteadOf rule can silently redirect a push to an
    # attacker-chosen host in a REAL deployment, which would then receive
    # the minted credential this package presents via GIT_ASKPASS -- fixing
    # that gap correctly makes this exact directive shape unusable here too,
    # since a fail-closed hazard check cannot distinguish this fixture's own
    # benign use from a hostile one.
    #
    # THE FIX: `remote.origin.pushurl` -- a normal, first-class, single-
    # remote push-URL override (distinct from a wildcard `url.*.insteadOf`
    # rewrite rule, which can redirect ANY remote matching its base-URL
    # prefix). It achieves the identical split this fixture needs (`git
    # remote get-url origin` still returns the placeholder; `git push
    # origin` reaches the real bare repo) without any of the four
    # unsuppressable hazard shapes check_repo_local_config_hazards scans
    # for (credential.*, http.*.extraheader, includeIf.*, url.*.insteadOf/
    # pushInsteadOf) -- confirmed directly against that function during
    # this fix.
    _git(["config", "remote.origin.pushurl", str(remote)], repo)

    return repo, remote


def _run_main(argv, *, token_provider=None, opener=None, stdin_text=None, monkeypatch=None):
    if stdin_text is not None:
        monkeypatch.setattr("sys.stdin", io.TextIOWrapper(io.BytesIO(stdin_text.encode("utf-8"))))
    return verb.main(argv, token_provider=token_provider, opener=opener)


class TestArgumentValidation:
    def test_missing_title_on_create_is_usage_error(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo"],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_update_pr_without_pr_number_is_usage_error(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--update-pr"],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_bot_name_without_email_is_usage_error(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--bot-name", "Bot",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_help_exits_ok_before_any_argument_parsing_side_effects(self, monkeypatch):
        code = _run_main(["--help"], token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_OK

    def test_git_host_base_url_help_does_not_claim_it_is_consulted(self, monkeypatch, capsys):
        """Regression test (lr-cd3113): --git-host-base-url's --help text
        previously implied its resolution chain (explicit flag, then
        CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL, then the localhost placeholder)
        was actually applied to a push-path API call. Traced end to end,
        it is not -- api_base is always derived from the git remote URL
        (push.git_coords.parse_forgejo_coords), regardless of this flag.
        The corrected help text must say so plainly rather than describing
        a resolution chain that never executes."""
        code = _run_main(["--help"], token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_OK
        # argparse rewraps help text across lines -- normalize whitespace
        # before substring-matching so the assertion is not brittle against
        # the exact column width the formatter chooses.
        out = " ".join(capsys.readouterr().out.split())
        assert "NOT currently consumed by any push-path API call" in out
        assert "always derived from the git remote URL" in out


class TestBodyStdin:
    def test_empty_stdin_fails_before_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text="",
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY

    def test_malformed_json_stdin_fails(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text="not json",
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY

    def test_missing_body_key_fails(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"not_body": "x"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY

    def test_malformed_json_error_points_to_body_env_only(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """lr-df5a11 (operator-directed), lr-e1e2fb (redesign), lr-efbcc6
        (retirement): an invalid --body-stdin JSON error must PUSH the
        caller to the sanctioned path -- name --body-env and the
        loadout-stage-body invocation -- and must NOT also name the retired
        raw printf/echo-redirect staging fallback (naming a retired
        mechanism in the same error text as the sanctioned one would
        recreate the exact contradiction this fix exists to close)."""
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text="not json",
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY
        stderr = capsys.readouterr().err
        assert "--body-env" in stderr
        assert "loadout-stage-body" in stderr
        assert "printf" not in stderr
        assert "echo/printf" not in stderr

    def test_missing_body_key_error_also_points_to_body_env(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"not_body": "x"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY
        stderr = capsys.readouterr().err
        assert "--body-env" in stderr

    def test_no_body_input_at_all_error_points_to_body_env(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t"],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY
        stderr = capsys.readouterr().err
        assert "--body-env" in stderr
        assert "--body-stdin" in stderr


class TestBodyEnv:
    """lr-e1e2fb: --body-env -- the sanctioned mechanism replacing the
    rejected caller-supplied --body-file. Reads a body a caller's harness
    already staged via loadout-stage-body's identity-stamped API
    (transport.body_env.stage_caller_body / read_caller_body_bytes) -- no
    filesystem path anywhere in this verb's own argv.

    lr-2b20d2: loadout-stage-body's documented stdin contract is the
    {"body": "..."} JSON envelope (the same shape --body-stdin requires),
    so every staged fixture in this class stages that envelope, and
    _read_body_env unwraps it -- see TestBodyEnvUnwrapsJsonEnvelope below
    for the regression coverage that this unwrap actually happens."""

    def test_body_env_happy_path_create_pr(self, repo_with_remote, monkeypatch, tmp_path):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 7})
            raise AssertionError("unexpected call")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE,
            body_bytes=json.dumps(
                {"body": "This is a plain-text PR body, staged ahead of time."}
            ).encode("utf-8"),
            create_branch=current_branch,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == "This is a plain-text PR body, staged ahead of time."

    def test_body_env_and_body_stdin_together_is_usage_error(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_nothing_staged_fails_before_token_resolution(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE

    def test_staged_for_a_different_branch_fails_closed(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE,
            body_bytes=b"body staged for the wrong branch",
            create_branch="some-other-branch-entirely",
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE

    def test_empty_staged_body_fails(self, repo_with_remote, monkeypatch, tmp_path):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE, body_bytes=b"   \n", create_branch=current_branch
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_EMPTY

    def test_no_caller_supplied_filesystem_path_anywhere_in_argv(self):
        """The actual property this whole redesign exists for: --body-env
        is a bare switch, never a flag taking a path-shaped value."""
        parser = verb._build_arg_parser()
        action = next(a for a in parser._actions if "--body-env" in a.option_strings)
        assert action.nargs == 0
        flags = [opt for action in parser._actions for opt in action.option_strings]
        assert "--body-file" not in flags

    def test_body_env_works_on_update_pr_path(self, repo_with_remote, monkeypatch, tmp_path):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "PATCH":
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(200, {"number": 42})
            raise AssertionError(f"unexpected call: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE,
            body_bytes=json.dumps({"body": "updated body text"}).encode("utf-8"),
            target_pr=42,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-env", "--replace-body",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == "updated body text"

    def test_body_env_staged_for_wrong_pr_on_update_path_fails_closed(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE, body_bytes=b"body for the wrong PR", target_pr=99
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-env", "--replace-body",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_BODY_ENV_UNAVAILABLE

    def test_detached_head_refused_before_consuming_staged_body(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        """Re-audit nit: on a detached HEAD, current_branch() returns
        the literal 'HEAD', which IS in git_coords.PROTECTED_BRANCHES --
        that refusal is not new. What IS new: the staged --body-env content
        must survive this refusal, since _run_create_pr's own protected-
        branch check fires LATER than the point --body-env consumes the
        staged body. An agent hitting this on a detached HEAD must be able
        to retry (after checking out a real branch) WITHOUT re-staging."""
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        _git(["checkout", "--detach", "HEAD"], repo)
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE,
            body_bytes=b"a body staged while HEAD is detached",
            create_branch="HEAD",
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED

        # The staged body must NOT have been consumed -- a retry after
        # checking out a real branch should still find it.
        still_staged = body_env.read_caller_body_bytes(
            caller=verb.DEFAULT_ROLE, expect_create_branch="HEAD"
        )
        assert still_staged == b"a body staged while HEAD is detached"


class TestBodyEnvBuilderCaller:
    """lr-efbcc6 (Part 1, additive half): loadout-stage-body has no
    role-entitlement concept at all -- transport.stage_body_verb only
    validates --caller against _SAFE_CALLER_RE (safe characters), never a
    reviewer-only allowlist -- so a builder caller staging through the CLI
    verb, then reading via push --caller <role> --body-env, was already
    mechanically possible. Nothing in this test class changes stage_body_
    verb.py or push.verb.py: it proves, end to end through BOTH real CLI
    entrypoints (not just the transport.body_env API other TestBodyEnv
    cases call directly), that a non-default/"builder" role works
    identically to DEFAULT_ROLE on both the PR-create and --update-pr
    paths -- the exact gap the task's acceptance criterion #1 names.

    Uses stage_body_verb.main() (the loadout-stage-body CLI) rather than
    calling transport.body_env.stage_caller_body directly, unlike every
    other TestBodyEnv case above -- this is what makes the coverage
    "first-class and PROVEN" (a real two-process CLI handoff) rather than
    "incidental" (only ever exercised via the transport layer's own API).

    NOTE ON STAGED CONTENT SHAPE (corrected lr-2b20d2 -- see that task for
    the full defect writeup): loadout-stage-body validates its own stdin as
    JSON-shaped ({"body": "..."}, the SAME shape --body-stdin requires --
    transport.git_host_api.validate_body_stdin_content) and stages the raw
    stdin BYTES VERBATIM (JSON wrapper included) -- that part is unchanged.
    push.verb's own --body-env read side (_read_body_env) now UNWRAPS that
    envelope via the same _unwrap_body_json helper --body-stdin uses, so a
    caller staging via loadout-stage-body's CLI and reading via --body-env
    gets back the CALLER'S ORIGINAL PROSE as the PR body -- no JSON
    wrapper, real newlines -- matching what --body-stdin has always
    produced. (Prior to this fix, _read_body_env used the staged bytes
    verbatim, so a builder's PR body rendered as the literal JSON
    envelope -- reproduced live against a Forgejo deployment and this
    repo's own PR #149.)"""

    _BUILDER_CALLER = "builder"

    def test_builder_caller_stage_body_cli_then_push_create_pr(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}
        staged_stdin = json.dumps({"body": "builder-staged plain-text PR body"})

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 7})
            raise AssertionError("unexpected call")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)

        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: staged_stdin.encode("utf-8")
        )
        stage_rc = stage_body_verb.main(
            ["--caller", self._BUILDER_CALLER, "--create-branch", current_branch]
        )
        assert stage_rc == stage_body_verb.EXIT_OK

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--caller", self._BUILDER_CALLER,
                "--title", "feat: t", "--body-env",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        # See class docstring: --body-env unwraps the staged JSON envelope,
        # so the PR body is the caller's original prose, not the wrapper.
        assert captured["body"] == "builder-staged plain-text PR body"
        assert provider.resolved_for == [self._BUILDER_CALLER]

    def test_builder_caller_stage_body_cli_then_push_update_pr(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}
        staged_stdin = json.dumps({"body": "builder-staged updated body text"})

        def opener(req, timeout=15):
            if req.get_method() == "PATCH":
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(200, {"number": 42})
            raise AssertionError(f"unexpected call: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))

        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: staged_stdin.encode("utf-8")
        )
        stage_rc = stage_body_verb.main(
            ["--caller", self._BUILDER_CALLER, "--target-pr", "42"]
        )
        assert stage_rc == stage_body_verb.EXIT_OK

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--caller", self._BUILDER_CALLER,
                "--update-pr", "--pr", "42", "--body-env", "--replace-body",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == "builder-staged updated body text"
        assert provider.resolved_for == [self._BUILDER_CALLER]

    def test_builder_stage_body_cli_content_round_trips_byte_for_byte(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        """The stage-then-read round trip through BOTH real CLI entry
        points (loadout-stage-body, then transport.body_env's own
        read-and-consume) is byte-for-byte identical for a builder caller,
        exactly as it already is for a reviewer caller
        (test_transport_stage_body_verb.py's own TestEndToEndWithReadSide) --
        no reviewer-specific branch anywhere in either module."""
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)

        staged_stdin = json.dumps({"body": "line one\\nline two, JSON-wrapped"})
        monkeypatch.setattr(
            "sys.stdin.buffer.read", lambda: staged_stdin.encode("utf-8")
        )
        stage_rc = stage_body_verb.main(
            ["--caller", self._BUILDER_CALLER, "--create-branch", current_branch]
        )
        assert stage_rc == stage_body_verb.EXIT_OK

        read_back = body_env.read_caller_body_bytes(
            caller=self._BUILDER_CALLER, expect_create_branch=current_branch
        )
        assert read_back.decode("utf-8") == staged_stdin


class TestNamespaceGuard:
    def test_denied_namespace_fires_before_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-namespace", "some-other-owner",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_NAMESPACE_DENIED

    def test_allowed_namespace_permits_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-namespace", "some-owner",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == [verb.DEFAULT_ROLE]

    def test_permissive_default_when_unconfigured(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        # No --allowed-namespace flag and no env var configured -- the
        # permissive default (namespace_guard.resolve_allowed_namespaces
        # returning an empty set) must not block this push.
        monkeypatch.delenv("CLAGENTIC_LOADOUT_ALLOWED_NAMESPACES", raising=False)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK


class TestHostGuard:
    """lr-0e39f9: push.verb attaches a live credential to a Forgejo API host
    derived exclusively from the live git remote
    (push.git_coords.parse_forgejo_coords) -- unlike the owner/repo
    namespace guard above, nothing previously anchored that HOST at all. A
    --allowed-host allowlist (push.host_guard, permissive when unconfigured,
    mirroring TestNamespaceGuard's own coverage shape for the sibling guard)
    now fires BEFORE token resolution, on both the create and --update-pr
    paths, and is skipped (never denies) on --platform github, where
    api_base is "" (github_backend hardcodes its own public API base) rather
    than git-remote-derived."""

    def test_denied_host_fires_before_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-host", "https://attacker.example.net",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_HOST_DENIED

    def test_allowed_host_permits_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-host", "http://git-host.example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == [verb.DEFAULT_ROLE]

    def test_permissive_default_when_unconfigured(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        # No --allowed-host flag and no env var configured -- the permissive
        # default (host_guard.resolve_allowed_hosts returning an empty set)
        # must not block this push. This is the byte-for-byte-unchanged
        # behavior for every deployment that has not opted into this guard.
        monkeypatch.delenv("CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS", raising=False)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_update_pr_host_denied(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "t",
                "--allowed-host", "https://attacker.example.net",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_HOST_DENIED

    def test_update_pr_allowed_host_permits_token_resolution(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
                "--allowed-host", "http://git-host.example.com",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_env_var_denied_host(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("CLAGENTIC_LOADOUT_PUSH_ALLOWED_HOSTS", "https://attacker.example.net")
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_HOST_DENIED

    def test_denial_error_names_the_offending_host(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-host", "https://good-host.example.com",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_HOST_DENIED
        stderr = capsys.readouterr().err
        assert "git-host.example.com" in stderr

    def test_github_platform_never_denied_by_host_guard(self, repo_with_remote, monkeypatch):
        """api_base is "" on --platform github (github_backend hardcodes its
        own public API base, never derived from the git remote) -- the host
        guard must be a no-op there regardless of what --allowed-host names,
        rather than requiring every GitHub deployment's allowlist to also
        carry a spurious empty-string entry."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _github_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
                "--allowed-host", "https://some-completely-different-host.example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK


class TestContentionCheck:
    """lr-78a584: optional, config-gated, default-OFF pre-flight read that
    refuses a create-PR push when the checked-out branch looks like another
    unit of work is already in flight in this checkout. Mirrors
    TestNamespaceGuard/TestHostGuard's own coverage shape for a sibling
    fail-closed-before-token-resolution guard, but DEFAULT-OFF (the other
    two guards are permissive-when-unconfigured but still consult their env
    var; this check does not run at all unless explicitly enabled)."""

    @staticmethod
    def _write_config(repo, *, enabled: bool, pattern: str | None = None) -> None:
        config_dir = repo / ".clagentic" / "loadout"
        config_dir.mkdir(parents=True, exist_ok=True)
        push_section = f"contention_check: {str(enabled).lower()}\n"
        if pattern is not None:
            push_section += f'  in_flight_branch_pattern: "{pattern}"\n'
        (config_dir / "config.yaml").write_text(f"push:\n  {push_section}", encoding="utf-8")

    def test_disabled_by_default_matching_branch_still_proceeds(
        self, repo_with_remote, monkeypatch
    ):
        """Hard acceptance criterion: absent config, behavior is
        byte-identical to today -- even the fixture's own 'feature' branch
        (not in-flight-shaped) proceeds unchanged, and this is true with NO
        config file present at all."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_enabled_matching_branch_refuses_before_token_resolution(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        self._write_config(repo, enabled=True, pattern=r"^feature")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_WORKING_TREE_CONTENTION

    def test_refusal_names_the_branch(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        self._write_config(repo, enabled=True, pattern=r"^feature")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_WORKING_TREE_CONTENTION
        stderr = capsys.readouterr().err
        assert "feature" in stderr

    def test_enabled_non_matching_branch_proceeds(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        # Enabled, but the default in_flight_branch_pattern
        # (feat|fix|chore|...) does not match the fixture's own 'feature'
        # branch name (no trailing '/').
        self._write_config(repo, enabled=True)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_override_flag_permits_the_push_and_warns(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        self._write_config(repo, enabled=True, pattern=r"^feature")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--override-contention-check",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "OVERRIDDEN" in stderr

    def test_update_pr_path_is_never_gated(self, repo_with_remote, monkeypatch):
        """--update-pr never mutates the working tree (metadata-only PATCH;
        see push.verb's own module docstring) -- there is nothing for this
        check to protect there, so it must never fire on that path even
        when enabled and the branch matches."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        self._write_config(repo, enabled=True, pattern=r"^feature")

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_default_branch_dirty_proceeds_even_when_enabled(
        self, repo_with_remote, monkeypatch
    ):
        """The task's own verified counter-example: a tree on a branch that
        does not match the in-flight pattern, carrying uncommitted changes,
        must proceed -- dirtiness is never an independent signal. The
        fixture's own 'feature' branch stands in for "not in-flight-shaped"
        here (see test_enabled_non_matching_branch_proceeds above for why it
        does not match the default pattern)."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        self._write_config(repo, enabled=True)
        (repo / "README.md").write_text("stale post-merge residue\n")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_help_documents_the_override_flag(self, monkeypatch, capsys):
        """The override flag must be discoverable via --help, not only in
        docs -- lr-78a584 comment #1: an operator must be able to find it
        WHILE BLOCKED."""
        code = _run_main(["--help"], token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_OK
        out = " ".join(capsys.readouterr().out.split())
        assert "--override-contention-check" in out


class TestRoleParameterization:
    def test_arbitrary_caller_flows_through_to_token_provider(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--caller", "some-arbitrary-role-xyz",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == ["some-arbitrary-role-xyz"]

    def test_default_role_used_when_caller_omitted(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == [verb.DEFAULT_ROLE]

    def test_token_fetch_failure_maps_to_exit_code(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote

        class _FailingProvider:
            def resolve_token(self, role):
                raise CredentialProviderError("boom")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_FailingProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TOKEN_FETCH_FAILED


class TestIssueLink:
    def test_issue_flag_injects_trailer_and_succeeds(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 5})
            raise AssertionError("unexpected call")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--issue", "99",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "fixes the thing"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "Closes #99" in captured["body"]

    def test_conflicting_trailer_fails_closed(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--issue", "99",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "body\n\nCloses #1\n"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_MISSING_ISSUE_LINK


class TestTaskIdTrailer:
    """lr-eb22f3: --task-id auto-injects a 'Task: <task_id>' trailer into
    the PR body -- the write-side counterpart of --issue's 'Closes #NN'
    trailer, together delivering 'both IDs on every applicable PR'. Unlike
    --issue, there is no fail-closed enforcement analog: a missing task_id
    is a legitimate ad-hoc-mode state, never a push failure."""

    def test_task_id_flag_injects_trailer_and_succeeds(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 5})
            raise AssertionError("unexpected call")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--task-id", "lr-eb22f3",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "Task: lr-eb22f3" in captured["body"]

    def test_no_task_id_omits_trailer(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 5})
            raise AssertionError("unexpected call")

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "Task:" not in captured["body"]

    def test_both_task_id_and_issue_trailers_survive_together(self, repo_with_remote, monkeypatch):
        """Acceptance: the opened PR body carries BOTH the task_id trailer
        AND the Closes #NN trailer when both --task-id and --issue are
        supplied."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 5})
            raise AssertionError("unexpected call")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--task-id", "lr-eb22f3", "--issue", "99",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert "Task: lr-eb22f3" in captured["body"]
        assert "Closes #99" in captured["body"]


class TestBotIdentity:
    def test_bot_identity_reauthors_before_push(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "bot@example.com"

    def test_require_bot_identity_fails_closed_when_missing(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--require-bot-identity",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_AUTHOR_MISMATCH

    def test_no_bot_identity_pushes_original_author(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_dirty_work_tree_fails_with_a_distinct_message_before_reauthoring(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """lr-4cd7ac (MILLER diagnosis lr-60781e): a push with a bot
        identity AND unstaged tracked changes must fail with a message
        naming the dirty tree, distinguishable from a genuine identity
        mismatch -- not the pre-fix generic 'commit re-authoring failed'
        with no cause, and not EXIT_AUTHOR_MISMATCH's mis-attribution
        framing (this is a local, recoverable condition, unrelated to
        commit authorship)."""
        repo, remote = repo_with_remote
        (repo / "feature.txt").write_text("unstaged edit after commit\n")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_DIRTY_WORK_TREE
        assert code != verb.EXIT_AUTHOR_MISMATCH
        stderr = capsys.readouterr().err
        assert "feature.txt" in stderr
        assert "unstaged changes" in stderr
        assert "LOCAL, RECOVERABLE" in stderr

        # Nothing was pushed to the remote -- the pre-flight must fire
        # BEFORE any re-authoring or push attempt.
        r = subprocess.run(
            ["git", "branch"], cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert "feature" not in r.stdout


class TestLeaseControl:
    """lr-f57f13, D5 DECIDED: push.verb no longer silently derives
    force_with_lease from "did bot-identity re-authoring rewrite this
    branch's history" alone -- an explicit --force-with-lease/
    --no-force-with-lease CLI flag always wins, and the resolved value plus
    its origin is always printed to stderr before the push runs (never
    inferred silently)."""

    def test_no_bot_identity_no_flags_resolves_lease_off_by_default(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "force-with-lease=False" in stderr
        assert "default-false" in stderr

    def test_bot_identity_reauthoring_auto_derives_lease_on(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "force-with-lease=True" in stderr
        assert "history-rewritten" in stderr

    def test_explicit_no_force_with_lease_overrides_auto_derivation(self, repo_with_remote, monkeypatch, capsys):
        """An explicit --no-force-with-lease wins even when bot-identity
        re-authoring would otherwise auto-derive force-on."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
                "--no-force-with-lease",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "force-with-lease=False" in stderr
        assert "cli-flag(--no-force-with-lease)" in stderr

    def test_explicit_force_with_lease_wins_with_no_reauthoring(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--force-with-lease",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "force-with-lease=True" in stderr
        assert "cli-flag(--force-with-lease)" in stderr

    def test_force_with_lease_and_no_force_with_lease_together_is_usage_error(self, repo_with_remote, monkeypatch):
        """--force-with-lease/--no-force-with-lease is an argparse
        mutually-exclusive group -- argparse itself refuses this
        combination (SystemExit(2)) before this verb's own _run ever
        executes, exactly like the pre-existing --replace-body/
        --append-body group above."""
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--force-with-lease", "--no-force-with-lease",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == 2

    def test_forced_lease_fetches_remote_tracking_ref_before_push(self, repo_with_remote, monkeypatch, capsys):
        """resolve_lease's pre-lease fetch (push.lease_control) must
        actually run when the resolved decision is to force -- proven by
        asserting the printed 'pre-lease fetch attempted=True' marker."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--force-with-lease",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "pre-lease fetch attempted=True" in stderr

    def test_no_force_never_attempts_a_pre_lease_fetch(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "pre-lease fetch attempted=False" in stderr


class TestCreatePrForgejo:
    def test_end_to_end_success(self, monkeypatch, capsys, tmp_path):
        # lr-361de3: repo_with_remote's `origin` is a pushInsteadOf-redirected
        # placeholder URL -- `git ls-remote` (a fetch-class op, unaffected by
        # pushInsteadOf) cannot resolve it. Now that remote_head_sha is the
        # SOLE SHA this envelope reports (the bare local head_sha this test
        # used to check is gone), this test needs a readback-safe fixture
        # instead -- see _repo_with_directly_resolvable_remote's own
        # docstring for why that means --platform github here.
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=77)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

        captured = capsys.readouterr()
        out = captured.out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["pr_number"] == 77
        assert payload["owner"] == "some-owner"
        assert payload["repo"] == "some-repo"

        # lr-361de3: the create-path envelope no longer carries a bare local
        # head_sha (see this task's own migration note in push.verb) --
        # remote_head_sha (confirmed via a genuine git ls-remote round-trip)
        # is now the sole SHA this envelope reports.
        local_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()
        assert "head_sha" not in payload
        assert payload["remote_head_sha"] == local_head, captured.err

        r = subprocess.run(["git", "branch"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" in r.stdout

    def test_pr_open_failure_maps_to_exit_code(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote

        def opener(req, timeout=15):
            import urllib.error

            raise urllib.error.HTTPError(req.full_url, 500, "err", {}, None)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_FAILED

    def test_protected_branch_refused(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _git(["checkout", "main"], repo)
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "t", "--body-stdin"],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED

    def test_token_never_leaks_on_push_failure(self, repo_with_remote, monkeypatch, capsys):
        """End-to-end no-token-in-logs guarantee: with the pushurl override
        removed, `git push` targets the neutral placeholder Forgejo-shaped
        URL directly and fails (unresolvable host, no real network access
        attempted) -- push.verb's own EXIT_PUSH_FAILED stderr line must
        never contain the resolved token value."""
        repo, _remote = repo_with_remote
        secret_token = "sk-end-to-end-secret-should-never-leak-anywhere"
        # Remove the remote.origin.pushurl override that redirects the push
        # target to the real local bare repo; git push now targets the
        # neutral placeholder URL directly and fails fast (unresolvable
        # host). See repo_with_remote's own comment for why this fixture
        # uses pushurl rather than a repo-local url.*.pushInsteadOf rule
        # (the latter is now a hazard push.git_hermeticity fails closed on).
        _git(["config", "--unset", "remote.origin.pushurl"], repo)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RecordingTokenProvider(token=secret_token),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED
        captured = capsys.readouterr()
        assert secret_token not in captured.out
        assert secret_token not in captured.err

    def test_local_pre_push_hook_message_reaches_the_caller(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """lr-1fb18b acceptance criterion: a caller receiving a local-hook
        rejection must see the HOOK'S OWN OUTPUT TEXT, not merely the
        sub_cause classification -- proven at the CLI/main() boundary (what
        an agent shelling out to `loadout-push` actually receives), not
        merely on GitPushError.local_hook_lines (already proven populated
        by test_push_git_push.py -- an assertion at that internal level
        would pass even if the handoff into verb.py/main() dropped the text,
        which is exactly the defect this task diagnoses).

        Installs a REAL `.git/hooks/pre-push` script printing a distinctive,
        unprefixed string (mirroring the lore repo's own local pre-push
        hook, which prints unprefixed since it never runs on the remote --
        see _extract_local_hook_lines's own docstring) and asserts that
        exact string is present in BOTH caller-visible channels:
          1. the human-readable stderr line (str(exc)); and
          2. the structured JSON failure envelope on stderr (the "prime
             candidate" the task named -- previously no JSON was emitted on
             any push failure at all, success-only).
        """
        repo, _remote = repo_with_remote
        distinctive_marker = "LR-1FB18B-DISTINCTIVE-HOOK-MARKER: docs-staleness gate failed"
        hooks_dir = repo / ".git" / "hooks"
        pre_push_hook = hooks_dir / "pre-push"
        pre_push_hook.write_text(
            "#!/bin/sh\n"
            f'echo "{distinctive_marker}" 1>&2\n'
            "exit 1\n"
        )
        pre_push_hook.chmod(0o755)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED
        captured = capsys.readouterr()

        # Channel 1: human-readable stderr text.
        assert distinctive_marker in captured.err
        assert "local-hook-rejected" in captured.err

        # Channel 2: the structured JSON failure envelope -- the LAST
        # stderr line is the envelope (printed immediately after the
        # human-readable "push: ..." line in main()'s except PushVerbError
        # handler).
        stderr_lines = [line for line in captured.err.splitlines() if line.strip()]
        envelope = json.loads(stderr_lines[-1])
        assert envelope["sub_cause"] == "local-hook-rejected"
        assert envelope["reached_transport"] is False
        assert any(distinctive_marker in line for line in envelope["local_hook_lines"])


class TestCleanlinessCheck:
    """lr-d7a8: pre-push scratch-litter check on the create-PR path. Default
    is warn-and-continue; --strict fails closed with EXIT_SCRATCH_LITTER_
    FOUND before any push or PR call. A gitignored scratch file is silent."""

    def test_clean_tree_pushes_with_no_warning(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "scratch litter" not in err

    def test_untracked_scratch_file_warns_and_still_pushes(self, repo_with_remote, monkeypatch, capsys):
        repo, remote = repo_with_remote
        (repo / "pr-body-lr-1234.txt").write_text("staged body\n")
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "pr-body-lr-1234.txt" in err
        assert "pr-body-*" in err

        # The push itself still went through -- warn, not block.
        r = subprocess.run(["git", "branch"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" in r.stdout

    def test_gitignored_scratch_file_does_not_warn(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        (repo / ".gitignore").write_text("pr-body-*\n")
        _git(["add", ".gitignore"], repo)
        _git(["commit", "-m", "add gitignore"], repo)
        (repo / "pr-body-lr-1234.txt").write_text("staged body\n")
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "scratch litter" not in err

    def test_strict_fails_before_push(self, repo_with_remote, monkeypatch, capsys):
        repo, remote = repo_with_remote
        (repo / "pr-body-lr-1234.txt").write_text("staged body\n")

        def _refusing_opener(req, timeout=15):
            raise AssertionError("PR-open call must not happen when --strict fails closed")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--strict",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=_refusing_opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_SCRATCH_LITTER_FOUND
        err = capsys.readouterr().err
        assert "pr-body-lr-1234.txt" in err

        # No push happened -- the feature branch never reached the remote.
        r = subprocess.run(["git", "branch", "-a"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" not in r.stdout

    def test_strict_clean_tree_pushes_normally(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--strict",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_config_override_pattern_list_honored(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        (repo / "custom-litter.tmp").write_text("x\n")
        loadout_dir = repo / ".clagentic" / "loadout"
        loadout_dir.mkdir(parents=True)
        (loadout_dir / "config.yaml").write_text(
            "push:\n  scratch_patterns:\n    - custom-litter.*\n"
        )
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "custom-litter.tmp" in err

    def test_update_pr_path_never_runs_cleanliness_check(self, repo_with_remote, monkeypatch, capsys):
        """--update-pr never pushes at all, so the cleanliness check (which
        only guards the create-PR pre-push point) must not fire even with a
        matching scratch file present."""
        repo, _remote = repo_with_remote
        (repo / "pr-body-lr-1234.txt").write_text("staged body\n")

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "scratch litter" not in err


class TestCreatePrGithub:
    def test_end_to_end_success(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=101)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        out = capsys.readouterr().out
        payload = json.loads(out.strip().splitlines()[-1])
        assert payload["pr_number"] == 101

    def test_missing_repo_flag_fails(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            ["--repo-path", str(repo), "--platform", "github", "--title", "t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_REMOTE_ERROR


class TestUpdatePr:
    def test_update_pr_never_pushes(self, repo_with_remote, monkeypatch):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()

        # feature branch was never pushed to remote before this call.
        r = subprocess.run(["git", "branch", "-a"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" not in r.stdout

        def opener(req, timeout=15):
            assert req.get_method() == "PATCH"
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(["git", "branch", "-a"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" not in r.stdout

    def test_git_host_base_url_flag_does_not_affect_the_api_call_target(self, repo_with_remote, monkeypatch):
        """Regression test (lr-cd3113): --git-host-base-url is accepted by
        argparse but its value is never consumed by this verb -- api_base
        for every Forgejo API call is derived exclusively from the git
        remote URL (push.git_coords.parse_forgejo_coords). Passing a bogus
        --git-host-base-url must have NO effect on the actual request
        target; an opener that ever sees a request to the bogus host fails
        the test via AssertionError."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            assert req.get_method() == "PATCH"
            assert req.full_url == (
                "http://git-host.example.com/api/v1/repos/some-owner/some-repo/pulls/42"
            ), f"unexpected request target: {req.full_url!r}"
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
                "--git-host-base-url", "http://bogus.invalid:9999",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_update_pr_body_stdin_used_when_supplied(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        captured = {}

        def opener(req, timeout=15):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--replace-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "new body text"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["body"] == "new body text"

    def test_update_pr_namespace_denied(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "t",
                "--allowed-namespace", "some-other-owner",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_NAMESPACE_DENIED


class TestUpdatePrBodyMode:
    """lr-2500b7 DEFECT 2 (operator directive): --update-pr with a body
    requires an EXPLICIT --replace-body/--append-body -- there is no
    default, and none is inferred. Non-vacuity: each usage-error test here
    FAILS if the required-mode check is reverted/removed, since the call
    would then reach the (refusing) token provider and return EXIT_OK
    instead of EXIT_USAGE."""

    def test_body_stdin_without_mode_flag_is_usage_error(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "new body text"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_body_env_without_mode_flag_is_usage_error(self, repo_with_remote, monkeypatch, tmp_path):
        repo, _remote = repo_with_remote
        monkeypatch.setenv("TMPDIR", str(tmp_path))
        body_env.stage_caller_body(
            caller=verb.DEFAULT_ROLE, body_bytes=b"updated body text", target_pr=42
        )
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-env",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_USAGE

    def test_replace_body_and_append_body_together_is_usage_error(self, repo_with_remote, monkeypatch):
        """argparse's own mutually-exclusive-group enforcement fires at
        parse time, before this module's own PushUsageError checks --
        exits via SystemExit(2), argparse's own usage-error code (distinct
        from this module's EXIT_USAGE=1, still a non-zero refusal before
        any I/O)."""
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin",
                "--replace-body", "--append-body",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "new body text"}),
            monkeypatch=monkeypatch,
        )
        assert code == 2

    def test_title_only_update_pr_needs_no_mode_flag(self, repo_with_remote, monkeypatch):
        """Omitting the body entirely on --update-pr is UNCHANGED behavior
        (existing body left untouched) -- no mode flag is required when
        there is no body to apply a mode to."""
        repo, _remote = repo_with_remote

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: new title only",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_replace_body_sends_supplied_content_verbatim(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        captured = {}

        def opener(req, timeout=15):
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--replace-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "replacement body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["body"] == "replacement body"

    def test_append_body_gets_current_body_then_concatenates(self, repo_with_remote, monkeypatch):
        """Non-vacuity: if append composed the PATCH from the supplied body
        alone (no GET, no concatenation -- i.e. defect 2 reintroduced as a
        silent default-to-replace), this assertion on the PATCH payload
        would fail, since it would equal only the new text, not the joined
        string."""
        repo, _remote = repo_with_remote
        calls = []

        def opener(req, timeout=15):
            calls.append(req.get_method())
            if req.get_method() == "GET":
                return _json_resp(200, {"number": 42, "body": "original body text"})
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload)
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--append-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "follow-up note"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert calls[0] == "GET"
        patch_payload = calls[2]
        assert patch_payload["body"] == "original body text\n\nfollow-up note"

    def test_append_body_on_empty_existing_body_has_no_leading_separator(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "GET":
                return _json_resp(200, {"number": 42, "body": None})
            captured["payload"] = json.loads(req.data.decode("utf-8"))
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--append-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "first note"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["payload"]["body"] == "first note"

    def test_ahead_of_remote_tracking_warns_on_update_pr(self, repo_with_remote, monkeypatch, capsys):
        """lr-2500b7: the exact scenario the originating incident was
        misdiagnosed as -- local commits sit unpushed while --update-pr (a
        metadata-only verb) reports success. Non-vacuity: removing the
        warning call makes this assertion fail, since stderr would then
        carry no such line."""
        repo, remote = repo_with_remote
        # Simulate a branch that HAS a remote-tracking ref configured but is
        # locally ahead of it (the exact "looks pushed, isn't" shape) --
        # push a placeholder ref then advance locally without re-pushing.
        _git(["push", "-u", "origin", "feature"], repo)
        (repo / "feature.txt").write_text("more work\n")
        _git(["add", "feature.txt"], repo)
        _git(["commit", "-m", "unpushed follow-up"], repo)

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: t",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "ahead of its remote-tracking ref" in stderr
        assert "--update-pr NEVER pushes" in stderr

    def test_up_to_date_with_remote_tracking_has_no_warning(self, repo_with_remote, monkeypatch, capsys):
        repo, remote = repo_with_remote
        _git(["push", "-u", "origin", "feature"], repo)

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: t",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "ahead of its remote-tracking ref" not in stderr

    def test_append_body_github_gets_current_body_then_concatenates(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        calls = []

        def opener(req, timeout=30):
            calls.append(req.get_method())
            if req.get_method() == "GET":
                return _json_resp(200, {"number": 42, "body": "original gh body"})
            payload = json.loads(req.data.decode("utf-8"))
            calls.append(payload)
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--update-pr", "--pr", "42", "--body-stdin", "--append-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "gh follow-up"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert calls[0] == "GET"
        assert calls[2]["body"] == "original gh body\n\ngh follow-up"


class TestAheadOfRemoteTrackingWarning:
    """lr-2500b7 DEFECT 1: --update-pr never pushes (see push.verb's own
    docstring, "PR open, and the update-existing-PR path"), so a caller
    whose local branch already sits ahead of its remote-tracking ref is the
    exact situation the originating incident was misdiagnosed as -- a
    metadata-only call believed to have also landed local commits. This is
    a best-effort stderr warning, never a hard failure."""

    def test_warns_when_local_branch_ahead_of_tracking_ref(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        # Give "feature" a remote-tracking ref (push it once via a real
        # local push, no token/opener needed for a bare-repo remote), then
        # add a further LOCAL-ONLY commit so the branch is ahead of it.
        _git(["push", "-u", "origin", "feature"], repo)
        (repo / "more.txt").write_text("more work\n")
        _git(["add", "more.txt"], repo)
        _git(["commit", "-m", "more work"], repo)

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: t",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "ahead" in err
        assert "--update-pr" in err

    def test_no_warning_when_branch_in_sync_with_tracking_ref(self, repo_with_remote, monkeypatch, capsys):
        """Non-vacuity companion to the warning test above: pushing the
        branch and then NOT adding a further local commit leaves it exactly
        in sync with its tracking ref -- the warning must NOT fire here, so
        a warning that fired unconditionally (rather than only when
        genuinely ahead) would be caught by this assertion."""
        repo, _remote = repo_with_remote
        _git(["push", "-u", "origin", "feature"], repo)

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: t",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "ahead" not in err

    def test_no_warning_when_branch_has_no_upstream(self, repo_with_remote, monkeypatch, capsys):
        """The feature branch in repo_with_remote has never been pushed in
        this test (no upstream configured at all) -- the ahead-check must
        decline silently (best-effort) rather than erroring or warning."""
        repo, _remote = repo_with_remote

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: t",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "ahead" not in err


class _RepoRecordingTokenProvider:
    """TokenProvider recording (role, repo) so a test can assert push's
    already-resolved owner/repo actually reaches resolve_token (lr-ea28)."""

    def __init__(self, token: str = "tok-123"):
        self.calls: list[tuple] = []
        self._token = token

    def resolve_token(self, role: str, *, repo: str | None = None) -> str:
        self.calls.append((role, repo))
        return self._token


class TestRepoContextReachesProvider:
    """lr-ea28: push resolves owner/repo before calling resolve_token on
    both the create-PR and update-PR paths, and passes it through."""

    def test_create_pr_passes_resolved_repo(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RepoRecordingTokenProvider()
        opener = _forgejo_create_opener()

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.calls == [(verb.DEFAULT_ROLE, "some-owner/some-repo")]

    def test_update_pr_passes_resolved_repo(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RepoRecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.calls == [(verb.DEFAULT_ROLE, "some-owner/some-repo")]

    def test_github_create_pr_passes_explicit_repo_flag(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RepoRecordingTokenProvider()
        opener = _github_create_opener()

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert provider.calls == [(verb.DEFAULT_ROLE, "some-owner/some-repo")]


class TestHeadShaEnvelope:
    """lr-e36dec (superseded by lr-361de3's dual-SHA fix, see below): every
    success envelope carries a validated post-push SHA value -- the tool
    that moves HEAD (bot-identity re-authoring rewrites every commit SHA in
    base..HEAD, including HEAD itself) is the only caller that knows the
    authoritative post-rewrite value, so it must report it rather than
    leaving consumers to re-fetch a SHA captured before push.

    lr-361de3: the create path no longer carries a BARE local `head_sha`
    alongside the remote-confirmed `remote_head_sha` -- see push.verb's own
    migration note at the envelope-build call site. `remote_head_sha` (from
    the genuine `git ls-remote` readback, push.remote_readback) is now the
    SOLE SHA field these tests assert against."""

    def test_head_sha_present_and_full_length_hex_on_create(self, monkeypatch, capsys, tmp_path):
        # lr-361de3: repo_with_remote's `origin` is pushInsteadOf-redirected
        # and unresolvable via `git ls-remote` -- now that remote_head_sha is
        # the SOLE SHA this envelope reports, this needs the readback-safe
        # fixture (see _repo_with_directly_resolvable_remote's docstring).
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=1)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "head_sha" not in payload
        assert validate_sha(payload["remote_head_sha"], allow_abbreviated=False) == payload["remote_head_sha"]
        assert len(payload["remote_head_sha"]) == 40

    def test_head_sha_reflects_post_rewrite_head_when_reauthored(self, monkeypatch, capsys, tmp_path):
        """The re-author case: --bot-name/--bot-email rewrites HEAD's SHA
        before push. The emitted remote_head_sha must be the NEW
        (post-rewrite) value, never the pre-rewrite local SHA captured
        before re-authoring ran."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        pre_rewrite_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=2)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        remote_head = subprocess.run(
            ["git", "log", "-1", "--format=%H", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        ).stdout.strip()

        assert "head_sha" not in payload
        assert payload["remote_head_sha"] == remote_head
        assert payload["remote_head_sha"] != pre_rewrite_head

    def test_head_sha_absent_on_update_pr(self, repo_with_remote, monkeypatch, capsys):
        """lr-2500b7 DEFECT 1: --update-pr never pushes, so its envelope must
        NEVER carry a head_sha (previously a LOCAL `git rev-parse HEAD`
        formatted identically to the create path's genuinely-pushed remote
        fact -- a caller reasonably read that as remote state; it never was).
        Non-vacuity: reverting this fix restores a "head_sha" key here, which
        this test's absence assertion would then fail to catch -- so this
        assertion FAILS if the defect is reintroduced."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "head_sha" not in payload
        assert payload["pushed"] is False


class TestTitleGate:
    """lr-6067: push runs the SAME Conventional Commits grammar
    (merge.title_gate.check_pr_title) as the merge gate, at PR-open time —
    the PR #35 repro (a two-scope title accepted at push, only caught later
    at a downstream merge gate) is rejected here instead."""

    def test_conformant_title_accepted_at_pr_open(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=9)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat(lr-6067): run title gate at push", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_multi_scope_title_rejected_at_pr_open(self, repo_with_remote, monkeypatch):
        """The PR #35 repro: a two-parenthesised-scope title must be
        rejected HERE, before any token resolution, push, or PR-open call —
        not only later at the merge gate."""
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat(lr-273d)(lr-7a6e): two scopes", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID

    def test_multi_scope_title_error_names_offending_title_and_grammar(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat(lr-273d)(lr-7a6e): two scopes", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID
        err = capsys.readouterr().err
        assert "feat(lr-273d)(lr-7a6e): two scopes" in err
        assert "Conventional Commits grammar" in err

    def test_skip_title_check_bypasses_at_pr_open(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=11)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat(lr-273d)(lr-7a6e): two scopes", "--body-stdin",
                "--skip-title-check",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        err = capsys.readouterr().err
        assert "BYPASSED via --skip-title-check" in err

    def test_update_pr_with_invalid_title_rejected(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42",
                "--title", "feat(lr-273d)(lr-7a6e): two scopes",
            ],
            token_provider=_RefusingTokenProvider(),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_TITLE_INVALID

    def test_update_pr_with_conformant_title_accepted(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42",
                "--title", "fix(lr-6067): retitle to conform",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_update_pr_skip_title_check_bypasses(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42",
                "--title", "feat(lr-273d)(lr-7a6e): two scopes",
                "--skip-title-check",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_update_pr_body_only_unaffected_by_title_gate(self, repo_with_remote, monkeypatch):
        """--update-pr with only --body-stdin (no --title) never reaches the
        title gate -- there is no title to validate."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--replace-body",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "new body text, no title change"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK


def _repo_with_directly_resolvable_remote(tmp_path):
    """Like `repo_with_remote`, but `origin` points DIRECTLY at the local
    bare repo path -- no pushInsteadOf indirection.

    `repo_with_remote`'s own indirection exists so coordinate-PARSING tests
    (`git remote get-url origin`) see a stable, Forgejo-shaped placeholder
    URL while `git push` transparently redirects to a real local bare repo.
    The remote-READBACK tests in this module need the OPPOSITE property: a
    `git ls-remote` call (a fetch-class operation, unaffected by
    pushInsteadOf) must resolve to the SAME real repo the push landed on.
    Using `--platform github` here sidesteps the conflict entirely --
    push.verb's GitHub coordinate path takes `--repo owner/repo` directly
    and never calls `git_coords.parse_forgejo_coords` on the remote URL at
    all (see `_run_create_pr`'s `if args.platform == PLATFORM_GITHUB`
    branch), so `origin` is free to be any git-resolvable URL, including a
    bare local path -- exactly what a genuine `git ls-remote origin` needs
    to succeed without a real network call anywhere in this test suite.
    """
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(["init", "--bare", "-b", "main"], remote)

    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "author@example.com"], repo)
    _git(["config", "user.name", "Author"], repo)
    (repo / "README.md").write_text("hello\n")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "initial"], repo)
    _git(["remote", "add", "origin", str(remote)], repo)
    _git(["push", "origin", "main"], repo)

    _git(["checkout", "-b", "feature"], repo)
    (repo / "feature.txt").write_text("work\n")
    _git(["add", "feature.txt"], repo)
    # Conventional-Commits-shaped (lr-dd1742, push.branch_commit_check) --
    # see repo_with_remote's own matching comment for why.
    _git(["commit", "-m", "feat: add feature work"], repo)

    return repo, remote


class TestRemoteReadbackEnvelope:
    """lr-4e8a43: the create-PR success envelope carries a post-push remote
    readback (push.remote_readback) -- additive only, never a new failure
    mode for a caller ignoring the new fields. lr-361de3: the bare local
    head_sha field this class's docstring originally described has since
    been REMOVED (see TestHeadShaEnvelope's own updated docstring) --
    remote_head_sha (and the new stable `readback` envelope key) is now the
    sole SHA source on this path."""

    def test_remote_head_sha_present_and_matches_actual_remote(
        self, tmp_path, monkeypatch, capsys
    ):
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=201)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])

        assert payload["remote_head_sha_source"] == "git_ls_remote"
        independent_remote_read = subprocess.run(
            ["git", "log", "-1", "--format=%H", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        ).stdout.strip()
        assert payload["remote_head_sha"] == independent_remote_read
        # lr-361de3: there is no bare local head_sha field anymore -- the
        # stable `readback` envelope key mirrors remote_head_sha instead.
        assert "head_sha" not in payload
        assert payload["readback"]["verified"] is True
        assert payload["readback"]["source"] == "git_ls_remote"
        assert payload["readback"]["detail"]["remote_head_sha"] == payload["remote_head_sha"]

    def test_acceptance_lr_60fac5_scenario_is_structurally_detectable(
        self, tmp_path, monkeypatch, capsys
    ):
        """THE ACCEPTANCE CRITERION (task lr-4e8a43): three local commits,
        push never invoked, a caller reporting status ok with a locally-read
        SHA must be DETECTABLE, without depending on any agent prompt text.

        This test proves the DETECTION MECHANISM itself, at the layer this
        task actually fixes: push.remote_readback.read_remote_head refuses
        to manufacture a remote fact for a branch the remote does not have
        -- a downstream caller that tries to report success using ONLY a
        local `git rev-parse HEAD` (exactly the lr-60fac5 incident's shape:
        a build agent reported a locally-read SHA as if it were confirmed
        remote state, for a push that was never invoked) can no longer get
        a matching, verb-supplied remote_head_sha for a push that never
        happened, because no such value exists to
        fabricate one from -- read_remote_head raises rather than returning
        anything a caller could echo back as a remote fact.
        """
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        # Three additional local-only commits, mirroring the incident's
        # shape (three local commits sitting unpushed) -- feature already
        # carries one commit from the fixture, so three more makes four
        # local commits total, none of them ever pushed.
        for i in range(3):
            (repo / f"local-only-{i}.txt").write_text(f"commit {i}\n")
            _git(["add", f"local-only-{i}.txt"], repo)
            _git(["commit", "-m", f"feat: local-only commit {i}"], repo)

        local_head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(repo), capture_output=True, text=True, check=True
        ).stdout.strip()

        # The defect this reproduces: a caller reaches for its own local
        # read and asserts it as a remote fact -- exactly what the pre-fix
        # push verb's own head_sha field (still present, unchanged) would
        # let happen if nothing cross-checked it against the remote.
        from clagentic_loadout.push.remote_readback import (
            RemoteReadbackError,
            read_remote_head,
        )

        with pytest.raises(RemoteReadbackError):
            # The branch was never pushed -- the authoritative remote read
            # this task adds refuses to invent a value, structurally
            # distinguishing "the push happened" from "a caller merely read
            # its own local HEAD," with no dependence on prompt text
            # anywhere in this assertion.
            read_remote_head("origin", "feature", repo)

        # And a REAL push through the verb, for the SAME local_head, DOES
        # produce a matching, verb-supplied remote_head_sha -- confirming
        # the detection is a genuine presence/absence signal, not a
        # tautology.
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=202)
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["remote_head_sha"] == local_head

    def test_readback_failure_after_successful_push_does_not_fail_the_push(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """ADDITIVE-ONLY (task scope): a transient failure of the diagnostic
        re-read itself must never turn an already-successful push+PR-open
        into a hard failure -- that would be a NEW failure mode for every
        existing external consumer, forbidden by the task's non-negotiable
        constraints."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=203)

        # Break the readback specifically (not the push itself): rename the
        # remote out from under 'origin' the INSTANT after push succeeds is
        # hard to simulate without monkeypatching read_remote_head directly.
        monkeypatch.setattr(
            "clagentic_loadout.push.verb.read_remote_head",
            lambda *a, **k: (_ for _ in ()).throw(
                verb.RemoteReadbackError("simulated transient readback failure")
            ),
        )

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        captured = capsys.readouterr()
        payload = json.loads(captured.out.strip().splitlines()[-1])
        assert payload["remote_head_sha"] is None
        assert payload["remote_head_sha_source"] is None
        # lr-361de3: an honest "could not verify" is reported via the stable
        # `readback` key too, verified=False so a consumer's ONE predicate
        # (readback.verified is True) never mistakes this for success --
        # push itself still exits EXIT_OK (this readback failure mode stays
        # additive/non-fatal by explicit design, unlike merge/close).
        assert payload["readback"]["verified"] is False
        assert payload["readback"]["source"] == "read_unavailable"
        assert "WARNING" in captured.err

    def test_update_pr_path_has_no_remote_readback_fields(self, repo_with_remote, monkeypatch, capsys):
        """--update-pr never pushes -- there is nothing new to read back
        from the remote, and the envelope shape on this path is unchanged."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()

        def opener(req, timeout=15):
            return _json_resp(200, {})

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--title", "feat: updated title",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "remote_head_sha" not in payload


class TestRemoteAuthorshipEnvelope:
    """lr-4e8a43 task ADDITION 1: the readback asserts AUTHORSHIP, not
    merely ref-advance."""

    def test_no_bot_identity_no_authorship_fields(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=210)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "authorship_checked" not in payload
        assert "authorship_matches" not in payload

    def test_bot_identity_authorship_confirmed(self, tmp_path, monkeypatch, capsys):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=211)

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "Bot Name", "--bot-email", "bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        payload = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert payload["authorship_checked"] is True
        assert payload["authorship_matches"] is True


class TestBuilderIdentityConfigWiring:
    """lr-4e8a43 task ADDITION 2: push.identity_config.load_builder_identity
    is now actually CONSULTED by this verb (previously read only by
    doctor.checks, never by the verb that performs the re-authoring) --
    opt-in, via the EXISTING `builder_identity:` user-config section."""

    def test_unconfigured_deployment_unaffected(self, repo_with_remote, monkeypatch, tmp_path):
        """No builder_identity: section anywhere -- push behaves exactly as
        before this task (no re-authoring, original commit author lands)."""
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=220)
        empty_config_root = tmp_path / "empty-config-root"
        empty_config_root.mkdir()

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=empty_config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "author@example.com"

    def test_config_supplies_bot_identity_when_cli_flags_omitted(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=221)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "builder_identity:\n"
            "  name: Config Bot\n"
            "  email: config-bot@example.com\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "config-bot@example.com"

    def test_explicit_cli_flags_win_over_config(self, repo_with_remote, monkeypatch, tmp_path):
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=222)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "builder_identity:\n"
            "  name: Config Bot\n"
            "  email: config-bot@example.com\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--bot-name", "CLI Bot", "--bot-email", "cli-bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "cli-bot@example.com"

    def test_malformed_config_fails_closed_not_silently(self, repo_with_remote, monkeypatch, tmp_path):
        """A present-but-malformed builder_identity: section must fail loud
        -- false assurance (doctor says healthy, verb silently ignores it)
        is worse than no config at all."""
        repo, _remote = repo_with_remote
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "builder_identity:\n"
            "  name: Config Bot\n"
            # email deliberately omitted -- malformed section.
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=_RefusingTokenProvider(),
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_AUTHOR_MISMATCH


class TestCrewCallerDerivedIdentity:
    """lr-f145d2: for a caller present in this deployment's declared
    github_app.callers registry, the bot commit identity is derived
    UNCONDITIONALLY from the SAME github_app.slugs.<caller> mapping already
    used for token-minting/login resolution -- no builder_identity: config
    section required, and no ambient-git-config fallback reachable for a
    recognized crew caller. Precedence (CLI > caller-derived > config >
    none-for-a-non-crew-caller) is exercised at every level."""

    def test_caller_derives_identity_with_no_builder_identity_config(
        self, tmp_path, monkeypatch
    ):
        """Acceptance criterion 1: --caller amos, NO builder_identity:
        config, NO local git identity flags -- the pushed commit's author
        resolves to the derived bot identity, not the ambient git author."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=301)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: some-builder-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "some-builder-app[bot]@users.noreply.github.com"

    def test_recognized_crew_caller_with_unresolvable_identity_fails_closed(
        self, tmp_path, monkeypatch
    ):
        """Acceptance criterion 2: a recognized crew caller (present in
        github_app.callers) with NO resolvable slug entry FAILS rather than
        silently inheriting ambient git config."""
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_AUTHOR_MISMATCH

    def test_recognized_crew_caller_on_forgejo_pushes_successfully_no_derivation(
        self, repo_with_remote, tmp_path, monkeypatch
    ):
        """CORRECTED, NOT WEAKENED (coordinator-flagged defect): an earlier
        revision of this test asserted a recognized crew caller pushing to
        Forgejo FAILS CLOSED with EXIT_AUTHOR_MISMATCH -- that assertion
        WAS THE BUG, not a security property. github_app.callers is a
        deployment-wide registry with no platform dimension; gating
        identity-derivation only inside the resolver (which raised for any
        non-GitHub platform) meant push.verb entered tier 2 unconditionally
        for ANY recognized caller regardless of platform, then hit that
        raise on every Forgejo push -- converting the overwhelming majority
        of this deployment's actual push traffic (every internal crew push,
        including this very PR's own) into a hard, unfixable push failure.

        Forgejo has no GitHub App-bot-slug concept in this contract at all
        (see push.crew_identity's own module docstring) -- there is nothing
        to derive and nothing to mis-attribute, so the original bug (an
        operator's PERSONAL GITHUB ACCOUNT landing on public GitHub
        commits) cannot even occur here. The CORRECT behavior for a
        recognized crew caller pushing to Forgejo is byte-identical to a
        NON-crew caller pushing to Forgejo: no re-authoring, ambient git
        identity lands, exactly as before this task's fix existed at all.

        Uses a real (recording) token provider and a real forgejo opener --
        this push must actually SUCCEED, not merely avoid crashing."""
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=306)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: some-builder-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK
        assert provider.resolved_for == ["amos"]

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        # Original ambient author lands, unrewritten -- NOT the GitHub-App
        # -derived identity (that identity has no meaning on this platform).
        assert r.stdout.strip() == "author@example.com"

    def test_explicit_cli_flags_win_over_caller_derivation(self, tmp_path, monkeypatch):
        """Precedence level 1: --bot-name/--bot-email still win over a
        resolvable caller-derived identity."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=302)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: some-builder-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
                "--bot-name", "CLI Bot", "--bot-email", "cli-bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "cli-bot@example.com"

    def test_caller_derivation_wins_over_builder_identity_config(self, tmp_path, monkeypatch):
        """Precedence level 2 over level 3: caller-derivation is consulted
        BEFORE the deployment-tier builder_identity: config section, for a
        recognized crew caller -- even when both are present."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=303)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "builder_identity:\n"
            "  name: Config Bot\n"
            "  email: config-bot@example.com\n"
            "github_app:\n"
            "  slugs:\n"
            "    amos: some-builder-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "some-builder-app[bot]@users.noreply.github.com"

    def test_non_crew_caller_falls_through_to_config_tier(self, tmp_path, monkeypatch):
        """A caller string NOT present in github_app.callers is not
        recognized as a crew caller -- falls through to the
        builder_identity: config tier exactly as before this task, proving
        the registry gate (not a bare 'is github_app configured at all'
        check) is what decides tier 2 eligibility."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=304)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "builder_identity:\n"
            "  name: Config Bot\n"
            "  email: config-bot@example.com\n"
            "github_app:\n"
            "  slugs:\n"
            "    amos: some-builder-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "some-external-consumer", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "config-bot@example.com"

    def test_external_consumer_no_github_app_section_zero_behavior_change(
        self, tmp_path, monkeypatch
    ):
        """Acceptance criterion 4: a deployment with no github_app section
        at all (the out-of-the-box external-consumer state) sees ZERO
        behavior change -- no re-authoring, ambient git author lands, even
        when --caller is supplied."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener(pr_number=305)
        empty_config_root = tmp_path / "empty-config-root"
        empty_config_root.mkdir()

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=empty_config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "author@example.com"


class _StructuredProvider:
    """A TokenProvider whose resolve_token returns a ResolvedToken carrying
    a provider-verified app_slug (lr-43c8d7) -- the reference minting
    provider's `--json` shape, without importing anything from it."""

    def __init__(self, *, token: str = "tok-structured", app_slug: str | None):
        self._token = token
        self._app_slug = app_slug
        self.resolved_for: list[str] = []

    def resolve_token(self, role: str):
        from clagentic_loadout.transport.credential_provider import ResolvedToken

        self.resolved_for.append(role)
        return ResolvedToken(token=self._token, app_slug=self._app_slug)


class TestProviderVerifiedIdentityTier:
    """lr-43c8d7: a credential provider's own verified app_slug
    (transport.credential_provider.ResolvedToken.app_slug) is a precedence
    tier ABOVE github_app.slugs config -- explicit CLI > provider-verified >
    caller-derived-from-config > builder_identity > fail closed. Every test
    here uses a DIFFERENT provider slug than the config slug (non-vacuity:
    a test using the same value for both would prove nothing)."""

    def test_provider_slug_wins_over_config_slug_when_they_differ(self, tmp_path, monkeypatch):
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _StructuredProvider(app_slug="provider-verified-app")
        opener = _github_create_opener(pr_number=401)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: config-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        # provider-verified-app WINS -- not config-app.
        assert r.stdout.strip() == "provider-verified-app[bot]@users.noreply.github.com"

    def test_config_fallback_still_works_when_provider_supplies_nothing(
        self, tmp_path, monkeypatch
    ):
        """A provider that returns a bare str (no app_slug concept at all,
        e.g. StaticTokenProvider or any bring-your-own minting command that
        never opted into structured output) must fall through to config
        exactly as it did before this task."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()  # bare str, no app_slug at all
        opener = _github_create_opener(pr_number=402)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: config-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "config-app[bot]@users.noreply.github.com"

    def test_config_fallback_when_provider_reports_empty_app_slug(
        self, tmp_path, monkeypatch
    ):
        """The real, reachable gatekeeper case named in this task's own
        dispatch brief: a role with no App-slug binding configured mints
        successfully but reports an EMPTY app_slug -- config must still be
        consulted, not treated as a resolvability failure."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _StructuredProvider(app_slug="")
        opener = _github_create_opener(pr_number=403)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: config-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "config-app[bot]@users.noreply.github.com"

    def test_bare_token_provider_bot_identity_flow_completely_unaffected(
        self, repo_with_remote, monkeypatch
    ):
        """ZERO-BEHAVIOR-CHANGE GUARANTEE, explicitly tested: a deployment
        with no github_app: config at all, using a bare-token provider
        (the shape every provider had before this task), sees the exact
        same ambient-author outcome as before this feature existed."""
        repo, remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener(pr_number=404)

        code = _run_main(
            ["--repo-path", str(repo), "--platform", "forgejo", "--title", "feat: t", "--body-stdin"],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "author@example.com"

    def test_explicit_cli_flags_still_win_over_provider_verified_slug(
        self, tmp_path, monkeypatch
    ):
        """Precedence level 1 is untouched by this task: --bot-name/
        --bot-email wins even over a provider-verified slug."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _StructuredProvider(app_slug="provider-verified-app")
        opener = _github_create_opener(pr_number=405)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  slugs:\n"
            "    amos: config-app\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
                "--bot-name", "CLI Bot", "--bot-email", "cli-bot@example.com",
            ],
            token_provider=provider,
            opener=opener,
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_OK

        r = subprocess.run(
            ["git", "log", "-1", "--format=%ae", "refs/heads/feature"],
            cwd=str(remote), capture_output=True, text=True, check=True,
        )
        assert r.stdout.strip() == "cli-bot@example.com"

    def test_fail_closed_bottom_still_reachable_and_still_fails(self, tmp_path, monkeypatch):
        """The fail-closed bottom (lr-f145d2) is unchanged: a recognized
        crew caller with NO resolvable slug ANYWHERE (config has no entry,
        provider is a bare-token provider with no app_slug concept at all)
        still fails EXIT_AUTHOR_MISMATCH, never inherits ambient git
        config. Uses a bare-token provider (never even reaches the
        provider-verified override) -- the pre-existing pre-mint gate is
        what still catches this, exactly as before this task."""
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        config_root = tmp_path / "user-config-root"
        config_root.mkdir()
        (config_root / "config.yaml").write_text(
            "github_app:\n"
            "  callers:\n"
            "    - amos\n"
        )

        monkeypatch.setattr(
            "sys.stdin", io.TextIOWrapper(io.BytesIO(json.dumps({"body": "some body"}).encode()))
        )
        code = verb.main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--caller", "amos", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            builder_identity_config_root=config_root,
        )
        assert code == verb.EXIT_AUTHOR_MISMATCH


class TestKnownTrapPrCreate409:
    """task requirement 5, KNOWN TRAP: loadout-push exits non-zero even when
    the git ref moved successfully, because a redundant PR-create sub-step
    409s when the PR already exists. Documented + the failure message
    enriched (additive-only: the exit code itself is unchanged, since
    silently returning EXIT_OK for a call that did not confirm/return a PR
    number would be its own new failure mode for an existing caller)."""

    def test_409_after_successful_push_names_the_landed_remote_sha(
        self, tmp_path, monkeypatch, capsys
    ):
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)

        def opener(req, timeout=15):
            import urllib.error

            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                raise urllib.error.HTTPError(
                    req.full_url, 409, "Conflict",
                    {}, io.BytesIO(b'{"message": "pull request already exists"}'),
                )
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PR_FAILED

        # The push itself landed -- confirm independently against the real
        # local bare-repo remote, then confirm the error message actually
        # says so rather than leaving the caller to guess from exit 4 alone.
        r = subprocess.run(["git", "branch"], cwd=str(remote), capture_output=True, text=True, check=True)
        assert "feature" in r.stdout

        err = capsys.readouterr().err
        assert "already landed" in err
        assert "may mean a PR for this head/base pair already exists" in err


class TestBodyEnvUnwrapsJsonEnvelope:
    """lr-2b20d2: the actual regression coverage for the defect this task
    fixes. TestBodyEnvBuilderCaller (PR #148) drove --body-env through
    BOTH real CLI entry points (loadout-stage-body then loadout-push) but
    only asserted the round trip and --caller passthrough -- it PASSED
    while the platform-bound body was the literal JSON wrapper, because it
    never inspected the payload shape the backend actually receives. These
    tests close that gap directly: stage via the real loadout-stage-body
    CLI (the same {"body": "..."} envelope contract every reader of
    body_env-staged content shares -- review.verb, transport.git_host_api,
    and now push.verb's _read_body_env, per this task's sibling-reader
    audit), then assert the payload HANDED TO THE BACKEND OPENER equals the
    caller's original prose byte-for-byte: no wrapper, no escaped
    newlines, real newlines, and a body whose prose legitimately starts
    with a brace surviving untouched -- proving there is no content-
    sniffing heuristic, only an unconditional unwrap.

    Covers: create path and --update-pr, both backends (forgejo/github).
    """

    _BODY_WITH_NEWLINE_AND_BRACE = (
        '{"not": "actually json, just prose that starts with a brace"}\n'
        "second line after a real newline\n"
        "third line"
    )

    @staticmethod
    def _stage(monkeypatch, *, caller, create_branch=None, target_pr=None, prose):
        staged_stdin = json.dumps({"body": prose})
        monkeypatch.setattr("sys.stdin.buffer.read", lambda: staged_stdin.encode("utf-8"))
        argv = ["--caller", caller]
        if create_branch is not None:
            argv += ["--create-branch", create_branch]
        if target_pr is not None:
            argv += ["--target-pr", str(target_pr)]
        stage_rc = stage_body_verb.main(argv)
        assert stage_rc == stage_body_verb.EXIT_OK

    def test_forgejo_create_path_renders_caller_prose_verbatim(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 7})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)
        self._stage(
            monkeypatch,
            caller=verb.DEFAULT_ROLE,
            create_branch=current_branch,
            prose=self._BODY_WITH_NEWLINE_AND_BRACE,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-env",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == self._BODY_WITH_NEWLINE_AND_BRACE
        assert "\\n" not in captured["body"]
        assert '{"body":' not in captured["body"]

    def test_forgejo_update_pr_renders_caller_prose_verbatim(
        self, repo_with_remote, monkeypatch, tmp_path
    ):
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=15):
            if req.get_method() == "PATCH":
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(200, {"number": 42})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(
            monkeypatch,
            caller=verb.DEFAULT_ROLE,
            target_pr=42,
            prose=self._BODY_WITH_NEWLINE_AND_BRACE,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-env", "--replace-body",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == self._BODY_WITH_NEWLINE_AND_BRACE
        assert "\\n" not in captured["body"]
        assert '{"body":' not in captured["body"]

    def test_github_create_path_renders_caller_prose_verbatim(
        self, tmp_path, monkeypatch
    ):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=30):
            if req.get_method() == "POST" and req.full_url.endswith("/pulls"):
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(201, {"number": 7})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        current_branch = verb.git_coords.current_branch(repo)
        self._stage(
            monkeypatch,
            caller=verb.DEFAULT_ROLE,
            create_branch=current_branch,
            prose=self._BODY_WITH_NEWLINE_AND_BRACE,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo", "--title", "feat: t", "--body-env",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == self._BODY_WITH_NEWLINE_AND_BRACE
        assert "\\n" not in captured["body"]
        assert '{"body":' not in captured["body"]

    def test_github_update_pr_renders_caller_prose_verbatim(
        self, tmp_path, monkeypatch
    ):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        captured = {}

        def opener(req, timeout=30):
            if req.get_method() == "PATCH":
                captured["body"] = json.loads(req.data.decode("utf-8"))["body"]
                return _json_resp(200, {"number": 42})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        monkeypatch.setenv("TMPDIR", str(tmp_path))
        self._stage(
            monkeypatch,
            caller=verb.DEFAULT_ROLE,
            target_pr=42,
            prose=self._BODY_WITH_NEWLINE_AND_BRACE,
        )

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--update-pr", "--pr", "42", "--body-env", "--replace-body",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        assert captured["body"] == self._BODY_WITH_NEWLINE_AND_BRACE
        assert "\\n" not in captured["body"]


class TestBranchCommitCheck:
    """Push-time backstop against a branch carrying a stray, not-yet-landed
    merge commit from another PR (lr-dd1742) -- CLI wiring over
    push.branch_commit_check.

    Tests exercising a genuinely REACHABLE `git fetch` use
    `_repo_with_directly_resolvable_remote` (--platform github, `origin`
    pointed directly at the real local bare repo -- see that helper's own
    docstring for why `repo_with_remote`'s pushInsteadOf-indirected fixture
    cannot support a real fetch). Tests that never reach the fetch at all
    (skip=True, a non-'merge' --merge-method) use the simpler
    `repo_with_remote` fixture, matching every other gate's own test
    convention in this file."""

    def test_stray_merge_commit_blocks_before_push(self, tmp_path, monkeypatch):
        # The gate runs AFTER token resolution/bot-identity re-authoring,
        # same ordering as the pre-existing cleanliness check
        # (_run_cleanliness_check) it sits beside -- a valid token provider
        # is expected to be consulted regardless of this gate's outcome.
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        _git(
            ["commit", "--allow-empty", "-m",
             "Merge pull request #377 from clagentic/fix/lr-f22787-x"],
            repo,
        )
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_STRAY_MERGE_COMMIT
        # Nothing was pushed -- the remote's main tip is unchanged and no
        # 'feature' ref exists there at all.
        refs = _git(["ls-remote", str(remote)], repo).stdout
        assert "refs/heads/feature" not in refs

    def test_stray_merge_commit_message_names_sha_subject_and_remediation(
        self, tmp_path, monkeypatch, capsys
    ):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        _git(
            ["commit", "--allow-empty", "-m",
             "Merge pull request #378 from clagentic/fix/lr-f969fc-y"],
            repo,
        )
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_STRAY_MERGE_COMMIT
        stderr = capsys.readouterr().err
        assert "Merge pull request #378 from clagentic/fix/lr-f969fc-y" in stderr
        assert "git fetch origin main" in stderr
        assert "git rebase origin/main" in stderr
        assert "--skip-branch-commit-check" in stderr

    def test_clean_branch_is_unaffected(self, tmp_path, monkeypatch):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_skip_flag_bypasses_even_a_stray_commit(self, repo_with_remote, monkeypatch):
        repo, _remote = repo_with_remote
        _git(
            ["commit", "--allow-empty", "-m", "Merge pull request #1 from x/y"],
            repo,
        )
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--skip-branch-commit-check",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_non_merge_merge_method_is_unaffected_by_stray_commit(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        _git(
            ["commit", "--allow-empty", "-m", "Merge pull request #1 from x/y"],
            repo,
        )
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
                "--merge-method", "squash",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_check_execution_failure_is_a_soft_fail_not_a_block(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """CommitCheckUnavailableError (the check's OWN execution failing)
        must never block a push that would otherwise be clean -- mirrors
        _run_cleanliness_check's own CleanlinessCheckError handling.
        `repo_with_remote`'s `origin` is a neutral, non-resolving
        placeholder host (pushInsteadOf redirects the PUSH transport only,
        never `git fetch` -- see that fixture's own docstring), so this
        module's own `git fetch origin <base>` fails fast (DNS resolution
        failure, no real network access) regardless of --base -- exactly
        the check-execution failure this test exercises."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "branch commit-subject check could not run" in stderr


#: Synthetic pattern, per CLAUDE.md rule 6 conformance -- no test in this
#: class depends on the real internal lr-XXXXXX task-id shape.
_SYNTHETIC_GUARD_PATTERN = r"\bWIDGET-\d+\b"


def _write_task_id_guard_config(repo, *, pattern: str, mode: str | None = None) -> None:
    import yaml

    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    push_section: dict = {"task_id_guard_pattern": pattern}
    if mode is not None:
        push_section["task_id_guard_mode"] = mode
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"push": push_section}), encoding="utf-8"
    )


class TestTaskIdGuard:
    """Config-driven refusal of an internal work-item identifier reaching a
    PR title or a branch commit subject (lr-4005f5, task_id_guard) -- CLI
    wiring over clagentic_loadout.task_id_guard.

    NO-OP BY DEFAULT (hard acceptance criterion): with no
    `push.task_id_guard_pattern` configured, this guard must never affect
    behavior -- proven directly by every OTHER test in this module (none of
    them configure the pattern, and all pass or fail exactly as they did
    before this guard existed)."""

    def test_no_pattern_configured_title_with_matching_shape_is_unaffected(
        self, repo_with_remote, monkeypatch
    ):
        """A title that WOULD match the synthetic pattern, on a repo with NO
        task_id_guard_pattern configured at all, must pass -- byte-identical
        behavior to before this guard existed."""
        repo, _remote = repo_with_remote
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: fix WIDGET-42 leak", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_configured_pattern_matching_title_blocks_by_default(
        self, repo_with_remote, monkeypatch
    ):
        """Operator-pinned default: once a pattern IS configured, mode
        defaults to block -- no explicit task_id_guard_mode needed."""
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: fix WIDGET-42 leak", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION

    def test_violation_message_names_field_value_and_config_key(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: fix WIDGET-42 leak", "--body-stdin",
            ],
            token_provider=_RefusingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION
        stderr = capsys.readouterr().err
        assert "WIDGET-42" in stderr
        assert "PR title" in stderr
        assert "task_id_guard_pattern" in stderr
        assert "task_id_guard_mode" in stderr

    def test_configured_pattern_non_matching_title_passes(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: add a thing", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_warn_mode_proceeds_and_prints_warning(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN, mode="warn")
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: fix WIDGET-42 leak", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "WIDGET-42" in stderr

    def test_off_mode_proceeds_silently_despite_configured_pattern(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN, mode="off")
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: fix WIDGET-42 leak", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "WIDGET-42" not in stderr

    def test_matching_branch_commit_subject_blocks_by_default(self, tmp_path, monkeypatch):
        """Acceptance criteria: a branch commit subject matching the
        configured pattern refuses the push -- exercised on a fetch-reachable
        repo (branch commit subjects are read via a real `git fetch`+`git
        log`, mirroring TestBranchCommitCheck's own fixture choice)."""
        repo, remote = _repo_with_directly_resolvable_remote(tmp_path)
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        _git(
            ["commit", "--allow-empty", "-m", "fix: address WIDGET-42 leak"],
            repo,
        )
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_TASK_ID_GUARD_VIOLATION
        # Nothing was pushed.
        refs = _git(["ls-remote", str(remote)], repo).stdout
        assert "refs/heads/feature" not in refs

    def test_non_matching_branch_commit_subjects_pass(self, tmp_path, monkeypatch):
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        provider = _RecordingTokenProvider()
        opener = _github_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--title", "feat: t", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_task_trailer_in_body_is_never_inspected(self, repo_with_remote, monkeypatch):
        """The PR body's `Task: <id>` trailer is out of scope for this guard
        by construction -- it never inspects a PR body at all, only a title
        or a commit subject."""
        repo, _remote = repo_with_remote
        _write_task_id_guard_config(repo, pattern=_SYNTHETIC_GUARD_PATTERN)
        provider = _RecordingTokenProvider()
        opener = _forgejo_create_opener()
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: add a thing", "--body-stdin",
            ],
            token_provider=provider,
            opener=opener,
            stdin_text=json.dumps({"body": "Task: WIDGET-42\n"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK


class TestDryRunAndVerbose:
    """--dry-run and --verbose/--trace (lr-68039e): a sanctioned diagnostic
    affordance so a caller with a failing push never needs to shell out to
    raw git under an ambient credential to see the full transcript."""

    def test_dry_run_help_documents_both_flags_and_the_env_var(self, monkeypatch, capsys):
        code = _run_main(["--help"], token_provider=_RefusingTokenProvider(), monkeypatch=monkeypatch)
        assert code == verb.EXIT_OK
        out = " ".join(capsys.readouterr().out.split())
        assert "--dry-run" in out
        assert "--verbose" in out
        assert "--trace" in out
        assert "CLAGENTIC_LOADOUT_PUSH_GIT_TRACE" in out

    def test_dry_run_updates_no_ref_and_exits_ok_without_opening_a_pr(
        self, repo_with_remote, monkeypatch
    ):
        repo, remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--dry-run",
            ],
            # A PR-open call would be a hard failure if reached -- --dry-run
            # must never call create_pr at all (nothing was pushed).
            token_provider=_RecordingTokenProvider(),
            opener=lambda req, timeout=15: (_ for _ in ()).throw(
                AssertionError("PR-open must not be called on --dry-run")
            ),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        refs = _git(["ls-remote", str(remote)], repo).stdout
        assert "refs/heads/feature" not in refs

    def test_dry_run_prints_transcript_to_stderr(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--dry-run",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        stderr = capsys.readouterr().err
        assert "--dry-run transcript" in stderr
        assert "--dry-run complete" in stderr

    def test_dry_run_never_leaks_the_minted_token(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        secret_token = "sk-verb-dry-run-secret-should-never-leak"
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--dry-run",
            ],
            token_provider=_RecordingTokenProvider(token=secret_token),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
        captured = capsys.readouterr()
        assert secret_token not in captured.out
        assert secret_token not in captured.err

    def test_verbose_flag_never_leaks_the_minted_token_on_a_failed_push(
        self, repo_with_remote, monkeypatch, capsys
    ):
        """--verbose triggers the GIT_TRACE passthrough, which dumps far
        more subprocess detail than the default -- the token-leak
        invariant must hold across it (task requirement 3)."""
        repo, _remote = repo_with_remote
        secret_token = "sk-verb-verbose-secret-should-never-leak"
        # Break ONLY the push transport (pushurl, a real bare-repo path this
        # fixture already relies on) while leaving `git remote get-url
        # origin` (coordinate parsing) intact -- a bogus, guaranteed-
        # unreachable pushurl fails the actual `git push` fast and
        # deterministically, with no real network access, exercising the
        # verbose/trace surface on the failure path.
        _git(["config", "remote.origin.pushurl", "/nonexistent/push/target.git"], repo)
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--verbose",
            ],
            token_provider=_RecordingTokenProvider(token=secret_token),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_PUSH_FAILED
        captured = capsys.readouterr()
        assert secret_token not in captured.out
        assert secret_token not in captured.err

    def test_trace_alias_is_accepted_as_a_synonym_for_verbose(self, repo_with_remote, monkeypatch):
        """--trace is documented as the SAME flag as --verbose (both set
        the identical dest), not a second, divergent mechanism."""
        repo, _remote = repo_with_remote
        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--title", "feat: t", "--body-stdin", "--trace", "--dry-run",
            ],
            token_provider=_RecordingTokenProvider(),
            stdin_text=json.dumps({"body": "some body"}),
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK

    def test_dry_run_ignored_on_update_pr_which_never_pushes(self, tmp_path, monkeypatch):
        """--dry-run has no effect on --update-pr -- that path never calls
        git_push_with_token at all (metadata-only PATCH), so --dry-run is
        simply unused there rather than raising a usage error."""
        repo, _remote = _repo_with_directly_resolvable_remote(tmp_path)
        provider = _RecordingTokenProvider()

        def opener(req, timeout=30):
            if req.get_method() == "PATCH":
                return _json_resp(200, {"number": 42})
            raise AssertionError(f"unexpected: {req.get_method()} {req.full_url}")

        code = _run_main(
            [
                "--repo-path", str(repo), "--platform", "github",
                "--repo", "some-owner/some-repo",
                "--update-pr", "--pr", "42", "--title", "feat: new title",
                "--dry-run",
            ],
            token_provider=provider,
            opener=opener,
            monkeypatch=monkeypatch,
        )
        assert code == verb.EXIT_OK
