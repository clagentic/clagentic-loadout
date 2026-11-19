"""The standard spec: its template and verb-sourced fields, the seal and the
store it lives in, the chain a change makes, and the `loadout-spec` verb."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.review import profile_verb, spec_review
from clagentic_loadout.review.evaluation import finding_state_id
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.profile_brief import declared_tests, profile_brief
from clagentic_loadout.review.profile_rules import rule_statements
from clagentic_loadout.review.repo_profile import parse_profile_document
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.spec import verb as spec_verb
from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record.review_record import (
    build_review_record,
    review_records_root,
    write_review_record,
)
from tests._review_evaluate_support import commit_files, make_repo

ROLE = "planner-role"
OTHER_ROLE = "second-role"
HEAD_A = "a" * 40
HEAD_B = "b" * 40
TEST_FULL = ["python3", "-m", "pytest", "--junitxml={junit}"]


def _run(argv, capsys, **kwargs):
    capsys.readouterr()
    code = spec_verb.main(argv, **kwargs)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def repo(tmp_path) -> Path:
    path = make_repo(tmp_path)
    profile = {"class": "released-tool", "test": {"full": TEST_FULL}}
    commit_files(path, {PROFILE_PATH: yaml.safe_dump(profile), "README.md": "x\n"})
    return path


def _fill(draft: dict, **overrides) -> dict:
    filled = json.loads(json.dumps(draft))
    filled["goal"] = "Add a thing."
    filled["acceptance_criteria"] = [
        {"id": "ac-1", "given": "a repo", "when": "the verb runs", "then": "it works"},
        {"id": "ac-2", "given": "bad input", "when": "the verb runs", "then": "it refuses"},
    ]
    filled.update(overrides)
    return filled


def _new_draft(repo: Path, capsys, role: str = ROLE) -> dict:
    code, out, err = _run(["new", "--role", role, "--repo-path", str(repo)], capsys)
    assert code == 0, err
    return json.loads(out)


def _write(tmp_path: Path, name: str, document) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _seal(tmp_path, repo, capsys, draft, *, head=None, name="draft.json"):
    path = _write(tmp_path, name, draft)
    argv = ["seal", "--file", str(path), "--repo-path", str(repo)]
    if head is not None:
        argv += ["--head", head]
    code, out, err = _run(argv, capsys)
    return code, (json.loads(out) if code == 0 else None), err


class TestNew:
    def test_rules_and_tests_come_from_the_profile_and_the_rest_are_empty_named_sections(
        self, repo, capsys
    ):
        draft = _new_draft(repo, capsys)

        profile = parse_profile_document(yaml.safe_load((repo / PROFILE_PATH).read_text()))
        assert draft["role"] == ROLE
        assert draft["class_rules"] == {
            "source": "profile",
            "note": None,
            "class": "released-tool",
            "rules": {"released-tool": rule_statements(profile, "released-tool")},
            "paths": [],
        }
        assert draft["test_plan"]["source"] == "profile"
        assert draft["test_plan"]["declared"] == {"full": TEST_FULL}
        assert draft["test_plan"]["plan"] == []
        # No source exists yet for consumers: present, empty, and saying so. The
        # group rules are empty here because the fixture names no repository and no groups file.
        assert draft["consumers"] == {"source": None, "note": "no source yet", "items": []}
        assert draft["group_rules"]["source"] is None
        assert draft["group_rules"]["items"] == []
        assert draft["group_rules"]["note"]
        # The fixture profile states no posture, so every posture key is an item.
        completeness = draft["profile_completeness"]
        assert (completeness["source"], completeness["note"]) == ("profile", None)
        assert [item.split(":")[0] for item in completeness["items"]] == [
            "exposure",
            "data",
            "visibility",
            "merge_control",
            "confirmed_at",
        ]
        assert draft["goal"] == ""
        assert draft["scope"] == {"in": [], "out": []}
        assert draft["acceptance_criteria"] == []
        assert draft["verification_evidence_owed"] == []
        assert draft["rulings_already_made"] == []
        assert draft["docs_only_justification"] is None
        assert draft["supersedes"] is None

    def test_a_complete_profile_gives_an_empty_completeness_section_that_says_so(
        self, tmp_path, capsys
    ):
        (tmp_path / "complete").mkdir()
        path = make_repo(tmp_path / "complete")
        profile = {
            "class": "internal",
            "exposure": "internal_authenticated",
            "data": "internal",
            "visibility": "private",
            "merge_control": "review_required",
            "confirmed_at": "2026-10-24",
        }
        commit_files(path, {PROFILE_PATH: yaml.safe_dump(profile)})
        code, out, err = _run(
            ["new", "--role", ROLE, "--repo-path", str(path)],
            capsys,
            now=dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc),
        )
        assert code == 0, err
        assert json.loads(out)["profile_completeness"] == {
            "source": "profile",
            "note": "the repo profile is complete",
            "items": [],
        }

    def test_an_expired_confirmation_is_named_in_the_completeness_items(self, tmp_path, capsys):
        (tmp_path / "stale").mkdir()
        path = make_repo(tmp_path / "stale")
        profile = {
            "class": "internal",
            "exposure": "internal_authenticated",
            "data": "internal",
            "visibility": "private",
            "merge_control": "review_required",
            "confirmed_at": "2026-01-01",
        }
        commit_files(path, {PROFILE_PATH: yaml.safe_dump(profile)})
        code, out, err = _run(
            ["new", "--role", ROLE, "--repo-path", str(path)],
            capsys,
            now=dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc),
        )
        assert code == 0, err
        (item,) = json.loads(out)["profile_completeness"]["items"]
        assert item.startswith("confirmed_at: expired")

    def test_a_ref_reads_the_completeness_at_that_revision(self, repo, capsys):
        complete = {
            "class": "internal",
            "exposure": "internal_authenticated",
            "data": "internal",
            "visibility": "private",
            "merge_control": "review_required",
            "confirmed_at": "2026-10-24",
        }
        (repo / PROFILE_PATH).write_text(yaml.safe_dump(complete), encoding="utf-8")
        now = dt.datetime(2026, 10, 24, tzinfo=dt.timezone.utc)
        _, working, _ = _run(["new", "--role", ROLE, "--repo-path", str(repo)], capsys, now=now)
        _, committed, _ = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--ref", "HEAD"], capsys, now=now
        )
        assert json.loads(working)["profile_completeness"]["items"] == []
        assert len(json.loads(committed)["profile_completeness"]["items"]) == 5

    def test_a_sealed_spec_carries_the_completeness_items(self, tmp_path, repo, capsys):
        draft = _fill(_new_draft(repo, capsys))
        code, sealed, err = _seal(tmp_path, repo, capsys, draft)
        assert code == 0, err
        assert json.loads(sealed["pr_body_copy"])["profile_completeness"]["items"]

    def test_no_posture_key_ever_appears(self, repo, capsys):
        draft = _new_draft(repo, capsys)
        assert not [key for key in draft if "posture" in key.lower()]
        assert "head_sha" not in draft and "sealed_at" not in draft

    def test_the_role_is_an_opaque_key_any_well_formed_one_is_accepted(self, repo, capsys):
        for role in ("planner-role", "R2_d2", "x", "a" * 64, "Mixed-Case_9"):
            assert _new_draft(repo, capsys, role)["role"] == role

    @pytest.mark.parametrize("role", ["", "-lead", "has space", "a/b", "a" * 65, "x\n", "ünï"])
    def test_a_role_that_is_not_a_key_is_a_usage_error(self, repo, capsys, role):
        code, _, err = _run(["new", f"--role={role}", "--repo-path", str(repo)], capsys)
        assert code == spec_verb.EXIT_USAGE
        assert repr(role) in err

    def test_a_repo_without_a_profile_leaves_both_sections_empty_and_says_why(
        self, tmp_path, capsys
    ):
        bare = make_repo(tmp_path)
        commit_files(bare, {"README.md": "x\n"})
        draft = _new_draft(bare, capsys)
        assert draft["class_rules"]["source"] is None
        assert "no repo profile" in draft["class_rules"]["note"]
        assert draft["class_rules"]["rules"] == {}
        assert draft["test_plan"]["declared"] is None
        assert "no repo profile" in draft["test_plan"]["note"]

    def test_an_invalid_profile_is_refused_not_papered_over(self, tmp_path, capsys):
        broken = make_repo(tmp_path)
        commit_files(broken, {PROFILE_PATH: "class: nonsense\n"})
        code, _, err = _run(["new", "--role", ROLE, "--repo-path", str(broken)], capsys)
        assert code == spec_verb.EXIT_PROFILE_INVALID
        assert "nonsense" in err

    def test_a_profile_without_test_commands_says_so(self, tmp_path, capsys):
        plain = make_repo(tmp_path)
        commit_files(plain, {PROFILE_PATH: "class: internal\n"})
        draft = _new_draft(plain, capsys)
        assert draft["class_rules"]["class"] == "internal"
        assert draft["test_plan"]["source"] is None
        assert draft["test_plan"]["declared"] is None
        assert "declares no test commands" in draft["test_plan"]["note"]

    def test_out_writes_a_new_file_and_never_replaces_one(self, repo, tmp_path, capsys):
        target = tmp_path / "draft.json"
        argv = ["new", "--role", ROLE, "--repo-path", str(repo), "--out", str(target)]
        assert _run(argv, capsys)[0] == 0
        assert json.loads(target.read_text(encoding="utf-8"))["role"] == ROLE
        before = target.read_bytes()
        code, _, err = _run(argv, capsys)
        assert code == spec_verb.EXIT_IO_FAILED and "already exists" in err
        assert target.read_bytes() == before

    def test_the_profile_can_be_read_at_a_revision(self, repo, capsys):
        first = commit_files(repo, {"other.txt": "y\n"})
        (repo / PROFILE_PATH).write_text("class: internal\n", encoding="utf-8")
        draft_wt = _new_draft(repo, capsys)
        assert draft_wt["class_rules"]["class"] == "internal"
        code, out, _ = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--ref", first], capsys
        )
        assert code == 0 and json.loads(out)["class_rules"]["class"] == "released-tool"


class TestProfileBrief:
    def test_the_brief_holds_the_class_its_rules_the_overrides_and_the_tests(self):
        profile = parse_profile_document(
            {
                "class": "released-tool",
                "paths": {"tests/**": {"class": "internal"}},
                "test": {"full": TEST_FULL, "base": "origin/main"},
            }
        )
        brief = profile_brief(profile)
        assert brief["class"] == "released-tool"
        assert set(brief["class_rules"]) == {"released-tool", "internal"}
        assert brief["paths"] == [{"glob": "tests/**", "class": "internal"}]
        assert brief["tests"] == {"full": TEST_FULL, "base": "origin/main"}

    def test_a_profile_with_no_test_block_declares_none(self):
        assert declared_tests(parse_profile_document({"class": "internal"}).test) is None


class TestModel:
    def test_serialization_is_canonical_whatever_the_key_order(self):
        one = {"b": 1, "a": {"y": 2, "x": "é"}}
        two = {"a": {"x": "é", "y": 2}, "b": 1}
        assert model.canonical_bytes(one) == model.canonical_bytes(two)
        assert model.canonical_bytes(one) == b'{"a":{"x":"\\u00e9","y":2},"b":1}'

    def test_the_hash_is_the_sha256_of_the_canonical_bytes(self):
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        assert sealed.spec_hash == hashlib.sha256(sealed.data).hexdigest()

    @pytest.mark.parametrize(
        "mutation",
        [
            {"goal": ""},
            {"acceptance_criteria": []},
            {
                "acceptance_criteria": [
                    {"id": "ac-1", "given": "g", "when": "w", "then": "t"},
                    {"id": "ac-1", "given": "g", "when": "w", "then": "t"},
                ]
            },
            {"acceptance_criteria": [{"id": "bad id", "given": "g", "when": "w", "then": "t"}]},
            {"acceptance_criteria": [{"id": "ac-1", "given": "", "when": "w", "then": "t"}]},
            {"posture": "x"},
            {"role": "not a role"},
            {"supersedes": "short"},
            {"verification_evidence_owed": [{"gate": "two\nlines"}]},
        ],
    )
    def test_an_incomplete_or_malformed_draft_is_not_sealed(self, mutation):
        with pytest.raises(model.SpecFormatError):
            model.seal(
                _fill(model.new_draft(ROLE), **mutation),
                head_sha=HEAD_A,
                sealed_at="2026-01-01T00:00:00Z",
            )

    def test_a_document_that_carries_a_seal_is_never_sealed_again(self):
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        with pytest.raises(model.AlreadySealedError, match="supersedes"):
            model.seal(sealed.doc, head_sha=HEAD_B, sealed_at="2026-01-02T00:00:00Z")

    def test_content_that_does_not_hash_to_its_name_is_refused(self):
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        assert model.parse_sealed(sealed.data, sealed.spec_hash) == sealed.doc
        with pytest.raises(model.SpecFormatError, match="hashes to"):
            model.parse_sealed(sealed.data + b" ", sealed.spec_hash)

    def test_non_canonical_content_is_refused_even_under_its_own_hash(self):
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        pretty = json.dumps(sealed.doc, indent=2).encode("utf-8")
        with pytest.raises(model.SpecFormatError, match="canonical"):
            model.parse_sealed(pretty, hashlib.sha256(pretty).hexdigest())


class TestSeal:
    def test_seal_prints_the_hash_stores_the_artifact_and_the_body_copy_matches_it_byte_for_byte(
        self, repo, tmp_path, capsys
    ):
        draft = _fill(_new_draft(repo, capsys))
        code, sealed, err = _seal(tmp_path, repo, capsys, draft, head=HEAD_A)
        assert code == 0, err

        spec_hash = sealed["spec_hash"]
        artifact = Path(sealed["artifact"])
        assert artifact == spec_store.specs_root() / HEAD_A / f"{spec_hash}.json"
        assert hashlib.sha256(artifact.read_bytes()).hexdigest() == spec_hash
        assert sealed["head_sha"] == HEAD_A
        assert sealed["role"] == ROLE and sealed["supersedes"] is None
        # The copy for the pull request body is the artifact's own bytes.
        assert sealed["pr_body_copy"].encode("ascii") == artifact.read_bytes()
        # And the artifact is retrievable by that hash, byte for byte.
        code, shown, err = _run(["show", spec_hash], capsys)
        assert code == 0, err
        assert shown == sealed["pr_body_copy"]
        assert json.loads(shown)["goal"] == "Add a thing."

    def test_the_head_defaults_to_the_head_of_the_repository(self, repo, tmp_path, capsys):
        head = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)))
        assert code == 0, err
        assert sealed["head_sha"] == head
        assert Path(sealed["artifact"]).parent.name == head

    def test_the_head_is_part_of_the_content_so_a_new_head_is_a_new_hash(
        self, repo, tmp_path, capsys
    ):
        draft = _fill(_new_draft(repo, capsys))
        _, first, _ = _seal(tmp_path, repo, capsys, draft, head=HEAD_A)
        _, second, _ = _seal(tmp_path, repo, capsys, draft, head=HEAD_B)
        assert first["spec_hash"] != second["spec_hash"]

    @pytest.mark.parametrize("head", ["abc123", "A" * 40, "../x", "a" * 39])
    def test_a_head_that_is_not_a_full_sha_is_refused(self, repo, tmp_path, capsys, head):
        code, _, err = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=head)
        assert code == spec_verb.EXIT_USAGE
        assert repr(head) in err
        assert not spec_store.specs_root().exists()

    @pytest.mark.parametrize(
        "mutation",
        [{"goal": ""}, {"acceptance_criteria": []}, {"posture": "x"}],
    )
    def test_an_incomplete_draft_is_refused_and_nothing_is_stored(
        self, repo, tmp_path, capsys, mutation
    ):
        draft = _fill(_new_draft(repo, capsys), **mutation)
        code, _, err = _seal(tmp_path, repo, capsys, draft, head=HEAD_A)
        assert code == spec_verb.EXIT_DRAFT_INVALID
        assert "not a valid spec" in err
        assert not spec_store.specs_root().exists()

    def test_a_draft_that_is_not_json_or_not_readable_is_refused(self, repo, tmp_path, capsys):
        bad = tmp_path / "bad.json"
        bad.write_text("{nope", encoding="utf-8")
        argv = ["seal", "--repo-path", str(repo), "--head", HEAD_A]
        assert _run([*argv, "--file", str(bad)], capsys)[0] == spec_verb.EXIT_DRAFT_INVALID
        code, _, err = _run([*argv, "--file", str(tmp_path / "absent.json")], capsys)
        assert code == spec_verb.EXIT_IO_FAILED and "absent.json" in err

    def test_a_specs_root_inside_the_repository_under_review_is_refused(
        self, repo, tmp_path, capsys, monkeypatch
    ):
        inside = repo / "specs-here"
        monkeypatch.setenv(spec_store.SPECS_DIR_ENV_VAR, str(inside))
        code, _, err = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        assert code == spec_verb.EXIT_STORE_UNUSABLE
        assert "the pull request under review can write" in err
        assert spec_store.SPECS_DIR_ENV_VAR in err
        assert not inside.exists()

    def test_sealing_the_same_sealed_bytes_twice_changes_nothing(self, repo, tmp_path):
        root = tmp_path / "specs"
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        first = spec_store.write_sealed(root, sealed)
        stamp = first.stat().st_mtime_ns
        assert spec_store.write_sealed(root, sealed) == first
        assert first.stat().st_mtime_ns == stamp

    def test_a_different_file_under_an_existing_name_is_an_error_never_overwritten(self, tmp_path):
        root = tmp_path / "specs"
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        path = spec_store.write_sealed(root, sealed)
        path.write_bytes(b"tampered")
        with pytest.raises(spec_store.SpecInvalidError):
            spec_store.write_sealed(root, sealed)
        assert path.read_bytes() == b"tampered"


class TestImmutability:
    def test_a_sealed_spec_cannot_be_sealed_again_in_place(self, repo, tmp_path, capsys):
        _, sealed, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        code, _, err = _run(
            ["seal", "--file", sealed["artifact"], "--repo-path", str(repo), "--head", HEAD_B],
            capsys,
        )
        assert code == spec_verb.EXIT_REFUSED
        assert "supersedes" in err
        assert [p.name for p in spec_store.specs_root().rglob("*.json")] == [
            f"{sealed['spec_hash']}.json"
        ]

    def test_the_verbs_offer_no_way_to_edit_a_stored_spec(self, capsys):
        for verb in ("edit", "update", "write", "amend"):
            assert _run([verb, "--file", "x"], capsys)[0] == spec_verb.EXIT_USAGE

    def test_a_change_is_a_new_seal_chained_to_the_old_one(self, repo, tmp_path, capsys):
        _, first, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        original = Path(first["artifact"]).read_bytes()

        code, out, err = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--supersedes", first["spec_hash"]],
            capsys,
        )
        assert code == 0, err
        draft = json.loads(out)
        assert draft["supersedes"] == first["spec_hash"]
        assert draft["goal"] == "Add a thing."
        assert "head_sha" not in draft and "sealed_at" not in draft

        draft["goal"] = "Add a different thing."
        code, second, err = _seal(tmp_path, repo, capsys, draft, head=HEAD_B, name="two.json")
        assert code == 0, err
        assert second["spec_hash"] != first["spec_hash"]
        assert second["supersedes"] == first["spec_hash"]
        # The earlier seal is untouched and still retrievable.
        assert Path(first["artifact"]).read_bytes() == original
        assert _run(["show", first["spec_hash"]], capsys)[1] == original.decode("ascii")

        code, out, _ = _run(["show", second["spec_hash"], "--chain"], capsys)
        assert code == 0
        assert json.loads(out)["chain"] == [second["spec_hash"], first["spec_hash"]]

    def test_a_new_seal_by_another_role_cannot_supersede(self, repo, tmp_path, capsys):
        _, first, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)

        code, _, err = _run(
            ["new", "--role", OTHER_ROLE, "--repo-path", str(repo), "--supersedes", first["spec_hash"]],
            capsys,
        )
        assert code == spec_verb.EXIT_REFUSED and ROLE in err and OTHER_ROLE in err

        forged = _fill(_new_draft(repo, capsys, OTHER_ROLE), supersedes=first["spec_hash"])
        code, _, err = _seal(tmp_path, repo, capsys, forged, head=HEAD_B)
        assert code == spec_verb.EXIT_REFUSED
        assert len(list(spec_store.specs_root().rglob("*.json"))) == 1

    def test_superseding_a_spec_that_is_not_stored_is_refused(self, repo, tmp_path, capsys):
        missing = "f" * 64
        code, _, err = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--supersedes", missing], capsys
        )
        assert code == spec_verb.EXIT_NOT_FOUND and missing in err
        draft = _fill(_new_draft(repo, capsys), supersedes=missing)
        code, _, _ = _seal(tmp_path, repo, capsys, draft, head=HEAD_A)
        assert code == spec_verb.EXIT_NOT_FOUND

    def test_new_refreshes_the_verb_sourced_sections_and_keeps_the_authored_ones(
        self, repo, tmp_path, capsys
    ):
        draft = _fill(_new_draft(repo, capsys))
        draft["test_plan"]["plan"] = ["cover the refusal"]
        _, first, _ = _seal(tmp_path, repo, capsys, draft, head=HEAD_A)
        (repo / PROFILE_PATH).write_text(
            yaml.safe_dump({"class": "released-tool", "test": {"full": ["other", "{junit}"]}}),
            encoding="utf-8",
        )
        _, out, _ = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--supersedes", first["spec_hash"]],
            capsys,
        )
        again = json.loads(out)
        assert again["test_plan"]["declared"] == {"full": ["other", "{junit}"]}
        assert again["test_plan"]["plan"] == ["cover the refusal"]


class TestShow:
    def test_a_stored_file_that_no_longer_matches_its_name_is_refused(self, repo, tmp_path, capsys):
        _, sealed, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        artifact = Path(sealed["artifact"])
        tampered = json.loads(artifact.read_text(encoding="utf-8"))
        tampered["goal"] = "Something else entirely."
        artifact.write_text(json.dumps(tampered, sort_keys=True, separators=(",", ":")), encoding="utf-8")
        code, out, err = _run(["show", sealed["spec_hash"]], capsys)
        assert code == spec_verb.EXIT_STORED_INVALID
        assert out == ""
        assert "hashes to" in err and sealed["spec_hash"] in err

    def test_a_spec_filed_under_another_head_is_refused(self, repo, tmp_path, capsys):
        _, sealed, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        artifact = Path(sealed["artifact"])
        moved = artifact.parent.parent / HEAD_B
        moved.mkdir()
        artifact.rename(moved / artifact.name)
        code, _, err = _run(["show", sealed["spec_hash"]], capsys)
        assert code == spec_verb.EXIT_STORED_INVALID and "filed under" in err

    def test_an_unknown_hash_is_not_found(self, capsys):
        code, _, err = _run(["show", "e" * 64], capsys)
        assert code == spec_verb.EXIT_NOT_FOUND and "e" * 64 in err

    @pytest.mark.parametrize("bad", ["", "abc", "../etc/passwd", "E" * 64, "e" * 63])
    def test_a_hash_is_validated_before_it_touches_a_path(self, capsys, bad):
        for argv in (["show", bad], ["show", bad, "--chain"]):
            assert _run(argv, capsys)[0] in (spec_verb.EXIT_USAGE,)

    def test_a_symlinked_spec_is_not_followed(self, repo, tmp_path, capsys):
        _, sealed, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        artifact = Path(sealed["artifact"])
        outside = tmp_path / "outside.json"
        outside.write_bytes(artifact.read_bytes())
        artifact.unlink()
        os.symlink(outside, artifact)
        assert _run(["show", sealed["spec_hash"]], capsys)[0] == spec_verb.EXIT_STORED_INVALID


def _finding(file="src/a.py", line=3, rule="R1", **extra):
    return {"file": file, "line": line, "rule_id": rule, "message": "m", **extra}


def _store_record(comment_id, evaluated, opened, head=HEAD_B):
    """Write a review record the way `post` does: the open entries carry the id,
    the rule and the fingerprint; the evaluated findings carry the location and
    the reviewer's class text."""
    record = build_review_record(
        comment_id=comment_id, repo="some-owner/some-repo", pr=3, head_sha=head, role="reviewer",
        policy_base_sha=HEAD_A, spec_hash=None, engines=[], evaluated=evaluated,
        open_findings=[
            {"id": finding_state_id(f["file"], f["line"], f["rule_id"]), "rule_id": f["rule_id"],
             "head": head, **({"fingerprint": f["fingerprint"]} if "fingerprint" in f else {})}
            for f in opened
        ],
        advisory=[], dropped=[], chunks=None,
    )
    write_review_record(review_records_root(), record)


FP = "fp2:0123456789abcdef"


def _fold_draft(repo, capsys, *ids, extra=()):
    argv = ["new", "--role", ROLE, "--repo-path", str(repo), *extra]
    for comment_id in ids:
        argv += ["--from-review", comment_id]
    code, out, err = _run(argv, capsys)
    assert code == 0, err
    return json.loads(out), err


class TestFoldRemedy:
    def test_each_open_blocking_finding_is_copied_with_its_class_fields_and_an_empty_decision(
        self, repo, capsys
    ):
        blocking = _finding(fingerprint=FP, defect_class="denylist", class_fix="allowlist")
        other = _finding("src/b.py", 9, "R2")
        _store_record("101", [blocking, other, _finding("src/c.py", 1, "R3")], [blocking, other])
        draft, _ = _fold_draft(repo, capsys, "101")

        first, second = draft["remedy"]["items"]
        assert first == {
            "id": finding_state_id("src/a.py", 3, "R1"), "rule": "R1", "file": "src/a.py",
            "line": 3, "fingerprint": FP, "comment_id": "101",
            "reviewer_text": {"defect_class": "denylist", "class_fix": "allowlist"},
            "decision": None, "sites": [],
        }
        assert second["reviewer_text"] == {"defect_class": None, "class_fix": None}
        assert second["fingerprint"] is None
        assert draft["remedy"]["note"] is None and len(draft["remedy"]["items"]) == 2

    def test_a_finding_repeated_across_records_is_listed_once(self, repo, capsys):
        one = _finding()
        _store_record("101", [one], [one], head=HEAD_A)
        _store_record("102", [one], [one], head=HEAD_B)
        draft, _ = _fold_draft(repo, capsys, "101", "102")
        assert [e["comment_id"] for e in draft["remedy"]["items"]] == ["101"]

    def test_one_fingerprint_at_another_line_in_another_record_is_listed_once(self, repo, capsys):
        first = _finding(line=3, fingerprint=FP)
        moved = _finding(line=40, fingerprint=FP)
        _store_record("101", [first], [first], head=HEAD_A)
        _store_record("102", [moved], [moved], head=HEAD_B)
        draft, _ = _fold_draft(repo, capsys, "101", "102")
        assert [(e["comment_id"], e["line"]) for e in draft["remedy"]["items"]] == [("101", 3)]

    @pytest.mark.parametrize("with_record", [False, True], ids=["no-from-review", "no-blocking"])
    def test_without_a_blocking_finding_the_section_is_empty_and_says_so(
        self, repo, capsys, with_record
    ):
        ids = []
        if with_record:
            _store_record("101", [_finding()], [])
            ids = ["101"]
        draft, _ = _fold_draft(repo, capsys, *ids)
        assert draft["remedy"]["items"] == []
        assert draft["remedy"]["note"].startswith("no remedy entries")

    @pytest.mark.parametrize("bad", ["404", "not-an-id", "../x"])
    def test_a_missing_or_invalid_record_warns_naming_the_id_and_the_draft_is_written(
        self, repo, capsys, bad
    ):
        kept = _finding()
        _store_record("101", [kept], [kept])
        draft, err = _fold_draft(repo, capsys, bad, "101")
        assert f"comment {bad}" in err and "WARNING" in err
        assert [e["comment_id"] for e in draft["remedy"]["items"]] == ["101"]

    def test_a_record_file_that_is_not_valid_is_a_warning_too(self, repo, capsys):
        broken = review_records_root() / HEAD_A / "505.json"
        broken.parent.mkdir(parents=True)
        broken.write_text("{nope", encoding="utf-8")
        draft, err = _fold_draft(repo, capsys, "505")
        assert "comment 505" in err and draft["remedy"]["items"] == []
        assert "could be read" in draft["remedy"]["note"]

    def test_the_guidance_is_the_text_the_profile_brief_prints_for_the_builder(
        self, repo, capsys
    ):
        draft = _new_draft(repo, capsys)
        code = profile_verb.main(["brief", "--role", "builder", "--repo-path", str(repo)])
        assert code == 0
        brief = json.loads(capsys.readouterr().out)["guidance"]
        guidance = draft["guidance"]
        assert (guidance["tests"], guidance["classes"]) == (brief["tests"], brief["classes"])
        assert guidance["tests"] and guidance["classes"]

    def test_a_fold_spec_is_sealed_at_the_pr_head_and_show_prints_decisions_and_guidance(
        self, repo, tmp_path, capsys
    ):
        _, build, _ = _seal(tmp_path, repo, capsys, _fill(_new_draft(repo, capsys)), head=HEAD_A)
        finding = _finding(defect_class="denylist", class_fix="allowlist")
        _store_record("101", [finding], [finding])
        draft, _ = _fold_draft(repo, capsys, "101", extra=["--supersedes", build["spec_hash"]])
        draft["remedy"]["items"][0].update(decision="fix_shared", sites=["src/a.py", "src/b.py"])

        code, fold, err = _seal(tmp_path, repo, capsys, draft, head=HEAD_B, name="fold.json")
        assert code == 0, err
        assert fold["remedy_undecided"] == [] and err == ""
        code, shown, _ = _run(["show", fold["spec_hash"]], capsys)
        doc = json.loads(shown)
        assert doc["remedy"]["items"][0]["decision"] == "fix_shared"
        assert doc["remedy"]["items"][0]["sites"] == ["src/a.py", "src/b.py"]
        assert doc["guidance"]["tests"] == draft["guidance"]["tests"]

        # The chain lists both seals, and the review's reader takes the fold spec by hash
        # although it was sealed at a head that is not the default branch's.
        _, chain, _ = _run(["show", fold["spec_hash"], "--chain"], capsys)
        assert json.loads(chain)["chain"] == [fold["spec_hash"], build["spec_hash"]]
        facts = spec_review.load_spec_facts(fold["spec_hash"])
        assert facts.criterion_ids == ("ac-1", "ac-2")

    def test_a_remedy_entry_with_no_decision_is_listed_at_seal_and_the_seal_succeeds(
        self, repo, tmp_path, capsys
    ):
        finding = _finding()
        _store_record("101", [finding], [finding])
        draft, _ = _fold_draft(repo, capsys, "101")
        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(draft), head=HEAD_B)
        assert code == 0
        entry_id = finding_state_id("src/a.py", 3, "R1")
        assert sealed["remedy_undecided"] == [entry_id]
        assert entry_id in err and "no decision" in err

    @pytest.mark.parametrize("decision", ["fix_everywhere", ""])
    def test_a_decision_outside_the_three_values_is_not_sealed(
        self, repo, tmp_path, capsys, decision
    ):
        finding = _finding()
        _store_record("101", [finding], [finding])
        draft, _ = _fold_draft(repo, capsys, "101")
        draft["remedy"]["items"][0]["decision"] = decision
        code, _, _ = _seal(tmp_path, repo, capsys, _fill(draft), head=HEAD_B)
        assert code == spec_verb.EXIT_DRAFT_INVALID

    def test_a_spec_sealed_before_the_sections_existed_still_reads(self):
        old = _fill(model.new_draft(ROLE))
        del old["remedy"], old["guidance"]
        sealed = model.seal(old, head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z")
        assert model.parse_sealed(sealed.data, sealed.spec_hash) == sealed.doc
        assert model.undecided_remedy(sealed.doc) == []


IMPACT_FILES = {
    "pkg/__init__.py": "",
    "pkg/core.py": "X = 1\n",
    "pkg/lonely.py": "Y = 1\n",
    "pkg/a.py": "import pkg.core\n",
    "pkg/sub/__init__.py": "",
    "pkg/sub/b.py": "from .. import core\n",
    "pkg/sub/c.py": "from ..core import X\n",
    "tests/test_core.py": "from pkg.core import X\n",
    "web/app.js": "export const x = 1;\n",
    "web/app.ts": "export const x: number = 1;\n",
    "web/data.json": "{}\n",
    # JavaScript: src/a.js is imported by a source file (ESM) and by a test file (require).
    "src/a.js": "export const a = 1;\n",
    "src/b.js": "import x from './a';\n",
    "tests/a.test.js": "const a = require('../src/a.js');\n",
    # A JavaScript importer and a JavaScript target that cannot be tokenized.
    "js/ok.js": "export {};\n",
    "js/broken-importer.js": "import './ok';\nconst s = 'unterminated;\n",
    "js/broken-target.js": "const t = `open ${1}\n",
    # A browser/Node app tree: a directory import through index.js, ESM and require callers,
    # a __tests__ file and a *.test.js file.
    "app/src/main.js": "import { Widget } from './widgets';\nimport './util.js';\n",
    "app/src/util.js": "export const u = 1;\n",
    "app/src/widgets/index.js": "export class Widget {}\n",
    "app/src/widgets.test.js": "const { Widget } = require('./widgets/index.js');\n",
    "app/src/__tests__/widgets.js": "const { Widget } = require('../widgets');\n",
    # A JavaScript import of a Python file is no edge, and a mixed scope keeps each language's importers.
    "mix/a.js": "import './foo.py';\n",
    "mix/foo.py": "X = 1\n",
    "mix/bar.py": "import foo\n",
    # A JSX file with a multi-line closing tag still has its import edge.
    "jsx/dep.js": "export const d = 1;\n",
    "jsx/view.jsx": "import { d } from './dep';\nexport const V = () => (\n  <div>\n    <span>x</span>\n  </div>\n);\n",
    # A namespace package: no __init__.py, so a relative import has no layout to lean on.
    "ns/x.py": "X = 1\n",
    "ns/y.py": "from . import x\n",
    # One file that is not Python, one that is over the read bound, and one that parses.
    "bad/broken.py": "def (:\n",
    "bad/big.py": "#" * (600 * 1024) + "\n",
    "bad/ok.py": "X = 1\n",
    # No tools/__init__.py: a root-level `import helper` is not tools/helper.py, but a
    # script inside tools/ importing its sibling is.
    "tools/helper.py": "X = 1\n",
    "tools/runner.py": "X = 1\n",
    "tools/main.py": "import runner\n",
    "app.py": "import helper\n",
}
CORE_IMPORTERS = ["pkg/a.py", "pkg/sub/b.py", "pkg/sub/c.py"]
SKIPPED = ["bad/big.py", "bad/broken.py"]
JS_SKIPPED = ["js/broken-importer.js", "js/broken-target.js"]


def _impact_repo(tmp_path: Path) -> tuple[Path, str]:
    path = make_repo(tmp_path)
    return path, commit_files(path, IMPACT_FILES)


def _impact(repo: Path, capsys, *globs: str, ref: str | None = None) -> dict:
    argv = ["new", "--role", ROLE, "--repo-path", str(repo)]
    for glob in globs:
        argv += ["--path", glob]
    if ref is not None:
        argv += ["--ref", ref]
    code, out, err = _run(argv, capsys)
    assert code == 0, err
    return json.loads(out)["impact"]


class TestImpact:
    @pytest.mark.parametrize(
        ("path", "cap", "files", "more", "tests", "status", "language", "says", "skipped", "supersede"),
        [
            # AC1 and AC6: an absolute import, a relative from-import of the module and a
            # relative import through the module name are all resolved to it.
            ("pkg/core.py", None, CORE_IMPORTERS, 0, ["tests/test_core.py"], "computed", "python", "", SKIPPED, False),
            # AC2: nothing imports it.
            ("pkg/lonely.py", None, [], 0, [], "computed", "python", "", SKIPPED, False),
            # AC3 and AC7: another language is listed, not computed, and named with the covered list.
            ("web/app.ts", None, None, None, None, "not_computed", "typescript", "typescript is not covered (covered: python, javascript)", [], False),
            ("web/data.json", None, None, None, None, "not_computed", "json", "json is not covered (covered: python, javascript)", [], False),
            # JS-AC1: an ESM importer and a require from a test file.
            ("src/a.js", None, ["src/b.js"], 0, ["tests/a.test.js"], "computed", "javascript", "", JS_SKIPPED, False),
            # JS-AC6: nothing imports it; the rest of the JavaScript tree is not Python-read.
            ("web/app.js", None, [], 0, [], "computed", "javascript", "", JS_SKIPPED, False),
            # JS-AC5: a target that cannot be tokenized is not computed, with the reason, never none found.
            ("js/broken-target.js", None, None, None, None, "not_computed", "javascript", "unterminated template literal at line 1", JS_SKIPPED, False),
            # JS-AC5: an importer that cannot be tokenized leaves the list short and is named.
            ("js/ok.js", None, [], 0, [], "computed", "javascript", "", JS_SKIPPED, False),
            # An app-shaped tree: index.js reached by a directory import (ESM), by require of
            # the file, and from a __tests__ file; the two test files are not importers.
            ("app/src/widgets/index.js", None, ["app/src/main.js"], 0,
             ["app/src/__tests__/widgets.js", "app/src/widgets.test.js"], "computed", "javascript", "", JS_SKIPPED, False),
            # A JS import of './foo.py' adds no edge, and a mixed scope keeps the Python importer.
            (("mix/foo.py", "mix/a.js"), None, ["mix/bar.py"], 0, [], "computed", "python", "", sorted(SKIPPED + JS_SKIPPED), False),
            # A closing JSX tag is not a regular expression, so the import edge of the file is present.
            ("jsx/dep.js", None, ["jsx/view.jsx"], 0, [], "computed", "javascript", "", JS_SKIPPED, False),
            # AC4: over the cap, the cap is listed and the rest counted.
            ("pkg/core.py", 2, CORE_IMPORTERS[:2], 1, ["tests/test_core.py"], "computed", "python", "", SKIPPED, False),
            # A target whose own source is over the read bound or is not Python is not
            # computed, with the reason, and is never reported as having no importers.
            ("bad/big.py", None, None, None, None, "not_computed", "python", "could not be read", SKIPPED, False),
            ("bad/broken.py", None, None, None, None, "not_computed", "python", "could not be parsed", SKIPPED, False),
            # An importer that cannot be parsed leaves the list short; the section names it.
            ("bad/ok.py", None, [], 0, [], "computed", "python", "", SKIPPED, False),
            # A relative import in a package with no __init__.py resolves from the file's path.
            ("ns/x.py", None, ["ns/y.py"], 0, [], "computed", "python", "", SKIPPED, False),
            # `import pkg.core` also runs pkg/__init__.py, so the package is imported too.
            ("pkg/__init__.py", None, CORE_IMPORTERS, 0, ["tests/test_core.py"], "computed", "python", "", SKIPPED, False),
            # A file outside any package is not known by its bare stem to a root importer.
            ("tools/helper.py", None, [], 0, [], "computed", "python", "", SKIPPED, False),
            # A script imports its sibling in the same directory.
            ("tools/runner.py", None, ["tools/main.py"], 0, [], "computed", "python", "", SKIPPED, False),
            # A superseding draft with no --path recomputes the carried section at its own
            # ref: pkg/a.py stops importing the core in the later commit.
            ("pkg/core.py", None, CORE_IMPORTERS[1:], 0, ["tests/test_core.py"], "computed", "python", "", SKIPPED, True),
        ],
        ids=[
            "importers-and-tests", "none-found", "typescript-not-computed", "json-not-computed",
            "javascript-importers-and-tests", "javascript-none-found",
            "javascript-unparsable-target", "javascript-unparsable-importer-is-named",
            "javascript-app-tree-directory-import-and-tests",
            "javascript-import-of-a-python-file-is-no-edge-in-a-mixed-scope", "jsx-closing-tag-is-not-a-regex",
            "over-the-cap",
            "unreadable-target", "unparsable-target", "unparsable-importer-is-named",
            "namespace-package-relative-import", "dotted-import-counts-the-package-init",
            "root-import-is-not-a-non-package-stem", "sibling-import-in-a-non-package-dir",
            "superseding-draft-recomputes-the-carried-section",
        ],
    )
    def test_each_path_lists_its_importers_or_says_why_not(
        self, tmp_path, capsys, monkeypatch, path, cap, files, more, tests, status, language, says,
        skipped, supersede,
    ):
        repo, head = _impact_repo(tmp_path)
        globs = (path,) if isinstance(path, str) else path
        path = globs[0]
        if cap is not None:
            monkeypatch.setattr("clagentic_loadout.spec.impact.IMPORTER_CAP", cap)

        if supersede:
            first = _fill(json.loads(_run(
                ["new", "--role", ROLE, "--repo-path", str(repo), "--path", path], capsys
            )[1]))
            code, sealed, err = _seal(tmp_path, repo, capsys, first)
            assert code == 0, err
            head = commit_files(repo, {"pkg/a.py": "import os\n"})
            code, out, err = _run(
                ["new", "--role", ROLE, "--repo-path", str(repo), "--supersedes", sealed["spec_hash"]],
                capsys,
            )
            assert code == 0, err
            section = json.loads(out)["impact"]
        else:
            section = _impact(repo, capsys, *globs)

        assert section["head_sha"] == head
        assert section["languages_covered"] == ["python", "javascript"]
        assert section["cap"] == (cap or 25)
        assert len(section["items"]) == len(globs)
        item = section["items"][0]
        assert (item["path"], item["status"], item["language"]) == (path, status, language)
        assert section["skipped_paths"] == skipped and section["skipped_files"] == len(skipped)
        if files is None:
            assert item["importers"] is None and item["importing_tests"] is None
            assert says in item["note"]
            return
        assert (item["importers"], item["importing_tests"]) == (
            {"files": files, "more": more},
            {"files": tests, "more": 0},
        )
        assert (item["note"] or "").startswith("none found") == (not files and not tests)

    @pytest.mark.parametrize(
        ("source", "edge"),
        [
            # JS-AC2: each import form with a plain literal makes an edge.
            ("import x from './a';", True),
            ("import x, { y as z, default as w } from './a';", True),
            ("import {\n  y,\n  z,\n} from './a';", True),
            ("import * as ns from './a';", True),
            ("import './a';", True),
            ("export { x } from './a';", True),
            ("export * from './a';", True),
            ("export * as ns from './a';", True),
            ("const x = require('./a');", True),
            ("import('./a').then(f);", True),
            ("require(`./a`);", True),
            ("const t = `${require('./a')}`;", True),
            ("const q = a / b; const r = c / d; require('./a');", True),
            ("n++ / 2; require('./a');", True),
            # JS-AC2: a non-literal, a substituted template, a bare, node: or absolute
            # specifier, a path outside the tree, a missing file and a member call are not edges.
            ("require(name);", False),
            ("require('./' + name);", False),
            ("require(`./${name}`);", False),
            ("import(`./${name}`);", False),
            ("import x from 'pkg';", False),
            ("import fs from 'node:fs';", False),
            ("import x from '/abs/a';", False),
            ("import x from '../../outside';", False),
            ("import x from './missing';", False),
            ("obj.require('./a');", False),
            ("export { x };", False),
            ("export const a = 1;", False),
            # JS-AC3: the text of a comment, string, template or regular expression is not code.
            ("// import './a'", False),
            ("/* require('./a') */", False),
            ("const s = \"import './a'\";", False),
            ("const s = 'require(\\'./a\\')';", False),
            ("const t = `import './a'`;", False),
            ("const t = `x ${1} require('./a')`;", False),
            ("const r = /require\\('\\.\\/a'\\)/;", False),
            ("const r = /[/]import '.\\/a'/;", False),
        ],
    )
    def test_javascript_import_forms_and_the_text_that_is_not_code(self, source, edge):
        from clagentic_loadout.spec.js_imports import build_js_import_graph

        tree = ["main.js", "a.js"]
        graph = build_js_import_graph({"main.js": source}, tree)

        assert graph.unparsed == ()
        assert dict(graph.importers) == ({"a.js": frozenset({"main.js"})} if edge else {})

    @pytest.mark.parametrize(
        ("importer", "specifier", "target"),
        [
            # JS-AC4: exact path, then the extensions in order, then index in the same order.
            ("main.js", "./dir", "dir/index.js"),
            ("main.js", "./a", "a.js"),
            ("main.js", "./a.mjs", "a.mjs"),
            ("main.js", "./x", "x.js"),
            ("main.js", "./x/", "x/index.js"),
            ("main.js", "./only", "only/index.cjs"),
            ("main.js", "./sub/../a", "a.js"),
            ("sub/m.js", "../a", "a.js"),
            ("main.js", "./nothing", None),
            ("main.js", ".", None),
            ("sub/m.js", "../../up", None),
        ],
    )
    def test_javascript_specifiers_resolve_in_the_documented_order(self, importer, specifier, target):
        from clagentic_loadout.spec.js_imports import build_js_import_graph

        tree = ["main.js", "a.js", "a.mjs", "dir/index.js", "dir/index.mjs", "x.js", "x/index.js",
                "only/index.cjs", "sub/m.js"]
        graph = build_js_import_graph({importer: f"import '{specifier}';"}, tree)

        assert {t: set(v) for t, v in graph.importers.items() if t != importer} == (
            {target: {importer}} if target else {}
        )

    @pytest.mark.parametrize(
        "source",
        ["const s = 'open;\n", "/* open", "const t = `open", "const t = `a ${b", "const r = /open\n"],
        ids=["string", "comment", "template", "template-substitution", "regex"],
    )
    def test_a_javascript_source_that_cannot_be_tokenized_is_unparsed_with_its_reason(self, source):
        from clagentic_loadout.spec.js_imports import build_js_import_graph

        graph = build_js_import_graph({"main.js": source}, ["main.js"])

        assert graph.unparsed == ("main.js",)
        assert "unterminated" in graph.unparsed_reasons["main.js"] and "line 1" in graph.unparsed_reasons["main.js"]

    def test_two_refs_that_differ_in_imports_give_two_sections_each_with_its_own_head(
        self, tmp_path, capsys
    ):
        repo, first = _impact_repo(tmp_path)
        second = commit_files(repo, {"pkg/a.py": "import os\n"})

        before = _impact(repo, capsys, "pkg/core.py", ref=first)
        after = _impact(repo, capsys, "pkg/core.py", ref=second)

        assert (before["head_sha"], after["head_sha"]) == (first, second)
        assert before["items"][0]["importers"]["files"] == CORE_IMPORTERS
        assert after["items"][0]["importers"]["files"] == CORE_IMPORTERS[1:]

    def test_the_tree_is_read_at_the_ref_not_from_the_working_tree(self, tmp_path, capsys):
        repo, head = _impact_repo(tmp_path)
        (repo / "pkg" / "a.py").write_text("import os\n", encoding="utf-8")
        (repo / "pkg" / "extra.py").write_text("import pkg.core\n", encoding="utf-8")

        section = _impact(repo, capsys, "pkg/core.py", ref=head)

        assert section["items"][0]["importers"]["files"] == CORE_IMPORTERS

    def test_a_glob_that_matches_nothing_is_reported_not_refused(self, tmp_path, capsys):
        repo, _ = _impact_repo(tmp_path)

        none = _impact(repo, capsys, "nothing/*.py")

        assert [(i["status"], i["path"]) for i in none["items"]] == [("no_match", "nothing/*.py")]

    def test_a_spec_with_an_impact_section_seals_and_one_sealed_without_it_still_reads(
        self, tmp_path, capsys
    ):
        repo, _ = _impact_repo(tmp_path)
        draft = json.loads(_run(["new", "--role", ROLE, "--repo-path", str(repo), "--path", "pkg/core.py"], capsys)[1])
        assert list(draft).index("impact") == list(draft).index("consumers") + 1

        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(draft))
        assert code == 0, err
        assert model.parse_sealed(sealed["pr_body_copy"].encode("ascii"), sealed["spec_hash"])["impact"]

        old = _fill(model.new_draft(ROLE))
        assert "impact" not in old
        legacy = model.seal(old, head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z")
        assert "impact" not in model.parse_sealed(legacy.data, legacy.spec_hash)

    @pytest.mark.parametrize(
        ("path", "files"), [("pkg/core.py", CORE_IMPORTERS), ("src/a.js", ["src/b.js"])], ids=["python", "javascript"]
    )
    def test_the_computation_makes_no_network_call(self, tmp_path, capsys, monkeypatch, path, files):
        import socket

        def refuse(*_args, **_kwargs):
            raise AssertionError("network access is forbidden while the impact section is computed")

        repo, _ = _impact_repo(tmp_path)
        monkeypatch.setattr(socket.socket, "connect", refuse)
        monkeypatch.setattr(socket, "getaddrinfo", refuse)

        section = _impact(repo, capsys, path)

        assert section["items"][0]["importers"]["files"] == files

    def test_a_spec_sealed_with_a_javascript_item_validates_against_the_schema(self, tmp_path, capsys):
        repo, _ = _impact_repo(tmp_path)
        draft = json.loads(_run(["new", "--role", ROLE, "--repo-path", str(repo), "--path", "src/a.js"], capsys)[1])

        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(draft))

        assert code == 0, err
        item = model.parse_sealed(sealed["pr_body_copy"].encode("ascii"), sealed["spec_hash"])["impact"]["items"][0]
        assert (item["language"], item["importers"]["files"]) == ("javascript", ["src/b.js"])


CALL_SITE_FILES = {
    "lib/__init__.py": "",
    "lib/resolve.py": "def resolve(x):\n    return x\n\n\nclass Resolver:\n    pass\n",
    "lib/use_a.py": "import lib.resolve as m\n\nvalue = m.resolve(1)\n",
    "lib/use_b.py": "import lib.resolve as m\n\n\ndef go():\n    return m.resolve(2)\n",
    "lib/use_c.py": "from lib.resolve import resolve\n",
    "lib/strings.py": "text = 'resolve'\n# resolve(1)\n",
    "tests/test_resolve.py": "import lib.resolve as m\n\n\ndef test_it():\n    assert m.resolve(3) == 3\n",
    "lib/a.js": "function doThing() {}\nmodule.exports = { doThing };\n",
    "lib/b.js": "const { doThing } = require('./a');\ndoThing();\n",
    "test/a.test.js": "const a = require('../lib/a');\na.doThing();\n// doThing in a comment\nconst s = 'doThing';\n",
    "lib/esm.js": "export function run() {}\n",
    "lib/esm_user.js": "import { run } from './esm';\nrun();\n",
    "scripts/run.sh": "tool-one --help\ntool-one-extra x\n",
    "scripts/other.sh": "my-tool-one\ntool-one_x\nTOOL-ONE\n",
    "docs/verbs.md": "Use `tool-one` here.\n",
    "tests/test_x.py": "main(['tool-one'])\n",
    "web/app.ts": "export const x = 1;\n",
    "bad/broken.py": "def (:\n",
    "js/bad.js": "const s = 'open;\n",
}
RESOLVE_SITES = [("lib/use_a.py", 3), ("lib/use_b.py", 5), ("lib/use_c.py", 1), ("tests/test_resolve.py", 5)]


def _call_sites_repo(tmp_path: Path) -> tuple[Path, str]:
    path = make_repo(tmp_path)
    return path, commit_files(path, CALL_SITE_FILES)


def _call_sites_draft(repo: Path, capsys, *symbols: str, extra=()) -> dict:
    argv = ["new", "--role", ROLE, "--repo-path", str(repo), *extra]
    for symbol in symbols:
        argv += ["--symbol", symbol]
    code, out, err = _run(argv, capsys)
    assert code == 0, err
    return json.loads(out)


def _site_tuples(item: dict) -> list[tuple[str, int]]:
    return [(site["file"], site["line"]) for site in item["sites"]]


class TestCallSites:
    @pytest.mark.parametrize(
        ("symbol", "kind", "status", "language", "sites", "says"),
        [
            # Name, Attribute and from-import nodes are sites; the definition, a string and a
            # comment are not, and a dotted module path is not a reference to the function.
            ("lib/resolve.py::resolve", "function", "computed", "python", RESOLVE_SITES, ""),
            ("lib/resolve.py::Resolver", "function", "computed", "python", [], "none found"),
            # An identifier in JavaScript, outside strings and comments; the export statement
            # and the declaration in the defining file are not sites.
            ("lib/a.js::doThing", "export", "computed", "javascript",
             [("lib/b.js", 1), ("lib/b.js", 2), ("test/a.test.js", 2)], ""),
            ("lib/esm.js::run", "export", "computed", "javascript",
             [("lib/esm_user.js", 1), ("lib/esm_user.js", 2)], ""),
            # A verb is a whole word in any tracked text file; '-' is part of the word.
            ("tool-one", "verb", "computed", "text",
             [("docs/verbs.md", 1), ("scripts/run.sh", 1), ("tests/test_x.py", 1)], ""),
            # Nothing is guessed: each of these says which condition it met, and the verb exits 0.
            ("lib/missing.py::x", "function", "no_match", "python", [], "no file 'lib/missing.py'"),
            ("lib/resolve.py::nothing", "function", "no_match", "python", [], "not a top-level function or class"),
            ("lib/a.js::nothing", "export", "no_match", "javascript", [], "not an exported or declared name"),
            ("web/app.ts::x", "function", "not_computed", "typescript", [], "typescript is not covered"),
            ("bad/broken.py::x", "function", "not_computed", "python", [], "could not be parsed as Python"),
            ("js/bad.js::x", "export", "not_computed", "javascript", [], "could not be parsed as JavaScript"),
        ],
    )
    def test_each_symbol_lists_its_call_sites_or_says_why_not(
        self, tmp_path, capsys, symbol, kind, status, language, sites, says
    ):
        repo, head = _call_sites_repo(tmp_path)

        section = _call_sites_draft(repo, capsys, symbol)["call_sites"]

        assert section["head_sha"] == head and section["cap"] == 50
        (item,) = section["items"]
        assert (item["symbol"], item["kind"], item["status"], item["language"]) == (symbol, kind, status, language)
        assert item["defined_in"] == (symbol.split("::")[0] if "::" in symbol else None)
        assert _site_tuples(item) == sites and item["more"] == 0
        assert all(
            site["disposition"] is None and site["test"] == site["file"].startswith(("tests/", "test/"))
            for site in item["sites"]
        )
        assert says in (item["note"] or "")

    def test_sites_past_the_cap_are_counted_and_the_section_follows_impact(self, tmp_path, capsys, monkeypatch):
        repo, _ = _call_sites_repo(tmp_path)
        monkeypatch.setattr("clagentic_loadout.spec.call_sites.CALL_SITE_CAP", 2)

        draft = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve", extra=("--path", "lib/resolve.py"))

        assert list(draft).index("call_sites") == list(draft).index("impact") + 1
        item = draft["call_sites"]["items"][0]
        assert (_site_tuples(item), item["more"], draft["call_sites"]["cap"]) == (RESOLVE_SITES[:2], 2, 2)

    @pytest.mark.parametrize("value", ["", "a b", "::name", "path::", "lib/x.py::two words"])
    def test_a_value_that_is_not_a_symbol_is_a_usage_error(self, tmp_path, capsys, value):
        repo, _ = _call_sites_repo(tmp_path)

        code, _, err = _run(["new", "--role", ROLE, "--repo-path", str(repo), "--symbol", value], capsys)

        assert code == spec_verb.EXIT_USAGE and "is not a symbol" in err

    def test_the_tree_is_read_at_the_ref_and_no_symbol_means_no_section(self, tmp_path, capsys):
        repo, first = _call_sites_repo(tmp_path)
        commit_files(repo, {"lib/use_a.py": "import os\n"})

        before = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve", extra=("--ref", first))
        after = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve")

        assert ("lib/use_a.py", 3) in _site_tuples(before["call_sites"]["items"][0])
        assert ("lib/use_a.py", 3) not in _site_tuples(after["call_sites"]["items"][0])
        assert "call_sites" not in _call_sites_draft(repo, capsys)

    def test_a_tree_that_cannot_be_read_gives_not_computed_items_and_the_draft_is_still_written(
        self, tmp_path, capsys, monkeypatch
    ):
        from clagentic_loadout.review.policy_reader import GitPolicyReader, PolicyReadError

        repo, _ = _call_sites_repo(tmp_path)

        def unreadable(self, base):
            raise PolicyReadError("cannot read the file list")

        monkeypatch.setattr(GitPolicyReader, "list_paths", unreadable)

        section = _call_sites_draft(repo, capsys, "tool-one")["call_sites"]

        assert section["head_sha"] is not None and section["source"] is None
        assert [(i["symbol"], i["status"]) for i in section["items"]] == [("tool-one", "not_computed")]
        assert "cannot read the file list" in section["items"][0]["note"]

    @pytest.mark.parametrize(
        ("edit", "kept"),
        [
            # AC8: the site moved down a line keeps its disposition; a site added later has none.
            ("import lib.resolve as m\n\n\nvalue = m.resolve(1)\n", True),
            # The same line number with other text is another site.
            ("import lib.resolve as m\n\nvalue = m.resolve(99)\n", False),
        ],
        ids=["moved-down-keeps-it", "edited-line-loses-it"],
    )
    def test_a_superseding_draft_recomputes_the_sites_and_carries_a_disposition_by_line_text(
        self, tmp_path, capsys, edit, kept
    ):
        repo, _ = _call_sites_repo(tmp_path)
        first = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve")
        for site in first["call_sites"]["items"][0]["sites"]:
            if site["file"] == "lib/use_a.py":
                site["disposition"] = {"kind": "unaffected", "note": "reads the value only"}
        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(first))
        assert code == 0, err
        head = commit_files(repo, {"lib/use_a.py": edit, "lib/use_d.py": "import lib.resolve as m\nm.resolve(4)\n"})

        code, out, err = _run(
            ["new", "--role", ROLE, "--repo-path", str(repo), "--supersedes", sealed["spec_hash"]], capsys
        )

        assert code == 0, err
        section = json.loads(out)["call_sites"]
        assert section["head_sha"] == head
        by_site = {(s["file"], s["line"]): s["disposition"] for s in section["items"][0]["sites"]}
        moved = ("lib/use_a.py", 4 if "\n\n\n" in edit else 3)
        assert by_site[moved] == ({"kind": "unaffected", "note": "reads the value only"} if kept else None)
        assert by_site[("lib/use_d.py", 2)] is None and by_site[("lib/use_b.py", 5)] is None

    def test_seal_names_each_undisposed_site_and_still_succeeds(self, tmp_path, capsys):
        repo, _ = _call_sites_repo(tmp_path)
        draft = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve")
        for site in draft["call_sites"]["items"][0]["sites"]:
            if site["file"] == "lib/use_a.py":
                site["disposition"] = {"kind": "changed", "note": "updated in this change"}

        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(draft))

        assert code == 0
        undisposed = [(s["file"], s["line"]) for s in sealed["call_sites_undisposed"]]
        assert undisposed == [("lib/use_b.py", 5), ("lib/use_c.py", 1), ("tests/test_resolve.py", 5)]
        assert all(s["symbol"] == "lib/resolve.py::resolve" for s in sealed["call_sites_undisposed"])
        assert "lib/resolve.py::resolve at lib/use_b.py:5" in err and "lib/use_a.py:3" not in err
        assert model.undisposed_call_sites(json.loads(sealed["pr_body_copy"])) == sealed["call_sites_undisposed"]

    @pytest.mark.parametrize(
        "disposition",
        [{"kind": "unaffected", "note": ""}, {"kind": "later", "note": "x"}, "unaffected", {"kind": "out"}],
        ids=["empty-note", "unknown-kind", "bare-string", "no-note"],
    )
    def test_a_disposition_in_none_of_the_three_forms_is_not_sealed(self, tmp_path, capsys, disposition):
        repo, _ = _call_sites_repo(tmp_path)
        draft = _call_sites_draft(repo, capsys, "lib/resolve.py::resolve")
        draft["call_sites"]["items"][0]["sites"][0]["disposition"] = disposition

        code, _, err = _seal(tmp_path, repo, capsys, _fill(draft))

        assert code == spec_verb.EXIT_DRAFT_INVALID

    def test_a_spec_sealed_without_call_sites_still_reads_and_has_nothing_undisposed(self):
        old = _fill(model.new_draft(ROLE))
        assert "call_sites" not in old
        legacy = model.seal(old, head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z")

        assert model.parse_sealed(legacy.data, legacy.spec_hash) == legacy.doc
        assert model.undisposed_call_sites(legacy.doc) == []

    def test_a_console_shaped_javascript_tree_seals_its_impact_and_show_returns_it_byte_for_byte(
        self, tmp_path, capsys
    ):
        repo = make_repo(tmp_path)
        commit_files(repo, {
            "lib/server-auth.js": "module.exports = { check };\nfunction check() {}\n",
            "lib/routes.js": "const auth = require('./server-auth');\nauth.check();\n",
            "test/server-auth.test.js": "const auth = require('../lib/server-auth');\n",
        })
        draft = json.loads(_run(["new", "--role", ROLE, "--repo-path", str(repo), "--path", "lib/*.js"], capsys)[1])
        items = {item["path"]: item for item in draft["impact"]["items"]}
        assert items["lib/server-auth.js"]["importers"]["files"] == ["lib/routes.js"]
        assert items["lib/server-auth.js"]["importing_tests"]["files"] == ["test/server-auth.test.js"]

        code, sealed, err = _seal(tmp_path, repo, capsys, _fill(draft))
        assert code == 0, err
        code, shown, err = _run(["show", sealed["spec_hash"]], capsys)

        assert code == 0 and shown == sealed["pr_body_copy"]


@pytest.mark.parametrize("surface", ["import-graph", "call-site-finder"])
def test_reading_python_with_an_invalid_escape_sequence_prints_no_syntax_warning(surface):
    import warnings

    from clagentic_loadout.spec.import_graph import build_import_graph
    from clagentic_loadout.spec.references import python_top_level

    source = 'PATTERN = "\\d+"\n\n\ndef find():\n    return PATTERN\n'
    with warnings.catch_warnings():
        warnings.simplefilter("error", SyntaxWarning)
        if surface == "import-graph":
            graph = build_import_graph({"a.py": source}, ["a.py"])
            assert graph.unparsed == ()
        else:
            assert python_top_level(source) == {"find": 4}


class TestSpecsRoot:
    def test_environment_variable_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv(spec_store.SPECS_DIR_ENV_VAR, str(tmp_path / "from-env"))
        assert spec_store.specs_root() == tmp_path / "from-env"

    def test_user_config_setting_is_next(self, monkeypatch, tmp_path):
        monkeypatch.delenv(spec_store.SPECS_DIR_ENV_VAR, raising=False)
        (tmp_path / "config.yaml").write_text(
            f"specs:\n  dir: {tmp_path / 'from-config'}\n", encoding="utf-8"
        )
        assert spec_store.specs_root(config_root=tmp_path) == tmp_path / "from-config"

    def test_an_unconfigured_deployment_still_has_a_working_default(self, monkeypatch, tmp_path):
        monkeypatch.delenv(spec_store.SPECS_DIR_ENV_VAR, raising=False)
        monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "cfg")
        assert spec_store.specs_root() == tmp_path / "cfg" / "state" / "specs"

    def test_a_root_the_tool_creates_is_0755_and_a_shared_root_sets_the_head_mode(self, tmp_path):
        sealed = model.seal(
            _fill(model.new_draft(ROLE)), head_sha=HEAD_A, sealed_at="2026-01-01T00:00:00Z"
        )
        created = tmp_path / "created"
        path = spec_store.write_sealed(created, sealed)
        assert created.stat().st_mode & 0o7777 == 0o755
        assert path.parent.stat().st_mode & 0o7777 == 0o755
        assert path.stat().st_mode & 0o777 == 0o644

        shared = tmp_path / "shared"
        shared.mkdir()
        os.chmod(shared, 0o2775)
        path = spec_store.write_sealed(shared, sealed)
        assert path.parent.stat().st_mode & 0o7777 == 0o2775

    def test_an_unreadable_root_is_the_documented_store_exit(self, monkeypatch, tmp_path, capsys):
        root = tmp_path / "specs"
        root.mkdir()
        monkeypatch.setenv(spec_store.SPECS_DIR_ENV_VAR, str(root))
        real_iterdir = Path.iterdir

        def deny(self):
            if self == root:
                raise PermissionError(13, "Permission denied", str(self))
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", deny)
        code, _, err = _run(["show", "e" * 64], capsys)
        assert code == spec_verb.EXIT_STORE_UNUSABLE
        assert "cannot read the specs root" in err


class TestStoreSharing:
    def test_verdict_records_and_specs_share_one_store_implementation(self):
        from clagentic_loadout import artifact_store
        from clagentic_loadout.verdict_record import store as record_store

        assert record_store.StoreError is artifact_store.StoreError
        assert spec_store.StoreError is artifact_store.StoreError
        assert record_store.RootUnreadableError is artifact_store.RootUnreadableError


@pytest.mark.parametrize(
    "module",
    [
        "clagentic_loadout.artifact_store",
        "clagentic_loadout.spec.store",
        "clagentic_loadout.spec.verb",
        "clagentic_loadout.review.spec_review",
        "clagentic_loadout.review.profile_brief",
    ],
)
def test_each_entry_module_imports_first_in_a_fresh_interpreter(module):
    src_dir = str(Path(__file__).resolve().parent.parent / "src")
    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        capture_output=True, text=True, env={**os.environ, "PYTHONPATH": src_dir},
    )
    assert result.returncode == 0, result.stderr
