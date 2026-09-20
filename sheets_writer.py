"""Запись собранных лидов в Google Таблицу через сервисный аккаунт."""

from __future__ import annotations

from typing import Iterable

import gspread
from google.oauth2.service_account import Credentials

from common import Lead

HEADER = ["Дата", "Источник", "Группа/канал", "Автор", "Текст", "Телефон", "Цена", "Ссылка", "Вердикт ИИ"]

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]


def _get_worksheet(cfg: dict) -> gspread.Worksheet:
    sheets_cfg = cfg["output"]["google_sheets"]
    creds = Credentials.from_service_account_file(
        sheets_cfg["credentials_file"], scopes=SCOPES
    )
    client = gspread.authorize(creds)
    sh = client.open_by_key(sheets_cfg["spreadsheet_id"])
    title = sheets_cfg.get("worksheet_name", "Лиды")
    try:
        ws = sh.worksheet(title)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=title, rows=1000, cols=len(HEADER))
        ws.append_row(HEADER)
        return ws

    # Если лист есть, но пустой — добавим заголовок.
    if not ws.get_all_values():
        ws.append_row(HEADER)
    return ws


def append_leads(cfg: dict, leads: Iterable[Lead]) -> int:
    """Дописывает лиды в конец таблицы. Возвращает число добавленных строк."""
    leads = list(leads)
    if not leads:
        return 0
    ws = _get_worksheet(cfg)
    rows = [lead.as_row() for lead in leads]
    ws.append_rows(rows, value_input_option="USER_ENTERED")
    return len(rows)
