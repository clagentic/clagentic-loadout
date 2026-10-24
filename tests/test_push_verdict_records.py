"""loadout-push leaves a verdict record for each push.verify entry it ran, bound
to the head that was actually pushed (read back from the remote) or, on the
update path, to the PR head the checkout was proven to be."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest
import yaml

from clagentic_loadout.push import verb
from clagentic_loadout.push.remote_readback import RemoteReadback
from clagentic_loadout.verdict_record import verb as record_verb
from clagentic_loadout.verdict_record.store import load_record, records_at_head, records_root
from tests._support.push_verb import (  # noqa: F401, F811  (fixtures reused, not redefined)
    RecordingTokenProvider as _RecordingTokenProvider,
    forgejo_create_opener as _forgejo_create_opener,
    isolate_user_config_root,
    json_resp as _json_resp,
    repo_with_remote,
    run_main as _run_main,
)

PY = sys.executable


def _write_verify(repo, entries) -> None:
    config_dir = repo / ".clagentic" / "loadout"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump({"push": {"verify": entries}}), encoding="utf-8"
    )
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    exclude.write_text(".clagentic/\n", encoding="utf-8")


def _entry(name, code="pass"):
    return {"name": name, "argv": [PY, "-c", code], "timeout_seconds": 30}


def _git(repo, *args) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _readback_from_the_bare_remote(repo_with_remote, monkeypatch):
    """The fixture's `origin` URL is a placeholder host that cannot be reached,
    so the real readback (a `git ls-remote` against it) could not confirm
    anything. Read the branch from the bare repository the push really went to,
    still with a genuine `git ls-remote`."""
    _repo, remote = repo_with_remote

    def read(remote_name, branch, project_root, token=None):
        listed = subprocess.run(
            ["git", "ls-remote", str(remote), f"refs/heads/{branch}"],
            capture_output=True, text=True, check=True,
        ).stdout.split()
        return RemoteReadback(remote_head_sha=listed[0], remote=remote_name, ref=f"refs/heads/{branch}")

    monkeypatch.setattr(verb, "read_remote_head", read)


def _create(repo, monkeypatch, *extra):
    return _run_main(
        [
            "--repo-path", str(repo), "--platform", "forgejo",
            "--title", "feat: t", "--body-stdin", *extra,
        ],
        token_provider=_RecordingTokenProvider(),
        opener=_forgejo_create_opener(),
        stdin_text=json.dumps({"body": "some body"}),
        monkeypatch=monkeypatch,
    )


def _envelope(capsys) -> dict:
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


class TestCreatePath:
    def test_a_completed_run_leaves_a_record_for_the_pushed_head(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        _write_verify(repo, [_entry("unit"), _entry("lint")])

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        envelope = _envelope(capsys)
        pushed = _git(remote, "rev-parse", "refs/heads/feature")
        assert envelope["remote_head_sha"] == pushed
        found = records_at_head(records_root(), pushed)
        assert sorted(r.gate for r in found.records) == ["push.verify:lint", "push.verify:unit"]
        assert sorted(envelope["verdict_records"]) == sorted(r.id for r in found.records)
        for record in found.records:
            assert record.head_sha == pushed
            assert record.tree_sha == _git(repo, "rev-parse", f"{pushed}^{{tree}}")
            assert record.tool == "loadout-push" and record.exit == 0
            assert record.repo == "some-owner/some-repo"
            assert record.started <= record.finished

    def test_the_record_is_readable_by_id(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        assert _create(repo, monkeypatch) == verb.EXIT_OK
        (record_id,) = _envelope(capsys)["verdict_records"]

        assert record_verb.main(["show", record_id]) == record_verb.EXIT_OK
        shown = json.loads(capsys.readouterr().out)
        assert shown["gate"] == "push.verify:unit"
        assert shown["head_sha"] == _git(repo, "rev-parse", "HEAD")
        assert shown["exit"] == 0 and shown["tool"] == "loadout-push"
        assert shown["started"] and shown["finished"]

    def test_the_record_names_the_head_after_commits_were_re_authored(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        before = _git(repo, "rev-parse", "HEAD")
        _write_verify(repo, [_entry("unit")])

        code = _create(
            repo, monkeypatch, "--bot-name", "Bot", "--bot-email", "bot@example.com",
            "--require-bot-identity",
        )

        assert code == verb.EXIT_OK
        pushed = _git(remote, "rev-parse", "refs/heads/feature")
        assert pushed != before
        assert records_at_head(records_root(), before).records == ()
        (record,) = records_at_head(records_root(), pushed).records
        assert record.head_sha == pushed
        assert _envelope(capsys)["remote_head_sha"] == pushed

    def test_no_record_when_the_remote_head_could_not_be_read_back(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        monkeypatch.setattr(
            verb, "_perform_remote_readback", lambda **kwargs: {"remote_head_sha": None}
        )

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        captured = capsys.readouterr()
        assert "verdict_records" not in json.loads(captured.out.strip().splitlines()[-1])
        head = _git(repo, "rev-parse", "HEAD")
        assert records_at_head(records_root(), head).records == ()
        assert "no verdict record was written" in captured.err

    def test_no_record_when_the_remote_holds_another_commit(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        other = "9" * 40
        monkeypatch.setattr(
            verb, "_perform_remote_readback", lambda **kwargs: {"remote_head_sha": other}
        )

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert records_at_head(records_root(), other).records == ()
        assert records_at_head(records_root(), _git(repo, "rev-parse", "HEAD")).records == ()

    def test_a_failing_run_pushes_nothing_and_records_nothing(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit", "raise SystemExit(1)")])
        assert _create(repo, monkeypatch) == verb.EXIT_VERIFY_FAILED
        assert records_at_head(records_root(), _git(repo, "rev-parse", "HEAD")).records == ()

    def test_skip_verify_leaves_no_record(self, repo_with_remote, monkeypatch, capsys):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        assert _create(repo, monkeypatch, "--skip-verify") == verb.EXIT_OK
        assert "verdict_records" not in _envelope(capsys)

    def test_a_repo_without_push_verify_has_an_unchanged_envelope(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        assert _create(repo, monkeypatch) == verb.EXIT_OK
        assert "verdict_records" not in _envelope(capsys)

    def test_a_store_that_cannot_be_written_warns_and_does_not_fail_the_push(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        # Inside the checkout being pushed, which the store refuses.
        monkeypatch.setenv("CLAGENTIC_LOADOUT_VERDICT_RECORDS_DIR", str(repo / "records"))

        assert _create(repo, monkeypatch) == verb.EXIT_OK

        assert _git(remote, "rev-parse", "refs/heads/feature")
        captured = capsys.readouterr()
        assert "verdict_records" not in json.loads(captured.out.strip().splitlines()[-1])
        assert "no verdict record was written" in captured.err
        assert "the pull request under review can write" in captured.err


class TestUpdatePath:
    def _opener(self, head_sha, sent):
        def opener(req, timeout=15):
            if req.get_method() == "GET":
                return _json_resp(200, {"body": "existing body", "head": {"sha": head_sha}})
            sent.append(json.loads(req.data.decode("utf-8")))
            return _json_resp(200, {})

        return opener

    def _update(self, repo, monkeypatch, head_sha, sent):
        return _run_main(
            [
                "--repo-path", str(repo), "--platform", "forgejo",
                "--update-pr", "--pr", "42", "--body-stdin", "--append-body",
            ],
            token_provider=_RecordingTokenProvider(),
            opener=self._opener(head_sha, sent),
            stdin_text=json.dumps({"body": "re-measured"}),
            monkeypatch=monkeypatch,
        )

    def test_re_measuring_a_pushed_head_with_no_new_commit_leaves_records(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        head = _git(repo, "rev-parse", "HEAD")
        _write_verify(repo, [_entry("unit")])
        sent: list = []

        assert self._update(repo, monkeypatch, head, sent) == verb.EXIT_OK

        result = _envelope(capsys)
        assert result["verification"] == "ran" and result["pushed"] is False
        (record,) = records_at_head(records_root(), head).records
        assert record.gate == "push.verify:unit" and record.exit == 0
        assert result["verdict_records"] == [record.id]
        assert load_record(records_root(), record.id) == record

    def test_a_failing_re_measurement_is_recorded_and_refused(
        self, repo_with_remote, monkeypatch
    ):
        repo, _remote = repo_with_remote
        head = _git(repo, "rev-parse", "HEAD")
        _write_verify(repo, [_entry("unit", "raise SystemExit(4)")])
        sent: list = []

        assert self._update(repo, monkeypatch, head, sent) == verb.EXIT_VERIFY_FAILED

        (record,) = records_at_head(records_root(), head).records
        assert record.exit == 4
        assert sent == []

    def test_a_checkout_that_is_not_the_pr_head_records_nothing(
        self, repo_with_remote, monkeypatch, capsys
    ):
        repo, _remote = repo_with_remote
        _write_verify(repo, [_entry("unit")])
        sent: list = []

        assert self._update(repo, monkeypatch, "b" * 40, sent) == verb.EXIT_OK

        assert records_at_head(records_root(), "b" * 40).records == ()
        assert "verdict_records" not in _envelope(capsys)
