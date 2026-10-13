"""merge.pre_merge_canary -- an operator-supplied command the merge gate runs
before it authorizes a merge.

The command is deployment configuration, never repo content: it is read from
the USER-LEVEL loadout config file (`<config_root>/config.yaml`, the same file
and `merge:` section `merge.foreign_config_globs` lives in), so a pull request
cannot add, drop or edit it. loadout ships no default command and no target
list; with no `pre_merge_canary` key the gate behaves exactly as it did before
this module existed and prints nothing extra.

    merge:
      pre_merge_canary:
        cmd: ["/usr/local/bin/my-canary", "--strict"]   # argv list, never a shell string
        timeout_seconds: 60                              # optional, default 60

CONTRACT
  - The command runs with `shell=False`, stdin closed, in the merge process's
    own working directory, with the scrubbed environment `merge.pre_check_env`
    builds for pre_checks (identity, attestation and credential-shaped
    variables withheld) plus four variables describing the merge:
    `CLAGENTIC_LOADOUT_CANARY_REPO` (`owner/repo`), `..._PR` (number),
    `..._HEAD_SHA` and `..._PLATFORM` (`forgejo` | `github`).
  - Exit 0 proceeds. Anything else refuses: a non-zero exit, a timeout, a
    command that cannot be found, one that is not executable. The refusal names
    the configured command and carries the tail of its stdout and stderr.
  - A config file that mentions the key but cannot be read as the shape above
    refuses too: a declared gate that cannot be shown to have been read is
    never treated as absent.

WHY NOT `merge.post_merge.run_post_merge_steps`: that executor is the pre_checks
runner and fits repo-declared steps run inside a merge-result clone. This command
is operator-owned and has no tree to run in, and the refusal here must carry the
exit and output tail in its own message; widening the shared executor's
messages would change what every pre_checks and post_merge_steps caller prints.
"""

from __future__ import annotations

import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.merge.pre_check_env import pre_check_env
from clagentic_loadout.numeric_validation import is_finite_positive_number
from clagentic_loadout.transport import provider_config

#: Key within the user-level `merge:` section naming the canary mapping.
CONFIG_KEY_PRE_MERGE_CANARY = "pre_merge_canary"

#: Seconds the command may run when the mapping sets no `timeout_seconds`. A
#: gate that can hang forever blocks every merge, so the bound is not optional.
DEFAULT_TIMEOUT_SECONDS = 60

#: Characters kept from the end of each output stream in a refusal.
OUTPUT_TAIL_CHARS = 400

ENV_REPO = "CLAGENTIC_LOADOUT_CANARY_REPO"
ENV_PR = "CLAGENTIC_LOADOUT_CANARY_PR"
ENV_HEAD_SHA = "CLAGENTIC_LOADOUT_CANARY_HEAD_SHA"
ENV_PLATFORM = "CLAGENTIC_LOADOUT_CANARY_PLATFORM"

_ALLOWED_KEYS = frozenset({"cmd", "timeout_seconds"})


class PreMergeCanaryConfigError(ValueError):
    """The canary declaration is present but cannot be used."""


class PreMergeCanaryRefusedError(RuntimeError):
    """The canary ran (or could not run) and the merge must be refused. The
    message names the configured command and the way it failed."""


@dataclass(frozen=True)
class PreMergeCanary:
    argv: tuple[str, ...]
    timeout_seconds: float


def _read_user_config(config_path: Path) -> dict | None:
    """The parsed user-level config, or None when the file does not mention the
    canary key at all (including when it does not exist)."""
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise PreMergeCanaryConfigError(f"{config_path}: cannot be read: {exc}") from exc
    if CONFIG_KEY_PRE_MERGE_CANARY not in text:
        return None
    try:
        parsed = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise PreMergeCanaryConfigError(f"{config_path}: is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PreMergeCanaryConfigError(f"{config_path}: top-level document must be a mapping")
    return parsed


def load_pre_merge_canary(config_root: str | Path | None = None) -> PreMergeCanary | None:
    """The configured canary, or None when none is configured.

    Raises:
        PreMergeCanaryConfigError: the key is present and the file or the
            mapping cannot be used.
    """
    root = Path(config_root) if config_root is not None else provider_config.DEFAULT_USER_CONFIG_ROOT
    config_path = root / provider_config.USER_CONFIG_FILENAME
    parsed = _read_user_config(config_path)
    if parsed is None:
        return None
    section = parsed.get("merge")
    if not isinstance(section, dict) or CONFIG_KEY_PRE_MERGE_CANARY not in section:
        return None
    declared = section[CONFIG_KEY_PRE_MERGE_CANARY]
    where = f"{config_path}: merge.{CONFIG_KEY_PRE_MERGE_CANARY}"
    if not isinstance(declared, dict):
        raise PreMergeCanaryConfigError(f"{where} must be a mapping, got {declared!r}")
    unknown = sorted(set(declared) - _ALLOWED_KEYS, key=str)
    if unknown:
        raise PreMergeCanaryConfigError(
            f"{where} has unknown key(s) {unknown!r}; allowed: {sorted(_ALLOWED_KEYS)!r}"
        )
    cmd = declared.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(t, str) and t for t in cmd):
        raise PreMergeCanaryConfigError(
            f"{where}.cmd must be a non-empty list of non-empty strings (an argv, "
            f"never a shell string), got {cmd!r}"
        )
    timeout = declared.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not is_finite_positive_number(timeout)
    ):
        raise PreMergeCanaryConfigError(
            f"{where}.timeout_seconds must be a finite number > 0, got {timeout!r}"
        )
    return PreMergeCanary(argv=tuple(cmd), timeout_seconds=timeout)


def _tail(data: bytes | str | None) -> str:
    if not data:
        return ""
    text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    return text.strip()[-OUTPUT_TAIL_CHARS:]


def _output_note(stdout: bytes | str | None, stderr: bytes | str | None) -> str:
    return f"; stdout tail: {_tail(stdout)!r}; stderr tail: {_tail(stderr)!r}"


def run_pre_merge_canary(
    canary: PreMergeCanary,
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    platform: str,
    base_env: Mapping[str, str] | None = None,
) -> None:
    """Run the canary for one merge; return on exit 0.

    *base_env* is the environment before the scrub (default `os.environ`).

    Raises:
        PreMergeCanaryRefusedError: non-zero exit, timeout, or a command that
            could not be started.
    """
    env = pre_check_env(base_env)
    env.update(
        {
            ENV_REPO: repo,
            ENV_PR: str(pr_number),
            ENV_HEAD_SHA: head_sha,
            ENV_PLATFORM: platform,
        }
    )
    command = list(canary.argv)
    try:
        result = subprocess.run(
            command,
            shell=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=canary.timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise PreMergeCanaryRefusedError(
            f"configured command {command!r} timed out after {canary.timeout_seconds}s"
            + _output_note(exc.stdout, exc.stderr)
        ) from exc
    except OSError as exc:
        raise PreMergeCanaryRefusedError(
            f"configured command {command!r} could not be started: {exc}"
        ) from exc
    if result.returncode != 0:
        raise PreMergeCanaryRefusedError(
            f"configured command {command!r} exited {result.returncode}"
            + _output_note(result.stdout, result.stderr)
        )


__all__ = [
    "CONFIG_KEY_PRE_MERGE_CANARY",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_HEAD_SHA",
    "ENV_PLATFORM",
    "ENV_PR",
    "ENV_REPO",
    "OUTPUT_TAIL_CHARS",
    "PreMergeCanary",
    "PreMergeCanaryConfigError",
    "PreMergeCanaryRefusedError",
    "load_pre_merge_canary",
    "run_pre_merge_canary",
]
