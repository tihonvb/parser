"""Transactional lead inbox, source checkpoints and durable delivery outbox."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

from lead_parser.application.errors import StorageError
from lead_parser.application.models import ClassifiedLead, DeliveryPlan, LeadStatisticsRecord, ScanResult
from lead_parser.core.models import Lead
from lead_parser.core.policies import unique_leads
from lead_parser.infrastructure.persistence.schema import SCHEMA_VERSION as SCHEMA_VERSION
from lead_parser.infrastructure.persistence.schema import initialize_or_validate
from lead_parser.infrastructure.tracking import click_token


def claim_token(key: str) -> str:
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def _decode_lead(value: dict) -> Lead:
    return Lead(**{key: item for key, item in value.items() if key in Lead.__dataclass_fields__})


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        except FileExistsError:
            pass
        self.db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=30000")
        try:
            initialize_or_validate(self.db)
        except BaseException:
            self.db.close()
            raise
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
                payload = json.dumps(asdict(lead), ensure_ascii=False)
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
                        original = _decode_lead(json.loads(existing["payload"]))
                        merged = unique_leads([original, lead])[0]
                        self.db.execute(
                            "UPDATE leads SET payload=?,content_hash=?,last_seen=? WHERE key=?",
                            (
                                json.dumps(asdict(merged), ensure_ascii=False),
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
        return _decode_lead(json.loads(row["payload"]))

    def pending_ai(self, *, now: float | None = None, limit: int = 100) -> list[Lead]:
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT payload FROM leads WHERE ai_state='pending' AND ai_next_at<=? ORDER BY first_seen,key LIMIT ?",
            (now, limit),
        ).fetchall()
        return [_decode_lead(json.loads(row["payload"])) for row in rows]

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
        if state not in {"accepted", "rejected", "pending", "review"}:
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
                    json.dumps(asdict(lead), ensure_ascii=False),
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

    def save_classification(self, result: ClassifiedLead, *, max_attempts: int, retry_seconds: float) -> None:
        """Map explicit decisions to the existing schema and Sheets metadata."""
        lead = deepcopy(result.lead)
        decision = result.decision
        for key in ("ai_pending", "ai_error", "ai_is_client", "ai_confidence", "ai_reason"):
            lead.extra.pop(key, None)
        if decision.verdict is not None:
            lead.extra.update(
                ai_is_client=decision.verdict.is_client,
                ai_confidence=decision.verdict.confidence,
                ai_reason=decision.verdict.reason,
            )
        if decision.state == "pending":
            lead.extra.update(ai_pending=True, ai_error=decision.error)
        self.classification(
            lead,
            decision.state.value,
            verdict=lead.extra,
            error=decision.error or None,
            max_attempts=max_attempts,
            retry_seconds=retry_seconds,
        )

    def statistics_rows(self) -> Iterator[LeadStatisticsRecord]:
        # Stream potentially large texts; consumers retain only per-source aggregates.
        for row in self.db.execute(
            "SELECT l.payload,l.ai_state,h.is_client FROM leads l "
            "LEFT JOIN human_reviews h ON h.lead_key=l.key ORDER BY l.first_seen,l.key"
        ):
            yield LeadStatisticsRecord(
                _decode_lead(json.loads(row["payload"])),
                row["ai_state"],
                bool(row["is_client"]) if row["is_client"] is not None else None,
            )

    def enqueue_plan(self, plan: DeliveryPlan) -> None:
        if plan.primary is None:
            return
        # Freeze destinations at the first enqueue, atomically with the whole dependency graph.
        with self.transaction():
            keys = self.db.execute(
                "SELECT key FROM leads WHERE ai_state='accepted' AND NOT EXISTS "
                "(SELECT 1 FROM deliveries WHERE lead_key=leads.key)"
            ).fetchall()
            for (key,) in keys:
                primary = self.db.execute(
                    "INSERT INTO deliveries(lead_key,kind,destination,created) VALUES(?,?,?,?)",
                    (key, plan.primary.kind, plan.primary.destination, time.time()),
                ).lastrowid
                for target in dict.fromkeys(plan.dependents):
                    self.db.execute(
                        "INSERT OR IGNORE INTO deliveries(lead_key,kind,destination,depends_on,created) "
                        "VALUES(?,?,?,?,?)",
                        (key, target.kind, target.destination, primary, time.time()),
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
                            "coverage": result.coverage,
                            "window": result.window,
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
            return {**dict(row), "state": "leased", "lease_owner": owner, "lease_until": now + lease_seconds}

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
                {"id": r[0], "updated_at": r[2], **json.loads(r[1])}
                for r in self.db.execute("SELECT id,report,updated FROM sources ORDER BY id")
            ],
        }

    def lead_key_by_token(self, token: str) -> str | None:
        """Telegram callback data is limited to 64 bytes, so buttons carry a short digest of the lead key."""
        for (key,) in self.db.execute("SELECT key FROM leads"):
            if claim_token(key) == token:
                return key
        return None

    def set_claim(
        self, key: str, user_id: str, user_name: str, status: str, *, now: float | None = None
    ) -> str | None:
        """Mark a lead as taken/skipped by a user; pressing the same mark again clears it. Returns the new mark."""
        if status not in {"taken", "skipped"}:
            raise StorageError("Unknown claim status")
        with self.transaction():
            current = self.db.execute(
                "SELECT status FROM claims WHERE lead_key=? AND user_id=?", (key, user_id)
            ).fetchone()
            if current and current["status"] == status:
                self.db.execute("DELETE FROM claims WHERE lead_key=? AND user_id=?", (key, user_id))
                return None
            self.db.execute(
                "INSERT INTO claims VALUES (?,?,?,?,?) ON CONFLICT(lead_key,user_id) DO UPDATE SET "
                "user_name=excluded.user_name,status=excluded.status,updated=excluded.updated",
                (key, user_id, user_name, status, time.time() if now is None else now),
            )
            return status

    def claim_of(self, key: str, user_id: str) -> str | None:
        row = self.db.execute(
            "SELECT status FROM claims WHERE lead_key=? AND user_id=?", (key, user_id)
        ).fetchone()
        return row["status"] if row else None

    def find_link(self, token: str, secret: str) -> tuple[Lead, str] | None:
        """The lead and recipient behind a tracking token (tokens are HMACs, so they are recomputed)."""
        for row in self.db.execute(
            "SELECT d.lead_key, d.destination, l.payload FROM deliveries d JOIN leads l ON l.key=d.lead_key "
            "WHERE d.kind='telegram'"
        ):
            if hmac.compare_digest(click_token(secret, row["lead_key"], row["destination"]), token):
                return _decode_lead(json.loads(row["payload"])), row["destination"]
        return None

    def record_click(self, key: str, chat_id: str, *, now: float | None = None) -> bool:
        """Count a click; True when it is the recipient's first one for this lead."""
        now = time.time() if now is None else now
        first = (
            self.db.execute("SELECT 1 FROM clicks WHERE lead_key=? AND chat_id=?", (key, chat_id)).fetchone()
            is None
        )
        self.db.execute(
            "INSERT INTO clicks VALUES (?,?,?,?,1) ON CONFLICT(lead_key,chat_id) DO UPDATE SET "
            "last_at=excluded.last_at,count=count+1",
            (key, chat_id, now, now),
        )
        return first

    def board(self, *, status: str = "", limit: int = 200) -> list[dict]:
        """Leads that were sent to Telegram, newest first, with everyone's marks."""
        rows = self.db.execute(
            """SELECT l.key, l.payload, l.first_seen FROM leads l
               WHERE EXISTS (SELECT 1 FROM deliveries d WHERE d.lead_key=l.key AND d.kind='telegram' AND d.state='done')
               ORDER BY l.first_seen DESC"""
        ).fetchall()
        marks: dict[str, list[dict]] = {}
        for claim in self.db.execute(
            "SELECT lead_key,user_id,user_name,status,updated FROM claims ORDER BY updated"
        ):
            marks.setdefault(claim["lead_key"], []).append(dict(claim))
        sent: dict[str, list[str]] = {}
        for d in self.db.execute(
            "SELECT lead_key,destination FROM deliveries WHERE kind='telegram' AND state='done' ORDER BY id"
        ):
            sent.setdefault(d["lead_key"], []).append(d["destination"])
        clicks: dict[tuple[str, str], dict] = {
            (c["lead_key"], c["chat_id"]): dict(c) for c in self.db.execute("SELECT * FROM clicks")
        }
        result = []
        for row in rows:
            lead = _decode_lead(json.loads(row["payload"]))
            current = marks.get(row["key"], [])
            recipients = [
                {"chat_id": chat, "click": clicks.get((row["key"], chat))}
                for chat in sent.get(row["key"], [])
            ]
            if status == "unopened" and all(r["click"] for r in recipients):
                continue
            if status == "taken" and not any(m["status"] == "taken" for m in current):
                continue
            if status == "free" and any(m["status"] == "taken" for m in current):
                continue
            result.append(
                {
                    "key": row["key"],
                    "lead": lead,
                    "first_seen": row["first_seen"],
                    "claims": current,
                    "recipients": recipients,
                }
            )
            if len(result) >= limit:
                break
        return result

    def backup(self, destination: str | Path) -> None:
        destination = Path(destination)
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise StorageError("Backup destination must be a new file")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(destination) as target:
            self.db.backup(target)
        if os.name == "posix":
            os.chmod(destination, 0o600)
