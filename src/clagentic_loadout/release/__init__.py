"""release — release-event verbs: task-shipped signal dispatch + tag-scan
automation.

Release-signal verbs (lr-51d4, Wave A slice 6, tome #688). Ported from the
reference implementation; the source copies stay primary until their
separate CUT OVER + RETIRE + VERIFY-GONE task per the migration plan.

    dispatch  — build + HMAC-sign + POST a "task shipped" release-event
                payload to a configured endpoint.
    detector  — scan a v*-tag commit range for distinct resolved work items
                and fire one dispatch per item, via dispatch's own
                entrypoint (no duplicated signing/payload logic).
"""

from __future__ import annotations

from clagentic_loadout.release.dispatch import (
    EXIT_HOOK_FAILED,
    EXIT_OK,
    EXIT_SECRET_FAILED,
    EXIT_USAGE,
    build_status_hook_payload,
    dispatch_task_shipped,
    fire_status_hook,
    is_valid_status_hook_url,
    parse_trailers,
    resolve_hook_secret,
    sign_payload,
)
from clagentic_loadout.release.detector import (
    compute_commit_range,
    dispatch_detected_tasks,
    dispatch_manual_task,
    extract_resolved_tasks,
    get_commit_messages,
    is_repo_authorized_for_auto_dispatch,
    is_semantic_release_owned,
    repo_identity_from_remote,
)

__all__ = [
    "EXIT_HOOK_FAILED",
    "EXIT_OK",
    "EXIT_SECRET_FAILED",
    "EXIT_USAGE",
    "build_status_hook_payload",
    "compute_commit_range",
    "dispatch_detected_tasks",
    "dispatch_manual_task",
    "dispatch_task_shipped",
    "extract_resolved_tasks",
    "fire_status_hook",
    "get_commit_messages",
    "is_repo_authorized_for_auto_dispatch",
    "is_semantic_release_owned",
    "is_valid_status_hook_url",
    "parse_trailers",
    "repo_identity_from_remote",
    "resolve_hook_secret",
    "sign_payload",
]
