"""workflow — start a repo's own CI workflow on a ref.

    dispatch_config  — the repo-config allow-list (`workflow_dispatch:`) a
                       dispatch must match before anything leaves the machine.
    dispatch_verb    — loadout-workflow-dispatch: the thin verb itself.

The verb starts a named workflow and reports what GitHub returned. It never
creates tags, releases or notes; the workflow owns those.
"""
