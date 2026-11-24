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


def _inline(area, subdir, records, *extra, capsys, spec=False, flag="--record"):
    """Run apply-drops with *records* given as one --record argument per line of the text
    (a blank line is a blank argument), outputs under *subdir*."""
    out = area.root / subdir
    out.mkdir(exist_ok=True)
    argv = [
        "apply-drops", "--findings", area.write("findings.json", DOCUMENT),
        "--judged-out", str(out / "judged.json"), "--dropped-out", str(out / "dropped.json"),
        *extra,
    ]
    for record in records.removesuffix("\n").split("\n") if records else []:
        argv += [flag, record]
    if spec:
        argv += ["--coverage-out", str(out / "coverage.json"),
                 "--reject-reason-out", str(out / "reject.txt")]
    capsys.readouterr()
    code = review_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out.replace(str(out), "OUT"), captured.err, out


def _file_form(area, subdir, records, *extra, capsys, spec=False):
    out = area.root / subdir
    out.mkdir(exist_ok=True)
    argv = [
        "apply-drops", "--findings", area.write("findings.json", DOCUMENT),
        "--records", area.write(f"{subdir}-records.txt", records, raw=True),
        "--judged-out", str(out / "judged.json"), "--dropped-out", str(out / "dropped.json"),
        *extra,
    ]
    if spec:
        argv += ["--coverage-out", str(out / "coverage.json"),
                 "--reject-reason-out", str(out / "reject.txt")]
    capsys.readouterr()
    code = review_cli.main(argv)
    captured = capsys.readouterr()
    return code, captured.out.replace(str(out), "OUT"), captured.err, out


#: (id, records, spec mode, scanner input, manifest)
PARITY = [
    ("plain", "drop 1 not a defect\n\ndrop 0 second\n", False, False, False),
    ("hostile-reason", f"drop 0 {HOSTILE}\n", False, False, False),
    ("manifest", "drop 2 r\n", False, False, True),
    ("scanner", "drop s2 fixture\ndrop 1 r\n", False, True, False),
    ("scanner-manifest", "drop s1 a\n", False, True, True),
    ("spec-tag-cover", "tag 0 in-spec\ntag 1 out-of-spec\ntag 2 fix-caused\ncover a met\ncover b unmet\n",
     True, False, False),
    ("spec-reject", "drop 1 r\ntag 0 in-spec\ntag 2 in-spec\ncover a met\nreject Behaviour is altered.\n",
     True, True, True),
]


@pytest.mark.parametrize("records,spec,scanner,manifest", [r[1:] for r in PARITY], ids=[r[0] for r in PARITY])
def test_inline_records_give_byte_identical_outputs_and_stdout_to_a_records_file(
    area, capsys, records, spec, scanner, manifest
):
    extra = (["--scanner-input", area.write("scanner.json", SCANNERS)] if scanner else []) + (
        ["--manifest", area.manifest()] if manifest else []
    )
    f_code, f_out, _, f_dir = _file_form(area, "file", records, *extra, capsys=capsys, spec=spec)
    i_code, i_out, _, i_dir = _inline(area, "inline", records, *extra, capsys=capsys, spec=spec)

    assert f_code == i_code == 0 and f_out == i_out
    names = sorted(p.name for p in f_dir.iterdir())
    assert names == sorted(p.name for p in i_dir.iterdir())
    for name in names:
        assert (f_dir / name).read_bytes() == (i_dir / name).read_bytes()


@pytest.mark.parametrize("records,needle,spec", [r[1:] for r in REFUSALS], ids=[r[0] for r in REFUSALS])
def test_an_unusable_inline_record_is_refused_naming_its_argument_and_nothing_is_written(
    area, capsys, records, needle, spec
):
    judged = area.root / "inline" / "judged.json"
    judged.parent.mkdir()
    judged.write_text("earlier judged", encoding="utf-8")
    records_out = area.root / "records-out.txt"
    scanner = area.write("scanner.json", SCANNERS)

    code, out, err, _ = _inline(
        area, "inline", records, "--scanner-input", scanner, "--records-out", str(records_out),
        capsys=capsys, spec=spec,
    )

    assert code == REFUSED and out == ""
    assert needle.replace("line ", "--record argument ") in err or needle in err
    assert judged.read_text(encoding="utf-8") == "earlier judged"
    assert not records_out.exists() and not (area.root / "inline" / "coverage.json").exists()


@pytest.mark.parametrize("bad", ["drop 0 a\nb", "drop 0 a\rb", "drop 0 a\r\n"])
def test_a_record_argument_holding_a_line_break_is_refused_and_nothing_is_written(area, capsys, bad):
    out = area.root / "o"
    out.mkdir()
    records_out = area.root / "records-out.txt"

    capsys.readouterr()
    code = review_cli.main([
        "apply-drops", "--findings", area.write("findings.json", DOCUMENT),
        "--record", "drop 1 fine", "--record", bad, "--records-out", str(records_out),
        "--judged-out", str(out / "j.json"), "--dropped-out", str(out / "d.json"),
    ])

    assert code == REFUSED and "--record argument 2" in capsys.readouterr().err
    assert list(out.iterdir()) == [] and not records_out.exists()


def test_records_and_record_together_are_a_usage_error_naming_both_flags(area, capsys):
    out = area.root / "o"
    out.mkdir()

    capsys.readouterr()
    code = review_cli.main([
        "apply-drops", "--findings", area.write("findings.json", DOCUMENT),
        "--records", area.write("records.txt", "", raw=True), "--record", "drop 0 r",
        "--records-out", str(area.root / "records-out.txt"),
        "--judged-out", str(out / "j.json"), "--dropped-out", str(out / "d.json"),
    ])

    err = capsys.readouterr().err
    assert code == review_cli.EXIT_USAGE and "--records" in err and "--record" in err
    assert list(out.iterdir()) == [] and not (area.root / "records-out.txt").exists()


def test_no_records_input_behaves_as_an_empty_records_file(area, capsys):
    extra = ["--scanner-input", area.write("scanner.json", SCANNERS)]
    f_code, f_out, _, f_dir = _file_form(area, "file", "", *extra, capsys=capsys)
    n_code, n_out, _, n_dir = _inline(area, "none", "", *extra, capsys=capsys)

    assert f_code == n_code == 0 and f_out == n_out
    assert json.loads(n_out)["hits"][0]["id"] == "s1"
    for name in ("judged.json", "dropped.json"):
        assert (f_dir / name).read_bytes() == (n_dir / name).read_bytes()
    assert area.read("none/judged.json") == DOCUMENT


def test_no_records_input_in_spec_mode_is_refused_as_an_empty_file_is(area, capsys):
    code, out, err, _ = _inline(area, "none", "", capsys=capsys, spec=True)

    assert code == REFUSED and out == "" and "no tag record" in err


def test_records_out_holds_the_inline_records_or_the_files_text_byte_for_byte(area, capsys):
    text = "drop 0 a\r\n\r\n  \ndrop 1 b  \n"
    from_file = area.root / "from-file.txt"
    from_inline = area.root / "from-inline.txt"

    _file_form(area, "file", text, "--records-out", str(from_file), capsys=capsys)
    code, *_ = _inline(
        area, "inline", "drop 0 a\n\n  \ndrop 1 b  ", "--records-out", str(from_inline), capsys=capsys
    )

    assert code == 0
    assert from_file.read_bytes() == text.encode("utf-8")
    assert from_inline.read_bytes() == b"drop 0 a\ndrop 1 b  \n"


def test_records_out_is_written_through_the_atomic_writer(area, capsys, monkeypatch):
    from clagentic_loadout.review import atomic_io

    seen: list[str] = []
    real = atomic_io.write_bytes_atomic
    monkeypatch.setattr(
        atomic_io, "write_bytes_atomic", lambda path, data: (seen.append(Path(path).name), real(path, data))[1]
    )

    code, *_ = _inline(
        area, "inline", "drop 0 a", "--records-out", str(area.root / "r.txt"), capsys=capsys
    )

    assert code == 0 and "r.txt" in seen
