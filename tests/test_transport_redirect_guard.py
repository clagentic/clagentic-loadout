"""test_transport_redirect_guard.py — tests for
clagentic_loadout.transport.redirect_guard (lr-412f pre-merge security
review finding).

This is the ONE no-redirect urllib opener every bearer-token-carrying call
in the transport layer builds through (extracted after the same
redirect-token-leak class recurred a fourth time, most recently in
review.github_backend's bespoke urlopen call). Coverage:
  - NoRedirectHandler.redirect_request always returns None (refuses to
    follow any 3xx code, not just one).
  - no_redirect_opener() builds a fresh urllib opener carrying
    NoRedirectHandler, and does so lazily (a new instance per call, never a
    shared module-level opener).
"""

from __future__ import annotations

import urllib.request

from clagentic_loadout.transport.redirect_guard import (
    NoRedirectHandler,
    no_redirect_opener,
)


class TestNoRedirectHandler:
    def test_redirect_request_always_returns_none(self):
        handler = NoRedirectHandler()
        for code in (301, 302, 303, 307, 308):
            result = handler.redirect_request(
                req=None, fp=None, code=code, msg="redirect",
                headers={}, newurl="http://attacker.example.net/collect",
            )
            assert result is None


class TestNoRedirectOpener:
    def test_builds_opener_carrying_the_handler(self):
        opener = no_redirect_opener()
        assert any(
            isinstance(h, NoRedirectHandler) for h in opener.handlers
        )

    def test_each_call_builds_a_fresh_opener_instance(self):
        first = no_redirect_opener()
        second = no_redirect_opener()
        assert first is not second

    def test_returns_a_real_urllib_opener_director(self):
        opener = no_redirect_opener()
        assert isinstance(opener, urllib.request.OpenerDirector)
