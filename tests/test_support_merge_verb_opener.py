"""The merge-verb test opener backfills a created_at on comments that lack one.
The backfill must stay a valid, id-ordered timestamp for any comment id."""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime

from tests._support.merge_verb import make_opener


def test_backfilled_created_at_is_valid_and_ordered_for_ids_past_sixty():
    ids = [1, 59, 60, 61, 3599, 3600, 100000]
    opener = make_opener(comments=[{"id": i, "body": "x"} for i in ids])

    served = json.loads(
        opener(urllib.request.Request("https://forgejo.example/api/v1/x/comments")).read()
    )

    stamps = [datetime.strptime(c["created_at"], "%Y-%m-%dT%H:%M:%SZ") for c in served]
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(ids)
