"""Read source statistics and explicitly record human labels or publish a report."""

import argparse
import json
from pathlib import Path

import yaml
from filelock import FileLock

from lead_parser.application.statistics import from_store, overrides
from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.google_sheets.reporting import write_stats_sheet
from lead_parser.infrastructure.persistence.sqlite import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--write", action="store_true", help="Explicitly update the statistics worksheet")
    parser.add_argument("--human-label", metavar="SOURCE:ID")
    parser.add_argument("--is-client", choices=["yes", "no"])
    args = parser.parse_args(argv)
    if args.top <= 0 or bool(args.human_label) != bool(args.is_client):
        parser.error("positive --top; human label requires --is-client")
    cfg = load_config(args.config, require_access=args.write)
    Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
    with FileLock(cfg["storage"]["lock_file"], timeout=0), Store(cfg["storage"]["database"]) as store:
        if args.human_label:
            store.human_label(args.human_label, args.is_client == "yes")
        records, ai_used = from_store(store)
        report = {"sources": records[: args.top], "coverage": store.summary()["sources"]}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(yaml.safe_dump(overrides(records, args.top), allow_unicode=True))
        if args.write:
            write_stats_sheet(cfg, records, ai_used)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
