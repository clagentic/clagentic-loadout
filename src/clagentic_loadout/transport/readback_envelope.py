"""transport.readback_envelope — the ONE stable readback shape every
remote-mutating verb's success envelope carries (lr-361de3).

WHY THIS EXISTS: before this module, three shapes coexisted for "did this
verb's remote-mutating call actually land, confirmed independently of the
mutating response itself":
  - push.remote_readback's RemoteReadback dataclass (push create path only).
  - transport.git_host_api.verify_comment_on_pr's bespoke id/url/login/body
    dict (the comment-post path).
  - NOTHING AT ALL on merge.verb (merge_pr) and merge.close_verb (close_pr) --
    both printed a bare {pr_number, owner, repo} envelope with no readback
    field whatsoever.

A downstream consumer (an integrator's own lr-f04775-tracked work, blocked on
this task landing) needs ONE predicate to test "did this verb's envelope
carry a confirmed remote fact" -- not a per-verb special case for each of
the shapes above. A
per-verb shape pushes that consumer back toward a hardcoded-roster anti-
pattern it has already rejected (see this task's own comment thread, seq 1).

THE SHAPE: every remote-mutating verb's JSON success envelope now carries a
`"readback"` key whose value is exactly what `Readback.to_dict()` produces:
  {"verified": bool, "source": str, "detail": <verb-specific dict, or {}>}

Three distinguishable states (task requirement, seq 2 item (d)):
  - verified=True:  the mutation was independently confirmed via a fresh
    read of platform/remote state, taken AFTER the mutating call succeeded.
  - verified=False, source names WHY: the verb attempted a readback and it
    did not confirm the expected state (a real verification failure), OR the
    verb genuinely could not attempt one (e.g. push.remote_readback's own
    ADDITIVE, non-fatal git-ls-remote failure path) -- `source` is never
    ambiguous between these two; see each call site's own `source` literal.
  - There is no THIRD, silent state: a verb either sets verified=True with a
    source naming the read mechanism, or verified=False with a source naming
    why not. A caller that only checks `readback.verified` can never
    mistake an honest "could not verify" for a confirmed fact -- the
    consumer's own stated requirement (task comment #1: "unverified is only
    safe if the crew side cannot silently treat it as success" -- this
    module makes that field ALWAYS present and ALWAYS boolean, never
    omitted, so a consumer's predicate is exactly `envelope["readback"]
    ["verified"] is True`, nothing more).

`detail` is where verb-specific fields live (e.g. the merge verb's
`merged_commit_sha`, the close verb's confirmed `state`, the push verb's
`remote_head_sha`) -- this module does not know or care what a given verb's
detail shape is; it only fixes the OUTER {verified, source, detail} contract
every verb agrees to render.

NOT A REPLACEMENT FOR push.remote_readback OR
transport.git_host_api.verify_comment_on_pr's OWN READ MECHANISMS: those
modules still do the actual work of reading the remote/platform back. This
module only provides the common ENVELOPE SHAPE those results (and the new
merge/close readbacks this task adds) are rendered into for a caller.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Envelope key every remote-mutating verb's JSON success output carries.
READBACK_ENVELOPE_KEY = "readback"

#: `source` value for a readback performed via a fresh GET issued AFTER the
#: mutating call, confirming the field(s) the mutation was supposed to
#: change (merge: merged==true + merge commit resolvable; close: state==
#: "closed"; comment-post: verify_comment_on_pr's own existing readback).
READBACK_SOURCE_API_GET = "api_get"

#: `source` value for push's existing `git ls-remote` readback
#: (push.remote_readback.REMOTE_READBACK_SOURCE_GIT_LS_REMOTE) -- kept as a
#: distinct, named literal here too so a consumer reading only this module
#: still recognizes it, without this module importing push.remote_readback
#: (no cross-layer coupling; the push verb itself does the string mapping).
READBACK_SOURCE_GIT_LS_REMOTE = "git_ls_remote"

#: `source` value when a readback was attempted and did NOT confirm the
#: expected state (a real verification failure, not an execution failure of
#: the read itself).
READBACK_SOURCE_VERIFY_FAILED = "verify_failed"

#: `source` value when the readback GET/read itself could not be performed
#: (network/transport failure, non-2xx on the read, malformed response) --
#: distinct from READBACK_SOURCE_VERIFY_FAILED: the mutation's own outcome is
#: unknown here, not merely unconfirmed.
READBACK_SOURCE_READ_UNAVAILABLE = "read_unavailable"


@dataclass(frozen=True)
class Readback:
    """The one stable {verified, source, detail} shape (see module
    docstring). `verified` and `source` are ALWAYS present in the rendered
    dict -- never omitted, never null -- so a consumer's predicate is always
    exactly `to_dict()["verified"] is True`."""

    verified: bool
    source: str
    detail: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Defensive copy at construction time (object.__setattr__ required --
        # this is a frozen dataclass): a caller's own detail dict, mutated
        # after constructing this Readback, must never retroactively change
        # an already-rendered envelope. Shallow is sufficient -- every
        # detail value in this package is a plain str/int/bool, never a
        # nested mutable structure a caller could still reach through.
        object.__setattr__(self, "detail", dict(self.detail))

    def to_dict(self) -> dict[str, Any]:
        return {"verified": self.verified, "source": self.source, "detail": dict(self.detail)}


__all__ = [
    "READBACK_ENVELOPE_KEY",
    "READBACK_SOURCE_API_GET",
    "READBACK_SOURCE_GIT_LS_REMOTE",
    "READBACK_SOURCE_READ_UNAVAILABLE",
    "READBACK_SOURCE_VERIFY_FAILED",
    "Readback",
]
