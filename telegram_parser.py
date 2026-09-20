"""Мониторинг Telegram-каналов/групп через официальный MTProto API (Telethon).

Никакого браузера/антидетекта не требуется — это штатный протокол Telegram,
работающий от имени вашего аккаунта (при первом запуске Telethon попросит
код подтверждения в консоли, дальше сессия сохраняется в файл).

Каналы обрабатываются параллельно (с ограничением одновременных, см.
config.yaml -> telegram.max_concurrent_channels) — это важно, если каналов
много: последовательный обход по одному был бы намного медленнее.

Название канала/группы пишется в Lead.source_group — по нему потом
строится статистика (см. stats.py), какие каналы дают больше всего
реальных заказов.

telegram.channel_overrides позволяет сканировать отдельные "горячие"
каналы активнее остальных (больше сообщений за прогон и/или более
глубокий lookback_hours), не трогая общие messages_per_run/lookback_hours
— см. пример в config.yaml. Обычно это заполняют по подсказке из
stats.py, после того как накопилась статистика.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from telethon import TelegramClient
from telethon.errors import (
    ChannelPrivateError,
    FloodWaitError,
    UsernameInvalidError,
)

from common import Lead, extract_phone, matches_keywords


async def _collect_from_channel(client: TelegramClient, channel: str, cfg: dict) -> list[Lead]:
    tg_cfg = cfg["telegram"]
    general = cfg["general"]

    # "Горячие" каналы можно сканировать глубже/чаще остальных — см.
    # telegram.channel_overrides в config.yaml (обычно заполняется по
    # подсказке stats.py, когда накопится статистика по источникам).
    overrides = tg_cfg.get("channel_overrides") or {}
    chan_override = overrides.get(channel, {})
    lookback_hours = chan_override.get("lookback_hours", tg_cfg.get("lookback_hours", 48))
    messages_per_run = chan_override.get("messages_per_run", tg_cfg.get("messages_per_run", 200))

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    leads: list[Lead] = []

    try:
        entity = await client.get_entity(channel)
    except (ValueError, UsernameInvalidError):
        print(f"[telegram] Не удалось найти канал/группу: {channel} — пропускаю")
        return leads
    except ChannelPrivateError:
        print(f"[telegram] Нет доступа к приватному каналу: {channel} — пропускаю")
        return leads

    source_group = getattr(entity, "title", None) or str(channel)

    try:
        async for msg in client.iter_messages(entity, limit=messages_per_run):
            if not msg.text:
                continue
            if msg.date and msg.date < cutoff:
                break  # сообщения идут от новых к старым — дальше всё старее cutoff
            if not matches_keywords(msg.text, general["keywords"], general.get("exclude_keywords")):
                continue

            sender = None
            try:
                sender = await msg.get_sender()
            except Exception:
                pass
            author = ""
            if sender is not None:
                author = getattr(sender, "username", None) or " ".join(
                    filter(None, [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")])
                ).strip()

            channel_username = getattr(entity, "username", None)
            link = f"https://t.me/{channel_username}/{msg.id}" if channel_username else ""

            leads.append(
                Lead(
                    source="telegram",
                    external_id=f"{getattr(entity, 'id', channel)}_{msg.id}",
                    date=msg.date.isoformat() if msg.date else "",
                    author=author,
                    text=msg.text,
                    phone=extract_phone(msg.text) or "",
                    url=link,
                    source_group=source_group,
                )
            )
    except FloodWaitError as e:
        print(f"[telegram] Флуд-контроль Telegram: нужно подождать {e.seconds} сек. Пропускаю остаток канала {channel}.")

    return leads


async def collect_leads_async(cfg: dict) -> list[Lead]:
    tg_cfg = cfg["telegram"]
    if not tg_cfg.get("enabled", True):
        return []
    channels = tg_cfg.get("channels") or []
    if not channels:
        print("[telegram] Список каналов пуст (telegram.channels в config.yaml) — нечего парсить.")
        return []

    client = TelegramClient(tg_cfg["session_name"], int(tg_cfg["api_id"]), tg_cfg["api_hash"])
    await client.start()  # при первом запуске спросит номер телефона и код в консоли

    max_concurrent = max(1, tg_cfg.get("max_concurrent_channels", 5))
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _bounded(channel: str) -> list[Lead]:
        async with semaphore:
            return await _collect_from_channel(client, channel, cfg)

    all_leads: list[Lead] = []
    try:
        print(f"[telegram] Обрабатываю {len(channels)} каналов (до {max_concurrent} одновременно)...")
        results = await asyncio.gather(*(_bounded(ch) for ch in channels))
        for leads in results:
            all_leads.extend(leads)
    finally:
        await client.disconnect()

    return all_leads


def collect_leads(cfg: dict) -> list[Lead]:
    """Синхронная обёртка для вызова из main.py."""
    return asyncio.run(collect_leads_async(cfg))
