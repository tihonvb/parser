"""Evaluate candidate walls with measured coverage of the configured time window."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.vk.assessment import _evaluate
from lead_parser.infrastructure.integrations.vk.collector import _normalize_group_ref
from lead_parser.infrastructure.integrations.vk.discovery import client_for, find_candidates


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
    raise SystemExit(main())
