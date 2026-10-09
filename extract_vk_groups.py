"""Извлекает ID VK-групп, которые реально засветились в таблице лидов
(из ссылок на посты), и готовит сниппет для vk.group_ids в config.yaml.

Берёт ВСЕ VK-лиды из таблицы (а не только подтверждённые ИИ как "клиент") —
это как раз список групп, которые нашёл глобальный поиск по ключевым
словам за время сбора; дальше по ним можно искать через wall.search по
конкретной группе (это нужно для сервера — там глобальный поиск недоступен,
см. claude/status.md про error 1051).

Запуск:
    .\\venv\\Scripts\\python.exe extract_vk_groups.py
"""

from __future__ import annotations

import re
from collections import Counter

import gspread
from google.oauth2.service_account import Credentials

from common import load_config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

IDX_SOURCE = 1       # "Источник"
IDX_GROUP = 2        # "Группа/канал"
IDX_URL = 7          # "Ссылка"
IDX_AI_VERDICT = 8   # "Вердикт ИИ"

WALL_RE = re.compile(r"vk\.com/wall-(\d+)_")


def main() -> None:
    cfg = load_config()
    sheets_cfg = cfg["output"]["google_sheets"]
    creds = Credentials.from_service_account_file(sheets_cfg["credentials_file"], scopes=SCOPES)
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheets_cfg["spreadsheet_id"])
    ws = sh.worksheet(sheets_cfg.get("worksheet_name", "Лиды"))
    rows = ws.get_all_values()[1:]  # без заголовка

    counts: Counter = Counter()
    confirmed: Counter = Counter()
    names: dict[int, str] = {}

    max_idx = max(IDX_SOURCE, IDX_GROUP, IDX_URL, IDX_AI_VERDICT)
    for row in rows:
        if len(row) <= max_idx:
            row = row + [""] * (max_idx + 1 - len(row))
        if row[IDX_SOURCE] != "vk":
            continue
        m = WALL_RE.search(row[IDX_URL] or "")
        if not m:
            continue
        gid = int(m.group(1))
        counts[gid] += 1
        if row[IDX_GROUP]:
            names[gid] = row[IDX_GROUP]
        if (row[IDX_AI_VERDICT] or "").startswith("клиент"):
            confirmed[gid] += 1

    if not counts:
        print("[extract] В таблице пока нет ни одного VK-лида со ссылкой на пост — рано собирать группы.")
        return

    print(f"[extract] Найдено {len(counts)} уникальных VK-групп в таблице:\n")
    for gid, total in counts.most_common():
        name = names.get(gid, "(без названия)")
        print(f"  {gid:>10}  всего={total:<4} подтверждено_клиентов={confirmed.get(gid, 0):<3} {name}")

    print("\n[extract] Готовый сниппет для vk.group_ids (вставить в config.yaml — и локально, и на сервере):\n")
    print("vk:")
    print("  group_ids:")
    for gid, _ in counts.most_common():
        name = names.get(gid, "")
        comment = f"  # {name}" if name else ""
        print(f"    - {gid}{comment}")


if __name__ == "__main__":
    main()
