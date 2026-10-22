# `loadout-workflow-dispatch` -- start a listed workflow on a listed ref

`clagentic_loadout.workflow.dispatch_verb` (also `clagentic-loadout workflow-dispatch`).
A thin verb: it starts a named workflow on a ref with given inputs, under the
calling role's own credential, and reports what GitHub returned. The workflow
file owns everything that happens next (building, tagging, releasing). The verb
never creates tags, releases or notes, runs no release logic, and has no wait
loop.

```
loadout-workflow-dispatch --caller <role> \
    --workflow <file.yml> --ref <branch-or-tag> \
    [--input KEY=VALUE ...] [--repo-path <dir>]
```

There is no `--repo` and no `--platform`. The target is derived from the
checkout (see [Target derivation](#target-derivation)).

- It sends `POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches` with
  `{"ref": ..., "inputs": {...}}` (no `inputs` key when none are given). Inputs
  are passed through as given; the first `=` splits, so a value may contain `=`.
  A malformed `--input`, or a key given twice, is a usage error.
- `--caller` is bound to the attested invoking identity before any I/O, and the
  credential comes from the credential-provider seam
  ([credentials.md](credentials.md)), exactly like every other verb. The
  credential must carry the **`actions: write`** permission on the repo.
- Only GitHub is supported. An origin on any other host is refused with a
  message saying so, and nothing is sent.

## Target derivation

The verb takes the git top-level that contains `--repo-path` (default: the
current directory) and reads that tree's `origin` remote with
`git remote get-url origin`. From the URL it takes the host, the owner and the
repo name. It dispatches against exactly that owner and repo, mints the
credential scoped to it, and reads the allow-list from that same top-level.

- Accepted origin forms: `https://github.com/<owner>/<repo>[.git]`,
  `ssh://git@github.com/<owner>/<repo>[.git]` and `git@github.com:<owner>/<repo>[.git]`.
  The host must be exactly `github.com` (case-insensitive). A suffix
  (`notgithub.com`), a subdomain (`api.github.com`), a lookalike prefix
  (`github.com.example.com`), a `github.com@other-host` userinfo trick and an
  explicit port are all refused.
- Only the remote named `origin` is read. A branch's tracking remote, or a
  `github.com` remote under another name, never substitutes for it.
- The config is read from the top-level only. A config that exists only in a
  directory above the checkout (a wrapper directory) is not used; the dispatch
  is refused with exit 31.
- Refused before any credential or network use, with the message ending
  "Nothing was sent": no git tree (exit 1), no readable `origin` (exit 1), a URL
  that cannot be parsed or does not name exactly one owner and repo (exit 1),
  an origin on another host (exit 4, "GitHub only"), a config outside the
  checkout (exit 31).

## The allow-list (the one policy check)

A dispatch runs only if the repo's own config lists **both** the workflow file
and the ref. Add a top-level `workflow_dispatch` list to
`.clagentic/loadout/config.yaml` ([provisioning.md](provisioning.md) documents
the rest of that file):

```yaml
workflow_dispatch:
  - workflow: release.yml     # file name, no path separators
    refs: [main]              # exact ref names, no globs
```

- Matching is exact on both fields. A workflow can appear in several entries;
  its allowed refs are the union.
- A missing config file, a missing `workflow_dispatch` key, or any malformed
  entry (not a list, unknown key, empty `refs`, a workflow name with a path
  separator, a ref with whitespace) is an error that names the file and the key
  path, for example `workflow_dispatch[1].refs[0]`. An unreadable allow-list is
  never treated as an open one.
- The check runs **before** any credential is minted and before any network
  call (the only earlier steps are the local `--caller` identity binding and the
  target derivation above). A refused dispatch costs nothing.

### What the allow-list protects

The allow-list is deployment policy, as trusted as the checkout it is read from.
It is a guard against dispatching an unlisted workflow or ref by mistake. It is
not a defence against someone who can edit that checkout: anyone who can change
the config, or the `origin` remote, in the tree the verb runs from can change
what it permits and where it dispatches. The credential is a separate control:
the credential provider scopes it to the derived repo, whatever the config says.

## Output

On success, one JSON object on stdout:

```json
{
  "platform": "github",
  "repo": "some-owner/some-repo",
  "workflow": "release.yml",
  "ref": "main",
  "dispatch": {"accepted": true, "status": 204},
  "run": {"id": 123, "url": "https://github.com/some-owner/some-repo/actions/runs/123"},
  "run_lookup": "newest_run"
}
```

When no run is reported, `run` is `null` and a `note` field is added.

GitHub's dispatch response is normally `204 No Content` with no run id. When the
response does carry one it is reported (`run_lookup: "dispatch_response"`).
Otherwise the verb makes **one** read of the newest `workflow_dispatch` run for
that workflow and ref (`run_lookup: "newest_run"`), and reports it only if it
was created strictly after the moment the dispatch request was made, so a run
that is not provably this dispatch's (an earlier one, or one created in the
same instant) is never reported. If no such run is visible yet the result is
`"run": null` with `run_lookup` of `"not_yet_visible"` or `"unavailable"` and a
`note` saying the run is not yet visible; the dispatch itself was still
accepted, and the exit code is 0. There is no wait loop, so a caller that needs
the run id reads it later.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Dispatch accepted |
| 1 | Usage error, or a target that cannot be derived (no git tree, no `origin`, unparseable URL) |
| 2 | Credential resolution failed |
| 4 | The origin remote is not on github.com (GitHub only) |
| 30 | Workflow or ref not listed in the repo config |
| 31 | Repo config missing, malformed, or found only outside the checkout |
| 32 | `--caller` does not match the attested invoking identity |
| 33 | The host rejected the dispatch or could not be reached (401, 404, 422, other) |
| 34 | HTTP 403: the credential lacks the `actions: write` permission |

Errors report the resolved repo, workflow and ref. A 422 usually means the
workflow has no `workflow_dispatch` trigger, the ref does not exist, or an input
is invalid.

## Provisioning

The verb label is `workflow-dispatch` (console script `loadout-workflow-dispatch`)
for a role's `roles:` verb set; see [provisioning.md](provisioning.md). It is in
no default role's set.
