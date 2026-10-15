"""The ruling model, the registry reader (trust rules) and the registry writer."""

from __future__ import annotations

import datetime as dt
import os
import re
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.review import ruling_provider as rp
from clagentic_loadout.review.ruling_store import (
    DuplicateRulingError,
    RulingStoreError,
    UntrustedRegistryError,
    record_ruling,
)
from clagentic_loadout.review.rulings import (
    RulingFormatError,
    parse_rulings,
    rulings_document,
    rulings_for_scope,
)
from tests._ruling_support import FP, OTHER_FP, REPO, TODAY, registry_dir, ruling, write_registry


def _entry(**overrides):
    return {**ruling().as_entry(), **overrides}


class TestModel:
    def test_a_registry_round_trips(self):
        original = [ruling(), ruling(fingerprint=OTHER_FP, reference="ABC-1", expires=dt.date(2026, 11, 1))]

        parsed = parse_rulings(yaml.safe_load(yaml.safe_dump(rulings_document(original))))

        # The stored form names the expiry an accepted_risk ruling gets by default.
        assert [r.as_entry() for r in parsed] == [r.as_entry() for r in original]
        assert [r.effective_expiry for r in parsed] == [r.effective_expiry for r in original]

    @pytest.mark.parametrize(
        "missing", ["repo", "pr", "fingerprint", "rule_id", "path", "kind", "rationale", "by",
                    "requested_by", "source_ref", "at"],
    )
    def test_every_required_key_is_required(self, missing):
        entry = _entry()
        del entry[missing]

        with pytest.raises(RulingFormatError, match=missing):
            parse_rulings({"rulings": [entry]})

    @pytest.mark.parametrize(
        "bad,match",
        [
            ({"kind": "waived"}, "kind"),
            ({"pr": 0}, "pr"),
            ({"pr": True}, "pr"),
            ({"expires": "2020-01-01"}, "before"),
            ({"kind": "mitigated", "expires": None, "reference": "ABC-1"}, "needs"),
            ({"extra": 1}, "unknown or misplaced"),
        ],
    )
    def test_a_malformed_entry_rejects_the_whole_registry(self, bad, match):
        with pytest.raises(RulingFormatError, match=match):
            parse_rulings({"rulings": [_entry(), _entry(**bad)]})

    def test_a_reference_must_match_the_configured_pattern(self):
        pattern = re.compile(r"\A[A-Z]+-[0-9]+\Z")
        entry = _entry(reference="not valid", expires="2026-11-01")

        with pytest.raises(RulingFormatError, match="reference pattern"):
            parse_rulings({"rulings": [entry]}, reference_pattern=pattern)

    def test_scope_selects_one_repository_and_one_pull_request(self):
        mine, other_pr, other_repo = ruling(), ruling(pr=2), ruling(repo="some-owner/other-repo")

        assert rulings_for_scope([mine, other_pr, other_repo], REPO.upper(), 1) == (mine,)

    def test_matching_needs_the_fingerprint_and_checks_rule_and_path(self):
        r = ruling()

        assert r.matches("src/a.py", "R1", FP)
        assert r.matches("./src/a.py", "R1", FP)
        assert not r.matches("src/a.py", "R1", OTHER_FP)
        assert not r.matches("src/a.py", "R1", None)
        assert not r.matches("src/a.py", "R2", FP)
        assert not r.matches("src/b.py", "R1", FP)


class TestConfiguration:
    def test_no_path_means_no_registry(self, tmp_path):
        assert rp.resolve_ruling_provider(tmp_path, env={}) is None

    def test_the_user_level_path_is_used(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"rulings": {"path": "/srv/rulings.yaml"}}), encoding="utf-8"
        )

        provider = rp.resolve_ruling_provider(tmp_path, env={})

        assert provider.path == Path("/srv/rulings.yaml")

    def test_the_environment_wins_over_the_file(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"rulings": {"path": "/srv/a.yaml"}}), encoding="utf-8"
        )

        provider = rp.resolve_ruling_provider(tmp_path, env={rp.RULINGS_FILE_ENV_VAR: "/srv/b.yaml"})

        assert provider.path == Path("/srv/b.yaml")

    def test_a_relative_path_is_refused_and_reported(self, tmp_path):
        provider = rp.resolve_ruling_provider(tmp_path, env={rp.RULINGS_FILE_ENV_VAR: "rulings.yaml"})

        read = provider.rulings_for(REPO, 1)
        assert read.rulings == ()
        assert "must be absolute" in read.degraded[0]

    def test_a_bad_reference_pattern_is_reported_not_ignored(self, tmp_path):
        (tmp_path / "config.yaml").write_text(
            yaml.safe_dump({"rulings": {"path": "/srv/a.yaml", "reference_pattern": "("}}),
            encoding="utf-8",
        )

        read = rp.resolve_ruling_provider(tmp_path, env={}).rulings_for(REPO, 1)

        assert "not a valid regex" in read.degraded[0]

    def test_no_agent_or_role_name_is_configured_here(self, tmp_path):
        assert not hasattr(rp, "load_authorized_roles")
        assert not hasattr(rp, "DEFAULT_AUTHORIZED_ROLES")


class TestReader:
    def test_it_reads_only_the_rulings_for_the_scope(self, tmp_path):
        path = write_registry(tmp_path, [ruling(), ruling(pr=2, fingerprint=OTHER_FP)])

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert [r.fingerprint for r in read.rulings] == [FP]
        assert read.degraded == ()

    def test_a_missing_registry_is_empty_and_not_degraded(self, tmp_path):
        read = rp.FileRulingProvider(registry_dir(tmp_path) / "none.yaml").rulings_for(REPO, 1)

        assert read == rp.RulingRead()

    def test_a_registry_inside_the_pull_requests_checkout_is_refused(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()
        path = write_registry(checkout, [ruling()])

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1, untrusted_roots=[checkout])

        assert read.rulings == ()
        assert "not a trusted source" in read.degraded[0] and "can write" in read.degraded[0]

    def test_a_registry_inside_a_git_work_tree_is_refused(self, tmp_path):
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        path = write_registry(tree, [ruling()])

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "git work tree" in read.degraded[0]

    def test_a_world_writable_registry_is_refused(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(path, 0o666)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "writable by every user" in read.degraded[0]

    def test_a_world_writable_directory_is_refused(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(path.parent, 0o777)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "directory" in read.degraded[0]

    def test_a_group_writable_registry_is_refused(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(path, 0o664)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "group" in read.degraded[0]

    def test_a_group_writable_directory_is_refused(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(path.parent, 0o775)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "group" in read.degraded[0]

    def test_a_group_writable_ancestor_directory_is_refused(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(tmp_path, 0o775)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == ()
        assert str(tmp_path) in read.degraded[0] and "group" in read.degraded[0]

    def test_a_sticky_shared_ancestor_is_allowed(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        os.chmod(tmp_path, 0o1777)

        assert rp.FileRulingProvider(path).rulings_for(REPO, 1).degraded == ()

    def test_a_registry_owned_by_someone_else_is_refused(self, tmp_path, monkeypatch):
        path = write_registry(tmp_path, [ruling()])
        if os.geteuid() == 0:
            os.chown(path, 12345, -1)
        else:
            monkeypatch.setattr(rp.os, "geteuid", lambda: os.stat(path).st_uid + 1)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "owned by user id" in read.degraded[0]

    def test_the_location_check_needs_no_reviewed_repository(self, tmp_path):
        """Inside a git work tree is refused although no untrusted_roots were
        passed: the check never depends on the optional argument."""
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        path = write_registry(tree, [ruling()])

        assert rp.untrusted_reason(path) is not None
        assert rp.untrusted_reason(path, ()) is not None

    def test_a_malformed_registry_applies_nothing_and_says_why(self, tmp_path):
        path = write_registry(tmp_path, [ruling()])
        path.write_text("rulings: [not a mapping]\n", encoding="utf-8")

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert read.rulings == () and "unusable" in read.degraded[0]


class TestWriter:
    def test_it_creates_the_registry_readable_by_every_user_writable_by_its_owner(self, tmp_path):
        path = registry_dir(tmp_path) / "rulings.yaml"

        count = record_ruling(path, ruling(), today=TODAY)

        assert count == 1
        assert path.stat().st_mode & 0o777 == 0o644
        assert [r.fingerprint for r in rp.FileRulingProvider(path).rulings_for(REPO, 1).rulings] == [FP]

    def test_what_it_writes_the_reader_reads_back(self, tmp_path):
        path = registry_dir(tmp_path) / "rulings.yaml"
        record_ruling(path, ruling(), today=TODAY)
        record_ruling(path, ruling(fingerprint=OTHER_FP), today=TODAY)

        read = rp.FileRulingProvider(path).rulings_for(REPO, 1)

        assert [r.fingerprint for r in read.rulings] == [FP, OTHER_FP]
        assert read.degraded == ()

    def test_an_unexpired_duplicate_is_refused_and_an_expired_one_is_not(self, tmp_path):
        path = registry_dir(tmp_path) / "rulings.yaml"
        record_ruling(path, ruling(), today=TODAY)

        with pytest.raises(DuplicateRulingError):
            record_ruling(path, ruling(), today=TODAY)
        assert record_ruling(path, ruling(), today=TODAY + dt.timedelta(days=60)) == 2

    def test_it_refuses_a_destination_the_pull_request_could_write(self, tmp_path):
        checkout = tmp_path / "checkout"
        checkout.mkdir()

        with pytest.raises(UntrustedRegistryError, match="can write"):
            record_ruling(checkout / "rulings.yaml", ruling(), today=TODAY, untrusted_roots=[checkout])
        assert not (checkout / "rulings.yaml").exists()

    def test_it_refuses_a_world_writable_directory(self, tmp_path):
        directory = registry_dir(tmp_path)
        os.chmod(directory, 0o777)

        with pytest.raises(UntrustedRegistryError, match="every user"):
            record_ruling(directory / "rulings.yaml", ruling(), today=TODAY)

    def test_it_refuses_a_group_writable_directory(self, tmp_path):
        directory = registry_dir(tmp_path)
        os.chmod(directory, 0o775)

        with pytest.raises(UntrustedRegistryError, match="group"):
            record_ruling(directory / "rulings.yaml", ruling(), today=TODAY)
        assert not (directory / "rulings.yaml").exists()

    def test_it_refuses_a_group_writable_ancestor(self, tmp_path):
        directory = registry_dir(tmp_path)
        os.chmod(tmp_path, 0o775)

        with pytest.raises(UntrustedRegistryError, match="group"):
            record_ruling(directory / "rulings.yaml", ruling(), today=TODAY)

    def test_it_refuses_a_destination_inside_a_git_work_tree_without_being_told_the_repository(
        self, tmp_path
    ):
        tree = tmp_path / "tree"
        (tree / ".git").mkdir(parents=True)
        directory = registry_dir(tree)

        with pytest.raises(UntrustedRegistryError, match="git work tree"):
            record_ruling(directory / "rulings.yaml", ruling(), today=TODAY)
        assert not (directory / "rulings.yaml").exists()

    def test_it_refuses_a_write_that_would_exceed_the_readers_limit_and_leaves_the_file_alone(
        self, tmp_path, monkeypatch
    ):
        from clagentic_loadout.review import ruling_store

        path = registry_dir(tmp_path) / "rulings.yaml"
        record_ruling(path, ruling(), today=TODAY)
        before = path.read_bytes()
        monkeypatch.setattr(ruling_store, "MAX_POLICY_BYTES", len(before) + 10)

        with pytest.raises(ruling_store.RegistryFullError, match="limit"):
            record_ruling(path, ruling(fingerprint=OTHER_FP), today=TODAY)
        assert path.read_bytes() == before

    def test_what_the_writer_accepts_the_reader_still_reads(self, tmp_path):
        """The writer's limit is the reader's: both use the one constant."""
        from clagentic_loadout.review import ruling_store

        assert ruling_store.MAX_POLICY_BYTES == rp.MAX_POLICY_BYTES

    def test_it_will_not_append_to_a_registry_the_readers_would_reject(self, tmp_path):
        path = registry_dir(tmp_path) / "rulings.yaml"
        path.write_text("rulings: nope\n", encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        with pytest.raises(RulingStoreError, match="unusable"):
            record_ruling(path, ruling(), today=TODAY)
        assert path.read_text(encoding="utf-8") == before
