"""`loadout-review procedure` through the CLI entry point, on both platforms, against the
synthetic review host: the printed lines are split and run, in order, through the entry
points of the verbs they name, with no stub of any of them."""

from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest

from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.policy_source import PROFILE_PATH
from clagentic_loadout.spec import model
from clagentic_loadout.spec import store as spec_store
from clagentic_loadout.spec import verb as spec_verb
from clagentic_loadout.transport import github_app_config, provider_config
from clagentic_loadout.transport.git_host_api import GIT_HOST_BASE_URL_ENV_VAR
from tests._review_cli_support import Env, identity_provider, make_diff
from tests._support.review_host import FORGEJO_BASE_URL, ReviewHost

CHANGED = "src/mod.py"
REPO = "some-owner/some-repo"
DOT_REPO = "some-owner/.profile"
ROLE = "reviewer"
CRITERIA = ("ac-1", "ac-2")
JUSTIFICATION = "Only wording in the guide changed; no behaviour is touched."
REJECTION = "The change alters documented behaviour."
STEPS = ["run", "apply", "check", "post_clean", "post_blocking", "keep", "teardown"]
STEPS_SPEC = [
    "criteria", "run", "apply", "check", "post_clean", "post_blocking", "post_clean_reject",
    "post_blocking_reject", "keep", "teardown",
]
REFUSED = 64


@dataclass
class World:
    env: Env
    host: ReviewHost
    root: Path

    def argv(self, out: Path, *extra: str, repo: str = REPO, role: str = ROLE, base_url=True):
        args = ["procedure", "--caller", ROLE, "--repo", repo, "--role", role, "--out", str(out)]
        platform = self.host.platform_args()
        if not base_url and "--git-host-base-url" in platform:
            platform = platform[:-2]
        return [*args, *platform, *extra]

    def procedure(
        self, out: Path, *extra: str, capsys, caller: str = ROLE, **kwargs
    ):
        argv = self.argv(out, *extra, **kwargs)
        capsys.readouterr()
        code = review_cli.main(
            argv,
            token_provider=self.env.token_provider,
            opener=self.host.opener,
            identity_provider=identity_provider(caller),
            config_root=self.env.cfg,
            run_root=self.env.runs,
        )
        captured = capsys.readouterr()
        objects = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(objects[-1]) if objects else {}), captured.err

    def execute(self, line: str, capsys):
        """Run one printed line through the entry point of the verb it names."""
        argv = shlex.split(line)
        capsys.readouterr()
        if argv[0] == "loadout-spec":
            code = spec_verb.main(argv[1:], config_root=self.env.cfg)
        else:
            assert argv[0] == "loadout-review", line
            code = review_cli.main(
                argv[1:],
                token_provider=self.env.token_provider,
                opener=self.host.opener,
                identity_provider=identity_provider(ROLE),
                config_root=self.env.cfg,
                run_root=self.env.runs,
            )
        captured = capsys.readouterr()
        objects = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(objects[-1]) if objects else {}), captured.err

    def seal(self) -> str:
        draft = model.new_draft("planner-role")
        draft["goal"] = "Change a module."
        draft["acceptance_criteria"] = [
            {"id": cid, "given": "g", "when": "w", "then": "t"} for cid in CRITERIA
        ]
        draft["docs_only_justification"] = JUSTIFICATION
        sealed = model.seal(draft, head_sha="c" * 40, sealed_at="2026-01-01T00:00:00Z")
        spec_store.write_sealed(spec_store.specs_root(), sealed)
        return sealed.spec_hash

    def scanner_input(self, *, hit: bool = True, name: str = "scanner-input.json") -> str:
        path = self.root / name
        hits = [{"file": CHANGED, "line": 1, "rule_id": "S1", "message": "a hit"}] if hit else []
        path.write_text(
            json.dumps(
                {
                    "head_sha": self.host.head_sha,
                    "scanners": [
                        {
                            "scanner": "sast-one", "status": "ran",
                            "hits": hits,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return str(path)


@pytest.fixture(params=["github", "forgejo"])
def world(request, tmp_path, monkeypatch) -> World:
    env = Env(tmp_path, seed_profile=False)
    env.configure(carrier_mode="facts_harmless")
    host = ReviewHost(
        request.param,
        diff=make_diff({CHANGED: 1}),
        base_sha=env.base_sha,
        files=[CHANGED],
        base_files={PROFILE_PATH: "class: internal\n"},
        head_files={CHANGED: b"print('head')\n"},
    )
    monkeypatch.setenv("STUB_DIR", str(env.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return World(env, host, tmp_path)


def _tokens(payload: dict) -> dict[str, list[str]]:
    return {item["step"]: shlex.split(item["line"]) for item in payload["lines"]}


def _value(tokens: list[str], flag: str) -> str:
    return tokens[tokens.index(flag) + 1]


#: One row per shape of invocation: (repository, spec, scanner input).
ROWS = {
    "plain": (REPO, False, False),
    "spec": (REPO, True, False),
    "scanner": (REPO, False, True),
    "spec-and-scanner": (REPO, True, True),
    "dot-named-repo": (DOT_REPO, False, False),
    "dot-named-repo-spec": (DOT_REPO, True, False),
}


def _records(spec: bool, scanner: bool, *, reject: bool) -> str:
    lines = ["tag 0 in-spec" if spec else "drop 0 not a defect: guarded upstream"]
    if spec:
        lines += [f"cover {cid} met" for cid in CRITERIA]
        if reject:
            lines.append(f"reject {REJECTION}")
    if scanner and not spec:
        # A spec row's scanner reports no hit, so there is nothing to drop there.
        lines.append("drop s1 a test fixture")
    return "\n".join(lines) + "\n"


def _print(world, tmp_path, row, capsys, *extra):
    repo, spec, scanner = ROWS[row]
    args = list(extra)
    spec_hash = world.seal() if spec else None
    if spec:
        args += ["--spec-hash", spec_hash]
    if scanner:
        # A spec review's scanner reports no hit: a hit has no spec tag for `post` to
        # carry, and a dropped hit is matched by `post` under another rule id.
        args += ["--scanner-input", world.scanner_input(hit=not spec)]
    code, payload, err = world.procedure(tmp_path / "review", *args, capsys=capsys, repo=repo)
    assert code == 0, err
    return payload, spec_hash


@pytest.mark.parametrize("row", ROWS)
def test_the_printed_object_pins_the_head_and_the_lines_hold_nothing_to_substitute(
    world, tmp_path, capsys, row
):
    repo, spec, scanner = ROWS[row]
    payload, spec_hash = _print(world, tmp_path, row, capsys)

    out = (tmp_path / "review").resolve()
    manifest = json.loads(Path(payload["manifest"]).read_text(encoding="utf-8"))
    assert payload["result"] == "procedure" and payload["head_sha"] == world.host.head_sha
    assert manifest["head_sha"] == world.host.head_sha and manifest["repo"] == repo
    assert (payload["role"], payload["repo"], payload["pr"]) == (ROLE, repo, world.host.number)
    assert payload["platform"] == world.host.platform and payload["spec_hash"] == spec_hash
    assert Path(payload["manifest"]).parent == out / "head"
    work = out / "work"
    assert payload["work_dir"] == str(work) and (work / "records.txt").read_text() == ""
    assert payload["files"]["records"] == str(work / "records.txt")
    assert payload["files"]["findings"] == str(work / "run" / "findings.json")
    expected = STEPS_SPEC if spec else STEPS
    steps = [item["step"] for item in payload["lines"]]
    at = expected.index("apply")
    assert steps == expected[:at] + (["hits"] if scanner else []) + expected[at:]
    for item in payload["lines"]:
        line = item["line"]
        assert "\n" not in line and not any(ch in line for ch in "<>$`")
        assert shlex.split(line)[0] in ("loadout-review", "loadout-spec")
    tokens = _tokens(payload)
    assert (repo in tokens["run"]) and _value(tokens["post_clean"], "--repo") == repo
    assert _value(tokens["keep"], "--repo") == repo


#: The reject variants of the post exist under a spec only.
RUN_CASES = [
    (row, reject)
    for row, (_, spec, _) in ROWS.items()
    for reject in (False, True)
    if spec or not reject
]


@pytest.mark.parametrize("row,reject", RUN_CASES)
def test_every_printed_line_runs_as_written_in_order(world, tmp_path, capsys, row, reject):
    repo, spec, scanner = ROWS[row]
    payload, spec_hash = _print(world, tmp_path, row, capsys)
    files = payload["files"]
    skipped = {"post_clean_reject", "post_blocking_reject"} if not reject else {
        "post_clean", "post_blocking"
    }
    results = {}

    for item in payload["lines"]:
        step = item["step"]
        if step in skipped:
            continue
        if step == "apply":
            Path(files["records"]).write_text(
                _records(spec, scanner, reject=reject), encoding="utf-8"
            )
        code, result, err = world.execute(item["line"], capsys)
        assert code == 0, (step, item["line"], err)
        results[step] = result

    assert Path(files["findings"]).is_file()
    dropped = json.loads(Path(files["dropped"]).read_text(encoding="utf-8"))
    # A spec review keeps its one finding, tagged; the others drop it, and a non-spec
    # scanner review drops its hit as well.
    assert len(dropped) == (0 if spec else 1 + (1 if scanner else 0))
    assert all(entry["read_at"] == world.host.head_sha for entry in dropped)
    assert results["apply"]["read_at"] == world.host.head_sha
    assert len(results["check"]["drops"]) == len(dropped)
    assert all(d["proven"] for d in results["check"]["drops"])
    if scanner:
        assert [h["id"] for h in results["hits"]["hits"]] == ([] if spec else ["s1"])
        assert json.loads(Path(files["hits_judged"]).read_text(encoding="utf-8"))["findings"]
    assert world.host.posted and world.host.head_sha in world.host.last_body
    assert results["keep"]["comment_id_source"] == "review record"
    assert results["keep"]["comment_id"] == str(world.host.posted[-1]["id"])
    kept = {entry["name"] for entry in results["keep"]["files"]}
    assert kept == {
        "findings.json", "judged.json", "dropped.json", "records.txt",
        *(["coverage.json"] if spec else []),
        *(["scanner-input.json"] if scanner else []),
    }
    if reject:
        assert REJECTION in world.host.last_body
    assert not Path(payload["manifest"]).parent.exists()
    assert Path(files["work_dir"]).is_dir()


def test_the_spec_lines_carry_the_spec_and_the_reject_variants_name_the_reason_file(
    world, tmp_path, capsys
):
    payload, spec_hash = _print(world, tmp_path, "spec-and-scanner", capsys)

    tokens = _tokens(payload)
    files = payload["files"]
    assert payload["lines"][0]["step"] == "criteria"
    assert tokens["criteria"] == ["loadout-spec", "show", spec_hash, "--field", "acceptance_criteria"]
    assert _value(tokens["run"], "--spec-hash") == spec_hash
    assert _value(tokens["apply"], "--coverage-out") == files["coverage"]
    assert _value(tokens["apply"], "--reject-reason-out") == files["reject_reason"]
    for step in ("post_clean", "post_blocking", "post_clean_reject", "post_blocking_reject"):
        assert _value(tokens[step], "--spec-hash") == spec_hash
        assert _value(tokens[step], "--spec-coverage") == files["coverage"]
        assert _value(tokens[step], "--scanner-input") == files["scanner_input"]
    for plain, reject in (("post_clean", "post_clean_reject"), ("post_blocking", "post_blocking_reject")):
        assert tokens[reject] == [
            *tokens[plain], "--reject-justification-file", files["reject_reason"]
        ]
    assert _value(tokens["keep"], "--file") == files["findings"]
    assert files["coverage"] in tokens["keep"] and files["scanner_input"] in tokens["keep"]


def test_the_scanner_step_lists_hits_without_touching_the_judged_or_dropped_files(
    world, tmp_path, capsys
):
    payload, _ = _print(world, tmp_path, "scanner", capsys)

    tokens = _tokens(payload)
    files = payload["files"]
    assert _value(tokens["hits"], "--records") == files["hits_records"]
    assert Path(files["hits_records"]).read_text() == ""
    assert {_value(tokens["hits"], f) for f in ("--judged-out", "--dropped-out")} == {
        files["hits_judged"], files["hits_dropped"],
    }
    assert files["judged"] not in tokens["hits"] and files["dropped"] not in tokens["hits"]
    for step in ("apply", "post_clean", "post_blocking"):
        assert _value(tokens[step], "--scanner-input") == files["scanner_input"]
    assert files["scanner_input"] in tokens["keep"]


def test_the_role_binds_the_lines_and_the_caller_binds_the_host_read(world, tmp_path, capsys):
    code, payload, _ = world.procedure(
        tmp_path / "review", capsys=capsys, role="invented-role"
    )

    assert code == 0
    tokens = _tokens(payload)
    for step in ("run", "post_clean", "post_blocking"):
        assert _value(tokens[step], "--caller") == "invented-role"
    assert _value(tokens["keep"], "--role") == "invented-role"
    assert all("reviewer" not in item["line"].split() for item in payload["lines"])


def test_a_caller_that_is_not_the_attested_identity_is_refused_before_any_io(
    world, tmp_path, capsys
):
    code, payload, err = world.procedure(tmp_path / "review", capsys=capsys, caller="someone-else")

    assert code == review_cli.EXIT_CALLER_INVOKER_MISMATCH and payload == {}
    assert world.host.requests == [] and not (tmp_path / "review").exists()


def test_the_base_url_is_carried_by_a_forgejo_procedure_only_when_given(
    world, tmp_path, capsys, monkeypatch
):
    given = ["--git-host-base-url", FORGEJO_BASE_URL] if world.host.platform == "github" else []
    code, payload, err = world.procedure(tmp_path / "with", *given, capsys=capsys, base_url=True)
    assert code == 0, err
    carried = {
        step for step, tokens in _tokens(payload).items() if "--git-host-base-url" in tokens
    }
    if world.host.platform == "forgejo":
        assert carried == {"run", "post_clean", "post_blocking"}
        assert _value(_tokens(payload)["run"], "--git-host-base-url") == FORGEJO_BASE_URL
    else:
        assert carried == set()

    if world.host.platform == "forgejo":
        monkeypatch.setenv(GIT_HOST_BASE_URL_ENV_VAR, FORGEJO_BASE_URL)
        code, payload, _ = world.procedure(tmp_path / "without", capsys=capsys, base_url=False)
        assert code == 0 and not any(
            "--git-host-base-url" in line["line"] for line in payload["lines"]
        )


def test_a_staging_gap_is_carried_and_never_changes_the_exit_code(world, tmp_path, capsys):
    world.host.pull["changed_files"] = 5

    code, payload, err = world.procedure(tmp_path / "review", capsys=capsys)

    assert code == 0 and payload["staging_gap"]["host"] == 5
    assert "staging gap" in err


def _refusal(world, tmp_path, capsys, case):
    out = tmp_path / "review"
    extra: list[str] = []
    if case == "out-not-empty":
        out.mkdir()
        (out / "keep.txt").write_text("mine", encoding="utf-8")
    elif case == "malformed-hash":
        extra = ["--spec-hash", "XYZ"]
    elif case == "unknown-hash":
        extra = ["--spec-hash", "e" * 64]
    elif case == "scanner-name-clash":
        extra = ["--scanner-input", str(tmp_path / "elsewhere" / "findings.json")]
    elif case == "newline-in-role":
        return out, world.procedure(out, capsys=capsys, role="two\nlines")
    return out, world.procedure(out, *extra, capsys=capsys)


@pytest.mark.parametrize(
    "case,expected,needle",
    [
        ("out-not-empty", REFUSED, "review"),
        ("malformed-hash", 1, "XYZ"),
        ("unknown-hash", review_cli.EXIT_SPEC_UNAVAILABLE, "e" * 64),
        ("scanner-name-clash", 1, "findings.json"),
        ("newline-in-role", 1, "two"),
    ],
)
def test_an_unusable_input_is_refused_naming_it_before_the_host_is_read(
    world, tmp_path, capsys, case, expected, needle
):
    out, (code, payload, err) = _refusal(world, tmp_path, capsys, case)

    assert code == expected and payload == {} and needle in err
    assert world.host.requests == []
    if case == "out-not-empty":
        assert sorted(p.name for p in out.iterdir()) == ["keep.txt"]
    else:
        assert not out.exists()
