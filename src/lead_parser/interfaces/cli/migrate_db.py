"""Inspect or explicitly migrate SQLite after stopping parser and web writers."""

import argparse
import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from filelock import FileLock

from lead_parser.application.errors import StorageError
from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.persistence.schema import migrate_database, migration_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--database", help="Override database; --lock-file is required when applying")
    parser.add_argument("--lock-file", help="Same lock_file as the parser configuration")
    parser.add_argument("--apply", action="store_true", help="Apply known migrations; default is dry-run")
    parser.add_argument("--backup", help="New SQLite backup path; required for --apply")
    args = parser.parse_args(argv)
    if args.apply and (not args.backup or (args.database and not args.lock_file)):
        parser.error("--apply requires --backup and the parser's --lock-file when using --database")
    try:
        cfg = load_config(args.config, require_access=False) if not args.database else None
        database = Path(args.database or cfg["storage"]["database"]).resolve()
        if not database.is_file():
            raise StorageError("Database does not exist")
        lock = args.lock_file or (cfg["storage"]["lock_file"] if cfg else str(database) + ".lock")
        with FileLock(lock, timeout=0), FileLock(str(lock) + ".web", timeout=0):
            with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
                plan = migration_plan(source)
                if args.apply:
                    destination = Path(args.backup).resolve()
                    os.close(os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
                    with closing(sqlite3.connect(destination)) as target:
                        source.backup(target)
            if args.apply:
                plan = {**migrate_database(database), "backup": str(destination)}
            print(json.dumps({**plan, "applied": args.apply}))
        return 0
    except (StorageError, OSError, ValueError, sqlite3.Error) as error:
        parser.error(
            str(error) if isinstance(error, StorageError) else "Migration failed; check paths and backup"
        )


if __name__ == "__main__":
    raise SystemExit(main())
