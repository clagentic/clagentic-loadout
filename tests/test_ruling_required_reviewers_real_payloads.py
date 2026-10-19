"""loadout-ruling and loadout-merge resolve the required reviewer roles the same
way, driven through host payloads captured from a real GitHub pull request and a
real Forgejo pull request.

The fixtures under ``tests/real_payloads`` are the pull request object and the
issue comments two reviewers posted on it (a BLOCKED fence carrying one evaluated
finding, and a CLEAN fence), read through the host APIs and then sanitized: the
repository, accounts and app slugs are replaced by invented names, avatar and
other link fields are dropped, and the reviewer names and rule ids inside the
fence text are replaced to match. Fence JSON, comment ids, fingerprints, timestamps
and SHAs are as the host served them. The finding id in ``findings_open`` is a
digest of the finding's file, line and rule id, so it was recomputed for the
renamed rule. Two further substitutions let a capture taken after the fact be
replayed: the Forgejo pull request is served in the state it had when the fence was
posted (open, with the head and base it then had), and the merge tests swap in the
base SHA of a local repository, since the gate reads the tracked gate file from a
real commit.

Each case runs the real verbs: ``loadout-ruling record`` against a fake host that
serves the capture, then ``loadout-merge`` and ``loadout-merge --report-only``
against the same capture and a real local repository."""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review import ruling_verb as ruling
from clagentic_loadout.transport import provider_config
from clagentic_loadout.transport.attestation import SOURCE_CONFIGURED, Identity
from tests._gate_repo import git, init_gate_repo
from tests._ruling_host import host_opener
from tests._ruling_support import REPO, TODAY, registry_dir
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)
from tests._support.unresolvable_roles import make_roles_unresolvable
from tests.test_anonymization_guard import PRODUCT_CODE_PATTERNS
from tests.test_merge_gate_rulings import _PLATFORMS

PAYLOADS = Path(__file__).parent / "real_payloads"
BLOCKED_ROLE = "reviewer-one"
CLEAN_ROLE = "reviewer-two"


@dataclass(frozen=True)
class Capture:
    platform: str
    pull_file: str
    comments_file: str
    pr: int
    blocked_comment: int
    fingerprint: str
    path: str
    line: int

    @property
    def pull(self) -> dict:
        return json.loads((PAYLOADS / self.pull_file).read_text(encoding="utf-8"))

    @property
    def comments(self) -> list[dict]:
        return json.loads((PAYLOADS / self.comments_file).read_text(encoding="utf-8"))


CAPTURES = {
    "github": Capture(
        "github", "github_pull.json", "github_comments.json", 91, 6268862568,
        "fp2:8a719de30549e9be", "src/clagentic_loadout/review/ruling_verb.py", 412,
    ),
    "forgejo": Capture(
        "forgejo", "forgejo_pull.json", "forgejo_comments.json", 707, 22247,
        "fp2:6e26e39c405b6002", "scripts/install-runtime.sh", 432,
    ),
}


@dataclass(frozen=True)
class Roster:
    """Where the merge's required roles come from: the tracked gate file at the
    base (``gate_roles``; None means the repository has no gate file) and the
    caller's own ``--required-reviewer`` values (``flags``)."""

    gate_roles: tuple[str, ...] | None
    flags: tuple[str, ...]

    def argv(self) -> list[str]:
        return [arg for role in self.flags for arg in ("--required-reviewer", role)]


ROSTERS = {
    "flag-only-no-gate-file": Roster(None, (BLOCKED_ROLE,)),
    "flag-only-gate-file-without-roles": Roster((), (BLOCKED_ROLE,)),
    "gate-only": Roster((BLOCKED_ROLE,), ()),
    "gate-and-flag": Roster((CLEAN_ROLE,), (BLOCKED_ROLE,)),
}

_PARAM_CAPTURES = pytest.mark.parametrize("capture", list(CAPTURES.values()), ids=list(CAPTURES))
_PARAM_ROSTERS = pytest.mark.parametrize("roster", list(ROSTERS.values()), ids=list(ROSTERS))
_PARAM_MODES = pytest.mark.parametrize(
    "mode", [[], ["--report-only"]], ids=["merge", "report-only"]
)


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")
    monkeypatch.setattr(
        "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
        lambda **kwargs: f"{kwargs['caller']}-app",
    )


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = registry_dir(tmp_path) / "rulings.yaml"
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))
    return path


def _record(capture: Capture, roster: Roster, *extra: str, gate: str | None = None):
    identity = Identity("merger", SOURCE_CONFIGURED)
    host = {"with_gate": roster.gate_roles is not None}
    if gate is not None:
        host["gate"] = gate
    elif roster.gate_roles is not None:
        host["gate_roles"] = roster.gate_roles
    argv = [
        "--role", "merger", "record",
        "--platform", capture.platform, "--repo", REPO, "--pr", str(capture.pr),
        "--fence-comment", str(capture.blocked_comment), "--fingerprint", capture.fingerprint,
        "--kind", "false_positive", "--rationale", "not a defect",
        "--requested-by", "some-caller", "--source-ref", "design note 4",
        *roster.argv(), *extra,
    ]
    return ruling.main(
        argv,
        token_provider=RecordingTokenProvider(),
        opener=host_opener(capture.comments, pr_info=capture.pull, **host),
        identity_provider=lambda: identity,
        today=TODAY,
    )


def _local_repo(tmp_path: Path, roster: Roster, *, gate: dict | None = None) -> Path:
    section = gate if gate is not None else (
        None if roster.gate_roles is None else {"required_reviewer_roles": list(roster.gate_roles)}
    )
    init_gate_repo(tmp_path / "repo", tracked_gate=section)
    return tmp_path / "repo"


def _merge(capture: Capture, roster: Roster, repo: Path, mode, capsys):
    pull = copy.deepcopy(capture.pull)
    pull["base"]["sha"] = git(repo, "rev-parse", "main")
    argv = base_args(
        **{"--platform": capture.platform, "--repo-path": str(repo), "--pr": capture.pr}
    )
    capsys.readouterr()
    code = merge_verb.main(
        [*argv, *roster.argv(), *mode],
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=_PLATFORMS[capture.platform](pr_info=pull, comments=capture.comments),
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _stored(registry: Path) -> dict:
    return yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]


class TestTheCapturesAreSanitized:
    @pytest.mark.parametrize("name", sorted(p.name for p in PAYLOADS.glob("*.json")))
    def test_no_deployment_identity_or_operator_host_remains(self, name):
        text = (PAYLOADS / name).read_text(encoding="utf-8")
        hits = [
            f"[{label}] {match.group(0)}"
            for label, pattern in PRODUCT_CODE_PATTERNS.items()
            for match in pattern.finditer(text)
        ]
        assert not hits

    @pytest.mark.parametrize("capture", list(CAPTURES.values()), ids=list(CAPTURES))
    def test_each_capture_carries_a_blocked_and_a_clean_fence_for_the_pull_request(self, capture):
        comments = capture.comments
        by_id = {c["id"]: c for c in comments}
        blocked = by_id[capture.blocked_comment]["body"]
        fences = [re.search(r"```review-result\n(.*?)\n```", c["body"], re.S) for c in comments]

        assert all(fences)
        outcomes = sorted(json.loads(f.group(1))["verdict_outcome"] for f in fences)
        assert outcomes == ["BLOCKED", "CLEAN"]
        assert capture.fingerprint in blocked
        head = capture.pull["head"]["sha"]
        assert all(json.loads(f.group(1))["head_sha"] == head for f in fences)


@_PARAM_CAPTURES
class TestLoadoutRulingOnACapturedFence:
    @_PARAM_ROSTERS
    def test_a_role_the_merge_requires_is_one_the_ruling_attributes(self, capture, roster, registry):
        assert _record(capture, roster) == ruling.EXIT_OK

        stored = _stored(registry)
        assert (stored["fingerprint"], stored["by"]) == (capture.fingerprint, "merger")
        assert stored["path"] == capture.path

    def test_no_gate_file_and_no_flag_leaves_the_fence_unattributable(
        self, capture, registry, capsys
    ):
        code = _record(capture, Roster(None, ()))

        assert code == ruling.EXIT_FENCE_INVALID
        assert "declares none and no --required-reviewer was given" in capsys.readouterr().err
        assert not registry.exists()

    @pytest.mark.parametrize(
        "roster",
        [Roster(None, (CLEAN_ROLE,)), Roster((CLEAN_ROLE,), ()), Roster((CLEAN_ROLE,), (CLEAN_ROLE,))],
        ids=["flag-names-another-role", "gate-names-another-role", "both-name-another-role"],
    )
    def test_a_ruling_for_a_role_the_merge_would_not_require_is_refused(
        self, capture, roster, registry, capsys
    ):
        code = _record(capture, roster)

        assert code == ruling.EXIT_FENCE_INVALID
        assert "not the login of a required reviewer role" in capsys.readouterr().err
        assert not registry.exists()

    def test_scanners_declared_for_a_role_that_is_not_required_are_a_usage_error(
        self, capture, registry, capsys
    ):
        orphan = yaml.safe_dump(
            {"merge": {"required_reviewer_roles": [], "required_scanners": {"orphan-role": ["scanner"]}}}
        )
        for roster in (Roster((), ()), Roster((), (BLOCKED_ROLE,))):
            assert _record(capture, roster, gate=orphan) == ruling.EXIT_USAGE
            assert "'orphan-role'" in capsys.readouterr().err
        assert not registry.exists()

        reachable = yaml.safe_dump(
            {"merge": {"required_reviewer_roles": [], "required_scanners": {BLOCKED_ROLE: ["scanner"]}}}
        )
        assert _record(capture, Roster((), (BLOCKED_ROLE,)), gate=reachable) == ruling.EXIT_OK

    def test_a_name_and_login_pair_is_refused(self, capture, registry, capsys):
        code = _record(capture, Roster(None, ()), "--required-reviewer", f"{BLOCKED_ROLE}:any-login")

        assert code == ruling.EXIT_USAGE
        assert f"{BLOCKED_ROLE}:any-login" in capsys.readouterr().err
        assert not registry.exists()


@_PARAM_CAPTURES
@_PARAM_ROSTERS
@_PARAM_MODES
class TestARulingRecordedOnACapturedFenceIsHonouredByTheMerge:
    def test_refused_then_ruled_then_merged(
        self, capture, roster, mode, registry, tmp_path, capsys
    ):
        repo = _local_repo(tmp_path, roster)

        code, _, err = _merge(capture, roster, repo, mode, capsys)
        assert code == merge_verb.EXIT_GATE_RESULT_BLOCKED, err
        assert f"{capture.path}:{capture.line}" in err

        assert _record(capture, roster) == ruling.EXIT_OK

        code, out, err = _merge(capture, roster, repo, mode, capsys)
        assert code == merge_verb.EXIT_OK, err
        assert "ruling(s) recorded for this PR" in err
        if not mode:
            evaluation = json.loads(out.strip().splitlines()[-1])["verdict_evaluations"][0]
            assert evaluation["gate_outcome"] == "CLEAN" and evaluation["fence_outcome"] == "BLOCKED"
            assert capture.path in evaluation["applied_rulings"][0]


class TestTheMergeKeepsItsDiagnosticOrder:
    def test_the_gate_warnings_print_before_the_unreachable_scanner_refusal(
        self, monkeypatch, tmp_path, capsys
    ):
        make_roles_unresolvable(monkeypatch, lambda name: name.startswith("ghost"))
        repo = tmp_path / "repo"
        init_gate_repo(
            repo,
            tracked_gate={
                "required_reviewer_roles": ["ghost"],
                "required_scanners": {"orphan-role": ["scanner"]},
            },
        )
        pull = {
            "head": {"sha": "b" * 40},
            "title": "feat: x",
            "base": {"ref": "main", "sha": git(repo, "rev-parse", "main")},
        }
        capsys.readouterr()

        code = merge_verb.main(
            base_args(**{"--repo-path": str(repo)}),
            token_provider=RecordingTokenProvider(),
            authority_provider=AllowingAuthorityProvider(),
            opener=make_opener(pr_info=pull, comments=[]),
        )

        err = capsys.readouterr().err
        assert code == merge_verb.EXIT_USAGE
        assert "DROPPED from the reviewer floor" in err
        assert "merge.required_scanners declares scanners" in err
        assert err.index("DROPPED from the reviewer floor") < err.index(
            "merge.required_scanners declares scanners"
        )
