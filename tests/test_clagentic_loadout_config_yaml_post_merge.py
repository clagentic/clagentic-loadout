"""test_clagentic_loadout_config_yaml_post_merge.py — conformance coverage
for this repo's own `.clagentic/loadout/config.yaml` post_merge_steps entry
(lr-e84ae1, path migrated from `.loadout/config.yaml` per lr-446c35).

lr-e84ae1: the general deployment env-override seam
(merge.post_merge_config.resolve_env_overrides, lr-52d7 PR #51) can't close
THIS repo's own bootstrap gap, because loadout-merge's post_merge_steps
subprocess runs the CURRENTLY-INSTALLED binary, not the just-merged source —
the seam only takes effect once itself is installed, and installing is
exactly the step that's failing (chicken-and-egg). The fix mirrors the
identical inline-prefix fix already applied on an earlier internal merge
tool's own path (.crew/naomi.yaml, lr-e8cc/lr-52d7): supply HOME (and
--git-host-base-url)
inline via the leading VAR=VALUE prefix that
merge.post_merge._split_env_assignments strips into the child env
(shell=False always, so no shell operator is introduced). This test uses
loadout's OWN `merge.post_merge_config.load_post_merge_steps` /
`merge.post_merge.validate_post_merge_steps` to catch a config-shape or
missing-prefix regression locally, without needing a live loadout-merge run
to discover it (same rationale as test_crew_naomi_yaml_post_merge.py).
"""

from __future__ import annotations

from pathlib import Path

from clagentic_loadout.merge.post_merge import validate_post_merge_steps
from clagentic_loadout.merge.post_merge_config import load_post_merge_steps

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_repo_post_merge_steps() -> list[dict]:
    return load_post_merge_steps(REPO_ROOT)


class TestPostMergeStepsPresent:
    def test_post_merge_steps_present(self):
        steps = _load_repo_post_merge_steps()
        assert isinstance(steps, list)
        assert len(steps) >= 1

    def test_install_step_present(self):
        """lr-3812: the self-install step must be present so a
        loadout-merge-executed merge on this repo actually installs the
        merged build -- without this, the merged build is never live
        (lr-52d7)."""
        steps = _load_repo_post_merge_steps()
        install_steps = [
            step for step in steps if "install.sh" in str(step.get("cmd", ""))
        ]
        assert len(install_steps) == 1, (
            "expected exactly one post_merge_steps entry invoking install.sh"
        )

    def test_install_step_on_failure_is_fail(self):
        """A silent install failure is how the exit-10 gap recurs invisibly
        -- on_failure must be 'fail', never the 'warn' default."""
        steps = _load_repo_post_merge_steps()
        install_step = next(
            step for step in steps if "install.sh" in str(step.get("cmd", ""))
        )
        assert install_step.get("on_failure") == "fail"


class TestInlineHomePrefix:
    """lr-e84ae1: the install step's cmd must carry an inline HOME=
    env-assignment prefix so the step self-bootstraps regardless of which
    binary happens to be installed when a given merge runs -- the general
    resolve_env_overrides seam (lr-52d7) cannot help THIS repo's own
    install step, because it only takes effect once itself is installed."""

    def _install_step_cmd(self) -> str:
        steps = _load_repo_post_merge_steps()
        install_step = next(
            step for step in steps if "install.sh" in str(step.get("cmd", ""))
        )
        cmd = install_step["cmd"]
        return cmd if isinstance(cmd, str) else " ".join(cmd)

    def test_cmd_carries_home_assignment_prefix(self):
        cmd = self._install_step_cmd()
        assert cmd.startswith("HOME="), (
            f"expected install step cmd to carry a leading HOME=VALUE "
            f"env-assignment prefix (lr-e84ae1 self-bootstrap fix), got: {cmd!r}"
        )

    def test_parsed_step_yields_home_in_env_assignments(self):
        """Resolve the step exactly the way merge.post_merge.run_post_merge_steps
        does (_resolve_argv -> _split_env_assignments) and assert HOME lands
        in the stripped env-assignment dict, not the argv."""
        steps = _load_repo_post_merge_steps()
        install_step = next(
            step for step in steps if "install.sh" in str(step.get("cmd", ""))
        )
        # Import lazily to reuse the exact private parser the runner uses,
        # matching this repo's own naomi.yaml-equivalent test's approach of
        # validating against loadout's OWN schema/parser.
        from clagentic_loadout.merge.post_merge import _resolve_argv

        argv, env_assignments = _resolve_argv(
            install_step["cmd"], step_label="post_merge_steps[install]"
        )
        assert env_assignments.get("HOME") == "/root"
        assert "scripts/install.sh" in argv
        assert not any(tok.startswith("HOME=") for tok in argv), (
            "HOME=VALUE must be stripped into the env-assignment dict, "
            "never left as a literal argv token"
        )


class TestPostMergeStepsWellFormed:
    """Validated against loadout's OWN post_merge_steps schema/parser
    (merge.post_merge.validate_post_merge_steps) -- the same parser
    run_post_merge_steps itself uses, so a config-shape regression here is
    caught locally rather than only surfacing on a live merge."""

    def test_post_merge_steps_pass_loadout_validation(self):
        steps = _load_repo_post_merge_steps()
        # Must not raise PostMergeConfigError.
        validate_post_merge_steps(steps)

    def test_no_shell_operator_tokens_in_any_step_cmd(self):
        steps = _load_repo_post_merge_steps()
        shell_operators = {"&&", "||", "|", ";", ">", ">>", "<"}
        for step in steps:
            cmd = step["cmd"]
            tokens = cmd.split() if isinstance(cmd, str) else cmd
            found = shell_operators.intersection(tokens)
            assert not found, f"shell operator token(s) {found} found in step cmd {cmd!r}"
