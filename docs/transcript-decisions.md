# `loadout-transcript decisions` — permission decisions in a session

```
loadout-transcript decisions TRANSCRIPT [--json]
clagentic-loadout transcript decisions TRANSCRIPT [--json]
```

Reads one session transcript (a JSONL file) and the subagent transcripts of the same
session, and prints how many permission decisions the session made per class and per
raw value combination, and, for every decision where a person was asked or the
combination is not classified, the agent that raised it, the tool and the tool's full
input. The caller passes the transcript path; the verb has no transcript store of its
own and resolves no session id.

The verb reports and changes nothing. It writes no file and exits `0` on readable
input whatever the counts; nothing in it checks, gates or refuses on a count.

## What it reads

| Item | Where |
|---|---|
| Decision | the `permissionDecision` object (`decision`, `source`, `reasonType`, optionally `ruleSource`) on a record whose `type` is `user` and whose `message.content[]` holds a `tool_result` block |
| Tool and input | the `tool_use` block (`id`, `name`, `input`) of an assistant record whose `id` equals the result's `tool_use_id`, in the same file |
| Subagent transcripts | `<directory of TRANSCRIPT>/<session id>/subagents/agent-<id>.jsonl`, where `<session id>` is the transcript file's stem |
| Attribution | the `agentId` each subagent record carries (the file name's `<id>` when a record has none); decisions in TRANSCRIPT itself are attributed to `main` |

One decision is counted per record whose `message.content[]` holds exactly one
`tool_result` block. A record carrying `permissionDecision` with no such block, or with
two or more, has no single call to tie the decision to: it yields no decision and is
counted in `ambiguous records`. A decision whose `tool_use` block is
not in the file is still counted; its tool and input print as `null`. A call that
has a `tool_use` and no result (a prompt unanswered when the session ended) is not
counted. The subagents' `meta.json` files are not read.

## Classes

One table in the code (`CLASSIFICATION` in `transcript/decisions.py`) maps the raw
values to a class. A decision matched by rows of two classes (for example `user_reject`
with `reasonType` `rule`) is a combination never recorded and is `unclassified`.

| Class | Rows |
|---|---|
| `rule-or-config` | `reasonType` is `rule`, with any `source` or `ruleSource` (this includes `source` `user_permanent` with `ruleSource` `userSettings`: a saved allow rule matching, no card shown); `source` `config` with `reasonType` `subcommandResults` |
| `person-asked` | `user_reject` as the value of `source` or of `decision`: a card that was shown and timed out or was rejected |
| `unclassified` | every other combination, including a missing value and a combination that rows of two classes match. It is counted and listed with its raw values, never guessed |

A newly recorded value is added as one row of the table.

## Output

Text:

```
transcript: <path>
subagent transcripts read: <n>
decisions: <n>
  person-asked: <n>
  unclassified: <n>
  rule-or-config: <n>
by decision/source/reasonType:
  <decision>/<source>/<reasonType>: <n> (<class>)
listed (person-asked and unclassified): <n>
  class: <class>
    agent: <main | agent id>
    tool: <name>
    input: <the full tool input as JSON>
    decision: <decision>
    source: <source>
    reasonType: <reasonType>
    ruleSource: <ruleSource, when present>
    timestamp: <timestamp>
ambiguous records: <n>
skipped lines: <n>
```

`--json` prints one object: `transcript`, `decisions`, `counts_by_class`,
`counts_by_combination` (a list of `class`, `decision`, `source`, `reasonType`,
`count`), `listed_decisions` (the person-asked and unclassified decisions, each with
`class`, `agent`, `tool`, `input`, `decision`, `source`, `reasonType`, `timestamp`,
plus `ruleSource` when present), `subagent_transcripts`, `skipped_lines` and
`ambiguous_records`. Inputs are never truncated in either form.

A line that is not a JSON object is skipped and counted in `skipped lines`. Blank
lines are ignored and not counted.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | ok, whatever the counts |
| 1 | usage error |
| 118 | unreadable input: the transcript, a subagent transcript or the subagents directory could not be read. Each path is named on stderr and no count is printed |
