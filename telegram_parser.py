"""Isolated channel scans with durable pages and stable message-ID cursors."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from pathlib import Path

from telethon import TelegramClient
from telethon.errors import FloodWaitError

from common import Lead, ScanResult, extract_phone, keyword_decision
from configuration import load_config
from security import safe_error


class TelegramAuthorizationRequired(RuntimeError):
    pass


async def _collect_from_channel(client, channel, cfg, store=None, reports=None) -> list[Lead]:
    settings = cfg["telegram"]
    result = ScanResult(f"telegram:ref:{channel}")
    leads = []
    page = []
    cursor = {}
    oldest = None

    def commit_page():
        if store:
            store.ingest(page)
            result.cursor = {**cursor, "resume_max_id": oldest}
            store.checkpoint(result)
        page.clear()

    try:
        async with asyncio.timeout(settings["channel_timeout_seconds"]):
            entity = await client.get_entity(channel)
            identity = str(entity.id)
            result.source_id = "telegram:" + identity
            overrides = settings.get("channel_overrides", {})
            override = overrides.get(identity, overrides.get(str(channel), {}))
            previous = store.cursor(result.source_id) if store else {}
            cursor = (
                previous
                if previous.get("resume_max_id")
                else {
                    "min_id": previous.get("high_watermark", 0),
                    "cutoff": time.time() - override.get("lookback_hours", settings["lookback_hours"]) * 3600
                    if not previous
                    else 0,
                    "scan_upper": 0,
                }
            )
            result.cursor = cursor.copy()
            result.window = {"cutoff": cursor.get("cutoff", 0), "min_message_id": cursor.get("min_id", 0)}
            budget = override.get("max_messages_per_channel", settings["max_messages_per_channel"])
            page_size = override.get("messages_per_run", settings["messages_per_run"])
            options = {"limit": budget + 1, "min_id": cursor.get("min_id", 0)}
            if cursor.get("resume_max_id"):
                options["max_id"] = cursor["resume_max_id"]
            async for message in client.iter_messages(entity, **options):
                if result.scanned >= budget:
                    result.complete = False
                    break
                stamp = message.date.timestamp() if message.date else None
                if stamp is not None and stamp < cursor.get("cutoff", 0):
                    break
                cursor["scan_upper"] = max(cursor.get("scan_upper", 0), message.id)
                oldest = message.id
                result.scanned += 1
                text = message.text or ""
                passed, reason = keyword_decision(
                    text, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
                )
                result.counted(reason)
                if passed:
                    username = getattr(entity, "username", None)
                    lead = Lead(
                        source="telegram",
                        external_id=f"{identity}_{message.id}",
                        date=message.date.isoformat() if message.date else "",
                        text=text,
                        phone=extract_phone(text) or "",
                        source_group=getattr(entity, "title", str(channel)),
                        source_group_id=result.source_id,
                        url=f"https://t.me/{username}/{message.id}" if username else "",
                        extra={"known_city": cfg["general"]["city"], "source_ref": str(channel)},
                    )
                    # Sender lookup is optional metadata; it must not block cursor advancement.
                    lead.author = str(getattr(message, "sender_id", "") or "")
                    leads.append(lead)
                    page.append(lead)
                    result.candidates += 1
                if result.scanned % page_size == 0:
                    result.complete = False
                    commit_page()
                    result.complete = True
            commit_page()
            result.cursor = (
                {"high_watermark": max(cursor.get("min_id", 0), cursor.get("scan_upper", 0))}
                if result.complete
                else {**cursor, "resume_max_id": oldest or cursor.get("resume_max_id")}
            )
    except asyncio.CancelledError:
        result.fail("Cancelled")
        commit_page()
        if reports is not None:
            reports.append(result)
        raise
    except FloodWaitError as error:
        result.fail(f"FloodWait:{error.seconds}")
        commit_page()
    except Exception as error:
        result.fail(safe_error(error))
        commit_page()
    finally:
        if store:
            store.checkpoint(result)
    if reports is not None:
        reports.append(result)
    return leads


async def collect_leads_async(cfg: dict, store=None, reports=None) -> list[Lead]:
    settings = cfg["telegram"]
    if not settings["enabled"]:
        return []
    session = Path(settings["session_name"])
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(
        str(session),
        int(settings["api_id"]),
        settings["api_hash"],
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=1,
        timeout=10,
    )
    try:
        await asyncio.wait_for(client.connect(), timeout=settings["channel_timeout_seconds"])
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
            raise TelegramAuthorizationRequired("Run telegram_parser.py login interactively")
        semaphore = asyncio.Semaphore(settings["max_concurrent_channels"])

        async def bounded(channel):
            async with semaphore:
                return await _collect_from_channel(client, channel, cfg, store, reports)

        batches = await asyncio.gather(
            *(bounded(channel) for channel in settings["channels"]), return_exceptions=True
        )
        return [lead for batch in batches if isinstance(batch, list) for lead in batch]
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=15)
        except Exception as error:
            result = ScanResult("telegram:cleanup")
            result.fail(safe_error(error))
            if reports is not None:
                reports.append(result)
            if store:
                store.checkpoint(result)
        finally:
            for path in session.parent.glob(session.name + ".session*"):
                if os.name == "posix":
                    path.chmod(0o600)


def collect_leads(cfg: dict, store=None, reports=None) -> list[Lead]:
    return asyncio.run(collect_leads_async(cfg, store, reports))


def main():
    parser = argparse.ArgumentParser(description="Explicit interactive Telegram login")
    parser.add_argument("command", choices=["login"])
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    cfg = load_config(args.config) if args.config else load_config()

    async def login():
        settings = cfg["telegram"]
        Path(settings["session_name"]).parent.mkdir(parents=True, exist_ok=True)
        client = TelegramClient(settings["session_name"], int(settings["api_id"]), settings["api_hash"])
        try:
            await client.start()
        finally:
            await client.disconnect()
            path = Path(settings["session_name"] + ".session")
            if path.exists() and os.name == "posix":
                path.chmod(0o600)

    asyncio.run(login())


if __name__ == "__main__":
    main()
