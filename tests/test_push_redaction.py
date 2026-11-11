"""Tests for the shared push redaction primitive: case-insensitive URL
userinfo, control-character-split keywords, and linear time on the known
worst-case inputs."""

from __future__ import annotations

import re
import time

import pytest

from clagentic_loadout.bounded_capture import TailBuffer
from clagentic_loadout.push.push_redaction import (
    TRUNCATION_MARKER,
    redact_push_secrets,
    redact_remote_locations,
    redact_then_bound,
)
from clagentic_loadout.push.verify_run import _tail
from clagentic_loadout.repo_test.runner import TAIL_MAX_CHARS, log_tail

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


def test_auth_header_lone_cr_mid_value_is_masked_through_line_end():
    out = redact_push_secrets("Authorization: Basic abc\rSECRET tail\nnext line")
    assert "SECRET" not in out
    assert out == "Authorization: [REDACTED]\nnext line"


def test_auth_header_crlf_line_ending_leaves_next_line_untouched():
    out = redact_push_secrets("Authorization: Basic abc\r\nnext line SECRET2")
    assert "abc" not in out
    assert out.endswith("\nnext line SECRET2")


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


# --- redact, then bound ----------------------------------------------------

#: (text, bound kwargs, fragments that must not survive). Every row puts a cut
#: inside a secret.
_BOUND_ROWS = [
    pytest.param(
        "see https://user:SECRETPWVALUE@host.example.test/x ok",
        {"max_chars": 30},
        ["SECRETPW", "RETPWVALUE", "PWVALUE", "VALUE"],
        id="userinfo-url-split-by-the-char-cut",
    ),
    pytest.param(
        "x" * 40 + " token=SECRETTOKEN12345 end",
        {"max_chars": 30},
        ["SECRETTOKEN", "TOKEN12345", "12345"],
        id="token-value-split-by-the-char-cut",
    ),
    pytest.param(
        "a" * 5 + "\n" + "see https://user:SECRETPWVALUE@host.example.test/x\n" * 3,
        {"max_chars": 40, "max_lines": 2},
        ["SECRETPW", "PWVALUE"],
        id="userinfo-url-split-by-the-line-and-char-cut",
    ),
    pytest.param(
        "TPWVALUE@host.example.test/x\nlast line\n",
        {"max_chars": 100, "start_cut": True},
        ["PWVALUE"],
        id="userinfo-url-split-by-the-start-cut",
    ),
    pytest.param(
        "oken=SECRETTOKEN12345 trailing\nlast line\n",
        {"max_chars": 100, "start_cut": True},
        ["SECRETTOKEN", "TOKEN12345"],
        id="token-value-split-by-the-start-cut",
    ),
    pytest.param(
        "arer SECRETBEARERVALUE12345",
        {"max_chars": 100, "start_cut": True},
        ["SECRETBEARER", "VALUE12345"],
        id="bearer-cut-mid-keyword-with-no-line-break",
    ),
    pytest.param(
        "ssword: SECRETPASSVALUE12345",
        {"max_chars": 100, "start_cut": True},
        ["SECRETPASS", "VALUE12345"],
        id="password-cut-mid-keyword-with-no-line-break",
    ),
    pytest.param(
        "ization: SECRETAUTHVALUE12345",
        {"max_chars": 100, "start_cut": True},
        ["SECRETAUTH", "VALUE12345"],
        id="authorization-cut-mid-keyword-with-no-line-break",
    ),
]


@pytest.mark.parametrize(("text", "bounds", "fragments"), _BOUND_ROWS)
def test_redact_then_bound_leaves_no_secret_fragment(text, bounds, fragments):
    out = redact_then_bound(text, **bounds)
    for fragment in fragments:
        assert fragment not in out


def test_redact_then_bound_no_secret_is_unchanged_apart_from_the_marker():
    assert redact_then_bound("alpha\nbeta\ngamma\n", max_chars=100, max_lines=10) == "alpha\nbeta\ngamma"
    assert redact_then_bound("alpha\nbeta\ngamma\n", max_chars=100, max_lines=2) == (
        TRUNCATION_MARKER + "beta\ngamma"
    )
    assert redact_then_bound("abcdefghij", max_chars=4) == TRUNCATION_MARKER + "ghij"


def test_a_start_cut_capture_with_no_line_break_is_one_partial_line_and_is_dropped():
    assert redact_then_bound("PWVALUE@host.example.test", max_chars=100, start_cut=True) == TRUNCATION_MARKER
    assert redact_then_bound("PWVALUE@ " + "x" * 300, max_chars=1000, start_cut=True) == TRUNCATION_MARKER
    # A lone partial line ending in a newline is not a complete line to keep.
    assert redact_then_bound("PWVALUE@" + "x" * 300 + "\n", max_chars=1000, start_cut=True) == TRUNCATION_MARKER


def test_verify_tail_cut_inside_a_token_value_leaves_no_fragment():
    buffer = TailBuffer(20)
    buffer.append(b"padding&token=ABCDEFGH12345678\nlast line\n")
    assert buffer.value() == b"H12345678\nlast line\n" and buffer.dropped
    tail = _tail(buffer)
    assert "12345678" not in tail
    assert tail == TRUNCATION_MARKER + "last line"


def test_verify_tail_with_no_secret_is_unchanged_apart_from_the_marker():
    buffer = TailBuffer(100)
    buffer.append(b"all good\n")
    assert _tail(buffer) == "all good"


def test_log_tail_window_cut_inside_a_password_leaves_no_fragment(tmp_path):
    line = "https://user:SECRETPWVALUE@h.example.test/x\n"
    # The 64 KiB window then begins five characters into the password.
    filler = "w" * (65554 - len(line) - 1) + "\n"
    log = tmp_path / "run.log"
    log.write_text(line + filler)
    tail = log_tail(log, 5)
    assert "PWVALUE" not in tail
    assert tail.startswith(TRUNCATION_MARKER)
    assert len(tail) <= TAIL_MAX_CHARS


def test_log_tail_with_no_secret_is_unchanged(tmp_path):
    log = tmp_path / "run.log"
    log.write_text("alpha\nbeta\ngamma\n")
    assert log_tail(log, 10) == "alpha\nbeta\ngamma"


def test_a_remote_location_with_a_credential_is_withheld_but_its_host_kept():
    url = "https://git.example.test/FAKEPATHCRED7f3a9c/owner/repo.git?sig=FAKEQUERYCRED5d2e1b"
    out = redact_remote_locations(
        f"To {url}\nhook echo: FAKEPATHCRED7f3a9c and FAKEQUERYCRED5d2e1b\n", [url]
    )
    assert "FAKEPATHCRED7f3a9c" not in out and "FAKEQUERYCRED5d2e1b" not in out
    assert "https://git.example.test/[REDACTED remote location]" in out


def test_a_userinfo_remote_with_percent_encoded_credentials_leaks_neither_form():
    url = "https://user:pw@git.example.test/abc%2Fdef%2BSECRET/repo.git?sig=a%2Bb%2Bsecretval"
    text = (
        f"To {url}\n"
        "To https://git.example.test/abc%2Fdef%2BSECRET/repo.git?sig=a%2Bb%2Bsecretval\n"
        "hook: abc%2Fdef%2BSECRET abc/def+SECRET a%2Bb%2Bsecretval a+b+secretval\n"
    )
    out = redact_remote_locations(text, [url])
    for value in ("abc%2Fdef%2BSECRET", "abc/def+SECRET", "a%2Bb%2Bsecretval", "a+b+secretval", "user:pw", "SECRET"):
        assert value not in out
    assert out.count("https://git.example.test/[REDACTED remote location]") == 2


def test_text_without_a_remote_location_is_unchanged():
    assert redact_remote_locations("Everything up-to-date\n", ["https://git.example.test/o/r.git"]) == (
        "Everything up-to-date\n"
    )
    assert redact_remote_locations("anything\n", []) == "anything\n"
