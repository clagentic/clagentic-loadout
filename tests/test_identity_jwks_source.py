"""Key-set locators and bounded fetching."""

from __future__ import annotations

import json
import shutil
import socket
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from clagentic_loadout.identity import errors
from clagentic_loadout.identity.jwks_source import MAX_JWKS_BYTES, derive_locator, load_jwks
from tests._support.identity_support import Issuer


@pytest.mark.parametrize(
    "iss,expected",
    [
        ("https://issuer.example", "https://issuer.example/.well-known/jwks.json"),
        ("https://issuer.example/path/", "https://issuer.example/path/.well-known/jwks.json"),
        ("http://127.0.0.1:8080", "http://127.0.0.1:8080/.well-known/jwks.json"),
        ("file:///etc/keys.json", "file:///etc/keys.json"),
        ("unix:///run/issuer.sock", "unix:///run/issuer.sock"),
        ("urn:opaque", None),
    ],
)
def test_derive_locator(iss: str, expected) -> None:
    assert derive_locator(iss) == expected


def _code(locator: str, **kwargs) -> str:
    with pytest.raises(errors.IdentityError) as caught:
        load_jwks(locator, **kwargs)
    assert caught.value.code == errors.JWKS_UNAVAILABLE
    return str(caught.value)


def test_file_source_reads_a_key_set(tmp_path: Path) -> None:
    issuer = Issuer()
    assert load_jwks(issuer.write_jwks(tmp_path / "j.json")) == issuer.jwks


def test_file_source_refusals(tmp_path: Path) -> None:
    issuer = Issuer()
    real = tmp_path / "real.json"
    locator = issuer.write_jwks(real)
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert "cannot open" in _code("file://" + str(link))
    real.chmod(0o666)
    assert "writable" in _code(locator)
    real.chmod(0o600)
    real.write_text("x" * (MAX_JWKS_BYTES + 1))
    assert "over" in _code(locator)
    real.write_text("not json")
    assert "not JSON" in _code(locator)
    real.write_text(json.dumps({"keys": [{"kty": "OKP", "kid": "a", "d": "secret"}]}))
    assert "private" in _code(locator)
    real.write_text(json.dumps({"nokeys": []}))
    assert "keys" in _code(locator)
    assert "cannot open" in _code("file://" + str(tmp_path / "absent.json"))


def test_plain_http_is_loopback_only() -> None:
    assert "loopback" in _code("http://issuer.example/.well-known/jwks.json")


class _Handler(BaseHTTPRequestHandler):
    body = b""
    redirect = False

    def do_GET(self) -> None:  # noqa: N802 -- stdlib override
        if self.redirect:
            self.send_response(302)
            self.send_header("Location", "http://other.invalid/")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args) -> None:
        pass


def _serve(handler_body: bytes, redirect: bool = False):
    handler = type("H", (_Handler,), {"body": handler_body, "redirect": redirect})
    server = HTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_loopback_http_fetch_and_redirect_refusal() -> None:
    issuer = Issuer()
    server = _serve(json.dumps(issuer.jwks).encode())
    try:
        assert load_jwks(f"http://127.0.0.1:{server.server_port}/.well-known/jwks.json") == issuer.jwks
    finally:
        server.shutdown()
    server = _serve(b"", redirect=True)
    try:
        assert "request failed" in _code(f"http://127.0.0.1:{server.server_port}/x")
    finally:
        server.shutdown()


@pytest.fixture
def short_dir():
    # A unix socket path is limited to about 100 bytes, shorter than a pytest tmp_path.
    directory = Path(tempfile.mkdtemp(prefix="id", dir="/tmp"))
    yield directory
    shutil.rmtree(directory, ignore_errors=True)


def test_unix_socket_fetch(short_dir: Path) -> None:
    tmp_path = short_dir
    issuer = Issuer()
    path = tmp_path / "s.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)
    body = json.dumps(issuer.jwks).encode()

    def serve() -> None:
        conn, _ = srv.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\nConnection: close\r\n\r\n" % len(body) + body)
        conn.close()

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        assert load_jwks("unix://" + str(path)) == issuer.jwks
    finally:
        thread.join(5)
        srv.close()
    assert "request failed" in _code("unix://" + str(tmp_path / "gone.sock"))
