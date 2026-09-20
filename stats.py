"""Отчёт: из каких пабликов/каналов чаще всего приходят реальные заказы.

Читает уже собранную таблицу лидов (тот же spreadsheet, что и sheets_writer),
группирует строки по колонкам "Источник" + "Группа/канал" и считает:
  - всего лидов из этого паблика/канала;
  - из них подтверждено ИИ-фильтром как реальный заказ ("Вердикт ИИ"
    начинается с "клиент") — если ИИ-фильтр включён (ai_filter.enabled: true
    в config.yaml), иначе колонка "Вердикт ИИ" пустая и ранжирование идёт
    просто по числу лидов.

Результат:
  1. Таблица в консоли, отсортированная по числу подтверждённых заказов
     (или просто по числу лидов, если ИИ-фильтр не запускался).
  2. Тот же рейтинг записывается на отдельный лист "Топ источников" в той
     же Google Таблице (лист пересоздаётся при каждом запуске).
  3. Готовый YAML-сниппет для config.yaml -> vk.group_overrides /
     telegram.channel_overrides по топ-N источникам — чтобы сканировать
     их активнее (больше страниц/сообщений за прогон). Для VK id
     подставляется автоматически, если получится сопоставить его с
     vk.group_ids через VK API; для Telegram укажите сами username/id
     канала как он записан в telegram.channels — колонка "Группа/канал"
     хранит человекочитаемое название, а не технический идентификатор.

Запуск:
    python stats.py
    python stats.py --top 10
"""

from __future__ import annotations

import argparse
from collections import defaultdict

import gspread
from google.oauth2.service_account import Credentials

from common import load_config

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]

STATS_SHEET_NAME = "Топ источников"

IDX_SOURCE = 1        # "Источник"
IDX_GROUP = 2          # "Группа/канал"
IDX_AI_VERDICT = 8    # "Вердикт ИИ"


def _open_spreadsheet(cfg: dict):
    sheets_cfg = cfg["output"]["google_sheets"]
    creds = Credentials.from_service_account_file(sheets_cfg["credentials_file"], scopes=SCOPES)
    client = gspread.authorize(creds)
    return client.open_by_key(sheets_cfg["spreadsheet_id"]), sheets_cfg.get("worksheet_name", "Лиды")


def _load_rows(cfg: dict) -> list[list[str]]:
    sh, title = _open_spreadsheet(cfg)
    ws = sh.worksheet(title)
    values = ws.get_all_values()
    if len(values) <= 1:
        return []
    return values[1:]  # без заголовка


def aggregate(rows: list[list[str]]) -> tuple[list[dict], bool]:
    """Возвращает (список записей на группу, использовался_ли_ИИ_фильтр)."""
    counts: dict[tuple[str, str], dict] = defaultdict(lambda: {"total": 0, "confirmed": 0})
    ai_used = False

    for row in rows:
        if len(row) <= max(IDX_SOURCE, IDX_GROUP, IDX_AI_VERDICT):
            row = row + [""] * (max(IDX_SOURCE, IDX_GROUP, IDX_AI_VERDICT) + 1 - len(row))
        source = row[IDX_SOURCE] or "?"
        group = row[IDX_GROUP] or "(без названия)"
        verdict = row[IDX_AI_VERDICT] or ""
        key = (source, group)
        counts[key]["total"] += 1
        if verdict:
            ai_used = True
            if verdict.startswith("клиент"):
                counts[key]["confirmed"] += 1

    records = [
        {"source": source, "group": group, "total": v["total"], "confirmed": v["confirmed"]}
        for (source, group), v in counts.items()
    ]

    sort_key = (lambda r: (r["confirmed"], r["total"])) if ai_used else (lambda r: r["total"])
    records.sort(key=sort_key, reverse=True)
    return records, ai_used


def print_report(records: list[dict], ai_used: bool, top: int) -> None:
    if not records:
        print("[stats] В таблице пока нет лидов — нечего анализировать.")
        return

    print(f"\n[stats] Рейтинг источников ({'по подтверждённым ИИ заказам' if ai_used else 'по числу лидов — ИИ-фильтр ещё не запускался'}):\n")
    header = f"{'Источник':<10} {'Всего':>7} {'Подтв.':>7}  Группа/канал"
    print(header)
    print("-" * len(header))
    for r in records[:max(top, len(records))]:
        print(f"{r['source']:<10} {r['total']:>7} {r['confirmed']:>7}  {r['group']}")


def write_stats_sheet(cfg: dict, records: list[dict], ai_used: bool) -> None:
    sh, _ = _open_spreadsheet(cfg)
    try:
        ws = sh.worksheet(STATS_SHEET_NAME)
        sh.del_worksheet(ws)
    except gspread.WorksheetNotFound:
        pass
    ws = sh.add_worksheet(title=STATS_SHEET_NAME, rows=max(len(records) + 1, 10), cols=4)
    header = ["Источник", "Группа/канал", "Всего лидов", "Подтверждено ИИ" if ai_used else "Подтверждено ИИ (фильтр не запускался)"]
    rows = [header] + [[r["source"], r["group"], r["total"], r["confirmed"]] for r in records]
    ws.update(rows, value_input_option="USER_ENTERED")
    print(f"[stats] Рейтинг записан на лист '{STATS_SHEET_NAME}' в той же таблице.")


def _resolve_vk_name_to_id(cfg: dict) -> dict[str, int]:
    """Пытается сопоставить название VK-группы с её id из vk.group_ids —
    чтобы подставить готовый id в YAML-сниппет для group_overrides.
    Если VK недоступен/токен не настроен — просто возвращает {}."""
    vk_cfg = cfg.get("vk") or {}
    group_ids = vk_cfg.get("group_ids") or []
    token = vk_cfg.get("access_token")
    if not group_ids or not token or "ВСТАВЬТЕ" in str(token).upper():
        return {}
    try:
        import vk_api
        from vk_api.exceptions import ApiError

        session = vk_api.VkApi(token=token, api_version=vk_cfg.get("api_version", "5.199"))
        api = session.get_api()
        resp = api.groups.getById(group_ids=",".join(str(abs(int(g))) for g in group_ids))
        items = resp.get("groups", resp) if isinstance(resp, dict) else resp
        return {g.get("name", ""): g["id"] for g in items}
    except Exception as e:  # ApiError, network issues, отсутствие модуля и т.п.
        print(f"[stats] Не удалось сопоставить названия VK-групп с id (не критично): {e}")
        return {}


def print_overrides_snippet(cfg: dict, records: list[dict], ai_used: bool, top: int) -> None:
    ranked = [r for r in records if (r["confirmed"] if ai_used else r["total"]) > 0][:top]
    if not ranked:
        return

    vk_name_to_id = _resolve_vk_name_to_id(cfg)

    vk_lines = []
    tg_lines = []
    for r in ranked:
        if r["source"] == "vk":
            gid = vk_name_to_id.get(r["group"])
            key = gid if gid is not None else f"<id группы '{r['group']}' — см. vk_candidates.csv или config.yaml>"
            vk_lines.append(f"    {key}:\n      max_pages: 3  # было по умолчанию {cfg.get('vk', {}).get('max_pages_per_query', 1)}")
        elif r["source"] == "telegram":
            tg_lines.append(
                f"    \"<username/id канала '{r['group']}' как в telegram.channels>\":\n"
                f"      messages_per_run: 400  # было по умолчанию {cfg.get('telegram', {}).get('messages_per_run', 200)}\n"
                f"      lookback_hours: 72     # было по умолчанию {cfg.get('telegram', {}).get('lookback_hours', 48)}"
            )

    if not vk_lines and not tg_lines:
        return

    print(f"\n[stats] Готовый сниппет для config.yaml — сканировать топ-{top} источников активнее:\n")
    if vk_lines:
        print("vk:")
        print("  group_overrides:")
        print("\n".join(vk_lines))
    if tg_lines:
        print("telegram:")
        print("  channel_overrides:")
        print("\n".join(tg_lines))
    print(
        "\n[stats] Для VK id подставлен автоматически там, где получилось сопоставить "
        "название с vk.group_ids. Для Telegram впишите username/id канала вручную "
        "(таблица хранит только название канала, а не технический идентификатор)."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--top", type=int, default=5, help="сколько лучших источников включить в сниппет overrides (по умолчанию 5)")
    args = parser.parse_args()

    cfg = load_config()
    rows = _load_rows(cfg)
    records, ai_used = aggregate(rows)

    print_report(records, ai_used, args.top)
    if records:
        write_stats_sheet(cfg, records, ai_used)
        print_overrides_snippet(cfg, records, ai_used, args.top)


if __name__ == "__main__":
    main()
