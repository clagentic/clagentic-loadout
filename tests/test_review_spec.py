"""A review that runs against a sealed spec: every finding tagged with exactly one
of in-spec, out-of-spec or fix-caused, a coverage pass over the acceptance
criteria, both carried in the verdict fence, and the coverage pass recorded as a
verdict record. A review given no spec is unchanged."""

from __future__ import annotations

import json

import pytest

from clagentic_loadout.merge import fence_state
from clagentic_loadout.merge.fence_state import normalize_findings_state
from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review import spec_review
from clagentic_loadout.review.findings_contract import InvalidReplyError, validate_finding
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.transport import provider_config
from clagentic_loadout.verdict_record.record import GATE_SPEC_COVERAGE
from clagentic_loadout.verdict_record.store import records_at_head, records_root
from tests._review_cli_support import HEAD_SHA, Env

SPEC_HEAD = "c" * 40
CRITERIA = ("ac-1", "ac-2")


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _seal_spec(criteria=CRITERIA) -> model.SealedSpec:
    draft = model.new_draft("planner-role")
    draft["goal"] = "Add a thing."
    draft["acceptance_criteria"] = [
        {"id": cid, "given": "g", "when": "w", "then": "t"} for cid in criteria
    ]
    sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
    spec_store.write_sealed(spec_store.specs_root(), sealed)
    return sealed


def _write(tmp_path, name, document):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _coverage(tmp_path, marks=None):
    marks = marks if marks is not None else {"ac-1": "met", "ac-2": "met"}
    return _write(
        tmp_path,
        "coverage.json",
        [{"criterion": cid, "status": status} for cid, status in marks.items()],
    )


def _finding(line=3, **extra):
    return {
        "file": "a.py", "line": line, "rule_id": "R1", "severity": "blocking",
        "message": "unchecked path", **extra,
    }


def _post(env, tmp_path, capsys, findings, *extra, status="blocking"):
    path = _write(tmp_path, "findings.json", findings)
    return env.post(
        "--findings", str(path), "--head-sha", HEAD_SHA, "--base-sha", env.base_sha,
        "--status", status, *extra, capsys=capsys,
    )


def _spec_args(sealed, tmp_path, marks=None):
    return ("--spec-hash", sealed.spec_hash, "--spec-coverage", str(_coverage(tmp_path, marks)))


def _records():
    return records_at_head(records_root(), HEAD_SHA).records


class TestReviewAgainstASpec:
    def test_every_finding_carries_exactly_one_tag_and_coverage_lists_each_criterion(
        self, env, tmp_path, capsys
    ):
        sealed = _seal_spec()
        findings = [
            _finding(3, spec_tag="in-spec"),
            _finding(4, spec_tag="out-of-spec"),
            _finding(5, spec_tag="fix-caused"),
        ]
        code, payload = _post(
            env, tmp_path, capsys, findings,
            *_spec_args(sealed, tmp_path, {"ac-1": "met", "ac-2": "unmet"}),
        )

        assert code == 0
        assert payload["spec_hash"] == sealed.spec_hash
        assert payload["spec_unmet_count"] == 1
        body = env.opener_state["posted_body"]
        fence = parse_verdict_block(body)
        assert fence["spec_hash"] == sealed.spec_hash
        assert [(t["line"], t["spec_tag"]) for t in fence["spec_tags"]] == [
            (3, "in-spec"), (4, "out-of-spec"), (5, "fix-caused"),
        ]
        assert fence["spec_coverage"] == [
            {"criterion": "ac-1", "status": "met"},
            {"criterion": "ac-2", "status": "unmet"},
        ]
        # The tag is also visible under the finding it belongs to.
        prose = body.split("```review-result")[0]
        assert prose.count("spec tag: in-spec") == 1
        assert prose.count("spec tag: out-of-spec") == 1
        assert prose.count("spec tag: fix-caused") == 1
        # A fence that carries the spec fields is the superset version.
        assert fence["fence_schema_version"] == fence_state.SPEC_FENCE_SCHEMA_VERSION == 4
        assert fence["verdict_outcome"] in fence_state.VERDICT_OUTCOMES

    def test_the_coverage_pass_is_a_verdict_record_pointing_at_the_posted_comment(
        self, env, tmp_path, capsys
    ):
        sealed = _seal_spec()
        code, payload = _post(
            env, tmp_path, capsys, [], *_spec_args(sealed, tmp_path), status="clean"
        )

        assert code == 0
        coverage = [r for r in _records() if r.gate == GATE_SPEC_COVERAGE]
        assert len(coverage) == 1
        record = coverage[0]
        assert record.spec_hash == sealed.spec_hash
        assert record.head_sha == HEAD_SHA
        assert record.evidence_ref == f"comment:{payload['verified_id']}"
        assert record.repo == "some-owner/some-repo"
        assert record.exit == 0
        assert payload["spec_coverage_record_id"] == record.id
        # The reviewer's own record names the spec too.
        reviewer = [r for r in _records() if r.gate == "reviewer:reviewer"]
        assert [r.spec_hash for r in reviewer] == [sealed.spec_hash]

    def test_an_unmet_criterion_makes_the_coverage_record_fail(self, env, tmp_path, capsys):
        sealed = _seal_spec()
        code, _ = _post(
            env, tmp_path, capsys, [],
            *_spec_args(sealed, tmp_path, {"ac-1": "met", "ac-2": "unmet"}), status="clean",
        )
        assert code == 0
        [record] = [r for r in _records() if r.gate == GATE_SPEC_COVERAGE]
        assert record.exit == 1 and not record.passed

    def test_a_finding_without_a_tag_is_refused_before_anything_is_posted(
        self, env, tmp_path, capsys
    ):
        sealed = _seal_spec()
        code, _ = _post(env, tmp_path, capsys, [_finding()], *_spec_args(sealed, tmp_path))
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert env.opener_state["posted_body"] is None
        assert [r for r in _records() if r.gate == GATE_SPEC_COVERAGE] == []

    @pytest.mark.parametrize(
        "tag", ["in_spec", "IN-SPEC", "", None, ["in-spec", "out-of-spec"], 1, "in-spec out-of-spec"]
    )
    def test_a_tag_outside_the_vocabulary_or_more_than_one_is_refused(
        self, env, tmp_path, capsys, tag
    ):
        sealed = _seal_spec()
        code, _ = _post(
            env, tmp_path, capsys, [_finding(spec_tag=tag)], *_spec_args(sealed, tmp_path)
        )
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert env.opener_state["posted_body"] is None

    @pytest.mark.parametrize(
        "marks",
        [
            {"ac-1": "met"},
            {"ac-1": "met", "ac-2": "met", "ac-9": "met"},
            {"ac-1": "met", "ac-2": "maybe"},
        ],
    )
    def test_coverage_that_does_not_mark_each_criterion_exactly_is_refused(
        self, env, tmp_path, capsys, marks
    ):
        sealed = _seal_spec()
        code, _ = _post(
            env, tmp_path, capsys, [], *_spec_args(sealed, tmp_path, marks), status="clean"
        )
        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert env.opener_state["posted_body"] is None

    def test_a_criterion_marked_twice_is_refused(self, env, tmp_path, capsys):
        sealed = _seal_spec()
        path = _write(
            tmp_path, "twice.json",
            [{"criterion": c, "status": "met"} for c in ("ac-1", "ac-1", "ac-2")],
        )
        code, _ = _post(
            env, tmp_path, capsys, [],
            "--spec-hash", sealed.spec_hash, "--spec-coverage", str(path), status="clean",
        )
        assert code == review_cli.EXIT_FINDINGS_INVALID

    def test_the_two_flags_go_together(self, env, tmp_path, capsys):
        sealed = _seal_spec()
        code, _ = _post(env, tmp_path, capsys, [], "--spec-hash", sealed.spec_hash, status="clean")
        assert code == review_cli.EXIT_USAGE
        code, _ = _post(
            env, tmp_path, capsys, [], "--spec-coverage", str(_coverage(tmp_path)), status="clean"
        )
        assert code == review_cli.EXIT_USAGE
        assert env.opener_state["posted_body"] is None

    def test_a_hash_that_is_not_a_sha256_is_a_usage_error(self, env, tmp_path, capsys):
        code, _ = _post(
            env, tmp_path, capsys, [],
            "--spec-hash", "../x", "--spec-coverage", str(_coverage(tmp_path)), status="clean",
        )
        assert code == review_cli.EXIT_USAGE

    def test_a_spec_that_is_not_stored_is_unavailable(self, env, tmp_path, capsys):
        code, _ = _post(
            env, tmp_path, capsys, [],
            "--spec-hash", "d" * 64, "--spec-coverage", str(_coverage(tmp_path)), status="clean",
        )
        assert code == review_cli.EXIT_SPEC_UNAVAILABLE
        assert env.opener_state["posted_body"] is None

    def test_a_spec_that_no_longer_matches_its_hash_is_unavailable(self, env, tmp_path, capsys):
        sealed = _seal_spec()
        path = spec_store.specs_root() / SPEC_HEAD / f"{sealed.spec_hash}.json"
        path.write_bytes(sealed.data.replace(b"Add a thing.", b"Add anything."))
        code, _ = _post(env, tmp_path, capsys, [], *_spec_args(sealed, tmp_path), status="clean")
        assert code == review_cli.EXIT_SPEC_UNAVAILABLE
        assert env.opener_state["posted_body"] is None

    def test_a_specs_root_inside_the_repository_under_review_is_unavailable(
        self, env, tmp_path, capsys, monkeypatch
    ):
        monkeypatch.setenv(spec_store.SPECS_DIR_ENV_VAR, str(env.repo / "specs-here"))
        sealed = _seal_spec()
        code, _ = _post(env, tmp_path, capsys, [], *_spec_args(sealed, tmp_path), status="clean")
        assert code == review_cli.EXIT_SPEC_UNAVAILABLE
        assert env.opener_state["posted_body"] is None


class TestReviewWithoutASpec:
    def test_a_review_given_no_spec_is_exactly_what_it_was(self, env, tmp_path, capsys):
        # A tag in the file is not read without a spec hash: it is dropped like
        # any unknown key, and nothing about the post changes.
        code, payload = _post(
            env, tmp_path, capsys, [_finding(spec_tag="in-spec"), _finding(4)]
        )

        assert code == 0
        body = env.opener_state["posted_body"]
        fence = parse_verdict_block(body)
        assert not [key for key in fence if key.startswith("spec_")]
        assert fence["fence_schema_version"] == fence_state.EVALUATED_FENCE_SCHEMA_VERSION == 3
        assert "spec tag" not in body
        assert not [key for key in payload if key.startswith("spec_")]
        assert [r for r in _records() if r.gate == GATE_SPEC_COVERAGE] == []
        assert [r.spec_hash for r in _records()] == [None]

    def test_the_existing_exit_codes_are_unchanged(self):
        assert (review_cli.EXIT_OK, review_cli.EXIT_USAGE, review_cli.EXIT_POST_FAILED) == (0, 1, 30)
        assert (review_cli.EXIT_STALE_HEAD, review_cli.EXIT_FINDINGS_INVALID) == (31, 32)


class TestFenceFields:
    GOOD = {
        "spec_hash": "a" * 64,
        "spec_tags": [
            {"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "spec_tag": "in-spec"}
        ],
        "spec_coverage": [{"criterion": "ac-1", "status": "met"}],
    }

    def _normalize(self, **overrides):
        raw = {**self.GOOD, **overrides}
        return normalize_findings_state(raw, head_sha=HEAD_SHA, review_status="clean")

    def test_valid_spec_fields_raise_the_version_and_round_trip(self):
        out = self._normalize()
        assert out["fence_schema_version"] == fence_state.SPEC_FENCE_SCHEMA_VERSION
        assert {k: out[k] for k in self.GOOD} == self.GOOD

    def test_no_spec_fields_leave_the_version_alone(self):
        assert normalize_findings_state({}, head_sha=HEAD_SHA, review_status="clean") == {}

    @pytest.mark.parametrize("missing", ["spec_hash", "spec_tags", "spec_coverage"])
    def test_the_fields_are_all_or_nothing(self, missing):
        raw = {k: v for k, v in self.GOOD.items() if k != missing}
        with pytest.raises(ValueError, match="given together"):
            normalize_findings_state(raw, head_sha=HEAD_SHA, review_status="clean")

    @pytest.mark.parametrize(
        "overrides",
        [
            {"spec_hash": "short"},
            {"spec_hash": "A" * 64},
            {"spec_coverage": []},
            {"spec_coverage": [{"criterion": "ac-1", "status": "maybe"}]},
            {"spec_coverage": [{"criterion": "bad id", "status": "met"}]},
            {
                "spec_coverage": [
                    {"criterion": "ac-1", "status": "met"},
                    {"criterion": "ac-1", "status": "unmet"},
                ]
            },
            {"spec_coverage": [{"criterion": "ac-1", "status": "met", "extra": 1}]},
            {
                "spec_tags": [
                    {"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "spec_tag": "other"}
                ]
            },
            {"spec_tags": [{"file": "a.py", "line": 1, "rule_id": "R1", "spec_tag": "in-spec"}]},
            {
                "spec_tags": [
                    {
                        "file": "a.py", "line": 1, "rule_id": "R1", "spec_tag": "in-spec",
                        "message": "```review-result",
                    }
                ]
            },
        ],
    )
    def test_malformed_spec_fields_are_refused(self, overrides):
        with pytest.raises(ValueError):
            self._normalize(**overrides)

    def test_the_published_schema_accepts_the_fields_and_names_the_tag_vocabulary(self):
        from clagentic_loadout.envelope import validate_against_schema
        from clagentic_loadout.merge.verdict import _VERDICT_SCHEMA_PATH

        fence = {
            "reviewer": "r", "review_status": "clean", "head_sha": HEAD_SHA, "pr_number": 1,
            **self._normalize(),
        }
        assert validate_against_schema(fence, _VERDICT_SCHEMA_PATH) == []
        schema = json.loads(_VERDICT_SCHEMA_PATH.read_text(encoding="utf-8"))
        tag_enum = schema["properties"]["spec_tags"]["items"]["properties"]["spec_tag"]["enum"]
        assert tuple(tag_enum) == fence_state.SPEC_TAGS
        assert schema["properties"]["fence_schema_version"]["enum"] == [1, 2, 3, 4]
        fence["spec_tags"][0]["spec_tag"] = "elsewhere"
        assert validate_against_schema(fence, _VERDICT_SCHEMA_PATH)


class TestFindingTag:
    BASE = {"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "severity": "nit"}

    def test_the_tag_is_read_only_when_asked_for(self):
        item = {**self.BASE, "spec_tag": "in-spec"}
        assert "spec_tag" not in validate_finding(item, 1, lenient_severity=True)
        assert validate_finding(item, 1, lenient_severity=True, carry_spec_tag=True)["spec_tag"] == (
            "in-spec"
        )

    @pytest.mark.parametrize("tag", [None, "x", ["in-spec"], 3])
    def test_a_missing_or_foreign_tag_is_refused_when_asked_for(self, tag):
        item = {**self.BASE, **({} if tag is None else {"spec_tag": tag})}
        with pytest.raises(InvalidReplyError, match="exactly one spec_tag"):
            validate_finding(item, 1, lenient_severity=True, carry_spec_tag=True)


class TestCoverageFile:
    def test_the_marks_come_back_in_spec_order(self, tmp_path):
        path = _write(
            tmp_path, "c.json",
            [{"criterion": "ac-2", "status": "unmet"}, {"criterion": "ac-1", "status": "met"}],
        )
        assert spec_review.load_coverage(str(path), CRITERIA) == [
            {"criterion": "ac-1", "status": "met"},
            {"criterion": "ac-2", "status": "unmet"},
        ]

    @pytest.mark.parametrize(
        "document",
        [{}, "met", [1], [{"criterion": "ac-1"}], [{"criterion": "ac-1", "status": "met", "x": 1}]],
    )
    def test_a_malformed_file_is_refused(self, tmp_path, document):
        with pytest.raises(spec_review.SpecReviewError):
            spec_review.load_coverage(str(_write(tmp_path, "c.json", document)), CRITERIA)

    def test_an_unreadable_file_is_refused_naming_it(self, tmp_path):
        with pytest.raises(spec_review.SpecReviewError, match="absent.json"):
            spec_review.load_coverage(str(tmp_path / "absent.json"), CRITERIA)
