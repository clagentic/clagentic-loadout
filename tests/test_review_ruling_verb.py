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
from tests._ruling_host import FENCE_COMMENT, REVIEWER, comment, fence_body, host_opener
from tests._ruling_support import FLOOR, FP, NON_FLOOR, OTHER_FP, REPO, TODAY, registry_dir, ruled_finding
from tests._support.fakes import RecordingTokenProvider, RefusingTokenProvider
from tests._support.merge_verb import MissingCredsTokenProvider

#: The role the built-in role-to-verb mapping grants the ruling verb.
WRITER = "merger"
OUTSIDER = "helper"
OTHER_HEAD = "c" * 40


def _identity(subject):
    identity = Identity(subject, SOURCE_CONFIGURED)
    return lambda: identity


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path, monkeypatch):
    """Point the default user-level config root at an empty directory, so a host's
    own config never decides who may record."""
    from clagentic_loadout.transport import provider_config

    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "no-user-config")


def _user_config(root, roles, *, mode=0o644, name="user-config"):
    directory = root / name
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o755)
    path = directory / "config.yaml"
    path.write_text(yaml.safe_dump({"roles": roles}), encoding="utf-8")
    os.chmod(path, mode)
    return directory


@pytest.fixture
def repo_path(tmp_path):
    """A checkout of the reviewed repository; it supplies no role policy."""
    path = tmp_path / "checkout"
    path.mkdir()
    return path


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = registry_dir(tmp_path) / "rulings.yaml"
    monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(path))
    return path


def _default_findings():
    return [ruled_finding(NON_FLOOR), ruled_finding(FLOOR, fingerprint=OTHER_FP, line=30, rule_id="R9")]


def _fence_comment(findings=None, *, comment_id=FENCE_COMMENT, login=REVIEWER, **fence):
    return comment(
        comment_id,
        fence_body(findings if findings is not None else _default_findings(), **fence),
        login=login,
    )


def _opener(*comments, **host):
    return host_opener(comments or [_fence_comment()], **host)


def _record_argv(**overrides):
    args = {
        "--platform": "forgejo",
        "--repo": REPO,
        "--pr": "1",
        "--fence-comment": FENCE_COMMENT,
        "--fingerprint": FP,
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


def _run(argv, *, role=WRITER, repo_path=None, **kwargs):
    """Run the verb. Authority is never injected here: the built-in mapping (or a
    provider a test passes explicitly) decides. The host is the fake one, serving
    a valid reviewer fence unless a test passes its own opener."""
    full = (["--role", role] if role else []) + argv
    if "record" in argv and repo_path is not None:
        full += ["--repo-path", str(repo_path)]
    kwargs.setdefault("identity_provider", _identity(role or verb.DEFAULT_ROLE))
    kwargs.setdefault("today", TODAY)
    if "record" in argv:
        kwargs.setdefault("token_provider", RecordingTokenProvider())
        kwargs.setdefault("opener", _opener())
    return verb.main(full, **kwargs)


def _record(*, role=WRITER, repo_path=None, **kwargs):
    overrides = kwargs.pop("argv", {})
    return _run(["record", *_record_argv(**overrides)], role=role, repo_path=repo_path, **kwargs)


class TestRecord:
    def test_a_role_that_holds_the_ruling_verb_records_a_non_floor_ruling(
        self, registry, repo_path, capsys
    ):
        code = _record(repo_path=repo_path)

        out = json.loads(capsys.readouterr().out)
        assert code == verb.EXIT_OK
        assert out["result"] == "recorded" and out["rulings_stored"] == 1
        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert stored["by"] == WRITER
        assert stored["requested_by"] == "some-caller"
        assert stored["source_ref"] == "design note 4"
        assert (stored["repo"], stored["pr"], stored["fingerprint"]) == (REPO, 1, FP)

    def test_the_rule_and_path_come_from_the_fence(self, registry):
        assert _record() == verb.EXIT_OK

        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert (stored["rule_id"], stored["path"]) == ("R1", "src/a.py")

    def test_the_host_is_read_with_the_callers_own_role_credential(self, registry):
        tokens = RecordingTokenProvider()

        assert _record(token_provider=tokens) == verb.EXIT_OK
        assert tokens.resolved_for == [WRITER]

    def test_the_documented_default_role_may_record_without_any_role_config(self, registry):
        assert _record(role="merger") == verb.EXIT_OK

    def test_a_default_role_without_the_ruling_verb_may_not(self, registry, capsys):
        code = _record(role="builder")

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()
        assert "may not record a ruling" in capsys.readouterr().err

    def test_a_checkout_the_caller_selects_cannot_grant_ruling_authority(self, tmp_path, registry):
        """A role config inside the checkout named by --repo-path (the pull
        request's own branch, say) must not decide who may record."""
        hostile = tmp_path / "pr-checkout"
        config = hostile / ".clagentic" / "loadout"
        config.mkdir(parents=True)
        (config / "config.yaml").write_text(
            yaml.safe_dump({"roles": {OUTSIDER: ["merge", "ruling"]}}), encoding="utf-8"
        )

        code = _record(role=OUTSIDER, repo_path=hostile)

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_an_unauthorized_role_is_refused_before_any_credential_is_resolved(self, registry):
        code = _record(role=OUTSIDER, token_provider=RefusingTokenProvider())

        assert code == verb.EXIT_AUTHORITY_DENIED

    def test_a_user_level_mapping_granting_ruling_authorizes_that_role(self, tmp_path, registry):
        root = _user_config(tmp_path, {"clerk": ["ruling"], "merger": ["merge"]})

        assert _record(role="clerk", config_root=root) == verb.EXIT_OK

    def test_a_user_level_mapping_replaces_the_default_rather_than_adding_to_it(self, tmp_path, registry):
        root = _user_config(tmp_path, {"clerk": ["ruling"], "merger": ["merge"]})

        code = _record(role="merger", config_root=root)

        assert code == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_a_group_writable_user_level_file_is_refused(self, tmp_path, registry, capsys):
        root = _user_config(tmp_path, {"clerk": ["ruling"]}, mode=0o664)

        code = _record(role="clerk", config_root=root)

        assert code == verb.EXIT_REGISTRY_UNTRUSTED
        assert "not a trusted source for role authority" in capsys.readouterr().err
        assert not registry.exists()

    def test_a_user_level_file_inside_a_work_tree_is_refused(self, tmp_path, registry):
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        root = _user_config(tree, {"clerk": ["ruling"]})

        code = _record(role="clerk", config_root=root)

        assert code == verb.EXIT_REGISTRY_UNTRUSTED
        assert not registry.exists()

    def test_a_user_level_mapping_naming_an_unknown_verb_is_a_configuration_error(
        self, tmp_path, registry
    ):
        root = _user_config(tmp_path, {"clerk": ["no-such-verb"]})

        assert _record(role="clerk", config_root=root) == verb.EXIT_CONFIG_INVALID

    def test_a_user_level_file_without_a_roles_key_leaves_the_default(self, tmp_path, registry):
        root = tmp_path / "plain-config"
        root.mkdir()
        os.chmod(root, 0o755)
        (root / "config.yaml").write_text("rulings: {}\n", encoding="utf-8")
        os.chmod(root / "config.yaml", 0o644)

        assert _record(role=WRITER, config_root=root) == verb.EXIT_OK

    @pytest.mark.parametrize("value", [None, [], "merger", ["merger"]])
    def test_a_present_roles_key_that_is_not_a_mapping_is_a_configuration_error(
        self, tmp_path, registry, value
    ):
        """Only an absent key leaves the default; a null or non-mapping value is
        malformed policy and never falls back to it."""
        root = _user_config(tmp_path, value, name="malformed-roles")

        assert _record(role=WRITER, config_root=root) == verb.EXIT_CONFIG_INVALID
        assert not registry.exists()

    def test_the_authorized_role_flag_does_not_exist(self, registry, capsys):
        code = _run(["record", *_record_argv(), "--authorized-role", OUTSIDER], role=OUTSIDER)

        assert code == verb.EXIT_USAGE
        assert not registry.exists()
        assert "--authorized-role" in capsys.readouterr().err

    def test_a_caller_naming_its_own_role_is_refused(self, registry):
        assert _record(role=OUTSIDER) == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_a_role_without_the_ruling_verb_in_the_deployments_mapping_may_not(
        self, registry, repo_path
    ):
        assert _record(role=OUTSIDER, repo_path=repo_path) == verb.EXIT_AUTHORITY_DENIED
        assert not registry.exists()

    def test_an_injected_authority_provider_decides_instead_of_the_mapping(self, registry):
        class Refusing:
            def authority_allows(self, role, owner, repo, pr_number):
                return False

        assert _record(authority_provider=Refusing(), role="merger") == verb.EXIT_AUTHORITY_DENIED

    def test_a_role_that_is_not_the_attested_identity_is_refused_before_anything_else(
        self, registry, repo_path
    ):
        code = _record(repo_path=repo_path, identity_provider=_identity(OUTSIDER))

        assert code == verb.EXIT_CALLER_INVOKER_MISMATCH
        assert not registry.exists()

    def test_a_floor_finding_is_refused_whatever_the_kind(self, registry, repo_path, capsys):
        for kind in ("by_design", "false_positive", "accepted_risk", "mitigated"):
            code = _record(
                argv={"--fingerprint": OTHER_FP, "--kind": kind}, repo_path=repo_path
            )

            assert code == verb.EXIT_FLOOR_REFUSED, kind
        assert not registry.exists()
        err = capsys.readouterr().err
        assert "security-floor" in err and "dispositions entry on the base branch" in err

    def test_a_finding_the_fence_does_not_hold_is_refused(self, registry, repo_path):
        code = _record(argv={"--fingerprint": "fp2:0000000000000000"}, repo_path=repo_path)

        assert code == verb.EXIT_FINDING_NOT_FOUND
        assert not registry.exists()

    def test_the_findings_flag_does_not_exist(self, registry, capsys):
        code = _run(["record", *_record_argv(), "--findings", "findings.json"])

        assert code == verb.EXIT_USAGE
        assert not registry.exists()
        assert "--findings" in capsys.readouterr().err


class TestFenceAcceptance:
    """The comment a ruling is taken from must be a reviewer's own fence for this
    repository, pull request and head."""

    def test_a_comment_that_is_not_on_this_pull_request_is_refused(self, registry, capsys):
        """A fence posted on another repository's or pull request's thread is not
        among the requested pull request's comments at all."""
        code = _record(argv={"--fence-comment": 99})

        assert code == verb.EXIT_FENCE_INVALID
        assert not registry.exists()
        assert "no comment with id 99" in capsys.readouterr().err

    def test_a_fence_that_exists_only_in_another_repository_is_refused(self, registry, capsys):
        """The fence is read from the requested repository's own pull request. A
        valid fence with the same comment id and pull request number that lives in a
        different repository is never reached."""
        foreign = _opener()
        local = _opener(comment(99, "unrelated prose", login=REVIEWER))
        seen = []

        def opener(req, timeout=15):
            seen.append(req.full_url)
            target = local if f"/{REPO}/" in req.full_url else foreign
            return target(req, timeout)

        code = _record(opener=opener, argv={"--repo": REPO})

        assert code == verb.EXIT_FENCE_INVALID
        assert not registry.exists()
        assert "no comment with id" in capsys.readouterr().err
        assert seen and all(f"/{REPO}/" in url for url in seen)

    def test_a_fence_for_another_pull_request_is_refused(self, registry, capsys):
        code = _record(opener=_opener(_fence_comment(pr=2)))

        assert code == verb.EXIT_FENCE_INVALID
        assert not registry.exists()

    def test_a_fence_at_another_head_is_refused(self, registry, capsys):
        code = _record(opener=_opener(_fence_comment(head=OTHER_HEAD)))

        assert code == verb.EXIT_FENCE_INVALID
        assert not registry.exists()
        assert "STALE" in capsys.readouterr().err

    def test_a_fence_at_the_head_the_caller_names_is_accepted(self, registry):
        code = _record(
            argv={"--head": OTHER_HEAD}, opener=_opener(_fence_comment(head=OTHER_HEAD))
        )

        assert code == verb.EXIT_OK

    def test_a_head_that_is_not_a_full_sha_is_refused(self, registry):
        assert _record(argv={"--head": "abc123"}) == verb.EXIT_FENCE_INVALID

    def test_a_fence_from_a_login_that_is_not_a_required_reviewer_is_refused(self, registry, capsys):
        code = _record(opener=_opener(_fence_comment(login="someone-else")))

        assert code == verb.EXIT_FENCE_INVALID
        assert not registry.exists()
        assert "not the login of a required reviewer role" in capsys.readouterr().err

    def test_a_fence_that_names_another_reviewer_is_refused(self, registry):
        """Posted under the required reviewer's login, but the fence itself says it
        is another reviewer's verdict."""
        code = _record(opener=_opener(_fence_comment(reviewer="other-reviewer")))

        assert code == verb.EXIT_FENCE_INVALID

    def test_a_fence_without_the_fingerprint_is_refused(self, registry):
        other = [ruled_finding(NON_FLOOR, fingerprint=OTHER_FP, rule_id="R2")]

        assert _record(opener=_opener(_fence_comment(other))) == verb.EXIT_FINDING_NOT_FOUND

    def test_a_fence_that_carries_no_evaluated_findings_is_refused(self, registry, capsys):
        code = _record(opener=_opener(_fence_comment(evaluated=False)))

        assert code == verb.EXIT_FENCE_INVALID
        assert "not an evaluated fence" in capsys.readouterr().err

    def test_a_comment_with_no_fence_is_refused(self, registry):
        prose = comment(FENCE_COMMENT, "looks fine to me", login=REVIEWER)

        assert _record(opener=_opener(prose)) == verb.EXIT_FENCE_INVALID

    def test_a_repository_that_declares_no_required_reviewer_cannot_attribute_a_fence(
        self, registry, capsys
    ):
        code = _record(opener=_opener(_fence_comment(), with_gate=False))

        assert code == verb.EXIT_FENCE_INVALID
        assert "no required reviewer role can be established" in capsys.readouterr().err

    def test_a_gate_that_cannot_be_parsed_attributes_nothing(self, registry):
        code = _record(opener=_opener(_fence_comment(), gate="merge: [not, a, mapping"))

        assert code == verb.EXIT_FENCE_INVALID

    def test_the_required_reviewer_is_read_at_the_base_not_named_by_the_caller(self, registry):
        """A repository whose gate requires a different reviewer does not accept
        this reviewer's fence, and no flag can add one."""
        code = _record(opener=_opener(_fence_comment(), gate_roles=("another-reviewer",)))

        assert code == verb.EXIT_FENCE_INVALID

    def test_an_unreadable_pull_request_is_a_host_failure(self, registry):
        assert _record(opener=_opener(pr_http=500)) == verb.EXIT_HOST_UNAVAILABLE

    def test_a_role_with_no_credential_cannot_read_the_host(self, registry):
        assert _record(token_provider=MissingCredsTokenProvider()) == verb.EXIT_TOKEN_FETCH_FAILED

    def test_the_github_platform_reads_the_same_fence(self, registry, monkeypatch):
        monkeypatch.setattr(
            "clagentic_loadout.merge.reviewer_login.resolve_github_app_slug",
            lambda **kwargs: "some-app",
        )
        code = _record(
            argv={"--platform": "github"},
            opener=_opener(_fence_comment(login="some-app[bot]")),
        )

        assert code == verb.EXIT_OK


class TestRegistry:
    def test_with_no_registry_configured_it_refuses(self, tmp_path, monkeypatch, repo_path, capsys):
        monkeypatch.delenv(rp.RULINGS_FILE_ENV_VAR, raising=False)

        code = _record(repo_path=repo_path, config_root=tmp_path / "none")

        assert code == verb.EXIT_REGISTRY_NOT_CONFIGURED
        assert "no ruling registry is configured" in capsys.readouterr().err

    def test_a_registry_inside_the_repo_path_is_refused(self, tmp_path, monkeypatch, repo_path):
        inside = repo_path / "rulings.yaml"
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(inside))

        code = _record(repo_path=repo_path)

        assert code == verb.EXIT_REGISTRY_UNTRUSTED
        assert not inside.exists()

    def test_a_world_writable_registry_directory_is_refused(self, registry, repo_path):
        os.chmod(registry.parent, 0o777)

        assert _record(repo_path=repo_path) == verb.EXIT_REGISTRY_UNTRUSTED

    def test_a_group_writable_registry_directory_is_refused(self, registry):
        os.chmod(registry.parent, 0o775)

        assert _record() == verb.EXIT_REGISTRY_UNTRUSTED
        assert not registry.exists()

    def test_a_registry_inside_a_git_work_tree_is_refused_without_a_repo_path(
        self, tmp_path, monkeypatch
    ):
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        inside = registry_dir(tree) / "rulings.yaml"
        monkeypatch.setenv(rp.RULINGS_FILE_ENV_VAR, str(inside))

        assert _record() == verb.EXIT_REGISTRY_UNTRUSTED
        assert not inside.exists()

    def test_recording_the_same_finding_twice_is_refused(self, registry, repo_path):
        assert _record(repo_path=repo_path) == verb.EXIT_OK
        assert _record(repo_path=repo_path) == verb.EXIT_DUPLICATE

    def test_a_deferral_records_its_reference_and_expiry(self, registry, repo_path):
        argv = {"--reference": "ABC-123", "--expires": "2026-11-01", "--kind": "mitigated"}

        assert _record(argv=argv, repo_path=repo_path) == verb.EXIT_OK
        stored = yaml.safe_load(registry.read_text(encoding="utf-8"))["rulings"][0]
        assert (stored["reference"], str(stored["expires"])) == ("ABC-123", "2026-11-01")

    def test_a_deferral_needs_an_expiry(self, registry, repo_path, capsys):
        code = _record(argv={"--reference": "ABC-123"}, repo_path=repo_path)

        assert code == verb.EXIT_USAGE
        assert "expires" in capsys.readouterr().err

    def test_a_reference_must_match_the_configured_pattern(self, tmp_path, registry, repo_path, capsys):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"rulings": {"reference_pattern": r"\A[A-Z]+-[0-9]+\Z"}}), encoding="utf-8"
        )

        code = _record(
            argv={"--reference": "nope", "--expires": "2026-11-01"},
            repo_path=repo_path,
            config_root=tmp_path,
        )

        assert code == verb.EXIT_USAGE
        assert "reference pattern" in capsys.readouterr().err


class TestList:
    def test_it_lists_what_was_recorded_and_filters_by_scope(self, registry, repo_path, capsys):
        _record(repo_path=repo_path)
        capsys.readouterr()

        assert _run(["list"], role=None) == verb.EXIT_OK
        everything = json.loads(capsys.readouterr().out)
        assert [r["fingerprint"] for r in everything["rulings"]] == [FP]
        assert everything["rulings"][0]["expired"] is False

        assert _run(["list", "--pr", "2"], role=None) == verb.EXIT_OK
        assert json.loads(capsys.readouterr().out)["rulings"] == []

    def test_it_marks_an_expired_ruling(self, registry, repo_path, capsys):
        _record(repo_path=repo_path)
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
    def test_the_verb_is_in_the_role_verb_vocabulary_and_default_merger_holds_it(self):
        from clagentic_loadout.provisioning.roles import DEFAULT_ROLE_VERBS, KNOWN_VERBS

        assert "ruling" in KNOWN_VERBS
        assert "ruling" in DEFAULT_ROLE_VERBS["merger"]

    def test_the_umbrella_dispatches_to_it(self, capsys):
        from clagentic_loadout.cli import main as umbrella

        assert umbrella(["ruling", "--version"]) == 0
        assert "loadout-ruling" in capsys.readouterr().out
