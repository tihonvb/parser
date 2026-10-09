"""Bounded retries with leases and per-destination delivery acknowledgements."""

from __future__ import annotations

from security import safe_error
from sheets_writer import SchemaConflict, SheetsWriter, destination
from telegram_notify import NotificationError, _collect_chat_ids, send_to


def drain(cfg: dict, store, *, writer_factory=SheetsWriter, sender=send_to) -> dict:
    settings = cfg["delivery"]
    sheet_dest = destination(cfg) if cfg["output"]["enabled"] else None
    notification = cfg["notifications"]["telegram"]
    recipients = _collect_chat_ids(notification) if notification["enabled"] else []
    if sheet_dest:
        store.enqueue_accepted(sheet_dest, recipients)
    counts = {"sheets": 0, "telegram": 0, "errors": 0}
    writer = None
    for kind, destinations in (("sheets", [sheet_dest] if sheet_dest else []), ("telegram", recipients)):
        for _ in range(settings["jobs_per_run"]):
            job = store.claim(kind, destinations, lease_seconds=settings["lease_seconds"])
            if job is None:
                break
            try:
                lead = store.lead(job["lead_key"])
                if kind == "sheets":
                    if writer is None:
                        writer = writer_factory(cfg)
                    writer.deliver(store, job, lead)
                else:
                    sender(cfg, lead, job["destination"])
                store.delivered(job)
                counts[kind] += 1
            except Exception as error:
                retry = min(
                    settings["retry_max_seconds"],
                    settings["retry_base_seconds"] * 2 ** min(job["attempts"], 16),
                )
                retry = max(retry, getattr(error, "retry_after", 0))
                status = getattr(getattr(error, "response", None), "status_code", None)
                permanent = (
                    isinstance(error, SchemaConflict)
                    or (isinstance(error, NotificationError) and error.permanent)
                    or status in {400, 401, 403, 404}
                )
                store.delivery_failed(
                    job,
                    safe_error(error),
                    permanent=permanent,
                    retry_seconds=retry,
                    max_attempts=settings["max_attempts"],
                )
                counts["errors"] += 1
    return counts
