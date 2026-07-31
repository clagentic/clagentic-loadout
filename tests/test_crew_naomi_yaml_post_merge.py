"""test_crew_naomi_yaml_post_merge.py — conformance coverage for the repo's
own .crew/naomi.yaml post_merge_steps entry (lr-1ec9).

.crew/naomi.yaml is OPERATOR-SEEDED deployment config, not product source
(CLAUDE.md rule 1 / the project's own "crew vocabulary in PRODUCT SOURCE"
carve-out) -- but a malformed post_merge_steps entry there is exactly the
kind of drift that reintroduces the lr-52d7 exit-10 recurrence silently (a
step that never actually runs, or is rejected by an earlier internal merge
tool at merge time with no local way to catch it first). This test
validates the entry using loadout's OWN
`merge.post_merge.validate_post_merge_steps` -- which enforces the
identical step schema (cmd/description/on_failure) and the identical
shell-operator-rejection rule that earlier tool's own post-merge runner
enforces (see that module's docstring: the schemas were kept in sync
deliberately at port time, lr-77d6) -- so a config-shape regression here is
caught locally, in CI, without needing a live NAOMI merge to discover it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from clagentic_loadout.merge.post_merge import validate_post_merge_steps

REPO_ROOT = Path(__file__).resolve().parent.parent
NAOMI_CONFIG_PATH = REPO_ROOT / ".crew" / "naomi.yaml"


def _load_naomi_config() -> dict:
    return yaml.safe_load(NAOMI_CONFIG_PATH.read_text(encoding="utf-8"))


class TestNaomiConfigParses:
    def test_file_exists_and_parses_as_mapping(self):
        config = _load_naomi_config()
        assert isinstance(config, dict)

    def test_schema_version_present(self):
        config = _load_naomi_config()
        assert config.get("schema_version") == 1


class TestPostMergeStepsPresent:
    def test_post_merge_steps_key_present(self):
        config = _load_naomi_config()
        assert "post_merge_steps" in config
        assert isinstance(config["post_merge_steps"], list)
        assert len(config["post_merge_steps"]) >= 1

    def test_install_step_present(self):
        """lr-1ec9 (retire-updated lr-afba): the self-install step must be
        present so a NAOMI-executed merge (loadout-merge, the live NAOMI
        merge path on this repo's flow) actually installs the merged build
        -- without this, the merged build is never live (lr-52d7)."""
        config = _load_naomi_config()
        install_steps = [
            step for step in config["post_merge_steps"]
            if "install.sh" in str(step.get("cmd", ""))
        ]
        assert len(install_steps) == 1, (
            "expected exactly one post_merge_steps entry invoking install.sh"
        )

    def test_install_step_on_failure_is_fail(self):
        """A silent install failure is how the exit-10 gap recurs invisibly
        -- on_failure must be 'fail', never the 'warn' default."""
        config = _load_naomi_config()
        install_step = next(
            step for step in config["post_merge_steps"]
            if "install.sh" in str(step.get("cmd", ""))
        )
        assert install_step.get("on_failure") == "fail"

    def test_install_step_cmd_is_repo_relative(self):
        """cmd must not be an absolute path -- an earlier internal merge
        tool documents that an absolute-path cmd in a project's naomi.yaml
        is a configuration bug (pre_checks docstring, same convention
        applies to
        post_merge_steps): repo-relative cmds are what makes the file
        portable across a clone/redeploy with no machine-specific edit."""
        config = _load_naomi_config()
        install_step = next(
            step for step in config["post_merge_steps"]
            if "install.sh" in str(step.get("cmd", ""))
        )
        cmd = install_step["cmd"]
        cmd_str = cmd if isinstance(cmd, str) else " ".join(cmd)
        assert not cmd_str.strip().startswith("/"), (
            f"post_merge_steps install cmd must be repo-relative, got: {cmd_str!r}"
        )


class TestPostMergeStepsWellFormed:
    """Validated against loadout's OWN post_merge_steps schema/parser
    (merge.post_merge.validate_post_merge_steps) -- ported at lr-77d6 to be
    schema-compatible with an earlier internal merge tool's own reader,
    including the shell-operator-rejection rule. A step that fails this
    validation would also be rejected (or worse, silently misparsed
    pre-lr-77d6) by that tool's live post-merge runner."""

    def test_post_merge_steps_pass_loadout_validation(self):
        config = _load_naomi_config()
        # Must not raise PostMergeConfigError.
        validate_post_merge_steps(config["post_merge_steps"])

    def test_no_shell_operator_tokens_in_any_step_cmd(self):
        config = _load_naomi_config()
        shell_operators = {"&&", "||", "|", ";", ">", ">>", "<"}
        for step in config["post_merge_steps"]:
            cmd = step["cmd"]
            tokens = cmd.split() if isinstance(cmd, str) else cmd
            found = shell_operators.intersection(tokens)
            assert not found, f"shell operator token(s) {found} found in step cmd {cmd!r}"
