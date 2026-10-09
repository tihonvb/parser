"""Evaluate candidate walls with measured coverage of the configured time window."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from common import matches_keywords
from configuration import CONFIG_PATH, load_config
from vk_discovery import client_for, find_candidates
from vk_parser import _normalize_group_ref


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
            from security import safe_error

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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["search", "check"])
    parser.add_argument("candidates", nargs="?")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    client = client_for(cfg)
    if args.mode == "search":
        groups = find_candidates(cfg, client)
    else:
        if not args.candidates:
            parser.error("check requires a file of group references")
        refs = [
            _normalize_group_ref(line.split("#")[0].strip().strip("\"'"))
            for line in Path(args.candidates).read_text(encoding="utf-8").splitlines()
            if line.split("#")[0].strip()
        ]
        groups = []
        for ref in dict.fromkeys(refs):
            response = client.method("groups.getById", {"group_ids": ref, "fields": "members_count,wall"})
            groups.extend(response["groups"] if isinstance(response, dict) else response)
    print(json.dumps(_evaluate(client, groups, cfg), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
