"""Durable collection, classification and delivery. See README for exit codes."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
import time
from pathlib import Path

from filelock import FileLock, Timeout

from ai_filter import filter_leads
from common import ScanResult
from configuration import CONFIG_PATH, ConfigError, load_config
from delivery import drain
from security import safe_error
from storage import StorageError, Store
from vk_token import TokenManager


def run_once(cfg: dict, *, deliver_only=False) -> tuple[int, dict]:
    Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
    with FileLock(cfg["storage"]["lock_file"], timeout=0), Store(cfg["storage"]["database"]) as store:
        store.import_legacy(cfg["storage"]["seen_store"])
        reports = []
        if not deliver_only:
            for source in ("telegram", "vk", "avito"):
                if not cfg[source]["enabled"]:
                    continue
                try:
                    if source == "vk" and cfg["vk"]["token_mode"] == "vk_id":
                        cfg["vk"]["access_token"] = TokenManager(cfg).refresh()
                    module = importlib.import_module(source + "_parser")
                    leads = module.collect_leads(cfg, store=store, reports=reports)
                    store.ingest(leads)  # also supports compatible collectors; page sinks already persisted
                except Exception as error:
                    report = ScanResult(source + ":collector")
                    report.fail(safe_error(error))
                    reports.append(report)
                    store.checkpoint(report)
        # Bound expensive work; unprocessed/pending candidates remain in SQLite for later runs.
        pending = store.pending_ai(limit=cfg["delivery"]["jobs_per_run"])
        if pending:
            accepted = {lead.dedupe_key() for lead in filter_leads(cfg, pending)}
            for lead in pending:
                state = (
                    "pending"
                    if lead.extra.get("ai_pending")
                    else "accepted"
                    if lead.dedupe_key() in accepted
                    else "rejected"
                )
                store.classification(
                    lead,
                    state,
                    verdict=lead.extra,
                    error=lead.extra.get("ai_error"),
                    max_attempts=cfg["ai_filter"]["max_attempts"],
                    retry_seconds=cfg["delivery"]["retry_base_seconds"],
                )
        delivered = drain(cfg, store)
        report = {
            **store.summary(),
            "this_run": {
                "delivery": delivered,
                "sources": [
                    {
                        "id": item.source_id,
                        "complete": item.complete,
                        "scanned": item.scanned,
                        "candidates": item.candidates,
                        "errors": item.errors,
                        "prefilter": item.prefilter,
                        "coverage": item.coverage,
                        "window": item.window,
                    }
                    for item in reports
                ],
            },
        }
        problems = (
            any(not result.complete for result in reports)
            or any(report["leads"].get(state, 0) for state in ("pending", "review"))
            or any(report["deliveries"].get(state, 0) for state in ("pending", "leased", "failed"))
        )
        return int(problems), report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--check-config", action="store_true", help="Validate without connecting to any API")
    parser.add_argument(
        "--deliver-only",
        action="store_true",
        help="Resume persisted classification/delivery without collecting",
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--backup", metavar="NEW_PATH")
    parser.add_argument("--reclassify", metavar="SOURCE:ID")
    parser.add_argument("--retry-failed", action="store_true")
    args = parser.parse_args(argv)
    try:
        storage_command = args.status or args.backup or args.reclassify or args.retry_failed
        cfg = load_config(args.config, require_access=not storage_command or args.check_config)
        if args.check_config:
            print("Configuration valid; no API calls made.")
            return 0
        if args.status or args.backup or args.reclassify or args.retry_failed:
            Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
            with FileLock(cfg["storage"]["lock_file"], timeout=0), Store(cfg["storage"]["database"]) as store:
                if args.backup:
                    store.backup(args.backup)
                if args.reclassify:
                    store.reclassify(args.reclassify)
                if args.retry_failed:
                    store.retry_failed()
                print(json.dumps(store.summary(), ensure_ascii=False, indent=2))
            return 0
        while True:
            cfg = load_config(args.config)  # reload and revalidate every cycle
            code, report = run_once(cfg, deliver_only=args.deliver_only)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            if not args.loop:
                return code
            time.sleep(cfg["schedule"]["interval_minutes"] * 60)
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        return 2
    except Timeout:
        print("Another parser/token operation holds the lock.", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        return 0
    except Exception as error:
        print(
            "Parser failed: " + (str(error) if isinstance(error, StorageError) else safe_error(error)),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
