"""transcript.verb -- `loadout-transcript`: read a session transcript.

  loadout-transcript decisions TRANSCRIPT [--json]

`decisions` prints how many permission decisions the session made, per class
(person-asked, rule-or-config, unclassified) and per raw decision/source/reasonType
combination, and for every person-asked or unclassified decision: the agent that
raised it, the tool and the tool's full input. The transcript's subagent transcripts
are read with it. The verb reports and changes nothing: it exits 0 on readable input
whatever the counts.

Exit codes:

  0    ok
  1    usage error
  118  unreadable_input: the transcript or a subagent transcript could not be read;
       each such path is named on stderr and no count is printed
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from clagentic_loadout._version import get_version
from clagentic_loadout.transcript.decisions import Report, read_decisions

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_UNREADABLE_INPUT = 118


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="loadout-transcript",
        description="Read a session transcript without changing it.",
    )
    parser.add_argument("--version", action="version", version=f"loadout-transcript {get_version()}")
    sub = parser.add_subparsers(dest="command", required=True)
    decisions = sub.add_parser(
        "decisions",
        help="Count permission decisions by class; list person-asked and unclassified ones.",
    )
    decisions.add_argument("transcript", help="Path of the session transcript (JSONL).")
    decisions.add_argument("--json", action="store_true", help="Print one JSON object.")
    return parser


def render_text(report: Report) -> str:
    lines = [
        f"transcript: {report.transcript}",
        f"subagent transcripts read: {report.subagent_transcripts}",
        f"decisions: {len(report.decisions)}",
    ]
    for cls, count in report.counts_by_class.items():
        lines.append(f"  {cls}: {count}")
    lines.append("by decision/source/reasonType:")
    for row in report.counts_by_combination:
        lines.append(
            f"  {row['decision']}/{row['source']}/{row['reasonType']}: {row['count']} ({row['class']})"
        )
    lines.append(f"listed (person-asked and unclassified): {len(report.listed)}")
    for item in report.listed:
        lines.append(f"  class: {item.cls}")
        lines.append(f"    agent: {item.agent}")
        lines.append(f"    tool: {item.tool}")
        lines.append(f"    input: {json.dumps(item.input, sort_keys=True)}")
        lines.append(f"    decision: {item.decision}")
        lines.append(f"    source: {item.source}")
        lines.append(f"    reasonType: {item.reason_type}")
        if item.rule_source is not None:
            lines.append(f"    ruleSource: {item.rule_source}")
        lines.append(f"    timestamp: {item.timestamp}")
    lines.append(f"skipped lines: {report.skipped_lines}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code."""
    if argv is None:
        argv = sys.argv[1:]
    try:
        args = _build_arg_parser().parse_args(argv)
    except SystemExit as exc:
        return EXIT_OK if exc.code in (0, None) else EXIT_USAGE
    report = read_decisions(Path(args.transcript))
    if report.unreadable:
        for path in report.unreadable:
            print(f"loadout-transcript: cannot read {path}", file=sys.stderr)
        return EXIT_UNREADABLE_INPUT
    if args.json:
        print(json.dumps(report.to_json_dict(), sort_keys=True))
    else:
        print(render_text(report))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
