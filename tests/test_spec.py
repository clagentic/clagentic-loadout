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

from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.review.profile_brief import declared_tests, profile_brief
from clagentic_loadout.review.profile_rules import rule_statements
from clagentic_loadout.review.repo_profile import parse_profile_document
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.spec import verb as spec_verb
from clagentic_loadout.transport import provider_config
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
        # No source exists yet for these: present, empty, and saying so.
        for section in ("group_rules", "consumers"):
            assert draft[section] == {"source": None, "note": "no source yet", "items": []}
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
