"""«Ресерчер» для Telegram: ищет публичные каналы/чаты по городу и теме
через тот же глобальный поиск, что использует сама строка поиска в
приложении Telegram, и выгружает список кандидатов для ручного отбора —
НИЧЕГО не добавляет в config.yaml и никуда не вступает автоматически.

Почему не автоматически: результаты поиска — это смесь актуальных
тематических каналов и случайного мусора (мёртвые чаты, другие города,
вообще не по теме). Массово и без разбора вступать в найденное одним
аккаунтом — верный способ словить ограничения от Telegram. Отбор глазами
занимает несколько минут и того стоит.

Запуск:
    python telegram_discovery.py

Результат: telegram_candidates.csv рядом со скриптами + список в консоли.
Понравившиеся @username переносите в config.yaml -> telegram.channels.
"""

from __future__ import annotations

import asyncio
import csv

from telethon import TelegramClient
from telethon.tl.functions.contacts import SearchRequest
from telethon.tl.types import Channel, Chat

from common import load_config

# Запросы для поиска — город + тема. Отдельно город без темы почти всегда
# бесполезен (Telegram-поиск найдёт что угодно), поэтому комбинируем.
QUERY_TOPICS = [
    "ремонт",
    "ремонт квартир",
    "ремонт под ключ",
    "строительство",
    "услуги ремонт",
    "мастер на час",
    "объявления услуги",
    "барахолка",
]


async def find_candidates(cfg: dict) -> list[dict]:
    tg_cfg = cfg["telegram"]
    city = cfg["general"].get("city", "Самара")

    client = TelegramClient(tg_cfg["session_name"], int(tg_cfg["api_id"]), tg_cfg["api_hash"])
    await client.start()

    seen_ids: set[int] = set()
    candidates: list[dict] = []

    try:
        for topic in QUERY_TOPICS:
            query = f"{city} {topic}"
            try:
                result = await client(SearchRequest(q=query, limit=50))
            except Exception as e:
                print(f"[telegram_discovery] Ошибка поиска по '{query}': {e}")
                continue

            for chat in result.chats:
                if not isinstance(chat, (Channel, Chat)):
                    continue
                cid = getattr(chat, "id", None)
                if cid is None or cid in seen_ids:
                    continue
                seen_ids.add(cid)

                username = getattr(chat, "username", None)
                is_megagroup = getattr(chat, "megagroup", False)
                candidates.append(
                    {
                        "id": cid,
                        "title": getattr(chat, "title", ""),
                        "username": f"@{username}" if username else "(без username, приватный/чат)",
                        "type": "чат/группа" if (isinstance(chat, Chat) or is_megagroup) else "канал",
                        "participants": getattr(chat, "participants_count", "") or "",
                        "url": f"https://t.me/{username}" if username else "",
                        "found_by_query": query,
                    }
                )
            await asyncio.sleep(1)  # не спамим глобальный поиск слишком часто
    finally:
        await client.disconnect()

    candidates.sort(key=lambda c: (c["participants"] == "", -(c["participants"] or 0)))
    return candidates


def save_csv(candidates: list[dict], path: str = "telegram_candidates.csv") -> None:
    if not candidates:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
        writer.writeheader()
        writer.writerows(candidates)


async def _main() -> None:
    cfg = load_config()
    candidates = await find_candidates(cfg)

    if not candidates:
        print("[telegram_discovery] Ничего не нашлось.")
        return

    save_csv(candidates)
    print(f"\n[telegram_discovery] Найдено {len(candidates)} каналов/чатов. Полный список — в telegram_candidates.csv")
    print("[telegram_discovery] Топ-30 (сначала с известным числом участников):\n")
    for c in candidates[:30]:
        print(f"  {c['username']:<30} {c['type']:<10} участников: {c['participants'] or '?'}  — {c['title']}")

    print(
        "\n[telegram_discovery] Каналы без username в списке — это приватные "
        "чаты/группы, обычный парсинг их не достанет (нужно приглашение).\n"
        "Понравившиеся @username добавьте в config.yaml -> telegram.channels."
    )


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
