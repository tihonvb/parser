"""Transactional lead inbox, source checkpoints and durable delivery outbox."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from common import Lead, ScanResult, unique_leads

SCHEMA_VERSION = 1


class StorageError(RuntimeError):
    pass


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        version = self.db.execute("PRAGMA user_version").fetchone()[0]
        if version not in {0, SCHEMA_VERSION}:
            self.db.close()
            raise StorageError("Unsupported database schema; use the documented migration")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
              ai_state TEXT NOT NULL DEFAULT 'pending', ai_attempts INTEGER NOT NULL DEFAULT 0,
              ai_next_at REAL NOT NULL DEFAULT 0, verdict TEXT, ai_error TEXT,
              first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_seen (key TEXT PRIMARY KEY);
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
        """)
        self.db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        if os.name == "posix":
            os.chmod(self.path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.path) + suffix)
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *args) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def import_legacy(self, path: str | Path) -> int:
        path = Path(path)
        marker = "legacy:" + str(path.resolve())
        if self.db.execute("SELECT 1 FROM meta WHERE key=?", (marker,)).fetchone() or not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise StorageError("Legacy seen store is unreadable; restore or explicitly archive it") from error
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("seen"), list)
            or any(not isinstance(key, str) or ":" not in key for key in data["seen"])
        ):
            raise StorageError("Invalid legacy seen store; refusing to discard history")
        with self.transaction():
            self.db.executemany(
                "INSERT OR IGNORE INTO legacy_seen VALUES (?)", [(key,) for key in data["seen"]]
            )
            self.db.execute(
                "INSERT INTO meta VALUES (?,?)", (marker, json.dumps({"count": len(data["seen"])}))
            )
        return len(data["seen"])

    def ingest(self, leads: list[Lead], *, now: float | None = None) -> int:
        now = time.time() if now is None else now
        added = 0
        with self.transaction():
            for lead in unique_leads(leads):
                key = lead.dedupe_key()
                if self.db.execute("SELECT 1 FROM legacy_seen WHERE key=?", (key,)).fetchone():
                    continue
                payload = json.dumps(lead.to_dict(), ensure_ascii=False)
                digest = hashlib.sha256(lead.text.encode()).hexdigest()
                existing = self.db.execute("SELECT * FROM leads WHERE key=?", (key,)).fetchone()
                if not existing:
                    self.db.execute(
                        "INSERT INTO leads(key,payload,content_hash,first_seen,last_seen) VALUES(?,?,?,?,?)",
                        (key, payload, digest, now, now),
                    )
                    added += 1
                else:
                    # Do not change the snapshot of a classified/delivered lead implicitly.
                    if existing["ai_state"] == "pending":
                        original = Lead.from_dict(json.loads(existing["payload"]))
                        merged = unique_leads([original, lead])[0]
                        self.db.execute(
                            "UPDATE leads SET payload=?,content_hash=?,last_seen=? WHERE key=?",
                            (
                                json.dumps(merged.to_dict(), ensure_ascii=False),
                                hashlib.sha256(merged.text.encode()).hexdigest(),
                                now,
                                key,
                            ),
                        )
                    else:
                        self.db.execute("UPDATE leads SET last_seen=? WHERE key=?", (now, key))
        return added

    def lead(self, key: str) -> Lead:
        row = self.db.execute("SELECT payload FROM leads WHERE key=?", (key,)).fetchone()
        if row is None:
            raise StorageError("Unknown lead key")
        return Lead.from_dict(json.loads(row["payload"]))

    def pending_ai(self, *, now: float | None = None, limit: int = 100) -> list[Lead]:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT payload FROM leads WHERE ai_state='pending' AND ai_next_at<=? ORDER BY first_seen,key LIMIT ?",
            (now, limit),
        ).fetchall()
        return [Lead.from_dict(json.loads(row["payload"])) for row in rows]

    def classification(
        self,
        lead: Lead,
        state: str,
        *,
        verdict: dict | None = None,
        error: str | None = None,
        max_attempts: int = 5,
        retry_seconds: float = 60,
        now: float | None = None,
    ) -> None:
        if state not in {"accepted", "rejected", "pending"}:
            raise ValueError("Invalid classification state")
        now = time.time() if now is None else now
        with self.transaction():
            row = self.db.execute(
                "SELECT ai_attempts FROM leads WHERE key=?", (lead.dedupe_key(),)
            ).fetchone()
            if row is None:
                raise StorageError("Classification requires a persisted lead")
            attempts = row[0] + 1
            if state == "pending" and attempts >= max_attempts:
                state = "review"
            self.db.execute(
                "UPDATE leads SET payload=?,ai_state=?,ai_attempts=?,ai_next_at=?,verdict=?,ai_error=? WHERE key=?",
                (
                    json.dumps(lead.to_dict(), ensure_ascii=False),
                    state,
                    attempts,
                    now + retry_seconds if state == "pending" else 0,
                    json.dumps(verdict, ensure_ascii=False) if verdict else None,
                    error,
                    lead.dedupe_key(),
                ),
            )

    def reclassify(self, key: str) -> None:
        with self.transaction():
            if not self.db.execute("SELECT 1 FROM leads WHERE key=?", (key,)).fetchone():
                raise StorageError("Unknown lead key")
            self.db.execute(
                "UPDATE leads SET ai_state='pending',ai_attempts=0,ai_next_at=0,ai_error=NULL WHERE key=?",
                (key,),
            )

    def cursor(self, source_id: str) -> dict[str, Any]:
        row = self.db.execute("SELECT cursor FROM sources WHERE id=?", (source_id,)).fetchone()
        return json.loads(row[0]) if row else {}

    def checkpoint(self, result: ScanResult) -> None:
        # Collectors persist every page first; this call never races an uncommitted page.
        with self.transaction():
            self.db.execute(
                "INSERT INTO sources VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET cursor=excluded.cursor,report=excluded.report,updated=excluded.updated",
                (
                    result.source_id,
                    json.dumps(result.cursor),
                    json.dumps(
                        {
                            "complete": result.complete,
                            "scanned": result.scanned,
                            "candidates": result.candidates,
                            "errors": result.errors,
                            "prefilter": result.prefilter,
                        },
                        ensure_ascii=False,
                    ),
                    time.time(),
                ),
            )

    def enqueue(self, key: str, sheet_destination: str, chat_ids: list[str]) -> None:
        with self.transaction():
            row = self.db.execute("SELECT ai_state FROM leads WHERE key=?", (key,)).fetchone()
            if not row or row[0] != "accepted":
                raise StorageError("Only accepted leads can be delivered")
            self.db.execute(
                "INSERT OR IGNORE INTO deliveries(lead_key,kind,destination,created) VALUES(?,?,?,?)",
                (key, "sheets", sheet_destination, time.time()),
            )
            sheet_id = self.db.execute(
                "SELECT id FROM deliveries WHERE lead_key=? AND kind='sheets' AND destination=?",
                (key, sheet_destination),
            ).fetchone()[0]
            for chat_id in dict.fromkeys(chat_ids):
                self.db.execute(
                    "INSERT OR IGNORE INTO deliveries(lead_key,kind,destination,depends_on,created) VALUES(?,?,?,?,?)",
                    (key, "telegram", str(chat_id), sheet_id, time.time()),
                )

    def enqueue_accepted(self, sheet_destination: str, chat_ids: list[str]) -> None:
        # Freeze routing at first enqueue; new recipients must not replay old leads.
        for row in self.db.execute(
            "SELECT key FROM leads WHERE ai_state='accepted' AND NOT EXISTS (SELECT 1 FROM deliveries WHERE lead_key=leads.key)"
        ).fetchall():
            self.enqueue(row[0], sheet_destination, chat_ids)

    def claim(
        self, kind: str, destinations: list[str], *, lease_seconds: int = 300, now: float | None = None
    ) -> dict | None:
        if not destinations:
            return None
        now = time.time() if now is None else now
        owner = uuid.uuid4().hex
        placeholders = ",".join("?" for _ in destinations)
        with self.transaction():
            row = self.db.execute(
                f"""SELECT d.* FROM deliveries d LEFT JOIN deliveries dependency ON d.depends_on=dependency.id
                WHERE d.kind=? AND d.destination IN ({placeholders})
                AND (d.state='pending' OR (d.state='leased' AND d.lease_until<=?)) AND d.next_at<=?
                AND (d.depends_on IS NULL OR dependency.state='done') ORDER BY d.id LIMIT 1""",
                (kind, *destinations, now, now),
            ).fetchone()
            if row is None:
                return None
            self.db.execute(
                "UPDATE deliveries SET state='leased',lease_owner=?,lease_until=? WHERE id=?",
                (owner, now + lease_seconds, row["id"]),
            )
            return {**dict(row), "lease_owner": owner}

    def reserve_row(self, job: dict, minimum_row: int, *, existing_row: int | None = None) -> int:
        with self.transaction():
            row = self.db.execute(
                "SELECT sheet_row FROM deliveries WHERE id=? AND state='leased' AND lease_owner=?",
                (job["id"], job["lease_owner"]),
            ).fetchone()
            if row is None:
                raise StorageError("Delivery lease is no longer owned")
            if row[0] is not None:
                return row[0]
            last = (
                self.db.execute(
                    "SELECT MAX(sheet_row) FROM deliveries WHERE kind='sheets' AND destination=?",
                    (job["destination"],),
                ).fetchone()[0]
                or 1
            )
            allocated = existing_row if existing_row is not None else max(minimum_row, last + 1)
            self.db.execute("UPDATE deliveries SET sheet_row=? WHERE id=?", (allocated, job["id"]))
            return allocated

    def delivered(self, job: dict, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        result = self.db.execute(
            "UPDATE deliveries SET state='done',delivered=?,lease_owner=NULL,lease_until=NULL,last_error=NULL WHERE id=? AND state='leased' AND lease_owner=?",
            (now, job["id"], job["lease_owner"]),
        )
        if result.rowcount != 1:
            raise StorageError("Cannot acknowledge a delivery after losing its lease")

    def delivery_failed(
        self,
        job: dict,
        error: str,
        *,
        permanent: bool = False,
        retry_seconds: float = 60,
        max_attempts: int = 8,
        now: float | None = None,
    ) -> None:
        now = time.time() if now is None else now
        attempts = job["attempts"] + 1
        result = self.db.execute(
            "UPDATE deliveries SET state=?,attempts=?,next_at=?,last_error=?,lease_owner=NULL,lease_until=NULL WHERE id=? AND state='leased' AND lease_owner=?",
            (
                "failed" if permanent or attempts >= max_attempts else "pending",
                attempts,
                now + retry_seconds,
                error,
                job["id"],
                job["lease_owner"],
            ),
        )
        if result.rowcount != 1:
            raise StorageError("Cannot change a delivery after losing its lease")

    def retry_failed(self) -> None:
        with self.transaction():
            self.db.execute(
                "UPDATE deliveries SET state='pending',attempts=0,next_at=0,last_error=NULL WHERE state='failed'"
            )
            self.db.execute(
                "UPDATE leads SET ai_state='pending',ai_attempts=0,ai_next_at=0,ai_error=NULL WHERE ai_state='review'"
            )

    def human_label(self, key: str, is_client: bool) -> None:
        self.lead(key)
        self.db.execute(
            "INSERT INTO human_reviews VALUES (?,?,?) ON CONFLICT(lead_key) DO UPDATE SET is_client=excluded.is_client,reviewed=excluded.reviewed",
            (key, int(is_client), time.time()),
        )

    def summary(self) -> dict:
        return {
            "leads": {
                r[0]: r[1] for r in self.db.execute("SELECT ai_state,COUNT(*) FROM leads GROUP BY ai_state")
            },
            "deliveries": {
                r[0]: r[1] for r in self.db.execute("SELECT state,COUNT(*) FROM deliveries GROUP BY state")
            },
            "sources": [
                {"id": r[0], **json.loads(r[1])}
                for r in self.db.execute("SELECT id,report FROM sources ORDER BY id")
            ],
        }

    def backup(self, destination: str | Path) -> None:
        destination = Path(destination)
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise StorageError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
        if os.name == "posix":
            os.chmod(destination, 0o600)
