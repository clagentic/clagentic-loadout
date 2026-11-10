"""Tests for the shared push redaction primitive: case-insensitive URL
userinfo, control-character-split keywords, and linear time on the known
worst-case inputs."""

from __future__ import annotations

import re
import time

import pytest

from clagentic_loadout.push.push_redaction import redact_push_secrets

_MB5 = 5 * 1024 * 1024
# Generous: the measured times are far below this; the bound only has to
# separate linear from quadratic behaviour on 5 MB.
_BOUND_SECONDS = 10.0

# The pre-change pattern, kept as the reference for "same result on every
# input the old pattern redacts".
_OLD_URL_RE = re.compile(r"(https?://[^:/@\s]+:)([^@\s]+)(@)")


def _old_redact_url(text: str) -> str:
    return _OLD_URL_RE.sub(lambda m: f"{m.group(1)}[REDACTED]{m.group(3)}", text)


def test_uppercase_and_mixed_case_url_userinfo_redacted():
    out = redact_push_secrets("a HTTPS://u:pw1@h b HtTpS://u:pw2@h c")
    assert "pw1" not in out
    assert "pw2" not in out
    assert "HTTPS://u:[REDACTED]@h" in out
    assert "HtTpS://u:[REDACTED]@h" in out


def test_control_char_inside_bearer_keyword_is_redacted():
    out = redact_push_secrets("Bea\x07rer abcdefgh123456")
    assert "abcdefgh123456" not in out


def test_ansi_escape_inside_bearer_keyword_is_redacted():
    out = redact_push_secrets("Bea\x1b[0mrer abcdefgh123456")
    assert "abcdefgh123456" not in out


@pytest.mark.parametrize(
    "text",
    [
        "https://u:p@h",
        "see http://user:secret@host/path and https://x:y@z",
        "http://a:b@c@d",
        "http://a:@h",
        "http://a:http://b:p@h",
        "http://a:http://a:http://a: tail http://u:p@h",
        "x@http://a:http://a:p@h",
        "http://a:pw\nhttps://b:q@h",
        "http://:p@h http://a/b:p@h http://a:p",
    ],
)
def test_url_userinfo_matches_previous_pattern_on_lowercase_input(text):
    assert redact_push_secrets(text) == _old_redact_url(text)


def test_auth_header_value_redacted_to_end_of_line():
    out = redact_push_secrets("Authorization: Basic dXNlcjpwYXNz  \nnext line")
    assert out == "Authorization: [REDACTED]\nnext line"


@pytest.mark.parametrize("keyword", ["authorization", "AUTHORIZATION", "Credential"])
def test_auth_header_keywords_case_insensitive(keyword):
    assert redact_push_secrets(f"{keyword}: topsecretvalue") == f"{keyword}: [REDACTED]"


def _timed(text: str) -> float:
    start = time.perf_counter()
    redact_push_secrets(text)
    return time.perf_counter() - start


def test_authorization_then_spaces_is_linear():
    text = "Authorization: x" + " " * _MB5 + "y"
    elapsed = _timed(text)
    assert elapsed < _BOUND_SECONDS, f"measured {elapsed:.3f}s"


def test_repeated_http_userinfo_prefix_without_at_is_linear():
    text = "http://a:" * (_MB5 // len("http://a:"))
    elapsed = _timed(text)
    assert elapsed < _BOUND_SECONDS, f"measured {elapsed:.3f}s"


def test_repeated_http_userinfo_prefix_after_at_is_linear():
    text = "x@" + "http://a:" * (_MB5 // len("http://a:"))
    elapsed = _timed(text)
    assert elapsed < _BOUND_SECONDS, f"measured {elapsed:.3f}s"


def test_ordinary_traceback_is_linear():
    block = (
        "Traceback (most recent call last):\n"
        '  File "/srv/app/main.py", line 42, in run\n'
        "    result = handler(request)\n"
        '  File "/srv/app/handler.py", line 7, in handler\n'
        "    raise ValueError('bad input: 12345')\n"
        "ValueError: bad input: 12345\n"
    )
    text = block * (_MB5 // len(block))
    elapsed = _timed(text)
    assert elapsed < _BOUND_SECONDS, f"measured {elapsed:.3f}s"
