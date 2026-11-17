"""repo_checks — run the commands a repository's profile declares as ``lint`` or
``build``, and read each run back by run id.

Both verbs are the same mechanism with a different block name: the entries are
parsed in ``repo_checks.spec``, run through ``push.verify_run.run_verify_entry``
(the runner ``loadout-push`` uses for its declared verification commands), and
recorded under a reports root owned by loadout (``repo_checks.store``). There is
no per-language logic: only the declared argument lists run.

This package marker imports nothing on purpose: ``review.repo_profile`` imports
``repo_checks.spec`` to parse the profile, and the other modules here import the
review package, so any import here would be a cycle.
"""
