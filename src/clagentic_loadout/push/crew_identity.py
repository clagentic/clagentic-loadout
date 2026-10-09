"""push.crew_identity — DEPRECATED alias of `push.agent_identity`.

The module was renamed because "crew" is one deployment's name for its
agents, not product vocabulary. This shim re-exports the renamed symbols
under their old names (the exception class is the SAME object, so an
existing `except` clause keeps matching) for one release and is deleted
in the following release. Import `clagentic_loadout.push.agent_identity`
instead.
"""

from __future__ import annotations

import warnings

from clagentic_loadout.push.agent_identity import (
    AgentBotIdentityNotResolvableError as CrewBotIdentityNotResolvableError,
)
from clagentic_loadout.push.agent_identity import (
    is_recognized_agent_caller as is_recognized_crew_caller,
)
from clagentic_loadout.push.agent_identity import (
    resolve_agent_bot_identity as resolve_crew_bot_identity,
)

warnings.warn(
    "clagentic_loadout.push.crew_identity is deprecated and will be removed "
    "in the next release; import clagentic_loadout.push.agent_identity "
    "(is_recognized_agent_caller, resolve_agent_bot_identity, "
    "AgentBotIdentityNotResolvableError) instead.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "CrewBotIdentityNotResolvableError",
    "is_recognized_crew_caller",
    "resolve_crew_bot_identity",
]
