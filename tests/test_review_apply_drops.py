"""`loadout-review apply-drops` through the CLI entry point, with synthetic findings,
scanner input, stage manifest and records files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli

HEAD = "b" * 40
REFUSED = 64
HOSTILE = "it's \"quoted\" {braces} $(touch /tmp/x) `tick` ; | & > < \\ $HOME ~"


def _finding(n: int, **extra) -> dict:
    return {
        "file": f"src/f{n}.py", "line": 10 + n, "rule_id": f"R{n}",
        "message": f"problem {n}\n  spans   lines", "severity": "high", **extra,
    }


DOCUMENT = {
    "owner": "some-owner", "repo": "some-repo", "pr_number": 7, "head_sha": HEAD,
    "chunks": [{"index": 0, "files": ["src/f0.py"]}], "note": {"keep": ["me"]},
    "findings": [_finding(0), _finding(1), _finding(2)],
}
SCANNERS = {
    "head_sha": HEAD,
    "scanners": [
        {"scanner": "scan-a", "status": "ran", "hits": [
            {"file": "a/one.py", "line": 4, "rule_id": "A1", "message": "first hit"},
            {"file": "a/two.py", "line": 5, "rule_id": "A2", "message": "second   hit"},
        ]},
        {"scanner": "scan-b", "status": "not_invoked", "reason": "no tool"},
    ],
}


class Area:
    def __init__(self, root: Path) -> None:
        self.root = root

    def write(self, name: str, content, *, raw: bool = False) -> str:
        path = self.root / name
        path.write_bytes(content if isinstance(content, bytes) else (
            content if raw else json.dumps(content)
        ).encode("utf-8"))
        return str(path)

    def manifest(self, head: str = HEAD) -> str:
        return self.write(".loadout-review-stage.json", {
            "schema": "loadout-review-stage/1", "dir": str(self.root), "repo": "some-owner/some-repo",
            "pr": 7, "platform": "github", "head_sha": head, "base_sha": "a" * 40,
            "files": [], "deleted": [], "missing": [],
        })

    def apply(self, records: str | bytes, *extra: str, capsys, spec: bool = False, document=DOCUMENT):
        argv = [
            "apply-drops", "--findings", self.write("findings.json", document),
            "--records", self.write("records.txt", records, raw=True),
            "--judged-out", str(self.root / "judged.json"),
            "--dropped-out", str(self.root / "dropped.json"),
            *extra,
        ]
        if spec:
            argv += ["--coverage-out", str(self.root / "coverage.json"),
                     "--reject-reason-out", str(self.root / "reject.txt")]
        capsys.readouterr()
        code = review_cli.main(argv)
        captured = capsys.readouterr()
        lines = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(lines[-1]) if lines else {}), captured.err

    def read(self, name: str):
        return json.loads((self.root / name).read_text(encoding="utf-8"))


@pytest.fixture
def area(tmp_path) -> Area:
    return Area(tmp_path)


def test_drops_a_finding_and_a_hit_and_lists_the_hits(area, capsys):
    scanner = area.write("scanner.json", SCANNERS)

    code, result, _ = area.apply(
        "drop 1 not a defect: guarded upstream\n\ndrop s2 test fixture\n",
        "--scanner-input", scanner, capsys=capsys,
    )

    assert code == 0 and result["result"] == "written"
    judged = area.read("judged.json")
    assert judged == {**DOCUMENT, "findings": [DOCUMENT["findings"][0], DOCUMENT["findings"][2]]}
    assert area.read("dropped.json") == [
        {"file": "src/f1.py", "line": 11, "rule_id": "R1", "message": "problem 1 spans lines",
         "reason": "not a defect: guarded upstream"},
        {"file": "a/two.py", "line": 5, "rule_id": "scan-a.A2", "message": "second hit",
         "reason": "test fixture"},
    ]
    assert [(h["id"], h["scanner"], h["file"], h["line"], h["rule_id"]) for h in result["hits"]] == [
        ("s1", "scan-a", "a/one.py", 4, "A1"), ("s2", "scan-a", "a/two.py", 5, "A2"),
    ]
    assert result["drops"] == {"findings": 1, "hits": 1}
    assert result["read_at"] is None and result["coverage"] is None


def test_an_empty_records_file_keeps_everything_and_lists_the_hits(area, capsys):
    code, result, _ = area.apply(
        "\n\n", "--scanner-input", area.write("scanner.json", SCANNERS), capsys=capsys
    )

    assert code == 0 and area.read("judged.json") == DOCUMENT and area.read("dropped.json") == []
    assert [h["id"] for h in result["hits"]] == ["s1", "s2"]


def test_a_bare_findings_array_is_judged_as_an_array(area, capsys):
    code, _, _ = area.apply("drop 0 r", capsys=capsys, document=DOCUMENT["findings"])

    assert code == 0 and area.read("judged.json") == DOCUMENT["findings"][1:]


REFUSALS = [
    ("unknown-keyword", "drop 0 ok\nfrob 1 y\n", "line 2", False),
    ("no-such-finding", "drop 9 reason\n", "line 1", False),
    ("duplicate-id", "drop 1 a\n\ndrop 1 b\n", "line 3", False),
    ("hit-beyond-the-list", "drop s3 reason\n", "line 1", False),
    ("hit-without-scanner-input-s0", "drop s0 reason\n", "line 1", False),
    ("not-an-id", "drop one reason\n", "line 1", False),
    ("drop-without-reason", "drop 1\n", "line 1", False),
    ("spec-record-outside-spec-mode", "drop 0 r\ntag 1 in-spec\n", "line 2", False),
    ("cover-outside-spec-mode", "cover ac-1 met\n", "line 1", False),
    ("reject-outside-spec-mode", "reject one line\n", "line 1", False),
    ("kept-finding-without-tag", "tag 0 in-spec\ncover ac-1 met\n", "no tag record", True),
    ("tag-on-dropped-finding", "drop 0 r\ntag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\ncover a met\n",
     "line 2", True),
    ("unknown-tag", "tag 0 maybe\n", "line 1", True),
    ("tag-twice", "tag 0 in-spec\ntag 0 out-of-spec\n", "line 2", True),
    ("no-cover-record", "tag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\n", "cover record", True),
    ("unknown-cover-status", "cover ac-1 half\n", "line 1", True),
    ("cover-twice", "tag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\ncover a met\ncover a unmet\n",
     "line 5", True),
    ("reject-with-fence-syntax", "tag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\ncover a met\n"
     "reject has ``` fence\n", "line 5", True),
    ("two-rejects", "tag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\ncover a met\nreject a\nreject b\n",
     "line 6", True),
    ("reject-text-empty", "reject\n", "line 1", True),
]


@pytest.mark.parametrize("records,needle,spec", [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_an_unusable_records_file_is_refused_and_every_output_stays_as_it_was(
    area, capsys, records, needle, spec
):
    judged, dropped, coverage = (area.root / "judged.json", area.root / "dropped.json", area.root / "coverage.json")
    judged.write_text("earlier judged", encoding="utf-8")
    dropped.write_text("earlier dropped", encoding="utf-8")
    reject = area.root / "reject.txt"
    reject.write_text("earlier reject", encoding="utf-8")
    scanner = area.write("scanner.json", SCANNERS)

    code, result, err = area.apply(
        records, "--scanner-input", scanner, capsys=capsys, spec=spec
    )

    assert code == REFUSED and result == {}
    assert needle in err and str((area.root / "records.txt").resolve()) in err
    assert judged.read_text(encoding="utf-8") == "earlier judged"
    assert dropped.read_text(encoding="utf-8") == "earlier dropped"
    assert reject.read_text(encoding="utf-8") == "earlier reject" and not coverage.exists()


@pytest.mark.parametrize(
    "case", ["records-missing", "records-not-utf8", "findings-missing", "findings-not-json",
             "findings-wrong-shape", "scanner-malformed", "manifest-not-a-manifest"],
)
def test_an_unreadable_or_malformed_input_file_is_refused_naming_its_path(area, capsys, case):
    findings = area.write("findings.json", DOCUMENT)
    records = area.write("records.txt", "drop 0 r", raw=True)
    extra: list[str] = []
    culprit = records
    if case == "records-missing":
        records = culprit = str(area.root / "none.txt")
    elif case == "records-not-utf8":
        records = culprit = area.write("bad.txt", b"drop 0 \xff\xfe\n")
    elif case == "findings-missing":
        findings = culprit = str(area.root / "none.json")
    elif case == "findings-not-json":
        findings = culprit = area.write("bad.json", "{nope", raw=True)
    elif case == "findings-wrong-shape":
        findings = culprit = area.write("shape.json", {"findings": "x"})
    elif case == "scanner-malformed":
        culprit = area.write("scan.json", {"head_sha": HEAD, "scanners": "x"})
        extra = ["--scanner-input", culprit]
    else:
        culprit = area.write("m.json", {"schema": "other"})
        extra = ["--manifest", culprit]
    capsys.readouterr()

    code = review_cli.main([
        "apply-drops", "--findings", findings, "--records", records,
        "--judged-out", str(area.root / "j.json"), "--dropped-out", str(area.root / "d.json"), *extra,
    ])

    assert code == REFUSED and not (area.root / "j.json").exists()
    assert str(Path(culprit).resolve()) in capsys.readouterr().err


def test_a_reason_is_stored_byte_for_byte_and_no_subprocess_is_started(area, capsys, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("a subprocess was started")

    monkeypatch.setattr(subprocess, "Popen", forbidden)

    code, _, _ = area.apply(f"drop 0 {HOSTILE}\ndrop 1   spaced  \t out  \n", capsys=capsys)

    assert code == 0
    assert [e["reason"] for e in area.read("dropped.json")] == [HOSTILE, "spaced  \t out  "]


SPEC_RECORDS = (
    "tag 0 in-spec\ntag 2 fix-caused\ncover ac-1 met\ncover ac-2 unmet\n"
    "drop 1 not real\nreject The guide change alters documented behaviour.\n"
)


def test_spec_mode_tags_kept_findings_and_writes_coverage_and_the_reject_reason(area, capsys):
    code, result, _ = area.apply(SPEC_RECORDS, capsys=capsys, spec=True)

    assert code == 0
    kept = area.read("judged.json")["findings"]
    assert [(f["rule_id"], f["spec_tag"]) for f in kept] == [("R0", "in-spec"), ("R2", "fix-caused")]
    assert area.read("coverage.json") == [
        {"criterion": "ac-1", "status": "met"}, {"criterion": "ac-2", "status": "unmet"},
    ]
    assert (area.root / "reject.txt").read_text(encoding="utf-8") == (
        "The guide change alters documented behaviour."
    )
    assert result["coverage"] == str((area.root / "coverage.json").resolve())
    assert result["reject_reason"] == str((area.root / "reject.txt").resolve())


def test_spec_mode_removes_the_reject_reason_an_earlier_run_left(area, capsys):
    (area.root / "reject.txt").write_text("left over", encoding="utf-8")
    records = "tag 0 in-spec\ntag 1 in-spec\ntag 2 in-spec\ncover ac-1 met\n"

    code, result, _ = area.apply(records, capsys=capsys, spec=True)

    assert code == 0 and not (area.root / "reject.txt").exists() and result["reject_reason"] is None


@pytest.mark.parametrize("given", ["coverage", "reject"])
def test_the_two_spec_mode_outputs_are_given_together(area, capsys, given):
    flag = {"coverage": "--coverage-out", "reject": "--reject-reason-out"}[given]

    code, _, err = area.apply("", flag, str(area.root / "x"), capsys=capsys)

    assert code == review_cli.EXIT_USAGE and "together" in err
    assert not (area.root / "judged.json").exists()


def test_a_manifest_stamps_read_at_on_every_dropped_entry(area, capsys):
    scanner = area.write("scanner.json", SCANNERS)
    records = "drop 0 r0\ndrop s1 r1\n"

    code, result, _ = area.apply(
        records, "--scanner-input", scanner, "--manifest", area.manifest(), capsys=capsys
    )
    assert code == 0 and result["read_at"] == HEAD
    assert [e["read_at"] for e in area.read("dropped.json")] == [HEAD, HEAD]

    code, result, _ = area.apply(records, "--scanner-input", scanner, capsys=capsys)
    assert code == 0 and result["read_at"] is None
    assert all("read_at" not in e for e in area.read("dropped.json"))


def test_a_scanner_input_stamped_for_another_head_than_the_manifest_is_refused(area, capsys):
    scanner = area.write("scanner.json", {**SCANNERS, "head_sha": "c" * 40})

    code, _, err = area.apply(
        "", "--scanner-input", scanner, "--manifest", area.manifest(), capsys=capsys
    )

    assert code == REFUSED and "stamped for head" in err
