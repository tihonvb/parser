"""Одноразовый скрипт: архивирует текущий 'грязный' лист 'Лиды' (с разным
числом колонок в разных строках, накопившимся за время разработки) и
создаёт новый чистый лист 'Лиды' с правильными 9 колонками и закреплённым
заголовком. Дальше main.py продолжит писать уже в новый чистый лист —
никаких изменений в config.yaml/коде для этого не нужно.

Запуск:
    .\\venv\\Scripts\\python.exe fix_sheet.py
"""
import datetime

import gspread
from google.oauth2.service_account import Credentials

import yaml

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive.file",
]
HEADER = ["Дата", "Источник", "Группа/канал", "Автор", "Текст", "Телефон", "Цена", "Ссылка", "Вердикт ИИ"]

with open("config.yaml", "r", encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

sheets_cfg = cfg["output"]["google_sheets"]
creds = Credentials.from_service_account_file(sheets_cfg["credentials_file"], scopes=SCOPES)
client = gspread.authorize(creds)
sh = client.open_by_key(sheets_cfg["spreadsheet_id"])
title = sheets_cfg.get("worksheet_name", "Лиды")

print("Листы сейчас:", [ws.title for ws in sh.worksheets()])

old = sh.worksheet(title)
row_count = len(old.get_all_values())
print(f"В текущем листе '{title}' строк: {row_count}")

archive_name = f"{title}_архив_{datetime.date.today().isoformat()}"
old.update_title(archive_name)
print(f"Переименовал старый лист в '{archive_name}' (старые данные никуда не делись, просто убраны с глаз)")

new_ws = sh.add_worksheet(title=title, rows=2000, cols=len(HEADER))
new_ws.append_row(HEADER)
new_ws.format("A1:I1", {"textFormat": {"bold": True}})
new_ws.freeze(rows=1)
print(f"Создал новый чистый лист '{title}' с заголовком и закреплённой первой строкой")

print("Листы теперь:", [ws.title for ws in sh.worksheets()])
