# Scanner input for `loadout-review post`

A review may be given the results of the scanners that examined the pull request's
head. `loadout-review post --scanner-input <file>` reads them, turns each hit that is
part of the change into a finding the reviewer must rule on, and keeps every other hit as
context that never blocks.

The file is JSON and follows
[`scanner-input.schema.json`](../src/clagentic_loadout/schemas/scanner-input.schema.json),
packaged under `clagentic_loadout/schemas/`. The format is generic: it names no scanner,
and any tool that can write it can feed a review.

```json
{
  "head_sha": "<40 hex characters: the head the scanners examined>",
  "scanners": [
    {
      "scanner": "sast-one",
      "status": "ran",
      "hits": [
        {"file": "pkg/parse.py", "line": 12, "rule_id": "unsafe-parse", "message": "..."}
      ]
    },
    {"scanner": "secret-scan", "status": "failed", "reason": "timed out", "kind": "secret"}
  ]
}
```

## Reading rules

- `head_sha` must equal the reviewed head. A file stamped for another head is refused,
  and the message names both.
- `status` is `ran`, `not_applicable`, `not_invoked` or `failed`. Any other value is
  refused. `not_applicable` needs a `reason`. Only a scanner that `ran` may list `hits`;
  hits under any other status are refused, because a scanner that did not complete gave
  no result to read.
- Each hit has `file` (repository-relative), `line` (1 or greater), `rule_id` and
  `message`. An unknown field, or a missing or malformed one, is refused. A scanner name
  appears once.
- `kind: secret` marks a scanner that looks for credentials (default `other`).
- A message is cut to one line of at most 300 characters, and backticks are replaced, so
  scanner text can never open a fenced block in the posted comment.

## Classification

The tool classifies each hit; the file never does. A hit is **in range** when its line is
one the pull request adds, read from the unified diff the host serves for the reviewed
head. A context line, a line outside every hunk, a file the change does not touch and a
deleted file are **outside range**. No field of the scanner file, the findings file, the
dropped file or a `run` output is read for this, and a `post` that cannot read the diff
refuses when there is a hit to classify rather than guess.

`post` is the step that ingests: it classifies at the live head after confirming it is the
reviewed head, in the same invocation that computes the verdict, so there is no earlier
output a reviewer could edit to move a hit.

## What each class becomes

**In range:** a finding with id `file:line:scanner.rule_id` whose facts the tool states
(reachable, no precondition, full impact on every dimension; a `secret` scanner's hits are
in the security floor). It blocks under the evaluator like any finding with those facts.
The reviewer judges it and, when it is not a defect of the change, lists it in
`--dropped` with a reason, matched on `file:line:rule_id`; the drop wins and is shown in
the comment. A base-branch disposition clears it by the usual route, except that the
security floor refuses a by-design disposition.

A reviewer finding with the same `file:line:rule_id` is replaced by the scanner finding,
so the location is counted once and the tool's facts stand. A reviewer finding on the same
line under a different rule is a separate finding: a finding cannot cover a hit by sitting
next to it.

**Outside range:** never a finding and never part of the verdict. It is listed under
`Scanner hits outside the changed lines` in the comment and in the fence as
`outside_range` (`scanner`, `file`, `line`, `rule_id`, `message`).

**Scanner status:** every scanner's status and reason go into the fence as
`scanners_run`, exactly as a `--state-file` entry would, and a required scanner that did
not complete degrades the verdict as it always has. `--state-file` may not also carry
`scanners_run` when `--scanner-input` is given.
