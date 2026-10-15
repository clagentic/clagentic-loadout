"""`loadout-ruling`: who may record a ruling, what it refuses, what it writes."""

from __future__ import annotations

import datetime as dt
import json
import os

import pytest
import yaml

from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review import ruling_verb as verb
from clagentic_loadout.transport.attestation import SOURCE_CONFIGURED, Identity
from tests._ruling_support import FLOOR, FP, NON_FLOOR, OTHER_FP, REPO, TODAY, registry_dir, ruled_finding

HEAD = "b" * 40
WRITER = "gatekeeper"
OUTSIDER = "helper"


def _identity(subject):
    identity = Identity(subject, SOURCE_CONFIGURED)
    return lambda: identity


@pytest.fixture
def repo_path(tmp_path):
    """A repo-local config declaring which synthetic roles hold the merge verb."""
    path = tmp_path / "role-config"
    config = path / ".clagentic" / "loadout"
    config.mkdir(parents=True)
    (config / "config.yaml").write_text(
        yaml.safe_dump({"roles": {WRITER: ["merge", "push"], OUTSIDER: ["push"]}}),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = registry_dir(tmp_path) / "rulings.yaml"
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))
    return path


@pytest.fixture
def findings_file(tmp_path):
    def write(findings=None, *, pr=1, repo=REPO):
        owner, name = repo.split("/")
        document = {
            "owner": owner,
            "repo": name,
            "pr_number": pr,
            "head_sha": HEAD,
            "findings": findings
            if findings is not None
            else [ruled_finding(NON_FLOOR), ruled_finding(FLOOR, fingerprint=OTHER_FP, line=30, rule_id="R9")],
        }
        path = tmp_path / "findings.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    return write


def _record_argv(findings, **overrides):
    args = {
        "--repo": REPO,
        "--pr": "1",
        "--findings": findings,
        "--fingerprint": FP,
        "--rule-id": "R1",
        "--path": "src/a.py",
        "--kind": "accepted_risk",
        "--rationale": "accepted for this change",
        "--requested-by": "some-caller",
        "--source-ref": "design note 4",
    }
    args.update(overrides)
    argv = []
    for key, value in args.items():
        if value is not None:
            argv += [key, str(value)]
    return argv


def _run(argv, *, role=WRITER, repo_path=None, authorized=(WRITER,), **kwargs):
    """Run the verb. ``authorized`` are the roles the invoker names as holding
    merge authority (the merge verb's own flag); () relies on the built-in mapping."""
    full = (["--role", role] if role else []) + argv
    if "record" in argv:
        for name in authorized:
            full += ["--authorized-role", name]
        if repo_path is not None:
            full += ["--repo-path", str(repo_path)]
    kwargs.setdefault("identity_provider", _identity(role or verb.DEFAULT_ROLE))
    kwargs.setdefault("today", TODAY)
    return verb.main(full, **kwargs)


class TestRecord:
    def test_a_role_that_holds_the_merge_verb_records_a_non_floor_ruling(
        self, registry, repo_path, findings_file, capsys
    ):
        code = _run(["record", *_record_argv(findings_file())], repo_path=repo_path)

        out = json.loads(capsys.readouterr().out)
        assert code == verb.EXIT_OK
        assert out["result"] == "recorded" and out["rulings_stored"] == 1
        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert stored["by"] == WRITER
        assert stored["requested_by"] == "some-caller"
        assert stored["source_ref"] == "design note 4"
        assert (stored["repo"], stored["pr"], stored["fingerprint"]) == (REPO, 1, FP)

    def test_the_documented_default_role_may_record_without_any_role_config(
        self, registry, findings_file
    ):
        code = _run(["record", *_record_argv(findings_file())], role="merger", authorized=())

        assert code == verb.EXIT_OK

    def test_a_default_role_without_the_merge_verb_may_not(self, registry, findings_file, capsys):
        code = _run(["record", *_record_argv(findings_file())], role="builder", authorized=())

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()
        assert "may not record a ruling" in capsys.readouterr().err

    def test_a_checkout_the_caller_selects_cannot_grant_merge_authority(
        self, tmp_path, registry, findings_file
    ):
        """A role config inside the checkout named by --repo-path (the pull
        request's own branch, say) must not decide who may record."""
        hostile = tmp_path / "pr-checkout"
        config = hostile / ".clagentic" / "loadout"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text(
            yaml.safe_dump({"roles": {OUTSIDER: ["merge"]}}), encoding="utf-8"
        )

        code = _run(
            ["record", *_record_argv(findings_file())],
            role=OUTSIDER,
            repo_path=hostile,
            authorized=(),
        )

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_a_role_without_the_merge_verb_in_the_deployments_mapping_may_not(
        self, registry, repo_path, findings_file
    ):
        code = _run(["record", *_record_argv(findings_file())], role=OUTSIDER, repo_path=repo_path)

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_an_injected_authority_provider_decides_instead_of_the_mapping(
        self, registry, findings_file
    ):
        class Refusing:
            def authority_allows(self, role, owner, repo, pr_number):
                return False

        code = _run(
            ["record", *_record_argv(findings_file())], authority_provider=Refusing(), role="merger"
        )

        assert code == verb.EXIT_AUTHORITY_DENIED

    def test_a_role_that_is_not_the_attested_identity_is_refused_before_anything_else(
        self, registry, repo_path, findings_file
    ):
        code = _run(
            ["record", *_record_argv(findings_file())],
            repo_path=repo_path,
            identity_provider=_identity(OUTSIDER),
        )

        assert code == verb.EXIT_CALLER_INVOKER_MISMATCH
        assert not registry.exists()

    def test_a_floor_finding_is_refused_whatever_the_kind(self, registry, repo_path, findings_file, capsys):
        for kind in ("by_design", "false_positive", "accepted_risk", "mitigated"):
            code = _run(
                ["record", *_record_argv(findings_file(), **{"--fingerprint": OTHER_FP, "--rule-id": "R9",
                                                              "--kind": kind})],
                repo_path=repo_path,
            )

            assert code == verb.EXIT_FLOOR_REFUSED, kind
        assert not registry.exists()
        err = capsys.readouterr().err
        assert "security-floor" in err and "dispositions entry on the base branch" in err

    def test_a_finding_that_cannot_be_found_is_refused(self, registry, repo_path, findings_file):
        code = _run(
            ["record", *_record_argv(findings_file(), **{"--fingerprint": "fp2:0000000000000000"})],
            repo_path=repo_path,
        )

        assert code == verb.EXIT_FINDING_NOT_FOUND
        assert not registry.exists()

    def test_a_findings_file_for_another_pull_request_is_refused(self, registry, repo_path, findings_file):
        code = _run(["record", *_record_argv(findings_file(pr=2))], repo_path=repo_path)

        assert code == verb.EXIT_FINDINGS_INVALID

    @pytest.mark.parametrize("pr", [1, 2])
    def test_a_findings_file_that_names_no_pull_request_is_refused(
        self, tmp_path, registry, findings_file, capsys, pr
    ):
        """A bare array carries no repository or pull request, so a list from
        another PR cannot be told apart from this one's and is refused."""
        path = tmp_path / "list-findings.json"
        path.write_text(json.dumps([ruled_finding(NON_FLOOR)]), encoding="utf-8")

        code = _run(["record", *_record_argv(str(path), **{"--pr": pr})])

        assert code == verb.EXIT_FINDINGS_INVALID
        assert not registry.exists()
        assert "does not name the repository and pull request" in capsys.readouterr().err

    def test_a_findings_file_for_another_repository_is_refused(self, registry, findings_file):
        code = _run(["record", *_record_argv(findings_file(repo="some-owner/other-repo"))])

        assert code == verb.EXIT_FINDINGS_INVALID

    def test_a_findings_file_for_another_head_is_refused(self, registry, findings_file, capsys):
        code = _run(["record", *_record_argv(findings_file()), "--head", "c" * 40])

        assert code == verb.EXIT_FINDINGS_INVALID
        assert "head" in capsys.readouterr().err

    def test_a_findings_file_for_the_requested_head_is_accepted(self, registry, findings_file):
        assert _run(["record", *_record_argv(findings_file()), "--head", HEAD]) == verb.EXIT_OK

    def test_with_no_registry_configured_it_refuses(self, tmp_path, monkeypatch, repo_path, findings_file, capsys):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)

        code = _run(["record", *_record_argv(findings_file())], repo_path=repo_path, config_root=tmp_path / "none")

        assert code == verb.EXIT_REGISTRY_NOT_CONFIGURED
        assert "no ruling registry is configured" in capsys.readouterr().err

    def test_a_registry_inside_the_repo_path_is_refused(
        self, tmp_path, monkeypatch, repo_path, findings_file
    ):
        inside = repo_path / "rulings.yaml"
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(inside))

        code = _run(["record", *_record_argv(findings_file())], repo_path=repo_path)

        assert code == verb.EXIT_REGISTRY_UNTRUSTED
        assert not inside.exists()

    def test_a_world_writable_registry_directory_is_refused(self, registry, repo_path, findings_file):
        os.chmod(registry.parent, 0o777)

        code = _run(["record", *_record_argv(findings_file())], repo_path=repo_path)

        assert code == verb.EXIT_REGISTRY_UNTRUSTED

    def test_a_group_writable_registry_directory_is_refused(self, registry, findings_file):
        os.chmod(registry.parent, 0o775)

        assert _run(["record", *_record_argv(findings_file())]) == verb.EXIT_REGISTRY_UNTRUSTED
        assert not registry.exists()

    def test_a_registry_inside_a_git_work_tree_is_refused_without_a_repo_path(
        self, tmp_path, monkeypatch, findings_file
    ):
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        inside = registry_dir(tree) / "rulings.yaml"
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(inside))

        assert _run(["record", *_record_argv(findings_file())]) == verb.EXIT_REGISTRY_UNTRUSTED
        assert not inside.exists()

    def test_recording_the_same_finding_twice_is_refused(self, registry, repo_path, findings_file):
        argv = ["record", *_record_argv(findings_file())]

        assert _run(argv, repo_path=repo_path) == verb.EXIT_OK
        assert _run(argv, repo_path=repo_path) == verb.EXIT_DUPLICATE

    def test_a_deferral_records_its_reference_and_expiry(self, registry, repo_path, findings_file):
        argv = ["record", *_record_argv(findings_file(), **{"--reference": "ABC-123",
                                                             "--expires": "2026-11-01",
                                                             "--kind": "mitigated"})]

        assert _run(argv, repo_path=repo_path) == verb.EXIT_OK
        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert (stored["reference"], str(stored["expires"])) == ("ABC-123", "2026-11-01")

    def test_a_deferral_needs_an_expiry(self, registry, repo_path, findings_file, capsys):
        argv = ["record", *_record_argv(findings_file(), **{"--reference": "ABC-123"})]

        assert _run(argv, repo_path=repo_path) == verb.EXIT_USAGE
        assert "expires" in capsys.readouterr().err

    def test_a_reference_must_match_the_configured_pattern(
        self, tmp_path, registry, repo_path, findings_file, capsys
    ):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"rulings": {"reference_pattern": r"\A[A-Z]+-[0-9]+\Z"}}), encoding="utf-8"
        )
        argv = ["record", *_record_argv(findings_file(), **{"--reference": "nope", "--expires": "2026-11-01"})]

        code = _run(argv, repo_path=repo_path, config_root=tmp_path)

        assert code == verb.EXIT_USAGE
        assert "reference pattern" in capsys.readouterr().err

class TestList:
    def test_it_lists_what_was_recorded_and_filters_by_scope(
        self, registry, repo_path, findings_file, capsys
    ):
        _run(["record", *_record_argv(findings_file())], repo_path=repo_path)
        capsys.readouterr()

        assert _run(["list"], role=None) == verb.EXIT_OK
        everything = json.loads(capsys.readouterr().out)
        assert [r["fingerprint"] for r in everything["rulings"]] == [FP]
        assert everything["rulings"][0]["expired"] is False

        assert _run(["list", "--pr", "2"], role=None) == verb.EXIT_OK
        assert json.loads(capsys.readouterr().out)["rulings"] == []

    def test_it_marks_an_expired_ruling(self, registry, repo_path, findings_file, capsys):
        _run(["record", *_record_argv(findings_file())], repo_path=repo_path)
        capsys.readouterr()

        _run(["list"], role=None, today=TODAY + dt.timedelta(days=90))

        assert json.loads(capsys.readouterr().out)["rulings"][0]["expired"] is True

    def test_with_no_registry_it_lists_nothing(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)

        assert _run(["list"], role=None, config_root=tmp_path / "none") == verb.EXIT_OK
        assert json.loads(capsys.readouterr().out) == {"registry": None, "rulings": [], "degraded": []}

    def test_an_untrusted_registry_is_reported_as_degraded(self, registry, capsys):
        registry.parent.chmod(0o777)
        registry.write_text("rulings: []\n", encoding="utf-8")

        _run(["list"], role=None)

        assert "not a trusted source" in json.loads(capsys.readouterr().out)["degraded"][0]


class TestSurface:
    def test_the_verb_is_not_in_the_role_verb_vocabulary(self):
        from clagentic_loadout.provisioning.roles import KNOWN_VERBS

        assert "ruling" not in KNOWN_VERBS

    def test_the_umbrella_dispatches_to_it(self, capsys):
        from clagentic_loadout.cli import main as umbrella

        assert umbrella(["ruling", "--version"]) == 0
        assert "loadout-ruling" in capsys.readouterr().out
