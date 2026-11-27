# Session profile: `loadout-session`

`loadout-session` builds one complete runtime settings layer for a role from the
user-level loadout config and starts the runtime with it. The layer turns the
runtime's native sandbox on, writes each credential path and each host in the two
places that need it from one list, pins the address of a listed host that lives on
this machine, adds the role's scratch directory with edit auto-accept, and allows each
configured MCP server by name. Roles are opaque keys: the verb names no agent, no
consumer and no deployment value; those arrive through config.

```sh
loadout-session layer  --role ROLE [--write]
loadout-session launch --role ROLE [--runtime COMMAND] [-- RUNTIME_ARGS...]
clagentic-loadout session layer|launch ...        # the same verbs through the umbrella
```

Nothing in this verb imports or calls another tool except the runtime command
`launch` execs. The layer is carried by the runtime's settings flag only: the verb has
no option that writes a user-level settings file or a file inside a repository.

## Configuration

The section lives only in the user-level file `~/.config/clagentic/loadout/config.yaml`
(the deployment tier). A repository's own `.clagentic/loadout/config.yaml` is never
read for it, so a cloned repository cannot widen a session's sandbox.

```yaml
session_profile:
  scratch_root: /srv/scratch/{role}     # required; must be absolute after {role} is replaced
  weaker_nested_sandbox: false          # default false
  runtime_command: claude               # default "claude"
  pin_same_host: true                   # default true
  default_mode: acceptEdits             # default "acceptEdits"
  hosts:                                # host or host:port, shared by every role
    - api.example.test:443
  credential_paths:                     # absolute paths, shared by every role
    - /etc/example/shared.env
  mcp_servers:                          # server names, shared by every role
    - notes
  roles:                                # per-role additions; the keys are opaque
    role-a:
      hosts: [git.example.test:443]
      credential_paths: [/etc/example/role-a.env]
      mcp_servers: [tickets]
```

Role lists are added to the shared lists, de-duplicated, order kept. A role with no
entry under `roles:` gets the shared lists and a note on stderr. A role key is never
checked against any roster; because it becomes the file name `<role>.json`, it must be a
plain token (letters, digits, `.`, `_`, `-`, starting with a letter or digit).

`default_mode` is the runtime permission mode the session starts in, written to
`permissions.defaultMode`. The default, `acceptEdits`, is the approved model's edit
auto-accept: the file tools edit inside the working and scratch directories without a
prompt while everything else still follows the deny rules. Set it when a deployment
wants a stricter or different start mode. The value is passed through as written; the
verb does not list the runtime's mode names, so the runtime judges an unknown name.

A value that is present but unusable (a relative scratch root, a non-boolean flag, an
empty `default_mode`, a list that is not a list) is a configuration error: the message names the key and the
value it resolved to, and the verb exits 114. An unrecognised key is reported on stderr
and ignored.

If there is no config file, no `session_profile` section, or a section without
`scratch_root`, the verb prints on stderr what is missing (the file path it read, the
section and each missing key), prints no layer, starts nothing, and exits 0.

## What `layer` emits

`layer --role ROLE` prints one JSON object. Every key below was measured against the
runtime; the right-hand column is what was observed.

| Key | Value | Measured |
|---|---|---|
| `sandbox.enabled` | `true` | Turns the native sandbox on for shell commands. |
| `sandbox.autoAllowBashIfSandboxed` | `true` | A sandboxed shell command runs without a prompt. There is no shell-command allow rule in the layer; no `permissions.allow` entry starts with `Bash`. The sandbox is the bound. |
| `sandbox.failIfUnavailable` | `true` | See [failIfUnavailable](#failifunavailable-as-measured). |
| `sandbox.enableWeakerNestedSandbox` | from config, default `false` | Needed on hosts where the sandbox itself runs inside a container; accepted as a documented setting. |
| `sandbox.network.allowedDomains` | every listed host exactly as written, then pinned address entries | The proxy admits only listed `host` or `host:port` targets. |
| `sandbox.filesystem.denyRead` | every credential path | Shell commands cannot read the path. |
| `permissions.defaultMode` | from `session_profile.default_mode`, default `acceptEdits` | With the default, the native file tools auto-accept edits inside the working directories and the additional directories. |
| `permissions.additionalDirectories` | `[<scratch_root with {role} replaced>]` | Clears the runtime's write wall around the role scratch root. `sandbox.filesystem.allowWrite` is not needed for it. |
| `permissions.allow` | `WebFetch(domain:<hostname>)` per host, `mcp__<server>` per server | The server-level form `mcp__<server>` allows every tool of that server without a prompt. |
| `permissions.deny` | `Read(//<path>)` and `Edit(//<path>)` per credential path | An absolute path takes the runtime's `//` form, for example `Read(//etc/example/shared.env)`. |

The sandbox bounds shell commands only. The native file and web tools are bounded by
permission rules. Each credential path and each host is therefore written twice from
one list, by one function each, so the sandbox key and the rule cannot differ:

- a credential path becomes a `denyRead` entry, a `Read(//path)` deny and an `Edit(//path)` deny;
- a host becomes an `allowedDomains` entry (as written) and a `WebFetch(domain:hostname)` allow (the hostname without its port).

A built layer for a synthetic role:

```json
{
  "sandbox": {
    "enabled": true,
    "autoAllowBashIfSandboxed": true,
    "failIfUnavailable": true,
    "enableWeakerNestedSandbox": false,
    "network": {
      "allowedDomains": ["api.example.test:443", "git.example.test:443", "192.0.2.10:443"]
    },
    "filesystem": {
      "denyRead": ["/etc/example/shared.env", "/etc/example/role-a.env"]
    }
  },
  "permissions": {
    "defaultMode": "acceptEdits",
    "additionalDirectories": ["/srv/scratch/role-a"],
    "allow": [
      "WebFetch(domain:api.example.test)",
      "WebFetch(domain:git.example.test)",
      "mcp__notes",
      "mcp__tickets"
    ],
    "deny": [
      "Read(//etc/example/shared.env)",
      "Edit(//etc/example/shared.env)",
      "Read(//etc/example/role-a.env)",
      "Edit(//etc/example/role-a.env)"
    ]
  }
}
```

## Writing and launching

Without `--write`, `layer` writes nothing. With `--write` it writes the layer to
`~/.clagentic/loadout/session/<role>.json` (the runtime dir is created mode 0700, the
file is mode 0600 and replaced atomically) and prints only that absolute path. The same
file serves both launch forms:

```sh
# 1. loadout launches the runtime
loadout-session launch --role role-a -- --resume

# 2. any launcher that takes a settings file: the runtime itself, or an optional
#    console's open-session command
claude --settings "$(loadout-session layer --role role-a --write)"
```

`launch` builds and writes the layer exactly as `layer --write` does, then execs
`<runtime> --settings <path> <runtime args>`. The runtime command is `claude` unless
`session_profile.runtime_command` or `--runtime` says otherwise (the flag wins). A
command that is not on PATH exits 115 with a message naming the command and where it
came from (the flag, the config value or the default). `clagentic: console` is an
optional second launch path only; nothing here depends on it.

The settings file carries the sandbox block and the additional directories into the
session, a spawned agent and a resumed session alike, the same as the flag does on the
command line. A bare by-name start of an agent is an ordinary session: its permissions,
hooks and servers come from settings, never from the agent definition, so only a session
started with the settings flag wears this layer.

## failIfUnavailable as measured

With `sandbox.failIfUnavailable` set to `true` and a sandbox that cannot start, the
session still starts. Every sandboxed command then fails at the sandbox (it fails
closed). A request to run a command outside the sandbox raises a permission prompt and
never runs silently. The layer sets the key because the approved model requires it; it
does not make the session refuse to start.

## Same-host address pin as measured

A listed `host:port` that resolves to an address this machine owns also gets an
`<address>:<port>` entry in `allowedDomains`, generated at build from resolution (never
read from config, never hardcoded), de-duplicated, and rebuilt on every `layer` or
`launch`. Rules:

- a listed host without a port that resolves to this machine gets no address entry and a
  warning, because an address entry without a port would admit every port;
- a host that does not resolve is kept by name with a warning;
- a wildcard entry is kept as written and never resolved;
- with `session_profile.pin_same_host: false` no address entry is emitted and nothing is
  resolved, so a listed same-host service is refused inside the sandbox.

Scope of the pin, measured on one runtime version with three runs:

- The pinned `<address>:<port>` entry admits every service this machine serves on that
  address and port, not only the listed hostnames, to any command that connects to the
  address literal through the sandbox proxy and names another service in TLS SNI or the
  Host header. The proxy filters on the CONNECT target, never on SNI or Host.
- Another service addressed by its own hostname is still refused unless it is listed,
  even when it is pinned to the same address.
- Other ports on the address are not admitted, with or without the entry.
- A plain request to the address that ignores the proxy environment dials directly and
  fails at connect.

The layer still pins the entry, because without it a listed same-host service is
refused. Whether that scope is acceptable is the deployment's decision; set
`pin_same_host: false` to refuse the pin.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok, including the missing-config report |
| 1 | usage error |
| 114 | a `session_profile` value is malformed |
| 115 | the runtime command is not on PATH |
| 116 | the layer could not be written |
| 117 | the runtime could not be started |
