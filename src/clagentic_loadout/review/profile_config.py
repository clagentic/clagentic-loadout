"""review.profile_config — review profiles: which carrier command reviews a
chunk, which fallback runs when the carrier is absent, which rulebook text is
appended to every chunk, and the chunk/timeout/retry bounds.

Config surface (``review:`` section, ``profiles`` map keyed by profile name,
which is normally the reviewer role):

    review:
      profiles:
        reviewer:
          carrier: ["my-review-cli", "--read-only"]   # argv list; prompt on stdin
          fallback: ["my-other-cli", "-p"]            # optional; same contract
          carrier_model: some-model                   # optional label, shown in the verdict
          fallback_model: other-model                 # optional label, shown in the verdict
          rulebook: /etc/loadout/rulebook.md          # optional
          chunk_lines: 600
          timeout_seconds: 480
          fallback_timeout_seconds: 400
          max_attempts: 3
          parallel: 4
          escalation_round: 2                         # 0 turns the churn breaker off
          churn_count: pr                             # pr (default): count blocked rounds PR-wide; key: per (rule_id, file)
          defect_class: true                          # false: reviewers state no defect classes
          guidance_in_comment: true                   # false: no Class guidance section in the comment
          passes: 1                                   # integer >= 1: independent full-review passes

Two tiers, with a deliberate asymmetry. The USER-LEVEL file
(``~/.config/clagentic/loadout/config.yaml``) is the host default and owns
every key. The REPO-LEVEL file may override only the bounds (``chunk_lines``,
``timeout_seconds``, ``fallback_timeout_seconds``, ``max_attempts``,
``parallel``) and the ``rulebook`` path (which must stay inside the repo). A
repo-level ``carrier`` or ``fallback`` is REJECTED with a stderr warning and
never honored: those keys name a command this process will execute, and a
cloned repository must never choose what runs on the operator's machine (the
same direction the credential-provider config takes). ``escalation_round`` is
deployment-only as well, for a different reason: it switches the churn breaker
(review.churn) off, which a repository under review must not be able to do. A
repo-level value is ignored with a stderr warning. ``churn_count`` is
deployment-only for the same reason.

``defect_class`` and ``guidance_in_comment`` (both booleans, default true) are
deployment-only for the same reason: a repository under review must not choose
what its reviewer is asked to state or what the posted comment says. With
``defect_class`` false the reviewer is not asked for defect classes, none are
kept, and the fence version is unchanged, which is the old contract exactly.
With ``guidance_in_comment`` false the guidance is still in the result, and the
comment carries no Class guidance section.

``passes`` (an integer of 1 or more, default 1) is how many independent passes a
full review makes of the same diff. Each pass samples on its own and the passes'
findings are joined into one set, judged once, with a stability report beside
the verdict (review.pass_union). It is deployment-only: it multiplies the work
of every review, which a repository under review must not be able to do. With
the default the review is exactly the single pass it always was.

The carrier contract: the argv is executed directly (no shell), the chunk
prompt is written to its stdin, and the reply is read from its stdout. Exit
127 (or a missing executable) means the engine is absent. Loadout never names
or selects a model; that is entirely the configured argv's business. The
optional ``carrier_model`` and ``fallback_model`` are display labels only: they
are never passed to the command, and exist so a posted verdict can say which
model answered when a fallback took over.
"""

from __future__ import annotations

import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import yaml

from clagentic_loadout.numeric_validation import is_finite_number, is_finite_positive_number
from clagentic_loadout.repo_config import (
    DEFAULT_CONFIG_RELATIVE_PATH,
    LEGACY_CONFIG_RELATIVE_PATH,
    resolve_repo_config_path,
    resolve_repo_config_root,
)
from clagentic_loadout.review.chunking import DEFAULT_CHUNK_LINES
from clagentic_loadout.review.churn import CHURN_COUNTS, COUNT_PR
from clagentic_loadout.transport.provider_config import load_user_config_section

CONFIG_SECTION_REVIEW = "review"
CONFIG_KEY_PROFILES = "profiles"
CONFIG_KEY_RUN_ROOT = "run_root"

DEFAULT_TIMEOUT_SECONDS = 480.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_PARALLEL = 4
#: The blocked round at which the churn breaker briefs the builder; 0 is off.
DEFAULT_ESCALATION_ROUND = 2
MAX_ESCALATION_ROUND = 10
CONFIG_KEY_ESCALATION_ROUND = "escalation_round"
CONFIG_KEY_CHURN_COUNT = "churn_count"
#: How the churn breaker counts rounds: ``pr`` counts every blocked head the role
#: posted on the pull request, ``key`` counts consecutive rounds per (rule, file).
DEFAULT_CHURN_COUNT = COUNT_PR

_REPO_OVERRIDABLE_KEYS = (
    "chunk_lines",
    "timeout_seconds",
    "fallback_timeout_seconds",
    "max_attempts",
    "parallel",
    "rulebook",
)
_REPO_REFUSED_KEYS = ("carrier", "fallback", "carrier_model", "fallback_model")
CONFIG_KEY_DEFECT_CLASS = "defect_class"
CONFIG_KEY_GUIDANCE_IN_COMMENT = "guidance_in_comment"
CONFIG_KEY_PASSES = "passes"
DEFAULT_PASSES = 1
_REPO_DEPLOYMENT_ONLY_KEYS = (
    CONFIG_KEY_ESCALATION_ROUND,
    CONFIG_KEY_CHURN_COUNT,
    CONFIG_KEY_DEFECT_CLASS,
    CONFIG_KEY_GUIDANCE_IN_COMMENT,
    CONFIG_KEY_PASSES,
)
_MAX_LABEL_CHARS = 100


#: A cloned repository must not be able to make this process spawn an
#: unbounded number of carrier processes or wait unboundedly on one. Repo-level
#: values are clamped to these bounds; the user-level config is not.
_REPO_MAX = {
    "parallel": 16,
    "max_attempts": 10,
    "timeout_seconds": 3600,
    "fallback_timeout_seconds": 3600,
}
_REPO_MIN = {"chunk_lines": 50}


class ReviewProfileError(ValueError):
    """The requested review profile is missing or malformed."""


def _bound_repo_value(key: str, value: object, profile: str) -> object:
    """Clamp a numeric repo-level override into its bound, saying so on
    stderr. Non-numeric values pass through to the normal validation."""
    if not is_finite_number(value):
        return value
    upper = _REPO_MAX.get(key)
    lower = _REPO_MIN.get(key)
    if upper is not None and value > upper:
        bounded: int | float = upper
    elif lower is not None and value < lower:
        bounded = lower
    else:
        return value
    print(
        f"clagentic-loadout: review.profiles.{profile}.{key}={value!r} in the repo-level "
        f"config is outside the allowed repo-level bound; using {bounded!r}.",
        file=sys.stderr,
    )
    return bounded


@dataclass(frozen=True)
class ReviewProfile:
    name: str
    carrier: tuple[str, ...]
    fallback: tuple[str, ...] | None
    rulebook_text: str
    chunk_lines: int
    timeout_seconds: float
    fallback_timeout_seconds: float
    max_attempts: int
    parallel: int
    carrier_model: str | None = None
    fallback_model: str | None = None
    escalation_round: int = DEFAULT_ESCALATION_ROUND
    churn_count: str = DEFAULT_CHURN_COUNT
    defect_class: bool = True
    guidance_in_comment: bool = True
    passes: int = DEFAULT_PASSES


def _passes(value: object, profile: str) -> int:
    """*value* as the pass count: an integer of 1 or more. A bool is not an
    integer here, since YAML `yes` must not read as one pass."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ReviewProfileError(
            f"review profile {profile!r}: {CONFIG_KEY_PASSES!r} must be an integer of 1 or "
            f"more, got {value!r}"
        )
    return value


def _escalation_round(value: object, profile: str) -> int:
    """*value* as the escalation round: an integer from 0 to MAX_ESCALATION_ROUND.
    A bool is not an integer here, since YAML `yes` must not read as round 1."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= MAX_ESCALATION_ROUND
    ):
        raise ReviewProfileError(
            f"review profile {profile!r}: {CONFIG_KEY_ESCALATION_ROUND!r} must be an integer "
            f"from 0 to {MAX_ESCALATION_ROUND} (0 turns the churn breaker off), got {value!r}"
        )
    return value


def load_escalation_round(name: str, *, config_root: str | Path | None = None) -> int:
    """The churn breaker's escalation round for profile *name*, from the
    user-level config only. A deployment with no such profile, or one that does
    not state the key, gets DEFAULT_ESCALATION_ROUND. Raises ReviewProfileError
    for a value that is not an integer from 0 to MAX_ESCALATION_ROUND."""
    entry = _profile_entry(
        load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root), name
    )
    return _escalation_round(
        entry.get(CONFIG_KEY_ESCALATION_ROUND, DEFAULT_ESCALATION_ROUND), name
    )


def _churn_count(value: object, profile: str) -> str:
    """*value* as the churn counting mode: one of CHURN_COUNTS."""
    if not isinstance(value, str) or value not in CHURN_COUNTS:
        raise ReviewProfileError(
            f"review profile {profile!r}: {CONFIG_KEY_CHURN_COUNT!r} must be one of "
            f"{', '.join(CHURN_COUNTS)}, got {value!r}"
        )
    return value


def load_churn_count(name: str, *, config_root: str | Path | None = None) -> str:
    """How the churn breaker counts rounds for profile *name*, from the
    user-level config only. A deployment with no such profile, or one that does
    not state the key, gets DEFAULT_CHURN_COUNT. Raises ReviewProfileError for a
    value that is not one of CHURN_COUNTS."""
    entry = _profile_entry(
        load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root), name
    )
    return _churn_count(entry.get(CONFIG_KEY_CHURN_COUNT, DEFAULT_CHURN_COUNT), name)


def _flag(value: object, key: str, profile: str) -> bool:
    """*value* as a boolean setting. Only a real boolean counts: a string such
    as "false" is truthy in Python and would silently switch the wrong way."""
    if not isinstance(value, bool):
        raise ReviewProfileError(
            f"review profile {profile!r}: {key!r} must be true or false, got {value!r}"
        )
    return value


@dataclass(frozen=True)
class ClassSettings:
    """The two deployment switches of the defect-class feature."""

    defect_class: bool = True
    guidance_in_comment: bool = True


def load_class_settings(name: str, *, config_root: str | Path | None = None) -> ClassSettings:
    """The class switches for profile *name*, from the user-level config only.
    A deployment with no such profile, or one that does not state a key, gets
    the default (true). Raises ReviewProfileError for a value that is not a
    boolean."""
    entry = _profile_entry(
        load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root), name
    )
    return ClassSettings(
        defect_class=_flag(entry.get(CONFIG_KEY_DEFECT_CLASS, True), CONFIG_KEY_DEFECT_CLASS, name),
        guidance_in_comment=_flag(
            entry.get(CONFIG_KEY_GUIDANCE_IN_COMMENT, True), CONFIG_KEY_GUIDANCE_IN_COMMENT, name
        ),
    )


def _label(value: object, key: str, profile: str) -> str | None:
    """An optional one-line display label, or None when the key is unset."""
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _MAX_LABEL_CHARS
        or "\n" in value
        or "\r" in value
    ):
        raise ReviewProfileError(
            f"review profile {profile!r}: {key!r} must be a one-line string of at most "
            f"{_MAX_LABEL_CHARS} characters, got {value!r}"
        )
    return value.strip()


def _argv(value: object, key: str, profile: str) -> tuple[str, ...]:
    if isinstance(value, str):
        try:
            parts = shlex.split(value)
        except ValueError as exc:
            raise ReviewProfileError(
                f"review profile {profile!r}: {key!r} is not a valid shell-quoted "
                f"string ({exc}): {value!r}"
            ) from exc
    elif isinstance(value, list) and all(isinstance(item, str) for item in value):
        parts = list(value)
    else:
        raise ReviewProfileError(
            f"review profile {profile!r}: {key!r} must be an argv list of strings "
            f"(or one shell-quoted string), got {value!r}"
        )
    if not parts or not parts[0].strip():
        raise ReviewProfileError(f"review profile {profile!r}: {key!r} is empty")
    return tuple(parts)


def _positive_number(value: object, key: str, profile: str, *, integer: bool) -> float:
    # Finiteness is checked before any conversion: int(inf) raises OverflowError.
    ok = is_finite_positive_number(value)
    if integer:
        ok = ok and float(value) == int(value)
    if not ok:
        kind = "positive integer" if integer else "positive number"
        raise ReviewProfileError(
            f"review profile {profile!r}: {key!r} must be a {kind}, got {value!r}"
        )
    return float(value)


def _read_yaml_mapping(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        # Treated as absent (a repo's broken file must not block a review), but
        # said out loud so an ignored override is never a mystery.
        print(
            f"clagentic-loadout: ignoring unreadable repo-level config {str(path)!r}: {exc}",
            file=sys.stderr,
        )
        return {}
    return raw if isinstance(raw, dict) else {}


def _profile_entry(section: dict, name: str) -> dict:
    profiles = section.get(CONFIG_KEY_PROFILES)
    if not isinstance(profiles, dict):
        return {}
    entry = profiles.get(name)
    return entry if isinstance(entry, dict) else {}


def load_run_root_override(*, config_root: str | Path | None = None) -> Path | None:
    """The user-level ``review.run_root`` setting, or None when unset."""
    section = load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root)
    value = section.get(CONFIG_KEY_RUN_ROOT)
    if isinstance(value, str) and value.strip():
        return Path(value).expanduser()
    return None


def _read_rulebook(path: Path, profile: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReviewProfileError(
            f"review profile {profile!r}: cannot read rulebook {str(path)!r}: {exc}"
        ) from exc


def load_review_profile(
    name: str,
    *,
    config_root: str | Path | None = None,
    repo_root: str | Path | None = None,
) -> ReviewProfile:
    """Resolve profile *name* from the user-level config, then apply the
    permitted repo-level overrides (see module docstring)."""
    user_entry = _profile_entry(
        load_user_config_section(CONFIG_SECTION_REVIEW, config_root=config_root), name
    )
    if not user_entry:
        raise ReviewProfileError(
            f"no review profile {name!r}: add review.profiles.{name} (with a "
            f"'carrier' argv list) to the user-level loadout config"
        )

    repo_entry: dict = {}
    repo_base: Path | None = None
    if repo_root is not None:
        config_path = resolve_repo_config_path(repo_root, warn=False)
        repo_base = resolve_repo_config_root(
            repo_root, DEFAULT_CONFIG_RELATIVE_PATH, LEGACY_CONFIG_RELATIVE_PATH
        )
        repo_section = _read_yaml_mapping(config_path).get(CONFIG_SECTION_REVIEW)
        if isinstance(repo_section, dict):
            repo_entry = _profile_entry(repo_section, name)
        for refused in _REPO_REFUSED_KEYS:
            if refused in repo_entry:
                print(
                    f"clagentic-loadout: ignoring review.profiles.{name}.{refused} in the "
                    f"repo-level config: it names a command to execute and is honored "
                    f"from the user-level config only.",
                    file=sys.stderr,
                )
        for deployment_only in _REPO_DEPLOYMENT_ONLY_KEYS:
            if deployment_only in repo_entry:
                print(
                    f"clagentic-loadout: ignoring review.profiles.{name}.{deployment_only} in "
                    f"the repo-level config: it is honored from the user-level config only.",
                    file=sys.stderr,
                )

    merged = dict(user_entry)
    # The rulebook's SOURCE decides how its path is read, not whether a repo
    # root happened to resolve: a repo-sourced path is never a host path.
    rulebook_from_repo = False
    for key in _REPO_OVERRIDABLE_KEYS:
        if key not in repo_entry:
            continue
        if key == "rulebook":
            if repo_entry[key] is None:
                print(
                    f"clagentic-loadout: ignoring review.profiles.{name}.rulebook=null in the "
                    f"repo-level config: it cannot remove the user-level rulebook.",
                    file=sys.stderr,
                )
                continue
            rulebook_from_repo = True
        merged[key] = _bound_repo_value(key, repo_entry[key], name)

    if "carrier" not in merged:
        raise ReviewProfileError(f"review profile {name!r}: 'carrier' is required")
    carrier = _argv(merged["carrier"], "carrier", name)
    fallback = _argv(merged["fallback"], "fallback", name) if "fallback" in merged else None

    rulebook_text = ""
    rulebook = merged.get("rulebook")
    if rulebook is not None:
        if not isinstance(rulebook, str) or not rulebook.strip():
            raise ReviewProfileError(f"review profile {name!r}: 'rulebook' must be a path string")
        if rulebook_from_repo:
            if repo_base is None:
                raise ReviewProfileError(
                    f"review profile {name!r}: repo-level rulebook {rulebook!r} cannot be "
                    f"read because no repository root was resolved to confine it to"
                )
            resolved = (repo_base / rulebook).resolve()
            if not resolved.is_relative_to(repo_base.resolve()):
                raise ReviewProfileError(
                    f"review profile {name!r}: repo-level rulebook {rulebook!r} "
                    f"resolves outside the repository"
                )
            rulebook_text = _read_rulebook(resolved, name)
        else:
            rulebook_text = _read_rulebook(Path(rulebook).expanduser(), name)

    timeout = _positive_number(
        merged.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS), "timeout_seconds", name, integer=False
    )
    return ReviewProfile(
        name=name,
        carrier=carrier,
        fallback=fallback,
        rulebook_text=rulebook_text,
        chunk_lines=int(
            _positive_number(
                merged.get("chunk_lines", DEFAULT_CHUNK_LINES), "chunk_lines", name, integer=True
            )
        ),
        timeout_seconds=timeout,
        fallback_timeout_seconds=_positive_number(
            merged.get("fallback_timeout_seconds", timeout),
            "fallback_timeout_seconds",
            name,
            integer=False,
        ),
        max_attempts=int(
            _positive_number(
                merged.get("max_attempts", DEFAULT_MAX_ATTEMPTS), "max_attempts", name, integer=True
            )
        ),
        parallel=int(
            _positive_number(merged.get("parallel", DEFAULT_PARALLEL), "parallel", name, integer=True)
        ),
        carrier_model=_label(merged.get("carrier_model"), "carrier_model", name),
        fallback_model=_label(merged.get("fallback_model"), "fallback_model", name),
        escalation_round=_escalation_round(
            merged.get(CONFIG_KEY_ESCALATION_ROUND, DEFAULT_ESCALATION_ROUND), name
        ),
        churn_count=_churn_count(
            merged.get(CONFIG_KEY_CHURN_COUNT, DEFAULT_CHURN_COUNT), name
        ),
        defect_class=_flag(
            merged.get(CONFIG_KEY_DEFECT_CLASS, True), CONFIG_KEY_DEFECT_CLASS, name
        ),
        guidance_in_comment=_flag(
            merged.get(CONFIG_KEY_GUIDANCE_IN_COMMENT, True), CONFIG_KEY_GUIDANCE_IN_COMMENT, name
        ),
        passes=_passes(merged.get(CONFIG_KEY_PASSES, DEFAULT_PASSES), name),
    )
