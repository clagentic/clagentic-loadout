"""merge.removed_notice -- the behaviour a pull request removes that its sealed spec
leaves without a ledger line, reported at merge.

The removal report (spec.removals) is computed between the pull request's recorded
base and head, from the local repository when it holds both and else the host API,
and matched against the removal ledger of the spec the reviewer fences name (found
in the local spec store exactly as merge.interface_notice finds it). Each removed
item with no ledger line is named in a notice; the merge prints them and carries the
whole list in its completion attestation, and proceeds: nothing here refuses,
blocks or changes the outcome of a merge. Every failure to compute it is returned as
a note for the merge to print and a line saying so for the attestation.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.merge.interface_notice import load_fence_specs
from clagentic_loadout.review.policy_reader import PolicyReader
from clagentic_loadout.spec import removal_ledger, removals

#: Most not-computed paths named in one merge; the count of the rest is stated.
_PATH_CAP = 5


@dataclass(frozen=True)
class RemovedNotices:
    #: One notice per removed item with no ledger line, for the merge output.
    lines: tuple[str, ...] = ()
    #: Why something could not be computed or read, for the merge output only.
    notes: tuple[str, ...] = ()
    #: The attestation row: each removed item with its ledger line or ``undisposed``,
    #: ``none``, or a line saying the computation did not run and why.
    attestation: tuple[str, ...] = ()
    #: The keys of the removed items with no ledger line.
    undisposed: tuple[str, ...] = ()


def _not_computed(reason: str) -> RemovedNotices:
    return RemovedNotices(
        notes=(f"removed behaviour not computed: {reason}",),
        attestation=(f"not computed: {reason}",),
    )


def _report_from(
    readers: Sequence[PolicyReader], base_sha: str, head_sha: str, paths: Sequence[str]
) -> dict:
    """The first computed report among *readers*, tried in order; when none
    computes, the last report, which says why."""
    report: dict = removals.not_computed_report("no reader was available", base_sha, head_sha)
    for reader in readers:
        report = removals.report_for_paths(reader, base_sha, head_sha, paths)
        if report["status"] == removals.STATUS_COMPUTED:
            return report
    return report


def collect_removed_notices(
    *,
    changed_files: Sequence[str],
    make_readers: Callable[[], Sequence[PolicyReader]],
    base_sha: str | None,
    head_sha: str | None,
    fence_spec_hashes: Sequence[str],
    repo_path: str | None = None,
    config_root: str | Path | None = None,
) -> RemovedNotices:
    """The notices for a pull request between *base_sha* and *head_sha*. *make_readers*
    is called once, since building a reader may open a connection."""
    if not base_sha or not head_sha:
        side = "base" if not base_sha else "head"
        return _not_computed(f"the pull request names no {side} commit")
    report = _report_from(make_readers(), base_sha, head_sha, changed_files)
    if report["status"] != removals.STATUS_COMPUTED:
        return _not_computed(report["note"])
    skipped = report["not_computed"]
    skipped_notes = tuple(
        f"removed behaviour not computed for {entry['path']}: {entry['reason']}" for entry in skipped[:_PATH_CAP]
    ) + ((f"removed behaviour not computed for {len(skipped) - _PATH_CAP} more paths",) if len(skipped) > _PATH_CAP else ())
    skipped_rows = tuple(f"not computed: {entry['path']}: {entry['reason']}" for entry in skipped[:_PATH_CAP])
    if not report["items"]:
        return RemovedNotices(notes=skipped_notes, attestation=("none", *skipped_rows))
    docs, spec_notes = load_fence_specs(fence_spec_hashes, repo_path, config_root)
    ledger: dict = {}
    for doc in docs:
        for key, disposition in removal_ledger.ledger_of(doc).items():
            if disposition or key not in ledger:
                ledger[key] = disposition
    matched = removal_ledger.match(report, ledger)
    missing = [entry for entry in matched if not entry["disposition"]]
    return RemovedNotices(
        lines=tuple(f"removed without a ledger line: {entry['item']} ({entry['kind']})" for entry in missing),
        # The spec notes explain an item that could not be checked against a spec;
        # with every item disposed they would only be noise.
        notes=(tuple(spec_notes) if missing else ()) + skipped_notes,
        attestation=tuple(
            f"{entry['item']} ({entry['kind']}): {removal_ledger.describe(entry['disposition'])}"
            for entry in matched
        )
        + skipped_rows,
        undisposed=tuple(entry["item"] for entry in missing),
    )


__all__ = ["RemovedNotices", "collect_removed_notices"]
