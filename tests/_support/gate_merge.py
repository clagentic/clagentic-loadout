"""Drive the merge verb against a `tests._gate_repo.GateRepo`.

Shared by the pre_checks gate tests so each does not carry its own copy of the
invocation (the per-process TMPDIR fixture, `scratch_tmp`, lives in
`tests/conftest.py`)."""

from __future__ import annotations

from clagentic_loadout.merge import verb
from tests._gate_repo import GateRepo
from tests._support.merge_verb import (
    AllowingAuthorityProvider,
    RecordingTokenProvider,
    base_args,
    make_opener,
)


def counting_opener(repo: GateRepo, merge_calls: list | None):
    """An opener serving the repo's PR payload that records each merge POST."""
    inner = make_opener(pr_info=repo.pr_info())

    def opener(req, timeout=15):
        if merge_calls is not None and req.get_method() == "POST" and req.full_url.endswith("/merge"):
            merge_calls.append(req.full_url)
        return inner(req, timeout=timeout)

    return opener


def run_gate_merge(repo: GateRepo, merge_calls: list | None = None) -> int:
    """Run the merge verb for *repo*'s PR with post-merge steps skipped."""
    argv = base_args(**{"--repo-path": str(repo.path)}) + ["--skip-post-merge"]
    return verb.main(
        argv,
        token_provider=RecordingTokenProvider(),
        authority_provider=AllowingAuthorityProvider(),
        opener=counting_opener(repo, merge_calls),
    )
