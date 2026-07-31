"""provisioning — the agent-provisioning contract (lr-4e04).

Closes the gap between "loadout installs verbs to PATH" and "a consuming
agent can actually invoke them without hitting a permission-prompt wall":

  - ``roles``: a ROLE -> verb-set declaration (config surface, never keyed
    on agent names — CLAUDE.md rule 1).
  - ``allowlist``: generates a PER-ROLE permission-allowlist fragment from
    that declaration (never a single global fragment — rejected shape, see
    each module's docstring).
  - ``cli``: the ``loadout-provision-allowlist`` console-script entry point.

See docs/provisioning.md for the end-to-end integrator workflow.
"""

from __future__ import annotations
