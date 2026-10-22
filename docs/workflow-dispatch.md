# `loadout-workflow-dispatch` -- start a listed workflow on a listed ref

`clagentic_loadout.workflow.dispatch_verb` (also `clagentic-loadout workflow-dispatch`).
A thin verb: it starts a named workflow on a ref with given inputs, under the
calling role's own credential, and reports what GitHub returned. The workflow
file owns everything that happens next (building, tagging, releasing). The verb
never creates tags, releases or notes, runs no release logic, and has no wait
loop.

```
loadout-workflow-dispatch --caller <role> --repo <owner/repo> \
    --workflow <file.yml> --ref <branch-or-tag> \
    [--input KEY=VALUE ...] [--platform github] [--repo-path <dir>]
```

- It sends `POST /repos/{owner}/{repo}/actions/workflows/{file}/dispatches` with
  `{"ref": ..., "inputs": {...}}` (no `inputs` key when none are given). Inputs
  are passed through as given; the first `=` splits, so a value may contain `=`.
  A malformed `--input`, or a key given twice, is a usage error.
- `--caller` is bound to the attested invoking identity before any I/O, and the
  credential comes from the credential-provider seam
  ([credentials.md](credentials.md)), exactly like every other verb. The
  credential must carry the **`actions: write`** permission on the repo.
- `--platform` is detected from the origin remote of `--repo-path` (default: the
  current directory) when omitted. Only `github` is supported; any other
  platform is refused with a message and nothing is sent.

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
  call (the only earlier step is the local `--caller` identity binding). A
  refused dispatch costs nothing.

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

GitHub's dispatch response is normally `204 No Content` with no run id. When the
response does carry one it is reported (`run_lookup: "dispatch_response"`).
Otherwise the verb makes **one** read of the newest `workflow_dispatch` run for
that workflow and ref (`run_lookup: "newest_run"`), and reports it only if it
was created at or after the dispatch (allowing a few seconds of clock skew), so
an earlier dispatch is never reported as this one. If no such run is visible yet
the result is `"run": null` with `run_lookup` of `"not_yet_visible"` or
`"unavailable"`; the dispatch itself was still accepted, and the exit code is 0.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Dispatch accepted |
| 1 | Usage error (including an undetectable platform: pass `--platform github`) |
| 2 | Credential resolution failed |
| 4 | Platform not supported (anything but GitHub) |
| 30 | Workflow or ref not listed in the repo config |
| 31 | Repo config missing or malformed |
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
