"""`loadout-session layer`: the built settings layer, through the verb's real entry
point with a synthetic user-level config. Synthetic names (*.test) and RFC 5737
addresses only."""

from __future__ import annotations

import copy
import json
import stat

import pytest
import yaml

from clagentic_loadout.session.verb import (
    EXIT_CONFIG_INVALID,
    EXIT_OK,
    main,
)

LOCAL = "192.0.2.10"
OTHER = "192.0.2.20"
HOSTS = {
    "api.example.test": [LOCAL],
    "git.example.test": [OTHER],
    "twin.example.test": [LOCAL],
    "multi.example.test": [LOCAL, "192.0.2.11"],
}


def _resolver(host: str) -> list[str]:
    if host not in HOSTS:
        raise OSError("name not known")
    return HOSTS[host]


def _is_local(address: str) -> bool:
    return address == LOCAL


BASE = {
    "scratch_root": "/srv/scratch/{role}",
    "hosts": ["api.example.test:443"],
    "credential_paths": ["/etc/example/shared.env"],
    "mcp_servers": ["notes"],
    "roles": {
        "role-a": {
            "hosts": ["git.example.test:443"],
            "credential_paths": ["/etc/example/role-a.env"],
            "mcp_servers": ["tickets"],
        }
    },
}


def run_layer(tmp_path, capsys, section, *extra, role="role-a", resolver=_resolver):
    cfg = tmp_path / "cfg"
    cfg.mkdir(exist_ok=True)
    doc = {} if section is None else {"session_profile": section}
    (cfg / "config.yaml").write_text(yaml.safe_dump(doc), encoding="utf-8")
    rc = main(
        ["layer", "--role", role, *extra],
        config_root=cfg,
        runtime_base=tmp_path / "rt",
        resolver=resolver,
        is_local=_is_local,
    )
    out = capsys.readouterr()
    return rc, out.out, out.err


def built(tmp_path, capsys, section=None, **kw):
    rc, out, err = run_layer(tmp_path, capsys, copy.deepcopy(BASE if section is None else section), **kw)
    assert rc == EXIT_OK, err
    return json.loads(out), err


def test_layer_holds_every_emitted_key(tmp_path, capsys):
    layer, _ = built(tmp_path, capsys)
    sandbox, perms = layer["sandbox"], layer["permissions"]
    assert sandbox["enabled"] is True
    assert sandbox["autoAllowBashIfSandboxed"] is True
    assert sandbox["failIfUnavailable"] is True
    assert sandbox["enableWeakerNestedSandbox"] is False
    assert sandbox["network"]["allowedDomains"] == [
        "api.example.test:443",
        "git.example.test:443",
        f"{LOCAL}:443",
    ]
    assert sandbox["filesystem"]["denyRead"] == ["/etc/example/shared.env", "/etc/example/role-a.env"]
    assert perms["defaultMode"] == "acceptEdits"
    assert perms["additionalDirectories"] == ["/srv/scratch/role-a"]
    assert perms["allow"] == [
        "WebFetch(domain:api.example.test)",
        "WebFetch(domain:git.example.test)",
        "mcp__notes",
        "mcp__tickets",
    ]
    assert perms["deny"] == [
        "Read(//etc/example/shared.env)",
        "Edit(//etc/example/shared.env)",
        "Read(//etc/example/role-a.env)",
        "Edit(//etc/example/role-a.env)",
    ]


def test_no_shell_allow_rule_in_any_built_layer(tmp_path, capsys):
    layer, _ = built(tmp_path, capsys)
    assert not [r for r in layer["permissions"]["allow"] if r.startswith("Bash")]
    # The exact-layer assertion above fixes every other string to a key, a config
    # value or the role, so no name can enter the layer from code.


def _credential_forms(layer):
    deny_read = layer["sandbox"]["filesystem"]["denyRead"]
    deny = layer["permissions"]["deny"]
    reads = [r[len("Read(/") : -1] for r in deny if r.startswith("Read(")]
    edits = [r[len("Edit(/") : -1] for r in deny if r.startswith("Edit(")]
    return deny_read, reads, edits


def _host_forms(layer):
    allowed = layer["sandbox"]["network"]["allowedDomains"]
    fetched = [
        r[len("WebFetch(domain:") : -1]
        for r in layer["permissions"]["allow"]
        if r.startswith("WebFetch(")
    ]
    return allowed, fetched


@pytest.mark.parametrize("extra", ["/etc/example/added.env", "/var/lib/example/key.pem"])
def test_credential_path_added_to_config_reaches_all_three_forms(tmp_path, capsys, extra):
    section = copy.deepcopy(BASE)
    section["credential_paths"].append(extra)
    layer, _ = built(tmp_path, capsys, section)
    deny_read, reads, edits = _credential_forms(layer)
    assert extra in deny_read
    assert f"Read(/{extra})" in layer["permissions"]["deny"]
    assert f"Edit(/{extra})" in layer["permissions"]["deny"]
    assert set(deny_read) == set(reads) == set(edits)


@pytest.mark.parametrize("extra", ["new.example.test:8443", "plain.example.test"])
def test_host_added_to_config_reaches_both_forms(tmp_path, capsys, extra):
    section = copy.deepcopy(BASE)
    section["hosts"].append(extra)
    layer, _ = built(tmp_path, capsys, section)
    allowed, fetched = _host_forms(layer)
    assert extra in allowed
    assert extra.split(":")[0] in fetched
    # every listed entry's hostname is a fetch rule; address entries are the only extras.
    listed = [e for e in allowed if not e.startswith("192.0.2.")]
    assert {e.split(":")[0] for e in listed} == set(fetched)


@pytest.mark.parametrize(
    "hosts,expected_extra,warn_fragment",
    [
        (["api.example.test:443"], [f"{LOCAL}:443"], None),
        (["api.example.test:443", "twin.example.test:443"], [f"{LOCAL}:443"], None),
        (["multi.example.test:443"], [f"{LOCAL}:443"], None),
        (["api.example.test"], [], "would admit every port"),
        (["git.example.test:443"], [], None),
        (["gone.invalid:443"], [], "does not resolve"),
        (["*.example.test:443"], [], None),
    ],
    ids=["local-port", "duplicate-address", "one-local-of-two", "local-no-port", "not-local", "unresolvable", "wildcard"],
)
def test_same_host_pin_rows(tmp_path, capsys, hosts, expected_extra, warn_fragment):
    section = copy.deepcopy(BASE)
    section["hosts"] = hosts
    section["roles"] = {"role-a": {}}
    layer, err = built(tmp_path, capsys, section)
    allowed = layer["sandbox"]["network"]["allowedDomains"]
    assert allowed == [*hosts, *expected_extra]
    if warn_fragment:
        assert warn_fragment in err
        assert hosts[0] in err
    else:
        assert "warning" not in err
    # the web-fetch rule never carries an address entry
    assert not [r for r in layer["permissions"]["allow"] if "192.0.2." in r]


def test_pin_same_host_false_emits_no_address_entry_and_no_resolution(tmp_path, capsys):
    section = copy.deepcopy(BASE)
    section["pin_same_host"] = False

    def refusing(_host):
        raise AssertionError("must not resolve when pinning is off")

    layer, err = built(tmp_path, capsys, section, resolver=refusing)
    assert layer["sandbox"]["network"]["allowedDomains"] == ["api.example.test:443", "git.example.test:443"]
    assert "warning" not in err


@pytest.mark.parametrize("value,expected", [(None, False), (True, True), (False, False)])
def test_weaker_nested_sandbox_default_and_set(tmp_path, capsys, value, expected):
    section = copy.deepcopy(BASE)
    if value is not None:
        section["weaker_nested_sandbox"] = value
    layer, _ = built(tmp_path, capsys, section)
    assert layer["sandbox"]["enableWeakerNestedSandbox"] is expected


def test_role_without_entry_gets_shared_lists_and_a_note(tmp_path, capsys):
    layer, err = built(tmp_path, capsys, role="role-z")
    assert layer["sandbox"]["filesystem"]["denyRead"] == ["/etc/example/shared.env"]
    assert layer["permissions"]["additionalDirectories"] == ["/srv/scratch/role-z"]
    assert "role-z" in err


@pytest.mark.parametrize(
    "section,fragment",
    [
        (None, "session_profile"),
        ({"unrelated": 1}, "session_profile"),
        ({"hosts": ["api.example.test:443"]}, "scratch_root"),
    ],
    ids=["empty-file", "no-section", "missing-required-key"],
)
def test_missing_config_is_reported_and_exits_ok_without_a_layer(tmp_path, capsys, section, fragment):
    rc, out, err = run_layer(tmp_path, capsys, section)
    assert rc == EXIT_OK
    assert out == ""
    assert fragment in err
    assert str(tmp_path / "cfg" / "config.yaml") in err
    assert not (tmp_path / "rt").exists()


def test_missing_config_file_names_the_path_it_read(tmp_path, capsys):
    rc = main(["layer", "--role", "role-a"], config_root=tmp_path / "absent")
    out = capsys.readouterr()
    assert rc == EXIT_OK and out.out == ""
    assert str(tmp_path / "absent" / "config.yaml") in out.err


@pytest.mark.parametrize(
    "patch,fragment",
    [
        ({"scratch_root": "relative/{role}"}, "relative/role-a"),
        ({"weaker_nested_sandbox": "yes"}, "weaker_nested_sandbox"),
        ({"hosts": "api.example.test"}, "hosts"),
        ({"credential_paths": ["relative.env"]}, "relative.env"),
    ],
    ids=["relative-scratch", "bad-bool", "bad-list", "relative-credential"],
)
def test_malformed_value_is_a_config_error_naming_key_and_value(tmp_path, capsys, patch, fragment):
    section = {**copy.deepcopy(BASE), **patch}
    rc, out, err = run_layer(tmp_path, capsys, section)
    assert rc == EXIT_CONFIG_INVALID
    assert out == ""
    assert fragment in err


def test_layer_without_write_writes_nothing(tmp_path, capsys):
    built(tmp_path, capsys)
    assert not (tmp_path / "rt").exists()


def test_write_stores_private_file_and_prints_only_the_path(tmp_path, capsys):
    printed, _ = built(tmp_path, capsys)
    rc, out, _err = run_layer(tmp_path, capsys, copy.deepcopy(BASE), "--write")
    assert rc == EXIT_OK
    path = tmp_path / "rt" / "session" / "role-a.json"
    assert out.strip() == str(path)
    assert json.loads(path.read_text(encoding="utf-8")) == printed
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "rt").stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_write_replaces_the_previous_layer(tmp_path, capsys):
    run_layer(tmp_path, capsys, copy.deepcopy(BASE), "--write")
    changed = copy.deepcopy(BASE)
    changed["mcp_servers"] = ["other"]
    run_layer(tmp_path, capsys, changed, "--write")
    stored = json.loads((tmp_path / "rt" / "session" / "role-a.json").read_text(encoding="utf-8"))
    assert "mcp__other" in stored["permissions"]["allow"]
    assert "mcp__notes" not in stored["permissions"]["allow"]


@pytest.mark.parametrize("role", ["../escape", "a/b", ".hidden", ""])
def test_role_must_be_a_plain_token(tmp_path, capsys, role):
    rc, out, err = run_layer(tmp_path, capsys, copy.deepcopy(BASE), role=role)
    assert rc == 1 and out == ""
    assert repr(role) in err
