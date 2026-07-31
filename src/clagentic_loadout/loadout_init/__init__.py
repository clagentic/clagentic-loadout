"""loadout_init — the `/loadout-init` guided per-repo initialization onto
loadout-native config.

Scaffolds a NEW repo's `.clagentic/loadout/config.yaml` from a clean,
committed starter template (`starter_config.yaml`) rather than hand-writing
one from scratch or copying a live deployment's own config (this package's
own `.clagentic/loadout/config.yaml` is deliberately NOT the copy source —
see that file's own header comment: it carries THIS deployment's real
`HOME=`/`--git-host-base-url` values inline, which must never propagate into
a template shipped to another repo, rule 1).

  - ``starter_template``: pure, testable functions that locate the packaged
    starter template and copy it to a target repo's resolved config path
    (`repo_config.DEFAULT_CONFIG_RELATIVE_PATH`). No elicitation, no
    prompting, no interactive I/O anywhere in this package -- that half of
    the `/loadout-init` workflow lives entirely in the LLM-invocable skill
    prose (`.claude/skills/loadout-init/SKILL.md`), which drives an operator
    conversation and then edits the copied file directly. This module only
    owns the deterministic, scriptable "get a clean copy onto disk" step and
    the schema doctor.checks.check_repo_loadout_schema already validates
    against.
"""

from __future__ import annotations
