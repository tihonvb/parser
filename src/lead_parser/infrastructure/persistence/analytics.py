"""Read immutable SQLite backups and optional non-sensitive request usage journals."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from lead_parser.application.analytics import AnalyticsDelivery, AnalyticsLead, Period


def read_snapshot(
    database: str | Path, period: Period
) -> tuple[list[AnalyticsLead], list[AnalyticsDelivery]]:
    path = Path(database).resolve()
    if not path.is_file():
        raise ValueError("Database snapshot does not exist; analytics never creates a database")
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError("Use a standalone SQLite backup, not a live database with WAL/journal files")
    # immutable forbids sidecar creation as well as DB writes; only standalone backups are supported.
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        if db.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("Unsupported snapshot schema: expected version 1")
        bounds = (period.start.timestamp(), period.end.timestamp())
        leads = []
        for row in db.execute(
            "SELECT l.key,l.payload,l.ai_state,l.first_seen,h.is_client FROM leads l "
            "LEFT JOIN human_reviews h ON h.lead_key=l.key "
            "WHERE l.first_seen>=? AND l.first_seen<? ORDER BY l.first_seen,l.key",
            bounds,
        ):
            payload = json.loads(row["payload"])
            if not isinstance(payload, dict):
                raise ValueError("Invalid lead payload in snapshot")
            leads.append(
                AnalyticsLead(
                    row["key"],
                    payload,
                    row["ai_state"],
                    row["first_seen"],
                    bool(row["is_client"]) if row["is_client"] is not None else None,
                )
            )
        deliveries = [
            AnalyticsDelivery(
                row["lead_key"], row["kind"], row["state"], row["delivered"], row["first_delivered"]
            )
            for row in db.execute(
                "SELECT d.lead_key,d.kind,d.state,d.delivered, "
                "(SELECT MIN(x.delivered) FROM deliveries x WHERE x.lead_key=d.lead_key "
                "AND x.state='done') AS first_delivered FROM deliveries d "
                "JOIN leads l ON l.key=d.lead_key "
                "WHERE (l.first_seen>=? AND l.first_seen<?) OR "
                "(d.state='done' AND d.delivered>=? AND d.delivered<?)",
                bounds + bounds,
            )
        ]
        return leads, deliveries
    except (sqlite3.DatabaseError, json.JSONDecodeError) as error:
        raise ValueError("Unreadable snapshot or incompatible schema/payload") from error
    finally:
        db.close()


def read_usage(path: str | Path) -> tuple[list[dict], bool, int]:
    path = Path(path)
    if not path.exists():
        return [], False, 0
    events, invalid = [], 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Expected usage object")
                events.append(event)
            except ValueError:
                invalid += 1
    return events, True, invalid
