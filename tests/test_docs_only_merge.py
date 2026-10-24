"""The merge verb and the docs-only waiver: the required gates are exactly what they
are for any review, and an accepted docs-only justification is shown as one audited
line per reviewer, in the merge output and in the merge-completion attestation.

Every case runs the real verb with an injected host opener and a real git
repository, because the gate reads its policy from the PR's base commit. Fences are
built with the product's own fence builder."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import verb
from clagentic_loadout.merge.fence_state import docs_only_waiver_line
from clagentic_loadout.transport import provider_config
from tests._review_evaluate_support import facts
from tests._ruling_support import HEAD, NAME
from tests._ruling_support import fence as _fence
from tests._ruling_support import gate_repo as _repo
from tests._ruling_support import tip as _tip
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

LOGIN = "reviewer-login"
OTHER_NAME, OTHER_LOGIN = "other-reviewer", "other-login"
JUSTIFICATION = "Only wording changed; no tests are needed."
SECRET = facts(
    category="secrets_exposure", confidentiality="full", integrity="full", availability="full"
)


def _docs_only(*, accepted=True, base="a" * 40, **overrides):
    value = {
        "classification": "docs-only",
        "allowlist_source": {"base_sha": base, "path": ".clagentic/loadout/profile.yaml"},
        "justification": JUSTIFICATION,
        "spec_hash": "e" * 64,
        "accepted": accepted,
        "rejection_reason": None if accepted else "behaviour is documented here",
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def _isolate_user_config_root(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _comment(comment_id, body, login=LOGIN):
    return {"id": comment_id, "user": {"login": login}, "body": body}


class _Run:
    def __init__(self, code, err, out, posted):
        self.code, self.err, self.out, self.posted = code, err, out, posted


def _merge(comments, repo, capsys, *, roles=((NAME, LOGIN),), extra=()):
    posted: list[str] = []
    info = {
        "head": {"sha": HEAD}, "title": "feat: x",
        "base": {"ref": "main", "sha": _tip(repo), "repo": {"private": True}},
    }
    inner = make_opener(pr_info=info, comments=comments)

    def opener(req, timeout=15):
        if req.get_method() == "POST" and "/comments" in req.full_url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return inner(req, timeout)

    argv = base_args(**{"--repo-path": str(repo)})
    for name, login in roles:
        argv += ["--required-reviewer", f"{name}:{login}"]
    capsys.readouterr()
    code = verb.main(
        [*argv, *extra],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return _Run(code, captured.err, captured.out, posted)


def _accepted_fence(repo, **more):
    return _fence("CLEAN", findings=[], base=_tip(repo), docs_only=_docs_only(), **more)


class TestWaiverIsShownOnce:
    def test_an_accepted_justification_passes_and_is_shown_in_the_output_and_attestation(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)

        run = _merge([_comment(1, _accepted_fence(repo))], repo, capsys)

        assert run.code == verb.EXIT_OK
        waiver_lines = [line for line in run.err.splitlines() if "WAIVER" in line]
        assert len(waiver_lines) == 1
        assert JUSTIFICATION in waiver_lines[0] and NAME in waiver_lines[0]
        assert "docs-only review waiver accepted" in waiver_lines[0]
        [attestation] = run.posted
        assert "| Docs-only waivers |" in attestation
        assert JUSTIFICATION in attestation
        result = json.loads(run.out.strip().splitlines()[-1])
        [evaluation] = result["verdict_evaluations"]
        assert waiver_lines[0].endswith(evaluation["docs_only_waiver"])

    def test_a_report_only_merge_shows_the_waiver_too(self, tmp_path, capsys):
        repo = _repo(tmp_path)

        run = _merge([_comment(1, _accepted_fence(repo))], repo, capsys, extra=["--report-only"])

        assert run.code == verb.EXIT_OK
        assert "report-only -- waiver" in run.out and JUSTIFICATION in run.out
        assert run.posted == []

    def test_a_fence_with_no_docs_only_object_shows_no_waiver(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        fence = _fence("CLEAN", findings=[], base=_tip(repo))

        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_OK
        assert "WAIVER" not in run.err
        assert "Docs-only waivers" not in run.posted[0]

    def test_one_line_per_reviewer_that_accepted(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        comments = [
            _comment(1, _accepted_fence(repo)),
            _comment(2, _accepted_fence(repo).replace(NAME, OTHER_NAME), login=OTHER_LOGIN),
        ]

        run = _merge(comments, repo, capsys, roles=((NAME, LOGIN), (OTHER_NAME, OTHER_LOGIN)))

        assert run.code == verb.EXIT_OK
        assert len([line for line in run.err.splitlines() if "WAIVER" in line]) == 2


class TestRequiredGatesAreUnchanged:
    def test_a_docs_only_pull_request_still_refuses_when_a_required_reviewer_has_no_verdict(
        self, tmp_path, capsys
    ):
        repo = _repo(tmp_path)

        run = _merge(
            [_comment(1, _accepted_fence(repo))],
            repo,
            capsys,
            roles=((NAME, LOGIN), (OTHER_NAME, OTHER_LOGIN)),
        )

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert OTHER_LOGIN in run.err
        assert run.posted == []

    def test_a_rejected_justification_with_a_blocking_finding_still_blocks_and_shows_no_waiver(
        self, tmp_path, capsys
    ):
        from tests._ruling_support import evaluated_finding

        repo = _repo(tmp_path)
        finding = evaluated_finding(fingerprint=None)
        fence = _fence(
            "BLOCKED", findings=[finding], base=_tip(repo), docs_only=_docs_only(accepted=False)
        )

        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "WAIVER" not in run.err

    def test_a_secret_finding_blocks_a_merge_even_on_a_fence_that_took_the_lighter_path(
        self, tmp_path, capsys
    ):
        from tests._ruling_support import evaluated_finding

        repo = _repo(tmp_path)
        secret = evaluated_finding(SECRET, fingerprint=None)
        fence = _fence("BLOCKED", findings=[secret], base=_tip(repo), docs_only=_docs_only())

        run = _merge([_comment(1, fence)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED

    def test_a_stale_head_is_still_refused(self, tmp_path, capsys):
        repo = _repo(tmp_path)
        stale = _fence(
            "CLEAN", findings=[], base=_tip(repo), docs_only=_docs_only(), head="c" * 40
        )

        run = _merge([_comment(1, stale)], repo, capsys)

        assert run.code == verb.EXIT_GATE_RESULT_BLOCKED
        assert "WAIVER" not in run.err


class TestWaiverLine:
    def test_it_names_the_reviewer_the_source_and_the_justification(self):
        line = docs_only_waiver_line("some-reviewer", _docs_only(base="a" * 40))

        assert line.startswith("some-reviewer: docs-only review waiver accepted")
        assert "aaaaaaaaaaaa" in line and ".clagentic/loadout/profile.yaml" in line
        assert JUSTIFICATION in line and "eeeeeeeeeeee" in line

    def test_a_rejection_has_no_waiver(self):
        assert docs_only_waiver_line("r", _docs_only(accepted=False)) is None

    def test_a_long_multi_line_justification_is_shown_on_one_bounded_line(self):
        line = docs_only_waiver_line("r", _docs_only(justification="word " * 200 + "\nend"))

        assert "\n" not in line and line.endswith("...")
