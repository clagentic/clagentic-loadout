"""A host opener that serves the tree and contents endpoints from fixed trees."""

from __future__ import annotations

import base64
import json
import urllib.parse

from tests._support.fakes import json_resp


def tree_host_opener(inner, trees: dict[str, dict[str, str]], posted: list[str], requests: list[str] | None = None):
    """*inner* with the tree and contents endpoints answered from *trees*, every
    posted comment body recorded in *posted*, and the URL of each tree or contents
    read recorded in *requests* when given."""

    def opener(req, timeout=15):
        url = req.full_url
        method = req.get_method()
        if method == "GET" and "/git/trees/" in url:
            if requests is not None:
                requests.append(url)
            ref = urllib.parse.unquote(url.split("/git/trees/")[1].split("?")[0])
            entries = [{"path": p, "type": "blob"} for p in trees[ref]]
            return json_resp(200, {"tree": entries, "truncated": False, "total_count": len(entries)})
        if method == "GET" and "/contents/" in url:
            if requests is not None:
                requests.append(url)
            path = urllib.parse.unquote(url.split("/contents/")[1].split("?")[0])
            ref = urllib.parse.unquote(url.split("ref=")[1])
            content = base64.b64encode(trees[ref][path].encode("utf-8")).decode("ascii")
            return json_resp(200, {"encoding": "base64", "content": content})
        if method == "POST" and "/comments" in url:
            posted.append(json.loads(req.data.decode("utf-8"))["body"])
        return inner(req, timeout)

    return opener
