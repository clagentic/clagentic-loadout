"""A breaking interface change whose consumers the sealed spec leaves undisposed is
named in the merge output and the merge-completion attestation, and nothing else
changes: the merge proceeds with the same outcome either way."""

from __future__ import annotations

import base64
import json
import urllib.parse

import pytest

from clagentic_loadout.interface.consumers import CONSUMERS_FILE_ENV_VAR
from clagentic_loadout.interface.model import INTERFACE_PATH
from clagentic_loadout.merge import verb
from clagentic_loadout.merge.interface_notice import collect_interface_notices
from clagentic_loadout.merge.verdict import build_verdict_block
from clagentic_loadout.review.policy_reader import PolicyReadError
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from tests._support.fakes import json_resp
from tests._support.interface_fixtures import (
    ADDED_INTERFACE,
    BASE_INTERFACE,
    RENAMED_INTERFACE,
    write_consumers,
)
from tests._support.merge_verb import (
    FULL_SHA,
    OTHER_FULL_SHA,
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)

BASE_SHA = OTHER_FULL_SHA
HEAD_SHA = FULL_SHA
SPEC_HEAD = "c" * 40


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.delenv(CONSUMERS_FILE_ENV_VAR, raising=False)
    monkeypatch.setenv("CLAGENTIC_LOADOUT_SPECS_DIR", str(tmp_path / "specs"))


def _seal_spec(items: list[dict[str, str]]) -> str:
    draft = model.new_draft("planner-role")
    draft["goal"] = "Change a thing."
    draft["acceptance_criteria"] = [{"id": "ac-1", "given": "g", "when": "w", "then": "t"}]
    draft["consumers"] = {"source": "test", "note": None, "items": items}
    sealed = model.seal(draft, head_sha=SPEC_HEAD, sealed_at="2026-01-01T00:00:00Z")
    spec_store.write_sealed(spec_store.specs_root(), sealed)
    return sealed.spec_hash


class FakeReader:
    """A policy reader over {revision: {path: text}}."""

    def __init__(self, revisions: dict[str, dict[str, str]]):
        self._revisions = revisions
        self.reads = 0

    def list_paths(self, base: str) -> list[str]:
        self.reads += 1
        if base not in self._revisions:
            raise PolicyReadError(f"cannot read the file list at {base[:12]}")
        return list(self._revisions[base])

    def read_text(self, base: str, path: str) -> str:
        return self._revisions[base][path]


def _reader(base: str | None, head: str | None) -> FakeReader:
    return FakeReader(
        {
            BASE_SHA: {INTERFACE_PATH: base} if base is not None else {},
            HEAD_SHA: {INTERFACE_PATH: head} if head is not None else {},
        }
    )


def _collect(reader, tmp_path, *, hashes=(), files=(INTERFACE_PATH,), base=BASE_SHA, with_map=True):
    path = write_consumers(tmp_path)
    env = {CONSUMERS_FILE_ENV_VAR: str(path)} if with_map else {}
    built: list[int] = []

    def make_readers():
        built.append(1)
        return [reader]

    result = collect_interface_notices(
        changed_files=list(files),
        make_readers=make_readers,
        base_sha=base,
        head_sha=HEAD_SHA,
        producer="some-repo",
        fence_spec_hashes=list(hashes),
        config_root=tmp_path / "cfg",
        env=env,
    )
    return result, built


class TestCollect:
    def test_a_change_that_does_not_touch_the_file_reads_nothing(self, tmp_path):
        reader = _reader(BASE_INTERFACE, RENAMED_INTERFACE)

        result, built = _collect(reader, tmp_path, files=["a.py"])

        assert result.lines == result.notes == () and built == [] and reader.reads == 0

    def test_a_non_breaking_change_prints_nothing(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, ADDED_INTERFACE), tmp_path)

        assert (result.lines, result.notes, result.undisposed) == ((), (), ())

    @pytest.mark.parametrize("base,head", [(None, BASE_INTERFACE), (BASE_INTERFACE, None), (None, None)])
    def test_a_missing_interface_file_prints_nothing(self, tmp_path, base, head):
        result, _ = _collect(_reader(base, head), tmp_path)

        assert (result.lines, result.notes) == ((), ())

    def test_every_consumer_disposed_prints_nothing(self, tmp_path):
        spec = _seal_spec(
            [
                {"consumer": "app-one: tool-one", "disposition": "task-1"},
                {"consumer": "app-two", "disposition": "none: being retired"},
            ]
        )

        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, hashes=[spec])

        assert result.lines == () and result.undisposed == ()

    def test_a_consumer_without_a_disposition_is_named_with_its_element(self, tmp_path):
        spec = _seal_spec([{"consumer": "app-one: tool-one", "disposition": "task-1"}])

        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, hashes=[spec])

        assert [(u.consumer, u.element) for u in result.undisposed] == [("app-two", "tool-one")]
        assert len(result.lines) == 1
        line = result.lines[0]
        assert "app-two" in line and "tool-one" in line and "verb tool-one" in line
        assert "BREAKING INTERFACE CHANGE" in line

    def test_a_consumer_listed_with_no_real_disposition_is_named(self, tmp_path):
        spec = _seal_spec(
            [
                {"consumer": "app-one", "disposition": "task-1"},
                {"consumer": "app-two", "disposition": "none"},
            ]
        )

        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, hashes=[spec])

        assert [u.consumer for u in result.undisposed] == ["app-two"]

    def test_with_no_spec_every_consumer_is_named_and_a_note_says_why(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path)

        assert {u.consumer for u in result.undisposed} == {"app-one", "app-two"}
        assert any("no sealed spec hash" in note for note in result.notes)

    def test_a_spec_that_is_not_in_the_store_is_a_note_not_a_failure(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, hashes=["d" * 64])

        assert {u.consumer for u in result.undisposed} == {"app-one", "app-two"}
        assert any("was not read" in note for note in result.notes)

    def test_an_unavailable_map_is_a_note(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, with_map=False)

        assert result.lines == () and any("map is unavailable" in n for n in result.notes)

    def test_an_unreadable_revision_is_a_note_not_a_failure(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, RENAMED_INTERFACE), tmp_path, base="f" * 40)

        assert result.lines == () and any("interface diff not computed" in n for n in result.notes)

    def test_an_invalid_interface_file_is_a_note_not_a_failure(self, tmp_path):
        result, _ = _collect(_reader(BASE_INTERFACE, "schema_version: 9\n"), tmp_path)

        assert result.lines == () and any("interface diff not computed" in n for n in result.notes)


def _host(inner, trees: dict[str, dict[str, str]], posted: list[str]):
    """*inner* with the tree and contents endpoints answered from *trees*, and
    every posted comment body recorded in *posted*."""

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET" and "/git/trees/" in url:
            ref = urllib.parse.unquote(url.split("/git/trees/")[1].split("?")[0])
            entries = [{"path": p, "type": "blob"} for p in trees[ref]]
            return json_resp(200, {"tree": entries, "truncated": False, "total_count": len(entries)})
        if method == "GET" and "/contents/" in url:
            path = urllib.parse.unquote(url.split("/contents/")[1].split("?")[0])
            ref = urllib.parse.unquote(url.split("ref=")[1])
            content = base64.b64encode(trees[ref][path].encode("utf-8")).decode("ascii")
            return json_resp(200, {"encoding": "base64", "content": content})
        if method == "POST" and "/comments" in url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return inner(req, timeout)

    return opener


def _merge(capsys, tmp_path, *, head_interface, spec_items, files=(INTERFACE_PATH, "a.py")):
    write_consumers(tmp_path)
    spec = _seal_spec(spec_items)
    fence = build_verdict_block(
        "some-reviewer",
        "clean",
        HEAD_SHA,
        1,
        None,
        {
            "spec_hash": spec,
            "spec_tags": [],
            "spec_coverage": [{"criterion": "ac-1", "status": "met"}],
        },
    )
    pr_info = {
        "head": {"sha": HEAD_SHA},
        "base": {"ref": "main", "sha": BASE_SHA},
        "title": "feat: change the interface",
    }
    posted: list[str] = []
    inner = make_opener(
        pr_info=pr_info,
        files=list(files),
        comments=[{"id": 1, "user": {"login": "reviewer-login"}, "body": fence}],
    )
    opener = _host(
        inner,
        {
            BASE_SHA: {INTERFACE_PATH: BASE_INTERFACE, "a.py": "x"},
            HEAD_SHA: {INTERFACE_PATH: head_interface, "a.py": "y"},
        },
        posted,
    )
    capsys.readouterr()
    code = verb.main(
        base_args(
            **{
                "--expected-head-sha": HEAD_SHA,
                "--required-reviewer": "some-reviewer:reviewer-login",
            }
        ),
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=opener,
    )
    captured = capsys.readouterr()
    return code, captured.out, captured.err, posted


@pytest.fixture
def with_map(tmp_path, monkeypatch):
    monkeypatch.setenv(CONSUMERS_FILE_ENV_VAR, str(tmp_path / "consumers.yaml"))


class TestMergeVerb:
    DISPOSED = [
        {"consumer": "app-one: tool-one", "disposition": "task-1"},
        {"consumer": "app-two: tool-one", "disposition": "none: retiring next week"},
    ]

    def test_all_consumers_disposed_proceeds_with_no_notice(self, tmp_path, capsys, with_map):
        code, out, err, posted = _merge(
            capsys, tmp_path, head_interface=RENAMED_INTERFACE, spec_items=self.DISPOSED
        )

        assert code == verb.EXIT_OK
        assert "consumer without a disposition" not in err and "undisposed_consumers" not in out
        assert len(posted) == 1 and "Interface notices" not in posted[0]

    def test_an_undisposed_consumer_proceeds_and_is_named_in_the_output_and_the_attestation(
        self, tmp_path, capsys, with_map
    ):
        code, out, err, posted = _merge(
            capsys,
            tmp_path,
            head_interface=RENAMED_INTERFACE,
            spec_items=[self.DISPOSED[0]],
        )

        assert code == verb.EXIT_OK
        assert "merge: NOTICE -- BREAKING INTERFACE CHANGE, consumer without a disposition: app-two" in err
        assert "tool-one" in err
        assert json.loads(out.strip().splitlines()[-1])["undisposed_consumers"] == [
            {"consumer": "app-two", "element": "tool-one", "confirmed_at": "2026-09-01"}
        ]
        assert len(posted) == 1
        assert "Interface notices" in posted[0] and "app-two" in posted[0]
        assert "app-one" not in posted[0].split("Interface notices")[1].split("\n")[0]

    def test_the_outcome_is_the_same_with_and_without_a_notice(self, tmp_path, capsys, with_map):
        without_notice = _merge(
            capsys, tmp_path, head_interface=RENAMED_INTERFACE, spec_items=self.DISPOSED
        )
        with_notice = _merge(capsys, tmp_path, head_interface=RENAMED_INTERFACE, spec_items=[])

        assert "consumer without a disposition" not in without_notice[2]
        assert "consumer without a disposition" in with_notice[2]
        assert without_notice[0] == with_notice[0] == verb.EXIT_OK
        assert len(without_notice[3]) == len(with_notice[3]) == 1

    def test_a_non_breaking_change_needs_no_consumers_and_prints_no_notice(
        self, tmp_path, capsys, with_map
    ):
        code, out, err, posted = _merge(
            capsys, tmp_path, head_interface=ADDED_INTERFACE, spec_items=[]
        )

        assert code == verb.EXIT_OK
        assert "NOTICE -- BREAKING" not in err and "NOTE --" not in err
        assert "undisposed_consumers" not in out and "Interface notices" not in posted[0]

    def test_a_pr_that_does_not_touch_the_file_asks_the_host_for_nothing_new(
        self, tmp_path, capsys, with_map
    ):
        code, _out, err, posted = _merge(
            capsys,
            tmp_path,
            head_interface=RENAMED_INTERFACE,
            spec_items=[],
            files=("a.py",),
        )

        assert code == verb.EXIT_OK
        assert "BREAKING" not in err and "Interface notices" not in posted[0]
