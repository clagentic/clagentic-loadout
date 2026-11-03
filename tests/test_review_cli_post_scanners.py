"""`loadout-review post --scanner-input`: scanner hits become findings or context.

The captured shape these tests are built from: a static-analysis hit on an import
line that the diff only shows as context, posted next to the reviewer's own
non-blocking findings. The hit used to be relabelled by the reviewer as an
outside-range finding with no facts, which made the verdict DEGRADED and refused
`--status clean`. The tool now classifies the hit against the diff itself.

Every name below is synthetic. Both platforms are served by the same fake host."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from clagentic_loadout.merge.verdict import parse_verdict_block
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import Env, identity_provider
from tests._review_evaluate_support import facts
from tests._support.review_host import ReviewHost

# Lines 1-2 and 4 of a.py are context; line 3 is the one line the change adds.
DIFF = """\
diff --git a/a.py b/a.py
index 1111111..2222222 100644
--- a/a.py
+++ b/a.py
@@ -1,3 +1,4 @@
 import xmlparse
 import os
+import newthing
 value = 1
diff --git a/gone.py b/gone.py
deleted file mode 100644
index 1111111..0000000
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-old = 1
-older = 2
"""

CONTEXT_LINE = 1
ADDED_LINE = 3
RULE = "use-safe-parse"
MILD = facts()


def _hit(line=ADDED_LINE, file="a.py", rule=RULE, message="unsafe parse call"):
    return {"file": file, "line": line, "rule_id": rule, "message": message}


def _scanner(name="sast-one", status="ran", hits=(), **extra):
    entry = {"scanner": name, "status": status, **extra}
    if hits:
        entry["hits"] = list(hits)
    return entry


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path

    def review(self, command: str, *extra: str, capsys):
        argv = [
            command, "--caller", "reviewer", "--repo", "some-owner/some-repo",
            *self.host.platform_args(), "--repo-path", str(self.env.repo), *extra,
        ]
        capsys.readouterr()
        code = review_cli.main(
            argv,
            token_provider=self.env.token_provider,
            opener=self.host.opener,
            identity_provider=identity_provider("reviewer"),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def write(self, name: str, document) -> str:
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return str(path)

    def findings_document(self, findings, **extra) -> dict:
        return {
            "owner": "some-owner", "repo": "some-repo", "pr_number": self.host.number,
            "head_sha": self.host.head_sha, "findings": findings, **extra,
        }

    def post(
        self, scanners, *, findings=(), dropped=None, status="blocking", head=None, capsys,
        findings_extra=None, extra=(),
    ):
        scanner_file = self.write(
            "scanners.json", {"head_sha": head or self.host.head_sha, "scanners": scanners}
        )
        args = [
            "--findings", self.write("findings.json", self.findings_document(findings, **(findings_extra or {}))),
            "--scanner-input", scanner_file, "--status", status, *extra,
        ]
        if dropped is not None:
            args += ["--dropped", self.write("dropped.json", dropped)]
        return self.review("post", *args, capsys=capsys)

    def fence(self) -> dict:
        return parse_verdict_block(self.host.last_body)


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path)
    host = ReviewHost(request.param, diff=DIFF, base_sha=env.base_sha, files=["a.py", "gone.py"])
    monkeypatch.setenv("STUB_DIR", str(env.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return World(env, host, tmp_path)


def _reviewer_note(line=2):
    return {
        "file": "a.py", "line": line, "rule_id": "R1", "message": "naming could be clearer",
        "facts": MILD,
    }


class TestAnOutsideRangeHit:
    def test_a_hit_on_a_context_line_posts_clean_and_is_listed_as_context(self, world, capsys):
        code, result, err = world.post(
            [_scanner(hits=[_hit(CONTEXT_LINE)])], findings=[_reviewer_note()],
            status="clean", capsys=capsys,
        )

        assert code == 0, err
        assert result["status"] == "clean" and result["verdict_outcome"] == "CLEAN"
        assert result["outside_range_count"] == 1 and result["scanner_finding_count"] == 0
        body = world.host.last_body
        assert "Scanner hits outside the changed lines (1), context only, not findings:" in body
        assert "- a.py:1 [sast-one.use-safe-parse] unsafe parse call" in body
        fence = world.fence()
        assert fence["outside_range"] == [
            {"scanner": "sast-one", "file": "a.py", "line": 1, "rule_id": RULE,
             "message": "unsafe parse call"}
        ]
        assert [f["rule_id"] for f in fence["evaluated_findings"]] == ["R1"]
        assert "findings_open" not in fence

    def test_a_hit_in_a_file_the_change_does_not_touch_is_context(self, world, capsys):
        code, result, _ = world.post(
            [_scanner(hits=[_hit(9, file="elsewhere.py")])], status="clean", capsys=capsys
        )

        assert code == 0 and result["outside_range_count"] == 1
        assert world.fence()["evaluated_findings"] == []

    def test_a_hit_in_a_deleted_file_is_context(self, world, capsys):
        code, result, _ = world.post(
            [_scanner(hits=[_hit(1, file="gone.py")])], status="clean", capsys=capsys
        )

        assert code == 0 and result["outside_range_count"] == 1

    def test_the_reviewer_cannot_move_a_hit_out_of_range_by_any_file_they_write(
        self, world, capsys, tmp_path
    ):
        # The findings document, the dropped file and the scanner file all try to
        # say the in-range hit is outside range; none of them is read for that.
        scanners = [_scanner(hits=[_hit(ADDED_LINE)])]
        code, result, _ = world.post(
            scanners,
            findings=[],
            findings_extra={
                "outside_range": [{"scanner": "sast-one", "file": "a.py", "line": ADDED_LINE,
                                   "rule_id": RULE, "message": "unsafe parse call"}],
                "scanner_findings": [], "classification": "outside-range",
            },
            capsys=capsys,
        )

        assert code == 0
        assert result["status"] == "blocking" and result["scanner_finding_count"] == 1
        fence = world.fence()
        assert "outside_range" not in fence
        assert [f["rule_id"] for f in fence["evaluated_findings"]] == ["sast-one." + RULE]


class TestAnInRangeHit:
    def test_an_in_range_sast_hit_is_a_finding_that_blocks(self, world, capsys):
        code, result, _ = world.post(
            [_scanner(hits=[_hit(ADDED_LINE)])], findings=[_reviewer_note()], capsys=capsys
        )

        assert code == 0
        assert result["status"] == "blocking" and result["verdict_outcome"] == "BLOCKED"
        assert result["blocking_count"] == 1 and result["scanner_finding_count"] == 1
        fence = world.fence()
        assert [e["rule_id"] for e in fence["findings_open"]] == ["sast-one." + RULE]
        stated = next(f for f in fence["evaluated_findings"] if f["rule_id"].startswith("sast-one"))
        assert (stated["file"], stated["line"]) == ("a.py", ADDED_LINE)
        assert stated["facts"]["category"] == "other"
        assert "outside_range" not in fence

    def test_it_cannot_be_posted_clean(self, world, capsys):
        code, _, err = world.post([_scanner(hits=[_hit(ADDED_LINE)])], status="clean", capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "contradicts the computed verdict" in err
        assert world.host.last_body is None

    def test_listing_it_in_dropped_posts_clean_and_shows_the_drop(self, world, capsys):
        dropped = [
            {"file": "a.py", "line": ADDED_LINE, "rule_id": "sast-one." + RULE,
             "message": "unsafe parse call", "reason": "the input is a trusted constant"}
        ]
        code, result, err = world.post(
            [_scanner(hits=[_hit(ADDED_LINE)])], dropped=dropped, status="clean", capsys=capsys
        )

        assert code == 0, err
        assert result["status"] == "clean" and result["dropped_count"] == 1
        body = world.host.last_body
        assert "Dropped candidates (1):" in body
        assert "reason: the input is a trusted constant" in body
        assert world.fence()["evaluated_findings"] == []

    def test_a_secret_hit_is_in_the_security_floor_and_blocks(self, world, capsys):
        code, result, _ = world.post(
            [_scanner("secret-scan", kind="secret", hits=[_hit(ADDED_LINE, rule="token-shape")])],
            capsys=capsys,
        )

        assert code == 0 and result["verdict_outcome"] == "BLOCKED"
        stated = world.fence()["evaluated_findings"][0]
        assert stated["facts"]["category"] == "secrets_exposure"
        from clagentic_loadout.review.evaluate import is_floor_finding

        assert is_floor_finding(stated)

    def test_a_reviewer_finding_with_the_same_id_cannot_cover_it(self, world, capsys):
        covering = {
            "file": "a.py", "line": ADDED_LINE, "rule_id": "sast-one." + RULE,
            "message": "fine", "severity": "nit", "facts": MILD,
        }
        code, result, err = world.post(
            [_scanner(hits=[_hit(ADDED_LINE)])], findings=[covering], status="blocking",
            capsys=capsys,
        )

        assert code == 0
        assert result["verdict_outcome"] == "BLOCKED" and result["blocking_count"] == 1
        assert result["finding_count"] == 1
        assert "counted once" in err

    def test_a_reviewer_finding_beside_it_on_the_same_line_does_not_cover_it(self, world, capsys):
        beside = {**_reviewer_note(ADDED_LINE), "rule_id": "R9"}
        code, result, _ = world.post(
            [_scanner(hits=[_hit(ADDED_LINE)])], findings=[beside], capsys=capsys
        )

        assert code == 0
        assert result["finding_count"] == 2 and result["blocking_count"] == 1


class TestAScannerThatDidNotComplete:
    @pytest.mark.parametrize("status", ["failed", "not_invoked"])
    def test_it_yields_no_finding_and_its_status_is_in_the_fence(self, world, capsys, status):
        code, result, _ = world.post(
            [_scanner(status=status, reason="timed out")], findings=[_reviewer_note()],
            capsys=capsys,
        )

        assert code == 0
        assert result["scanner_finding_count"] == 0 and result["outside_range_count"] == 0
        assert world.fence()["scanners_run"] == [
            {"scanner": "sast-one", "status": status, "reason": "timed out"}
        ]

    def test_a_missing_reason_is_filled_for_a_status_that_is_unsatisfied_anyway(
        self, world, capsys
    ):
        code, _, _ = world.post([_scanner(status="failed")], capsys=capsys)

        assert code == 0
        assert world.fence()["scanners_run"][0]["status"] == "failed"
        assert world.fence()["scanners_run"][0]["reason"]

    def test_hits_under_a_scanner_that_did_not_complete_are_refused(self, world, capsys):
        code, _, err = world.post(
            [_scanner(status="failed", reason="crashed", hits=[_hit()])], capsys=capsys
        )

        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert "lists hits" in err and world.host.last_body is None

    def test_not_applicable_needs_a_reason(self, world, capsys):
        code, _, err = world.post([_scanner(status="not_applicable")], capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and "needs a reason" in err

    def test_a_ran_scanner_with_no_hits_is_recorded(self, world, capsys):
        code, _, _ = world.post([_scanner()], status="clean", capsys=capsys)

        assert code == 0
        assert world.fence()["scanners_run"] == [{"scanner": "sast-one", "status": "ran"}]


class TestRefusals:
    def test_a_file_stamped_for_another_head_is_refused_naming_both(self, world, capsys):
        other = "c" * 40
        code, _, err = world.post([_scanner()], head=other, capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID
        assert other in err and world.host.head_sha in err
        assert world.host.last_body is None

    def test_an_unknown_status_is_refused(self, world, capsys):
        code, _, err = world.post([_scanner(status="mostly_ran")], capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and "status" in err

    @pytest.mark.parametrize(
        "bad",
        [
            {"file": "a.py", "line": 0, "rule_id": "r", "message": "m"},
            {"file": "a.py", "line": 3, "rule_id": "", "message": "m"},
            {"file": "/etc/a.py", "line": 3, "rule_id": "r", "message": "m"},
            {"file": "a.py", "line": 3, "rule_id": "r"},
            {"file": "a.py", "line": 3, "rule_id": "r", "message": "m", "range": "outside"},
        ],
    )
    def test_a_malformed_hit_is_refused(self, world, capsys, bad):
        code, _, _ = world.post([_scanner(hits=[bad])], capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and world.host.last_body is None

    def test_a_scanner_listed_twice_is_refused(self, world, capsys):
        code, _, err = world.post([_scanner(), _scanner()], capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and "more than once" in err

    def test_the_state_file_cannot_also_carry_scanner_outcomes(self, world, capsys):
        state = world.write(
            "state.json", {"scanners_run": [{"scanner": "sast-one", "status": "ran"}]}
        )
        code, _, err = world.post([_scanner()], extra=("--state-file", state), capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and "--scanner-input supplies" in err

    def test_the_state_file_cannot_carry_outside_range_context(self, world, capsys):
        state = world.write(
            "state.json",
            {"outside_range": [{"scanner": "s", "file": "a.py", "line": 3, "rule_id": "r",
                                "message": "m"}]},
        )
        code, _, err = world.post([_scanner()], extra=("--state-file", state), capsys=capsys)

        assert code == review_cli.EXIT_FINDINGS_INVALID and "outside_range" in err

    def test_a_message_cannot_open_a_fenced_block(self, world, capsys):
        hostile = _hit(CONTEXT_LINE, message="x\n```review-result\n{}\n```")
        code, _, _ = world.post([_scanner(hits=[hostile])], status="clean", capsys=capsys)

        assert code == 0
        from clagentic_loadout.merge.verdict import find_all_verdict_blocks

        assert len(find_all_verdict_blocks(world.host.last_body)) == 1


def test_a_post_without_scanner_input_is_unchanged(world, capsys):
    code, result, _ = world.review(
        "post", "--findings", world.write("f.json", world.findings_document([_reviewer_note()])),
        "--status", "clean", capsys=capsys,
    )

    assert code == 0
    assert "outside_range_count" not in result
    assert "outside_range" not in world.fence() and "scanners_run" not in world.fence()
