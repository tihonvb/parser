"""Noninteractive public Telegram discovery; existing session required, no auto-join."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat

from configuration import CONFIG_PATH, load_config
from security import safe_error

QUERY_TOPICS = ["ремонт", "ремонт квартир", "строительство", "объявления услуги", "барахолка", "соседи"]


async def find_candidates(cfg, report=None):
    settings = cfg["telegram"]
    client = TelegramClient(
        settings["session_name"],
        int(settings["api_id"]),
        settings["api_hash"],
        flood_sleep_threshold=0,
        request_retries=1,
        connection_retries=1,
    )
    found = {}
    errors = []
    try:
        await asyncio.wait_for(client.connect(), timeout=30)
        if not await asyncio.wait_for(client.is_user_authorized(), timeout=15):
            raise RuntimeError("TelegramAuthorizationRequired")
        for topic in QUERY_TOPICS:
            query = cfg["general"]["city"] + " " + topic
            try:
                result = await asyncio.wait_for(client(SearchRequest(q=query, limit=50)), timeout=30)
                for chat in result.chats:
                    if isinstance(chat, (Channel, Chat)):
                        username = getattr(chat, "username", None)
                        found[chat.id] = {
                            "id": chat.id,
                            "source_id": f"telegram:{chat.id}",
                            "title": getattr(chat, "title", ""),
                            "username": "@" + username if username else "",
                            "participants": getattr(chat, "participants_count", None),
                            "url": f"https://t.me/{username}" if username else "",
                            "found_by_query": query,
                        }
            except Exception as error:
                errors.append(safe_error(error))
            await asyncio.sleep(1)
    finally:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=15)
        except Exception as error:
            errors.append("Cleanup:" + safe_error(error))
    if report is not None:
        report.update(
            complete=not bool(errors),
            errors=errors,
            groups=len(found),
            coverage="Telegram public search results only",
        )
    return sorted(
        found.values(),
        key=lambda item: (item["participants"] is not None, item["participants"] or 0),
        reverse=True,
    )


def save_csv(candidates, path="telegram_candidates.csv"):
    if candidates:
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(candidates[0]))
            writer.writeheader()
            writer.writerows(candidates)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--output", default="telegram_candidates.csv")
    args = parser.parse_args(argv)
    report = {}
    candidates = asyncio.run(find_candidates(load_config(args.config), report))
    save_csv(candidates, args.output)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
