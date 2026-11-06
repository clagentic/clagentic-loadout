"""The reviewer's defect_class and class_fix on findings: parsed and bounded,
carried through run and post into the fence (version 8), summarised as guidance
in the post result and the comment, quoted by the churn brief, and switched off
by the deployment keys with the old contract left byte for byte."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clagentic_loadout.merge import fence_state
from clagentic_loadout.merge.errors import VerdictMalformedError
from clagentic_loadout.merge.fence_state import state_from_fence
from clagentic_loadout.merge.guidance_section import REPEATED_LINE, validate_guidance
from clagentic_loadout.merge.verdict import build_findings_verdict_body, parse_verdict_block
from clagentic_loadout.review import churn, churn_brief, churn_ledger, class_guidance, profile_config
from clagentic_loadout.review import cli as review_cli
from clagentic_loadout.review.findings_contract import (
    OUTPUT_CONTRACT,
    OUTPUT_CONTRACT_WITHOUT_CLASSES,
    InvalidReplyError,
    parse_chunk_reply,
)
from clagentic_loadout.transport import provider_config
from tests._review_cli_support import HEAD_SHA, Env, prompts
from tests._review_evaluate_support import facts
from tests._support.churn_fences import replace_fence, verdict_comment

SERIOUS = facts(confidentiality="full", integrity="full", availability="full")
CLASS = "unchecked path join"
FIX = "one path-join helper that refuses escapes"
HEAD_A, HEAD_B = "a" * 40, "d" * 40


@pytest.fixture
def env(tmp_path, monkeypatch) -> Env:
    environment = Env(tmp_path)
    monkeypatch.setenv("STUB_DIR", str(environment.stubs))
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    monkeypatch.delenv(review_cli.RUN_ROOT_ENV_VAR, raising=False)
    monkeypatch.setattr(provider_config, "DEFAULT_USER_CONFIG_ROOT", tmp_path / "user-config")
    return environment


def _finding(line=3, rule="R1", **extra):
    return {
        "file": "a.py", "line": line, "rule_id": rule, "message": f"{rule} at {line}",
        "facts": SERIOUS, **extra,
    }


def _post(env, tmp_path, capsys, findings):
    path = tmp_path / "findings.json"
    path.write_text(json.dumps(findings), encoding="utf-8")
    return env.post(
        "--findings", str(path), "--head-sha", HEAD_SHA, "--base-sha", env.base_sha,
        "--status", "blocking", capsys=capsys,
    )


def _fence(env):
    return parse_verdict_block(env.opener_state["posted_body"])


def _body(env) -> str:
    return env.opener_state["posted_body"]


def _two_of_a_class(**extra):
    return [
        _finding(3, defect_class=CLASS, class_fix=FIX, **extra),
        _finding(9, "R2", defect_class=CLASS, **extra),
    ]


# --- parsing ---------------------------------------------------------------


def test_a_reply_with_class_keys_keeps_them_and_leaves_the_rest_unchanged():
    plain = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "facts": SERIOUS}]
    classed = [{**plain[0], "defect_class": CLASS, "class_fix": FIX}]

    [with_class] = parse_chunk_reply(json.dumps(classed), carry_class=True)
    [without] = parse_chunk_reply(json.dumps(plain), carry_class=True)

    assert (with_class["defect_class"], with_class["class_fix"]) == (CLASS, FIX)
    assert {k: v for k, v in with_class.items() if k not in ("defect_class", "class_fix")} == without
    assert "defect_class" not in without and "severity" not in with_class


def test_a_long_class_with_control_characters_is_bounded_and_clean_and_the_finding_is_kept():
    dirty = "x\x00y\x1b[31m" + "z" * 2000 + "‮`"
    reply = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "defect_class": dirty,
              "class_fix": "fix\nmore"}]

    [parsed] = parse_chunk_reply(json.dumps(reply), carry_class=True)

    assert len(parsed["defect_class"]) <= 200
    assert all(ch.isprintable() for ch in parsed["defect_class"])
    assert "`" not in parsed["defect_class"]
    assert parsed["class_fix"] == "fix more"
    assert parsed["file"] == "a.py"


@pytest.mark.parametrize("bad", [None, 7, ["a"], {"a": 1}, "", "  \x00 ", True])
def test_a_missing_or_malformed_class_is_null_and_costs_only_the_key(bad):
    reply = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "defect_class": bad}]

    [parsed] = parse_chunk_reply(json.dumps(reply), carry_class=True)

    assert "defect_class" not in parsed and parsed["message"] == "m"


def test_class_keys_are_dropped_unless_asked_for():
    reply = [{"file": "a.py", "line": 1, "rule_id": "R1", "message": "m", "defect_class": CLASS}]

    assert "defect_class" not in parse_chunk_reply(json.dumps(reply))[0]


def test_a_reply_that_is_not_findings_is_still_invalid():
    with pytest.raises(InvalidReplyError):
        parse_chunk_reply("nothing to say", carry_class=True)


# --- prompt and the deployment keys -----------------------------------------


def test_the_default_contract_names_the_keys_and_the_off_contract_does_not():
    assert '"defect_class"' in OUTPUT_CONTRACT and '"class_fix"' in OUTPUT_CONTRACT
    assert "isolated is a complete answer" in OUTPUT_CONTRACT
    assert "never change a" in OUTPUT_CONTRACT
    assert "defect_class" not in OUTPUT_CONTRACT_WITHOUT_CLASSES
    assert OUTPUT_CONTRACT.replace(class_guidance.CLASS_FIELDS_CONTRACT, "") == (
        OUTPUT_CONTRACT_WITHOUT_CLASSES
    )


def test_run_keeps_class_keys_and_sends_the_class_prompt(env, capsys):
    env.configure(carrier_mode="facts_serious_class")

    code, payload = env.run(capsys=capsys)

    assert code == 0
    finding = json.loads(Path(payload["findings_file"]).read_text())["findings"][0]
    assert (finding["defect_class"], finding["class_fix"]) == (CLASS, FIX)
    # Severity is computed from facts alone: the class keys take no part.
    assert finding["blocking"] is True and finding["computed_severity"] != "none"
    assert prompts(env.stubs, "carrier")[-1].rstrip().endswith(OUTPUT_CONTRACT.rstrip())


def test_defect_class_false_sends_the_old_prompt_and_keeps_no_keys(env, capsys):
    env.configure(carrier_mode="facts_serious_class", defect_class=False)

    code, payload = env.run(capsys=capsys)

    assert code == 0
    sent = prompts(env.stubs, "carrier")[0]
    assert "defect_class" not in sent and "class_fix" not in sent
    assert sent.endswith(OUTPUT_CONTRACT_WITHOUT_CLASSES)
    finding = json.loads(Path(payload["findings_file"]).read_text())["findings"][0]
    assert "defect_class" not in finding and "class_fix" not in finding


@pytest.mark.parametrize("key", ["defect_class", "guidance_in_comment"])
@pytest.mark.parametrize("bad", ["false", "no", 0, None, 1])
def test_a_deployment_key_that_is_not_a_boolean_is_refused_by_name(tmp_path, key, bad):
    root = tmp_path / "cfg"
    root.mkdir()
    (root / "config.yaml").write_text(
        f"review:\n  profiles:\n    reviewer:\n      carrier: [c]\n      {key}: {json.dumps(bad)}\n"
    )

    with pytest.raises(profile_config.ReviewProfileError, match=key):
        profile_config.load_class_settings("reviewer", config_root=root)


def test_both_keys_default_to_true_without_a_profile_or_a_key(tmp_path):
    assert profile_config.load_class_settings("reviewer", config_root=tmp_path) == (
        profile_config.ClassSettings(True, True)
    )


def test_a_repo_level_class_key_is_ignored_with_a_warning(env, capsys):
    env.configure()
    repo_config = env.repo / ".clagentic" / "loadout" / "config.yaml"
    repo_config.parent.mkdir(parents=True, exist_ok=True)
    repo_config.write_text(
        "review:\n  profiles:\n    reviewer:\n      defect_class: false\n"
        "      guidance_in_comment: false\n"
    )

    profile = profile_config.load_review_profile("reviewer", config_root=env.cfg, repo_root=env.repo)

    assert profile.defect_class is True and profile.guidance_in_comment is True
    err = capsys.readouterr().err
    assert "defect_class" in err and "guidance_in_comment" in err


# --- post: fence, guidance, comment -----------------------------------------


def test_post_carries_the_class_keys_into_the_fence_at_version_8(env, tmp_path, capsys):
    code, payload = _post(env, tmp_path, capsys, _two_of_a_class())

    assert code == 0
    fence = _fence(env)
    assert fence["fence_schema_version"] == fence_state.CLASS_FENCE_SCHEMA_VERSION == 8
    first, second = fence["evaluated_findings"]
    assert (first["defect_class"], first["class_fix"]) == (CLASS, FIX)
    assert second["defect_class"] == CLASS and "class_fix" not in second
    assert payload["blocking_count"] == 2


def test_a_finding_without_a_class_leaves_the_fence_at_its_old_version(env, tmp_path, capsys):
    code, payload = _post(env, tmp_path, capsys, [_finding()])

    assert code == 0
    assert _fence(env)["fence_schema_version"] == 3
    assert payload["guidance"]["classes"] == []
    assert "Class guidance" not in _body(env)


def test_class_keys_do_not_change_the_computed_verdict(env, tmp_path, capsys):
    _, plain = _post(env, tmp_path, capsys, [_finding(), _finding(9, "R2")])
    plain_fence = _fence(env)
    _, classed = _post(env, tmp_path, capsys, _two_of_a_class())

    for key in ("status", "verdict_outcome", "blocking_count", "finding_count", "open_finding_ids"):
        assert classed[key] == plain[key]
    fence = _fence(env)
    assert fence["verdict_outcome"] == plain_fence["verdict_outcome"]
    assert fence["findings_open"] == plain_fence["findings_open"]


def test_two_findings_of_one_class_make_the_guidance_repeated(env, tmp_path, capsys):
    code, payload = _post(env, tmp_path, capsys, _two_of_a_class())

    assert code == 0
    guidance = payload["guidance"]
    assert guidance["class_repeated"] is True
    assert REPEATED_LINE in guidance["text"] and "Stop patching" in guidance["text"]
    assert guidance["classes"] == [
        {"defect_class": CLASS, "class_fix": FIX, "findings": ["a.py:3:R1", "a.py:9:R2"]}
    ]
    assert guidance["more"] == 0


def test_one_finding_per_class_is_not_repeated(env, tmp_path, capsys):
    findings = [_finding(3, defect_class="alpha"), _finding(9, "R2", defect_class="beta")]

    _, payload = _post(env, tmp_path, capsys, findings)

    assert payload["guidance"]["class_repeated"] is False
    assert "Stop patching" not in payload["guidance"]["text"]
    assert payload["guidance"]["text"]


def test_guidance_is_capped_at_five_classes_with_an_explicit_remainder(env, tmp_path, capsys):
    findings = [_finding(10 + i, f"R{i}", defect_class=f"class {i}") for i in range(7)]

    _, payload = _post(env, tmp_path, capsys, findings)

    assert len(payload["guidance"]["classes"]) == 5
    assert payload["guidance"]["more"] == 2
    assert "- and 2 more" in _body(env)


def test_the_comment_has_a_class_guidance_section_after_the_findings(env, tmp_path, capsys):
    _post(env, tmp_path, capsys, _two_of_a_class())

    prose = _body(env).split("```review-result")[0]
    assert prose.index("Blocking findings") < prose.index("Class guidance")
    assert f"- {CLASS} (fix: {FIX}): a.py:3:R1, a.py:9:R2" in prose
    assert "reviewer's own statements" in prose


def test_guidance_in_comment_false_keeps_the_object_and_drops_the_section(env, tmp_path, capsys):
    env.configure(guidance_in_comment=False)

    code, payload = _post(env, tmp_path, capsys, _two_of_a_class())

    assert code == 0
    assert payload["guidance"]["classes"]
    assert "Class guidance" not in _body(env)
    assert _fence(env)["fence_schema_version"] == 8


def test_defect_class_false_posts_the_old_contract(env, tmp_path, capsys):
    env.configure(defect_class=False)

    code, payload = _post(env, tmp_path, capsys, _two_of_a_class())

    assert code == 0
    assert "guidance" not in payload
    fence = _fence(env)
    assert fence["fence_schema_version"] == 3
    assert all("defect_class" not in f and "class_fix" not in f for f in fence["evaluated_findings"])
    assert "Class guidance" not in _body(env) and CLASS not in _body(env)


def test_a_class_written_with_fence_syntax_is_neutralised_not_fatal(env, tmp_path, capsys):
    code, _ = _post(env, tmp_path, capsys, [_finding(defect_class="a ```review-result b")])

    assert code == 0
    assert "'''review-result" in _fence(env)["evaluated_findings"][0]["defect_class"]


# --- the guidance primitives -------------------------------------------------


def test_the_comment_builder_refuses_a_guidance_object_that_is_not_one_line_text():
    for bad in (
        {"classes": [{"defect_class": "a\nb", "class_fix": None, "findings": []}]},
        {"classes": [{"defect_class": "a ```review-result", "class_fix": None, "findings": []}]},
        {"classes": "x"},
    ):
        with pytest.raises(ValueError):
            validate_guidance(bad)


def test_the_comment_section_wording_is_fixed_whatever_the_object_says():
    guidance = {"classes": [{"defect_class": "c", "class_fix": None, "findings": ["a:1:R"]}],
                "more": 0, "class_repeated": False, "text": "free prose that must not render"}
    state = {
        "verdict_outcome": "BLOCKED", "verdict_reasons": [], "evaluator_version": "2",
        "evaluated_findings": [],
    }

    body = build_findings_verdict_body(
        "reviewer", "blocking", HEAD_SHA, 42,
        [{"file": "a.py", "line": 1, "rule_id": "R", "message": "m", "blocking": True}],
        None, state, guidance,
    )

    assert "free prose that must not render" not in body and "Class guidance" in body


def test_a_praise_note_and_a_classless_finding_are_not_guidance():
    got = class_guidance.build_guidance(
        [
            {"file": "a", "line": 1, "rule_id": "R", "severity": "praise", "defect_class": "x"},
            {"file": "a", "line": 2, "rule_id": "R"},
        ]
    )

    assert got["classes"] == [] and got["text"] == "" and got["class_repeated"] is False


# --- readers: fence 7 and 8 --------------------------------------------------


def _fence_dict(version, **extra):
    return {
        "reviewer": "reviewer", "review_status": "clean", "head_sha": HEAD_A, "pr_number": 1,
        "fence_schema_version": version, **extra,
    }


@pytest.mark.parametrize("version", [7, 8])
def test_the_merge_reader_accepts_fence_versions_7_and_8_without_guidance(version):
    body = "```review-result\n" + json.dumps(_fence_dict(version)) + "\n```\n"

    data = parse_verdict_block(body)

    assert data is not None and state_from_fence(data).fence_schema_version == version
    assert version in fence_state.FENCE_SCHEMA_VERSIONS
    assert fence_state.raw_mentions_findings_state(body)


def test_a_fence_of_version_9_is_still_refused():
    body = "```review-result\n" + json.dumps(_fence_dict(9)) + "\n```\n"

    with pytest.raises(VerdictMalformedError):
        parse_verdict_block(body)


@pytest.mark.parametrize("version", [7, 8])
def test_the_churn_ledger_reads_fence_versions_7_and_8(version):
    comment = replace_fence(
        verdict_comment("github", comment_id=11, login="bot", head=HEAD_A, pr_number=5,
                        blocks_on=(("R1", "a.py", 3),)),
        fence_schema_version=version,
    )

    [round_] = churn_ledger.rounds_from_comments([comment], login="bot", pr_number=5)

    assert round_.head_sha == HEAD_A and round_.keys == frozenset({("R1", "a.py")})


# --- churn brief step 5 --------------------------------------------------------


def _classed_round(head, comment_id, **cls):
    base = verdict_comment("github", comment_id=comment_id, login="bot", head=head, pr_number=5,
                           blocks_on=(("R1", "a.py", 3),))
    return replace_fence(
        base,
        fence_schema_version=8,
        evaluated_findings=[
            {"file": "a.py", "line": 3, "rule_id": "R1", "message": "R1 is wrong", **cls}
        ],
    )


def test_the_ledger_reads_a_keys_class_from_the_fence():
    comments = [
        _classed_round(HEAD_A, 11, defect_class=CLASS, class_fix=FIX),
        _classed_round(HEAD_B, 12, defect_class=CLASS, class_fix=FIX),
    ]

    rounds = churn_ledger.rounds_from_comments(comments, login="bot", pr_number=5)
    assessment = churn.assess(rounds, 2)
    brief = churn_brief.render(assessment.level, assessment.deciding, None)

    assert assessment.deciding.defect_class == CLASS
    step5 = brief.split("5. ")[1].split("6. ")[0]
    assert f"defect_class: {CLASS}" in step5 and f"class_fix: {FIX}" in step5
    assert "reviewer's own statements" in step5


def test_a_brief_without_a_class_is_the_brief_it_always_was():
    key = churn.KeyChurn("R1", "a.py", 2, (HEAD_A, HEAD_B))

    text = churn_brief.render(churn.LEVEL_BRIEF, key, None)

    assert "defect_class" not in text and "class_fix" not in text


def test_the_brief_quotes_a_class_without_a_fix():
    key = churn.KeyChurn("R1", "a.py", 2, (HEAD_A, HEAD_B), CLASS, None)

    text = churn_brief.render(churn.LEVEL_BRIEF, key, None)

    assert f"defect_class: {CLASS}" in text and "class_fix" not in text


def test_the_post_path_quotes_the_class_of_the_deciding_key():
    from clagentic_loadout.review.churn_verb import churn_after_post

    key = ("R1", "a.py")
    prior = [_classed_round(HEAD_A, 11, defect_class=CLASS, class_fix=FIX)]

    class Listing:
        def fetch_issue_comments(self, *, owner, repo, pr_number):
            return prior

    from clagentic_loadout.acquire.contract import CommentBackend

    backend = Listing()
    assert isinstance(backend, CommentBackend)
    report = churn_after_post(
        backend, owner="o", repo="r", pr_number=5, login="bot", head_sha=HEAD_B,
        blocking=[key], escalation_round=2, spec=None, classes={key: (CLASS, FIX)},
    )

    assert report["level"] == "brief"
    assert report["keys"][0]["defect_class"] == CLASS and report["keys"][0]["class_fix"] == FIX
    assert f"defect_class: {CLASS}" in report["brief"]


def test_a_key_without_a_class_has_no_class_fields_in_the_churn_object():
    from clagentic_loadout.review import churn_report

    rounds = [churn.Round(HEAD_A, frozenset({("R1", "a.py")})), churn.Round(HEAD_B, frozenset({("R1", "a.py")}))]

    report = churn_report.build(rounds, 2, backend=object(), owner="o", repo="r", spec=None)

    assert "defect_class" not in report["keys"][0]
