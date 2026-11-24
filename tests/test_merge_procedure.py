"""`loadout-merge-procedure` through its entry point, on both platforms, against the
synthetic merge-gate hosts: the printed lines are split and run through the entry points
of the verbs they name, with no stub of any of them."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from clagentic_loadout.merge import close_verb, post_merge_verb, procedure_verb
from clagentic_loadout.merge import verb as merge_verb
from clagentic_loadout.review import ruling_verb
from clagentic_loadout.review.ruling_authority import DEFAULT_SOURCE
from clagentic_loadout.transport import github_app_config, provider_config
from tests._gate_repo import init_gate_repo, make_origin_confirmable
from tests._review_cli_support import identity_provider
from tests._ruling_support import FP, OTHER_FP, TODAY
from tests._support.fakes import RecordingTokenProvider, json_resp
from tests._support.platform_openers import forgejo_opener, github_opener
from tests._support.review_host import FORGEJO_BASE_URL

REPO = "some-owner/some-repo"
DOT_REPO = "some-owner/.profile"
ROLE = "releaser"
OTHER_ROLE = "co-releaser"
REVIEWERS = ("reviewer-a", "reviewer-b")
STEPS = ["head", "merge_check", "merge", "post_merge", "close"]
REFUSED = 64
SHA40 = "0123456789abcdef0123456789abcdef01234567"

RULINGS = [
    {
        "fence_comment": 7, "fingerprint": FP, "kind": "accepted_risk",
        "rationale": "accepted for this change", "requested_by": "some-caller",
        "source_ref": "design note 4",
    },
    {
        "fence_comment": 8, "fingerprint": OTHER_FP, "kind": "mitigated",
        "rationale": "covered elsewhere", "requested_by": "some-caller",
        "source_ref": "design note 5", "expires": "2099-01-01", "reference": "ITEM-1",
    },
]


def write_user_config(root: Path, *, roles=None, mode=0o644, extra: dict | None = None) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    document = dict(extra or {})
    if roles is not None:
        document["roles"] = roles
    path = root / "config.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    os.chmod(path, mode)
    return path


@dataclass
class World:
    platform: str
    repo: str
    tree: Path
    cfg: Path
    head_sha: str
    base_sha: str
    requests: list[str]
    opener: object
    root: Path

    def argv(
        self, *, role: str = ROLE, caller: str = ROLE, repo: str | None = None, **flags
    ) -> list[str]:
        """Every flag in the `=` form, so a value that starts with '-' reaches the
        verb's own checks. A flag set to None is left out."""
        values = {
            "--caller": caller,
            "--role": role,
            "--repo": repo or self.repo,
            "--platform": self.platform,
            "--pr": "1",
            "--repo-path": str(self.tree),
            **flags,
        }
        if self.platform == "forgejo" and "--git-host-base-url" not in values:
            values["--git-host-base-url"] = FORGEJO_BASE_URL
        return [f"{flag}={value}" for flag, value in values.items() if value is not None]

    def procedure(self, capsys, *extra: str, caller: str = ROLE, identity: str | None = None, **kwargs):
        """Run the verb; *identity* is the attested identity (default: the caller's)."""
        argv = [*self.argv(caller=caller, **kwargs), *extra]
        capsys.readouterr()
        code = procedure_verb.main(
            argv,
            token_provider=RecordingTokenProvider(),
            opener=self.opener,
            identity_provider=identity_provider(identity or caller),
        )
        captured = capsys.readouterr()
        objects = [line for line in captured.out.strip().splitlines() if line.startswith("{")]
        return code, (json.loads(objects[-1]) if objects else {}), captured.err

    def execute(self, line: str, capsys):
        """Run one printed line through the entry point of the verb it names."""
        argv = shlex.split(line)
        kwargs = dict(
            token_provider=RecordingTokenProvider(),
            opener=self.opener,
            identity_provider=identity_provider(ROLE),
        )
        verbs = {
            "loadout-merge": merge_verb.main,
            "loadout-post-merge": post_merge_verb.main,
            "loadout-close-pr": close_verb.main,
            "loadout-ruling": lambda rest, **kw: ruling_verb.main(rest, today=TODAY, **kw),
        }
        assert argv[0] in verbs, line
        capsys.readouterr()
        code = verbs[argv[0]](argv[1:], **kwargs)
        return code, capsys.readouterr().err

    def rulings_file(self, entries=RULINGS, name="rulings.json") -> str:
        path = self.root / name
        path.write_text(json.dumps(entries), encoding="utf-8")
        return str(path)


def build_world(
    tmp_path, monkeypatch, platform: str, *, repo: str = REPO, tracked_gate="default",
    state: str = "open",
) -> World:
    root = tmp_path / "w"
    root.mkdir()
    gate = {"required_reviewer_roles": list(REVIEWERS)} if tracked_gate == "default" else tracked_gate
    fixture = init_gate_repo(root / "checkout", tracked_gate=gate)
    origin = (
        f"{FORGEJO_BASE_URL}/{repo}.git" if platform == "forgejo" else f"git@github.com:{repo}.git"
    )
    make_origin_confirmable(fixture.path, url=origin)
    cfg = root / "user-config"
    write_user_config(
        cfg,
        roles={ROLE: ["merge", "ruling"], OTHER_ROLE: ["merge"], "bystander": ["push"]},
        extra={"github_app": {"slugs": {name: f"slug-{name}" for name in REVIEWERS}}},
    )
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", cfg)
    monkeypatch.setattr(github_app_config, "DEFAULT_USER_CONFIG_ROOT", cfg)
    monkeypatch.setenv("TMPDIR", str(root))

    pr_info = {
        "head": {"sha": fixture.head_sha},
        "title": "feat: a change",
        "state": state,
        "base": {"ref": "main", "sha": fixture.base_sha},
    }
    inner = (forgejo_opener if platform == "forgejo" else github_opener)(pr_info=pr_info)
    requests: list[str] = []

    def opener(req, timeout=30):
        requests.append(f"{req.get_method()} {req.full_url}")
        if req.get_method() == "PATCH":
            return json_resp(200, {**pr_info, "state": "closed"})
        return inner(req, timeout)

    return World(
        platform, repo, fixture.path, cfg, fixture.head_sha, fixture.base_sha, requests,
        opener, root,
    )


@pytest.fixture(params=["forgejo", "github"])
def world(request, tmp_path, monkeypatch) -> World:
    return build_world(tmp_path, monkeypatch, request.param)


def tokens_of(payload: dict) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for item in payload["lines"]:
        result.setdefault(item["step"], shlex.split(item["line"]))
    return result


def value(tokens: list[str], flag: str) -> str:
    return tokens[tokens.index(flag) + 1]


def values(tokens: list[str], flag: str) -> list[str]:
    return [tokens[i + 1] for i, token in enumerate(tokens) if token == flag]


def test_the_object_pins_the_live_head_and_every_line_carries_the_same_facts(world, capsys):
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file()}")

    assert code == 0, err
    assert payload["result"] == "procedure" and payload["head_sha"] == world.head_sha
    assert (payload["repo"], payload["pr"], payload["platform"]) == (REPO, 1, world.platform)
    assert payload["base_sha"] == world.base_sha and payload["base_branch"] == "main"
    assert payload["state"] == "open" and payload["title"] == "feat: a change"
    assert payload["required_reviewers"]["roles"] == list(REVIEWERS)
    assert payload["required_reviewers"]["warnings"] == []
    assert world.base_sha[:12] in payload["required_reviewers"]["source"]
    assert payload["authorized_roles"]["roles"] == sorted([ROLE, OTHER_ROLE])
    assert payload["authorized_roles"]["source"] == str(world.cfg / "config.yaml")
    assert payload["merge_method"] == "merge" and payload["notes"] == []
    assert [item["step"] for item in payload["lines"]] == [*STEPS, "ruling", "ruling"]

    for item in payload["lines"]:
        line = item["line"]
        assert "\n" not in line and not any(ch in line for ch in "<>$`|;&")
        assert shlex.split(line)[0].startswith("loadout-")

    tokens = tokens_of(payload)
    for step in ("merge_check", "merge"):
        t = tokens[step]
        assert [value(t, f) for f in ("--role", "--platform", "--repo", "--pr")] == [
            ROLE, world.platform, REPO, "1",
        ]
        assert value(t, "--expected-head-sha") == world.head_sha
        assert values(t, "--required-reviewer") == list(REVIEWERS)
        assert values(t, "--authorized-role") == sorted([ROLE, OTHER_ROLE])
        assert value(t, "--merge-method") == "merge" and value(t, "--repo-path") == str(world.tree)
    assert tokens["merge_check"] == [*tokens["merge"], "--report-only"]

    post = tokens["post_merge"]
    assert values(post, "--authorized-role") == sorted([ROLE, OTHER_ROLE])
    assert value(post, "--repo-path") == str(world.tree) and "--expected-head-sha" not in post
    close = tokens["close"]
    assert values(close, "--authorized-role") == sorted([ROLE, OTHER_ROLE])
    assert "--repo-path" not in close and "--delete-branch" not in close
    assert tokens["head"][:3] == ["loadout-git-host-api", "--caller", ROLE]


def test_every_printed_line_runs_as_written_through_the_real_entry_points(world, capsys):
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file()}")
    assert code == 0, err
    refusals = {"merge_check": merge_verb.EXIT_GATE_RESULT_BLOCKED}

    for item in payload["lines"]:
        if item["step"] == "head":
            continue
        code, err = world.execute(item["line"], capsys)
        assert "usage:" not in err and "unrecognized" not in err, (item, err)
        if item["step"] in refusals:
            # The gate chain is reached with the pinned head and the printed reviewer
            # roles, and refuses on the stub's facts: no reviewer posted a verdict.
            assert code == refusals[item["step"]], (item, err)
            assert REVIEWERS[0] in err and payload["head_sha"] in err, err


def test_a_dot_named_repository_prints_and_runs(tmp_path, monkeypatch, capsys):
    world = build_world(tmp_path, monkeypatch, "forgejo", repo=DOT_REPO)
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file()}")
    assert code == 0, err
    assert value(tokens_of(payload)["merge"], "--repo") == DOT_REPO
    for item in payload["lines"][1:]:
        _, run_err = world.execute(item["line"], capsys)
        assert "usage:" not in run_err and "does not match" not in run_err, (item, run_err)


#: One row per merge method choice: (flags, expected method, expected message).
METHOD_ROWS = {
    "squash-with-message": (["--merge-method=squash", "--merge-message=a suffix"], "squash", "a suffix"),
    "default-is-explicit": ([], "merge", None),
    "rebase": (["--merge-method=rebase"], "rebase", None),
}


@pytest.mark.parametrize("row", METHOD_ROWS)
def test_the_merge_method_is_the_callers_and_always_printed(world, capsys, row):
    flags, method, message = METHOD_ROWS[row]
    code, payload, err = world.procedure(capsys, *flags)
    assert code == 0, err
    tokens = tokens_of(payload)
    assert payload["merge_method"] == method
    for step in ("merge_check", "merge"):
        assert value(tokens[step], "--merge-method") == method
        assert (values(tokens[step], "--merge-message") or [None])[0] == message
    assert "--merge-method" not in tokens["post_merge"] + tokens["close"]


def test_the_authority_set_comes_from_the_trusted_mapping_or_the_default(world, capsys):
    # Built-in default: no user-level file at all.
    (world.cfg / "config.yaml").unlink()
    code, payload, err = world.procedure(capsys, role="merger", caller="merger")
    assert code == 0, err
    assert payload["authorized_roles"] == {"roles": ["merger"], "source": DEFAULT_SOURCE}
    assert values(tokens_of(payload)["merge"], "--authorized-role") == ["merger"]


def test_no_flag_of_the_procedure_contributes_to_the_authority_set(world, capsys):
    code, payload, err = world.procedure(capsys, "--authorized-role=intruder")
    assert code == procedure_verb.EXIT_USAGE and payload == {}
    assert world.requests == []


@pytest.mark.parametrize(
    "case",
    ["group-writable", "roles-not-a-mapping", "roles-names-an-unknown-verb", "unreadable-yaml"],
)
def test_an_untrusted_or_unusable_role_mapping_is_a_configuration_error(world, capsys, case):
    path = world.cfg / "config.yaml"
    if case == "group-writable":
        os.chmod(path, 0o666)
    elif case == "roles-not-a-mapping":
        write_user_config(world.cfg, roles=["merge"])
    elif case == "roles-names-an-unknown-verb":
        write_user_config(world.cfg, roles={ROLE: ["not-a-verb"]})
    else:
        path.write_text("roles: [unclosed", encoding="utf-8")

    code, payload, err = world.procedure(capsys)

    assert code == procedure_verb.EXIT_CONFIG_INVALID and payload == {}
    assert str(path) in err
    assert world.requests == []


#: One row per condition the procedure notes and never refuses.
NOTE_ROWS = {
    "role-without-merge": (dict(role="bystander", caller="bystander"), "does not hold"),
    "pr-not-open": (dict(state="closed"), "not open (state: closed)"),
    "no-reviewer-declared": (dict(tracked_gate={"ci_strict": False}), "declares no reviewer role"),
    "gate-warning": (
        dict(tracked_gate={"required_reviewer_roles": "not-a-list"}),
        "NOT ENFORCED",
    ),
}


@pytest.mark.parametrize("row", NOTE_ROWS)
def test_a_condition_is_a_note_and_nothing_is_refused(tmp_path, monkeypatch, capsys, row):
    options, needle = NOTE_ROWS[row]
    build = {k: v for k, v in options.items() if k in ("tracked_gate", "state")}
    call = {k: v for k, v in options.items() if k in ("role", "caller")}
    world = build_world(tmp_path, monkeypatch, "forgejo", **build)

    code, payload, err = world.procedure(capsys, **call)

    assert code == 0, err
    assert any(needle in note for note in payload["notes"]), payload["notes"]
    assert "NOTE -- " in err and needle in err
    assert [item["step"] for item in payload["lines"]] == STEPS
    if row == "gate-warning":
        assert payload["required_reviewers"]["warnings"]


def _ruling(**overrides):
    entry = dict(RULINGS[0])
    entry.update(overrides)
    return entry


#: One row per unusable rulings file: (file text or entries, expected needle).
RULINGS_ROWS = {
    "unreadable": (None, "cannot be read"),
    "not-json": ("{not json", "cannot be read"),
    "not-an-array": ({"fence_comment": 1}, "JSON array"),
    "entry-not-an-object": ([RULINGS[0], "text"], "entry 1 must be an object"),
    "missing-key": ([RULINGS[0], {k: v for k, v in RULINGS[1].items() if k != "rationale"}],
                    "entry 1 is missing 'rationale'"),
    "fence-comment-not-an-integer": ([_ruling(fence_comment="7")], "entry 0 fence_comment"),
    "fence-comment-is-a-boolean": ([_ruling(fence_comment=True)], "entry 0 fence_comment"),
    "unknown-kind": ([RULINGS[0], _ruling(kind="whatever")], "entry 1 kind 'whatever'"),
    "unknown-key": ([_ruling(extra="x")], "entry 0 has unknown key"),
    "empty-value": ([_ruling(rationale=" ")], "entry 0 'rationale'"),
}


@pytest.mark.parametrize("row", RULINGS_ROWS)
def test_an_unusable_rulings_file_is_refused_naming_it_before_the_host_is_read(
    world, capsys, row
):
    content, needle = RULINGS_ROWS[row]
    path = world.root / "bad-rulings.json"
    if content is not None:
        path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    code, payload, err = world.procedure(capsys, f"--rulings={path}")

    assert code == REFUSED and payload == {} and needle in err and str(path) in err
    assert world.requests == []


#: One row per value that cannot be printed: (flags, expected needle).
UNUSABLE_ROWS = {
    "newline-in-role": (dict(role="two\nlines"), "--role"),
    "role-starts-with-dash": (dict(role="-x"), "'-x' starts with"),
    "repo-starts-with-dash": (dict(repo="-o/name"), "'-o/name' starts with"),
    "repo-path-starts-with-dash": ({"--repo-path": "-tree"}, "'-tree' starts with"),
    "base-url-starts-with-dash": ({"--git-host-base-url": "-http://h"}, "starts with"),
    "base-url-control-character": ({"--git-host-base-url": "http://h\x07"}, "--git-host-base-url"),
    "message-with-control-character": ({"--merge-message": "a\tb"}, "--merge-message"),
    "message-starts-with-dash": ({"--merge-message": "-m"}, "'-m' starts with"),
    "repo-path-with-newline": ({"--repo-path": "a\nb"}, "--repo-path"),
}


@pytest.mark.parametrize("row", UNUSABLE_ROWS)
def test_a_value_that_cannot_be_printed_is_refused_naming_it_before_the_host_is_read(
    world, capsys, row
):
    flags = UNUSABLE_ROWS[row][0]
    code, payload, err = world.procedure(capsys, **flags)

    assert code == procedure_verb.EXIT_USAGE and payload == {}
    assert UNUSABLE_ROWS[row][1] in err
    assert world.requests == []


@pytest.mark.parametrize(
    "key,bad",
    [("rationale", "multi\nline"), ("fingerprint", "-fp"), ("source_ref", "-ref"), ("reference", "a\x1bb")],
)
def test_a_rulings_value_that_cannot_be_printed_is_refused_before_the_host_is_read(
    world, capsys, key, bad
):
    entry = {**RULINGS[1], key: bad}
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file([entry])}")

    assert code == procedure_verb.EXIT_USAGE and payload == {}
    assert f"--rulings entry 0 {key}" in err
    assert world.requests == []


def test_a_caller_that_is_not_the_attested_identity_is_refused_before_any_io(world, capsys):
    code, payload, _ = world.procedure(capsys, identity="someone-else")
    assert code == procedure_verb.EXIT_CALLER_INVOKER_MISMATCH and payload == {}
    assert world.requests == []


def test_the_ruling_lines_follow_the_file_and_carry_the_required_roles_and_the_head(world, capsys):
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file()}")
    assert code == 0, err
    rulings = [shlex.split(i["line"]) for i in payload["lines"] if i["step"] == "ruling"]

    assert [t[:2] for t in rulings] == [["loadout-ruling", "record"]] * 2
    assert [value(t, "--fence-comment") for t in rulings] == ["7", "8"]
    assert [value(t, "--fingerprint") for t in rulings] == [FP, OTHER_FP]
    assert [value(t, "--kind") for t in rulings] == ["accepted_risk", "mitigated"]
    for t in rulings:
        assert values(t, "--required-reviewer") == list(REVIEWERS)
        assert value(t, "--head") == world.head_sha and value(t, "--repo-path") == str(world.tree)
        assert [value(t, f) for f in ("--role", "--platform", "--repo", "--pr")] == [
            ROLE, world.platform, REPO, "1",
        ]
    assert "--expires" not in rulings[0] and value(rulings[1], "--expires") == "2099-01-01"
    assert value(rulings[1], "--reference") == "ITEM-1"


def test_close_carries_delete_branch_only_when_the_procedure_was_given_it(world, capsys):
    _, plain, _ = world.procedure(capsys)
    _, deleting, _ = world.procedure(capsys, "--delete-branch")
    assert "--delete-branch" not in tokens_of(plain)["close"]
    assert tokens_of(deleting)["close"].count("--delete-branch") == 1
    assert tokens_of(deleting)["post_merge"] == tokens_of(plain)["post_merge"]


def test_the_base_url_is_carried_by_a_forgejo_procedure_only_and_the_head_read_follows_the_platform(
    world, capsys
):
    code, payload, err = world.procedure(capsys, f"--rulings={world.rulings_file()}")
    assert code == 0, err
    tokens = tokens_of(payload)
    carriers = {s for s, t in tokens.items() if "--git-host-base-url" in t}
    if world.platform == "forgejo":
        assert carriers == {"head", "merge_check", "merge", "post_merge", "close", "ruling"}
        assert {value(t, "--git-host-base-url") for s, t in tokens.items()} == {FORGEJO_BASE_URL}
        assert tokens["head"][-2:] == ["GET", f"/api/v1/repos/{REPO}/pulls/1"]
    else:
        assert carriers == set()
        assert tokens["head"][-2:] == ["GET", f"https://api.github.com/repos/{REPO}/pulls/1"]


def test_a_forgejo_procedure_without_a_base_url_prints_none(tmp_path, monkeypatch, capsys):
    world = build_world(tmp_path, monkeypatch, "forgejo")
    monkeypatch.setenv("CLAGENTIC_LOADOUT_GIT_HOST_BASE_URL", FORGEJO_BASE_URL)
    code, payload, err = world.procedure(capsys, **{"--git-host-base-url": None})
    assert code == 0, err
    assert not any("--git-host-base-url" in item["line"] for item in payload["lines"])


def test_out_writes_a_new_file_and_never_replaces_one(world, capsys):
    target = world.root / "procedure.json"
    code, payload, err = world.procedure(capsys, f"--out={target}")

    assert code == 0, err
    assert payload == {"result": "written", "path": str(target)}
    written = json.loads(target.read_text(encoding="utf-8"))
    assert written["result"] == "procedure" and written["head_sha"] == world.head_sha

    before = target.read_text(encoding="utf-8")
    seen = len(world.requests)
    code, payload, err = world.procedure(capsys, f"--out={target}")
    assert code == REFUSED and payload == {} and str(target) in err
    assert target.read_text(encoding="utf-8") == before and len(world.requests) == seen


@pytest.mark.parametrize(
    "case,expected",
    [
        ("head-not-a-sha", procedure_verb.EXIT_HOST_UNAVAILABLE),
        ("no-credential", procedure_verb.EXIT_TOKEN_FETCH_FAILED),
    ],
)
def test_a_host_or_credential_failure_prints_no_lines(world, capsys, case, expected):
    if case == "head-not-a-sha":
        inner = world.opener

        def opener(req, timeout=30):
            response = inner(req, timeout)
            if req.get_method() == "GET" and req.full_url.rstrip("/").endswith("/pulls/1"):
                body = json.loads(response.read())
                body["head"]["sha"] = "short"
                return json_resp(200, body)
            return response

        world.opener = opener
        code, payload, _ = world.procedure(capsys)
    else:
        from tests._support.merge_verb import MissingCredsTokenProvider

        capsys.readouterr()
        code = procedure_verb.main(
            world.argv(),
            token_provider=MissingCredsTokenProvider(),
            opener=world.opener,
            identity_provider=identity_provider(ROLE),
        )
        payload = {}
    assert code == expected and payload == {}
