"""role_vocabulary -- the role words loadout knows, and the class each belongs to.

A role is what an actor does; a class groups the roles whose behaviour loadout
treats alike. A feature keys on the class where behaviour is shared and on the
role where it is not, and never on the name of an agent: agent names live in a
deployment's own registry and appear nowhere in this package.

Classes: ``coordinate``, ``scope``, ``build``, ``gate``, ``inform``, ``host``.

The default vocabulary below is data. A deployment overrides or extends it in
the ``role_classes:`` section of the user-level config file, a mapping of role
word to class; an entry there wins over the default for that word and a word
the default does not know becomes a role of its own::

    role_classes:
      some-role: build

Legacy words stay accepted exactly as before; ``LEGACY_ALIASES`` maps each to the
role it is now called. An alias resolves to its role's class, and a deployment
entry for the legacy word itself wins over the alias.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.transport.provider_config import load_user_config_section

CLASS_COORDINATE = "coordinate"
CLASS_SCOPE = "scope"
CLASS_BUILD = "build"
CLASS_GATE = "gate"
CLASS_INFORM = "inform"
CLASS_HOST = "host"
CLASSES = (CLASS_COORDINATE, CLASS_SCOPE, CLASS_BUILD, CLASS_GATE, CLASS_INFORM, CLASS_HOST)

#: The final role vocabulary and each role's class.
ROLE_CLASSES: dict[str, str] = {
    "coordinator": CLASS_COORDINATE,
    "project-orchestrator": CLASS_SCOPE,
    "planner": CLASS_SCOPE,
    "builder": CLASS_BUILD,
    "reviewer": CLASS_GATE,
    "security-reviewer": CLASS_GATE,
    "release-manager": CLASS_GATE,
    "troubleshooter": CLASS_INFORM,
    "observer": CLASS_INFORM,
    "researcher": CLASS_INFORM,
    "sysadmin": CLASS_HOST,
}

#: Words loadout already accepts for a role, and the role each is now called.
#: ``analysis`` covers both the troubleshooter and the observer, which share one
#: checker; it maps to the first. Not a rename: every word here keeps working.
LEGACY_ALIASES: dict[str, str] = {
    "security": "security-reviewer",
    "merger": "release-manager",
    "lead": "project-orchestrator",
    "analysis": "troubleshooter",
    "research": "researcher",
    "planning_reader": "planner",
    "infra": "sysadmin",
}

CONFIG_SECTION_ROLE_CLASSES = "role_classes"

SOURCE_DEFAULT = "default"
SOURCE_ALIAS = "alias"
SOURCE_DEPLOYMENT = "deployment"

_WORD_RE = re.compile(r"\A[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z")


class RoleVocabularyError(ValueError):
    """The ``role_classes`` section is not a mapping of role word to class, or a
    word asked for is in no vocabulary. The message names the value and the
    known set."""


@dataclass(frozen=True)
class RoleResolution:
    """A role word resolved: the role it names, its class, and where that came
    from (``default``, ``alias`` or ``deployment``)."""

    word: str
    role: str
    role_class: str
    source: str


def parse_role_classes(section: object, *, source: str) -> dict[str, str]:
    """Validate a ``role_classes`` value (role word -> class)."""
    if section is None:
        return {}
    if not isinstance(section, Mapping):
        raise RoleVocabularyError(
            f"{source}: {CONFIG_SECTION_ROLE_CLASSES!r} must be a mapping of role word to class, "
            f"got {type(section).__name__}"
        )
    out: dict[str, str] = {}
    for word, value in section.items():
        if not isinstance(word, str) or not _WORD_RE.match(word):
            raise RoleVocabularyError(f"{source}: role word {word!r} is not a bare token")
        if value not in CLASSES:
            raise RoleVocabularyError(
                f"{source}: role {word!r} has class {value!r}; known classes: {', '.join(CLASSES)}"
            )
        out[word] = value
    return out


def load_role_classes(config_root: str | Path | None = None) -> dict[str, str]:
    """The deployment's ``role_classes`` overrides from the user-level config
    file; empty when the file or the section is absent."""
    # load_user_config_section degrades a missing section to {}, so only the
    # shape of a present one can be wrong.
    section = load_user_config_section(CONFIG_SECTION_ROLE_CLASSES, config_root=config_root)
    return parse_role_classes(section or None, source=CONFIG_SECTION_ROLE_CLASSES)


def resolve_role(word: str, overrides: Mapping[str, str] | None = None) -> RoleResolution:
    """Resolve *word* to its role and class. A deployment entry wins, then the
    default vocabulary, then the legacy alias table. Raises RoleVocabularyError
    for a word in none of them."""
    deployment = overrides or {}
    if word in deployment:
        return RoleResolution(word, word, deployment[word], SOURCE_DEPLOYMENT)
    if word in ROLE_CLASSES:
        return RoleResolution(word, word, ROLE_CLASSES[word], SOURCE_DEFAULT)
    if word in LEGACY_ALIASES:
        role = LEGACY_ALIASES[word]
        return RoleResolution(word, role, deployment.get(role, ROLE_CLASSES[role]), SOURCE_ALIAS)
    known = sorted({*ROLE_CLASSES, *LEGACY_ALIASES, *deployment})
    raise RoleVocabularyError(f"role {word!r} is not known; known roles: {', '.join(known)}")


def roles_in_class(role_class: str, overrides: Mapping[str, str] | None = None) -> list[str]:
    """The role words of *role_class* under the default vocabulary and
    *overrides*, in sorted order."""
    merged = {**ROLE_CLASSES, **(overrides or {})}
    return sorted(word for word, value in merged.items() if value == role_class)


__all__ = [
    "CLASSES",
    "CLASS_BUILD",
    "CLASS_COORDINATE",
    "CLASS_GATE",
    "CLASS_HOST",
    "CLASS_INFORM",
    "CLASS_SCOPE",
    "CONFIG_SECTION_ROLE_CLASSES",
    "LEGACY_ALIASES",
    "ROLE_CLASSES",
    "RoleResolution",
    "RoleVocabularyError",
    "load_role_classes",
    "parse_role_classes",
    "resolve_role",
    "roles_in_class",
]
