"""merge.interface_notice -- a breaking interface change whose consumers the spec
leaves undisposed, reported at merge.

When a pull request changes the repository's declared interface file and the
change is breaking, the consumer map lists who consumes what broke, and the
PR's sealed spec lists a disposition for each consumer. A consumer with none is
named here, with the element it consumes. The merge prints the notice and
carries it in its completion attestation, and proceeds: nothing here refuses,
blocks or changes the outcome of a merge. Every failure to compute it is
returned as a note for the merge to print.

The spec is found by the hash the reviewer fences carry, in the local spec
store. A pull request that does not change the interface file costs nothing:
no revision is read.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from clagentic_loadout.interface.model import INTERFACE_PATH, InterfaceError
from clagentic_loadout.interface.notice import (
    Undisposed,
    render_undisposed,
    spec_dispositions,
    undisposed_consumers,
)
from clagentic_loadout.interface.report import breaking_report
from clagentic_loadout.review.policy_reader import PolicyReader, PolicyReadError
from clagentic_loadout.spec import store as spec_store

_SHORT = 12


@dataclass(frozen=True)
class InterfaceNotices:
    #: One notice per undisposed consumer, for the merge output and attestation.
    lines: tuple[str, ...] = ()
    #: Why something could not be computed, for the merge output only.
    notes: tuple[str, ...] = ()
    undisposed: tuple[Undisposed, ...] = ()


def _spec_dispositions(
    hashes: Sequence[str], repo_path: str | None, config_root: str | Path | None
) -> tuple[list[tuple[str, str]], list[str]]:
    """The dispositions of every spec that can be read, and a note for each that
    cannot."""
    if not hashes:
        return [], ["no sealed spec hash in the reviewer fences, so no dispositions were read"]
    dispositions: list[tuple[str, str]] = []
    notes: list[str] = []
    untrusted = (Path(repo_path),) if repo_path else ()
    try:
        root = spec_store.specs_root(config_root=config_root)
    except spec_store.StoreError as exc:
        return [], [f"the specs root could not be resolved: {exc}"]
    for spec_hash in hashes:
        try:
            stored = spec_store.load_sealed(root, spec_hash, untrusted_roots=untrusted)
        except spec_store.StoreError as exc:
            notes.append(f"spec {spec_hash[:_SHORT]} was not read: {exc}")
            continue
        dispositions.extend(spec_dispositions(stored.doc))
    return dispositions, notes


def collect_interface_notices(
    *,
    changed_files: Sequence[str],
    make_readers: Callable[[], Sequence[PolicyReader]],
    base_sha: str | None,
    head_sha: str | None,
    producer: str,
    fence_spec_hashes: Sequence[str],
    repo_path: str | None = None,
    config_root: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> InterfaceNotices:
    """The notices for a pull request that changes *producer*'s interface file
    between *base_sha* and *head_sha*. Empty when the file is not among
    *changed_files*, when the change is not breaking, or when every consumer of
    it has a disposition. *make_readers* is called only when the file is among
    the changed files, since building a reader may open a connection."""
    if INTERFACE_PATH not in changed_files:
        return InterfaceNotices()
    if not base_sha or not head_sha:
        return InterfaceNotices(
            notes=(f"interface diff not computed: the pull request names no {'base' if not base_sha else 'head'} commit",)
        )
    try:
        report = breaking_report(
            make_readers(), base_sha, head_sha, producer, env=env, config_root=config_root
        )
    except (PolicyReadError, InterfaceError) as exc:
        return InterfaceNotices(notes=(f"interface diff not computed: {exc}",))
    if not report.breaking:
        return InterfaceNotices()
    changed = report.changed_elements()
    status = report.map_status
    if status is None or status.consumers is None:
        reason = status.unavailable if status is not None else "no map was read"
        return InterfaceNotices(
            notes=(
                f"breaking interface change in {producer} ({', '.join(changed)}); the consumer "
                f"map is unavailable ({reason}), so its consumers are not listed",
            )
        )
    if not report.hits:
        return InterfaceNotices(
            notes=(
                f"breaking interface change in {producer} ({', '.join(changed)}); the consumer "
                f"map at {status.consumers.path!r} lists no consumer of it",
            )
        )
    dispositions, spec_notes = _spec_dispositions(fence_spec_hashes, repo_path, config_root)
    missing = undisposed_consumers(report.hits, dispositions)
    lines = tuple(render_undisposed(item, changed=changed) for item in missing)
    # The notes explain a notice that could not be checked against a spec; with
    # every consumer disposed they would only be noise.
    return InterfaceNotices(
        lines=lines,
        notes=tuple(spec_notes) if missing else (),
        undisposed=tuple(missing),
    )


__all__ = ["InterfaceNotices", "collect_interface_notices"]
