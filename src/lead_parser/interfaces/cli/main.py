"""Durable collection, classification and delivery. See README for exit codes."""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from filelock import FileLock, Timeout

from lead_parser.bootstrap import run_once
from lead_parser.infrastructure.configuration import CONFIG_PATH, ConfigError, load_config
from lead_parser.infrastructure.persistence.sqlite import StorageError, Store
from lead_parser.infrastructure.security import safe_error


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Additional lead-parser commands: evaluate, stats, analytics, discover, vk-auth, telegram-auth, "
            "migrate-sheet, vk-discovery, telegram-discovery, vk-groups, extract-vk-groups. "
            "Use COMMAND --help for command-specific options."
        ),
    )
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
    raise SystemExit(main())
