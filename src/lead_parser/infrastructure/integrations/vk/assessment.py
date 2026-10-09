"""Measure candidate wall coverage without coupling discovery to its CLI."""

import time

from lead_parser.core.policies import matches_keywords
from lead_parser.infrastructure.security import safe_error


def _evaluate(client, groups, cfg):
    rows = []
    since = time.time() - cfg["discovery"]["days"] * 86400
    for group in groups[: cfg["discovery"]["max_groups"]]:
        scanned = candidates = 0
        complete = False
        error = ""
        try:
            for page in range(cfg["discovery"]["max_pages"]):
                response = client.method(
                    "wall.get", {"owner_id": -int(group["id"]), "count": 100, "offset": page * 100}
                )
                items = response["items"]
                recent = [post for post in items if post.get("date", 0) >= since]
                scanned += len(recent)
                candidates += sum(
                    matches_keywords(
                        post.get("text", ""), cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
                    )
                    for post in recent
                )
                ordinary = [post for post in items if not post.get("is_pinned")]
                if len(items) < 100 or (ordinary and all(post.get("date", 0) < since for post in ordinary)):
                    complete = True
                    break
        except Exception as exception:
            error = safe_error(exception)
        rows.append(
            {
                "source_id": f"vk:{group['id']}",
                "name": group.get("name", ""),
                "members": group.get("members_count"),
                "posts_in_window": scanned,
                "prefilter_candidates": candidates,
                "window_days": cfg["discovery"]["days"],
                "complete": complete,
                "error": error,
            }
        )
    return sorted(rows, key=lambda row: row["prefilter_candidates"], reverse=True)
