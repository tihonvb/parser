"""Versioned SQLite schema and the additive, transactional 1 -> 2 migration."""

from __future__ import annotations

import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path

from lead_parser.application.errors import StorageError

SCHEMA_VERSION = 2
WEB_TABLES = {"claims", "clicks"}
SCHEMA_SQL = r"""
            CREATE TABLE IF NOT EXISTS leads (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
              ai_state TEXT NOT NULL DEFAULT 'pending', ai_attempts INTEGER NOT NULL DEFAULT 0,
              ai_next_at REAL NOT NULL DEFAULT 0, verdict TEXT, ai_error TEXT,
              first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_seen (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS clicks (
              lead_key TEXT NOT NULL REFERENCES leads(key), chat_id TEXT NOT NULL,
              first_at REAL NOT NULL, last_at REAL NOT NULL, count INTEGER NOT NULL,
              PRIMARY KEY(lead_key, chat_id));
            CREATE TABLE IF NOT EXISTS claims (
              lead_key TEXT NOT NULL REFERENCES leads(key), user_id TEXT NOT NULL,
              user_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, updated REAL NOT NULL,
              PRIMARY KEY(lead_key, user_id));
            CREATE TABLE IF NOT EXISTS human_reviews (
              lead_key TEXT PRIMARY KEY REFERENCES leads(key), is_client INTEGER NOT NULL,
              reviewed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '{}', report TEXT NOT NULL DEFAULT '{}',
              updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, lead_key TEXT NOT NULL REFERENCES leads(key),
              kind TEXT NOT NULL, destination TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
              lease_owner TEXT, lease_until REAL, last_error TEXT, sheet_row INTEGER,
              depends_on INTEGER REFERENCES deliveries(id), created REAL NOT NULL, delivered REAL,
              UNIQUE(lead_key, kind, destination));
            CREATE UNIQUE INDEX IF NOT EXISTS sheet_slots ON deliveries(destination,sheet_row)
              WHERE kind='sheets' AND sheet_row IS NOT NULL;
            CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state,next_at);
            CREATE INDEX IF NOT EXISTS ai_due ON leads(ai_state,ai_next_at);
"""


def schema_objects(db: sqlite3.Connection) -> dict[tuple[str, str], str | None]:
    def normalize(sql):
        if sql is None:
            return None
        # Whitespace/case inside SQL literals is data, not cosmetic formatting.
        parts = re.split(r"('(?:''|[^'])*'|\"(?:\"\"|[^\"])*\")", sql)
        return "".join(
            part if index % 2 else re.sub(r"\s+", "", part).casefold() for index, part in enumerate(parts)
        )

    return {
        (row[0], row[1]): normalize(row[2]) for row in db.execute("SELECT type,name,sql FROM sqlite_schema")
    }


@lru_cache(maxsize=2)
def expected_schema(version: int) -> dict:
    db = sqlite3.connect(":memory:")
    try:
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip() and (
                version == 2
                or not any(f"CREATE TABLE IF NOT EXISTS {name} (" in statement for name in WEB_TABLES)
            ):
                db.execute(statement)
        return schema_objects(db)
    finally:
        db.close()


def inspect_schema(db: sqlite3.Connection) -> int:
    """Reject unknown columns/tables/indexes instead of guessing a migration."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    actual = schema_objects(db)
    if version == 0 and not actual:
        return 0
    if version not in {1, 2}:
        raise StorageError("Unsupported database schema; restore a supported backup")
    allowed = expected_schema(2)
    required = (
        expected_schema(2)
        if version == 2
        else {("table", name): allowed[("table", name)] for name in ("leads", "deliveries", "sources")}
    )
    if not required.keys() <= actual.keys() or any(
        key not in allowed or definition != allowed[key] for key, definition in actual.items()
    ):
        raise StorageError("Unknown database structure; migration refused without modifying data")
    # Some unreleased feature builds created the web tables while still marking version 1.
    # Accept complete, known tables only; never repair an incompatible one in place.
    for name in WEB_TABLES:
        table, index = ("table", name), ("index", f"sqlite_autoindex_{name}_1")
        if (table in actual) != (index in actual):
            raise StorageError("Incomplete web table; migration refused")
    return version


def initialize_or_validate(db: sqlite3.Connection) -> None:
    version = inspect_schema(db)
    if version == 1:
        raise StorageError("Database migration 1 -> 2 required; run lead-parser migrate-db with a backup")
    if version == 0:
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA_SQL.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise


def migration_plan(db: sqlite3.Connection) -> dict:
    version = inspect_schema(db)
    return {
        "from_version": version,
        "to_version": SCHEMA_VERSION,
        "migration": "web-claims-clicks-v2" if version == 1 else "initialize" if version == 0 else None,
        "changed": version != SCHEMA_VERSION,
    }


def migrate_database(database: str | Path, *, initialize: bool = False) -> dict:
    """Caller must stop writers, hold the app lock and back up existing data first.

    Only additive migration 1 -> 2 is allowed. DDL and user_version commit together.
    This function never restores a backup or deletes user data.
    """
    path = Path(database).resolve()
    if not path.is_file() and not initialize:
        raise StorageError("Database does not exist; migration never creates it implicitly")
    if initialize and not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
    db = sqlite3.connect(path.as_uri() + "?mode=rw", uri=True, timeout=30, isolation_level=None)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("BEGIN IMMEDIATE")
        plan = migration_plan(db)
        if plan["from_version"] == 0 and not initialize:
            raise StorageError("Empty unversioned database; explicit initialization is required")
        if [row[0] for row in db.execute("PRAGMA quick_check")] != ["ok"]:
            raise StorageError("Database integrity check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone():
            raise StorageError("Database has orphaned records; migration refused")
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        inspect_schema(db)
        db.execute("COMMIT")
        return plan
    except BaseException:
        if db.in_transaction:
            db.execute("ROLLBACK")
        raise
    finally:
        db.close()
