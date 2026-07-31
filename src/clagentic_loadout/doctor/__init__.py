"""doctor — the loadout deployment-conformance verb (lr-e625, lr-1659's named
'conformance polish' slice).

MOTIVATION (lr-e41f incident, 2026-07-09): the lr-d31e/lr-d72d
`github_app.slugs` config seam shipped in code but was never provisioned on
disk in a live deployment. Nothing checked that gap until it produced a
deterministic identity-resolution failure in production
(`review.github_backend.resolve_own_login` 403-then-unconfigured-slug). This
package is the conformance suite that would have caught it: a single verb
(`loadout-doctor`) that inspects a deployment's ACTUAL on-disk/env state
against the config seams `transport.credential_provider`,
`transport.provider_config`, and `transport.github_app_config` already
define, and reports RESOLVED VALUES (never a guess) for each check.

  - ``checks``: the individual check functions, each pure/testable in
    isolation and returning a structured `CheckResult`, never printing or
    calling sys.exit.
  - ``cli``: the ``loadout-doctor`` console-script entry point — aggregates
    check results, prints a report, and maps outcome to the reserved exit
    range (CLI-NAMING-STANDARD.md).

No dependency on any unreleased internal task-tracking tool anywhere in this
package (repo CLAUDE.md rule 6a) — every check is exercised in tests with a
synthetic config root and no network dependency; probe execution is
injectable for the unit suite.
"""

from __future__ import annotations
