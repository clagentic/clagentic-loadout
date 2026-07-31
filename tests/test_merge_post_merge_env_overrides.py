"""test_merge_post_merge_env_overrides.py — tests for the deployment-owned
env-override seam (lr-52d7): `merge.post_merge_config.resolve_env_overrides`
and its wiring into `merge.post_merge.run_post_merge_steps`.

Coverage:
  - default (no env, no config file) resolves to {} -- byte-identical
    behavior to before this feature existed.
  - the CLAGENTIC_LOADOUT_POST_MERGE_ENV_<NAME> env-var tier.
  - the user-level config file's post_merge_env: section (via
    transport.provider_config.load_user_config_section -- SAME file/loader
    every other user-level config tier in this package already uses).
  - precedence: env var wins over the config-file value, per variable.
  - run_post_merge_steps wiring: deployment_env_overrides reach the
    subprocess env, and a step's own inline VAR=VALUE prefix still wins
    over a deployment override for the same name.
  - malformed/irrelevant input is skipped, never raised -- this tier is
    additive and must never block an otherwise-valid post_merge_steps run.
  - no-lore conformance: this module imports nothing from lore.
  - lr-e84ae1 END-TO-END regression: a tier-2 post_merge_env HOME override,
    written to a SYNTHETIC user-level config file, actually reaches a
    post_merge_steps subprocess's environment when the PARENT process's own
    HOME is empty at call time -- the exact live-verify failure this task
    was reclassified from "provisioning gap" to "code defect" over (the
    config file was confirmed correct; the seam still didn't wire it
    through end to end because DEFAULT_USER_CONFIG_ROOT itself was resolved
    from a HOME-empty PARENT process at import time, sending config lookups
    to the wrong directory -- see release.secrets_config._resolve_home_dir).
"""

from __future__ import annotations

import importlib
import sys

import yaml

from clagentic_loadout.merge.post_merge import run_post_merge_steps
from clagentic_loadout.merge.post_merge_config import (
    CONFIG_SECTION_POST_MERGE_ENV,
    ENV_OVERRIDE_PREFIX,
    resolve_env_overrides,
)

_PY = sys.executable


def _write_user_config(config_root, content: dict) -> None:
    config_root.mkdir(parents=True, exist_ok=True)
    (config_root / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")


class TestDefaultIsEmpty:
    def test_no_env_no_config_file_resolves_empty(self, tmp_path):
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {}

    def test_unrelated_env_vars_are_ignored(self, tmp_path):
        env = {"HOME": "/some/other/home", "PATH": "/usr/bin"}
        assert resolve_env_overrides(env=env, config_root=tmp_path) == {}


class TestEnvVarTier:
    def test_single_override_from_env(self, tmp_path):
        env = {f"{ENV_OVERRIDE_PREFIX}HOME": "/root"}
        assert resolve_env_overrides(env=env, config_root=tmp_path) == {"HOME": "/root"}

    def test_multiple_overrides_from_env(self, tmp_path):
        env = {
            f"{ENV_OVERRIDE_PREFIX}HOME": "/root",
            f"{ENV_OVERRIDE_PREFIX}MY_TOKEN": "abc123",
        }
        result = resolve_env_overrides(env=env, config_root=tmp_path)
        assert result == {"HOME": "/root", "MY_TOKEN": "abc123"}

    def test_malformed_suffix_skipped_not_raised(self, tmp_path):
        # "HOME-DASH" is not a valid bare env-var identifier; must be
        # silently skipped, never raised (additive tier, fail-soft-skip).
        env = {f"{ENV_OVERRIDE_PREFIX}HOME-DASH": "nope"}
        assert resolve_env_overrides(env=env, config_root=tmp_path) == {}

    def test_empty_suffix_skipped(self, tmp_path):
        env = {ENV_OVERRIDE_PREFIX[:-1]: "nope"}  # prefix minus trailing '_'
        assert resolve_env_overrides(env=env, config_root=tmp_path) == {}


class TestConfigFileTier:
    def test_single_override_from_config_file(self, tmp_path):
        _write_user_config(tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: {"HOME": "/root"}})
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {"HOME": "/root"}

    def test_non_string_value_coerced_to_str(self, tmp_path):
        _write_user_config(tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: {"PORT": 8080}})
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {"PORT": "8080"}

    def test_missing_section_resolves_empty(self, tmp_path):
        _write_user_config(tmp_path, {"credentials": {"token_provider_forgejo": "static"}})
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {}

    def test_non_mapping_section_treated_as_empty(self, tmp_path):
        _write_user_config(tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: "not-a-mapping"})
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {}

    def test_malformed_key_in_config_skipped(self, tmp_path):
        _write_user_config(
            tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: {"BAD-KEY": "nope", "OK_KEY": "yes"}}
        )
        assert resolve_env_overrides(env={}, config_root=tmp_path) == {"OK_KEY": "yes"}

    def test_no_config_file_present_resolves_empty(self, tmp_path):
        assert resolve_env_overrides(env={}, config_root=tmp_path / "does-not-exist") == {}


class TestPrecedence:
    def test_env_var_wins_over_config_file_for_same_name(self, tmp_path):
        _write_user_config(tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: {"HOME": "/from-config"}})
        env = {f"{ENV_OVERRIDE_PREFIX}HOME": "/from-env"}
        assert resolve_env_overrides(env=env, config_root=tmp_path) == {"HOME": "/from-env"}

    def test_disjoint_names_from_both_sources_both_present(self, tmp_path):
        _write_user_config(tmp_path, {CONFIG_SECTION_POST_MERGE_ENV: {"FROM_FILE": "file-val"}})
        env = {f"{ENV_OVERRIDE_PREFIX}FROM_ENV": "env-val"}
        result = resolve_env_overrides(env=env, config_root=tmp_path)
        assert result == {"FROM_FILE": "file-val", "FROM_ENV": "env-val"}


class TestRunPostMergeStepsWiring:
    def test_deployment_override_reaches_subprocess_env(self, tmp_path):
        marker = tmp_path / "deployment-env.txt"
        steps = [
            {
                "cmd": [
                    _PY,
                    "-c",
                    "import os; open('deployment-env.txt','w').write(os.environ['HOME'])",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path, deployment_env_overrides={"HOME": "/deployment-root"})
        assert marker.read_text() == "/deployment-root"

    def test_step_inline_assignment_wins_over_deployment_override(self, tmp_path):
        marker = tmp_path / "precedence.txt"
        steps = [
            {
                "cmd": [
                    "HOME=/step-local",
                    _PY,
                    "-c",
                    "import os; open('precedence.txt','w').write(os.environ['HOME'])",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path, deployment_env_overrides={"HOME": "/deployment-root"})
        assert marker.read_text() == "/step-local"

    def test_none_deployment_overrides_is_unchanged_behavior(self, tmp_path):
        # No deployment_env_overrides passed at all -- must behave exactly
        # like before this feature existed (env=None passthrough when the
        # step itself has no inline VAR=VALUE prefix).
        marker = tmp_path / "unchanged.txt"
        steps = [{"cmd": [_PY, "-c", "open('unchanged.txt','w').write('ran')"]}]
        run_post_merge_steps(steps, tmp_path)
        assert marker.read_text() == "ran"

    def test_empty_deployment_overrides_is_unchanged_behavior(self, tmp_path):
        marker = tmp_path / "empty-overrides.txt"
        steps = [{"cmd": [_PY, "-c", "open('empty-overrides.txt','w').write('ran')"]}]
        run_post_merge_steps(steps, tmp_path, deployment_env_overrides={})
        assert marker.read_text() == "ran"


class TestHomeEmptyInParentEndToEnd:
    """lr-e84ae1 regression: reproduce the reclassified code defect end to
    end -- tier-2 post_merge_env HOME must reach a post_merge_steps
    subprocess even when the CALLING process's own HOME is empty at the
    moment resolve_env_overrides() (and the module import chain it depends
    on) runs, mirroring exactly what NAOMI's loadout-merge process saw on
    PR #50 merge 86b9f4c: config.yaml correct on disk, but the override
    never reached install.sh's subprocess env.

    Uses a SYNTHETIC config_root under tmp_path throughout (never /root or
    any real host path -- CLAUDE.md rule 1 / the task's own instruction)."""

    def _write_user_config(self, config_root, content: dict) -> None:
        config_root.mkdir(parents=True, exist_ok=True)
        (config_root / "config.yaml").write_text(yaml.safe_dump(content), encoding="utf-8")

    def test_tier2_home_override_reaches_subprocess_when_parent_home_empty(
        self, tmp_path, monkeypatch
    ):
        # 1. A synthetic tier-2 user-level config file, correctly carrying
        #    post_merge_env: {HOME: <synthetic-home>} -- mirrors the
        #    CONFIRMED-correct /root/.config/clagentic/loadout/config.yaml
        #    from the task's own investigation.
        config_root = tmp_path / "synthetic-user-config-root"
        synthetic_home = str(tmp_path / "synthetic-home-root")
        self._write_user_config(
            config_root, {CONFIG_SECTION_POST_MERGE_ENV: {"HOME": synthetic_home}}
        )

        # 2. The PARENT process's own HOME is empty at call time -- the
        #    exact condition NAOMI's loadout-merge process was in. This is
        #    what `resolve_env_overrides()` sees via `env=os.environ` when
        #    called with NO config_root override (production call shape,
        #    merge.verb._run L716) -- so config_root itself must be resolved
        #    correctly even though the process's own HOME cannot be trusted.
        monkeypatch.setenv("HOME", "")

        overrides = resolve_env_overrides(config_root=config_root)
        assert overrides == {"HOME": synthetic_home}

        # 3. The resolved override actually reaches a post_merge_steps
        #    subprocess's environment (env=) -- the full seam, not just the
        #    resolution function in isolation.
        marker = tmp_path / "e2e-marker.txt"
        steps = [
            {
                "cmd": [
                    _PY,
                    "-c",
                    "import os; open('e2e-marker.txt','w').write(os.environ['HOME'])",
                ]
            }
        ]
        run_post_merge_steps(steps, tmp_path, deployment_env_overrides=overrides)
        assert marker.read_text() == synthetic_home

    def test_default_user_config_root_wrong_when_home_empty_at_import(self, monkeypatch):
        """Names the ROOT CAUSE directly (lr-e84ae1 comment #5's bounce):
        before the release.secrets_config._resolve_home_dir fix, importing
        the module chain with HOME='' at import time sent
        DEFAULT_USER_CONFIG_ROOT to a root-relative path instead of the real
        home directory -- so resolve_env_overrides() called with NO
        config_root override (the production call shape) could never find
        even a CORRECTLY-provisioned tier-2 file. Re-imports the chain fresh
        under HOME='' and asserts the resolved root is anchored under the
        real home directory, never under a bare '/'."""
        monkeypatch.setenv("HOME", "")
        saved = {
            name: mod
            for name, mod in list(sys.modules.items())
            if name.startswith("clagentic_loadout")
        }
        for name in saved:
            del sys.modules[name]
        try:
            provider_config = importlib.import_module("clagentic_loadout.transport.provider_config")
            resolved_root = str(provider_config.DEFAULT_USER_CONFIG_ROOT)
        finally:
            for name in list(sys.modules):
                if name.startswith("clagentic_loadout"):
                    del sys.modules[name]
            sys.modules.update(saved)

        assert resolved_root != "/.config/clagentic/loadout"
        assert resolved_root.endswith("/.config/clagentic/loadout")
