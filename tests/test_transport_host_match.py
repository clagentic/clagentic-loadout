"""test_transport_host_match.py — tests for
clagentic_loadout.transport.host_match (lr-0e39f9, extracted from
transport.git_host_api's own _absolute_url_host_matches_git_host_base,
lr-69af67).
"""

from __future__ import annotations

from clagentic_loadout.transport.host_match import host_matches


class TestHostMatches:
    def test_same_host_and_port_matches(self):
        assert host_matches(
            "http://git-host.example.com:3000/api/v1/repos/o/r",
            "http://git-host.example.com:3000",
        )

    def test_different_host_does_not_match(self):
        assert not host_matches(
            "http://attacker.example.net:3000/api/v1/repos/o/r",
            "http://git-host.example.com:3000",
        )

    def test_same_host_different_port_does_not_match(self):
        assert not host_matches(
            "http://git-host.example.com:9999/api/v1/repos/o/r",
            "http://git-host.example.com:3000",
        )

    def test_case_insensitive_host_match(self):
        assert host_matches(
            "http://GIT-HOST.EXAMPLE.COM:3000/api/v1/repos/o/r",
            "http://git-host.example.com:3000",
        )

    def test_loopback_base_matches_itself(self):
        assert host_matches("http://127.0.0.1:3000", "http://127.0.0.1:3000")

    def test_bare_authority_both_sides(self):
        assert host_matches("git-host.example.com:3000", "git-host.example.com:3000")

    def test_bare_authority_against_full_url(self):
        assert host_matches(
            "http://git-host.example.com:3000/api/v1/repos/o/r",
            "git-host.example.com:3000",
        )
