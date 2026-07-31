"""test_transport_readback_envelope.py — unit coverage for
clagentic_loadout.transport.readback_envelope (lr-361de3).

Proves the ONE stable shape every remote-mutating verb's envelope carries:
`to_dict()` always renders `verified` (bool) and `source` (str), never
omitted/null -- the structural property a downstream consumer (an
integrator's own lr-f04775-tracked work) depends on for a single predicate
across every verb.
"""

from __future__ import annotations

from clagentic_loadout.transport.readback_envelope import (
    READBACK_ENVELOPE_KEY,
    Readback,
)


class TestReadbackToDict:
    def test_verified_true_shape(self):
        rb = Readback(verified=True, source="api_get", detail={"x": 1})
        d = rb.to_dict()
        assert d == {"verified": True, "source": "api_get", "detail": {"x": 1}}

    def test_verified_false_shape(self):
        rb = Readback(verified=False, source="verify_failed")
        d = rb.to_dict()
        assert d["verified"] is False
        assert d["source"] == "verify_failed"
        assert d["detail"] == {}

    def test_verified_and_source_always_present(self):
        """Structural guarantee: neither field is ever omitted, regardless
        of construction -- a consumer's predicate is always exactly
        `envelope[READBACK_ENVELOPE_KEY]["verified"] is True`."""
        for rb in (
            Readback(verified=True, source="api_get"),
            Readback(verified=False, source="read_unavailable"),
        ):
            d = rb.to_dict()
            assert "verified" in d
            assert "source" in d
            assert isinstance(d["verified"], bool)
            assert isinstance(d["source"], str)

    def test_envelope_key_is_the_stable_name(self):
        assert READBACK_ENVELOPE_KEY == "readback"

    def test_detail_is_a_copy_not_the_same_object(self):
        """Mutating the caller's own detail dict after construction must
        never retroactively change an already-rendered envelope."""
        detail = {"a": 1}
        rb = Readback(verified=True, source="api_get", detail=detail)
        detail["a"] = 999
        assert rb.to_dict()["detail"] == {"a": 1}
