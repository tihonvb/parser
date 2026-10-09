"""Legacy spreadsheet row projection and optional source-report writes."""

from collections import defaultdict

import gspread

from lead_parser.infrastructure.integrations.google_sheets.gateway import _get_worksheet
from lead_parser.infrastructure.integrations.google_sheets.schema import HEADER


def aggregate(rows, header=HEADER):
    indexes = {name: index for index, name in enumerate(header)}
    counts = defaultdict(lambda: {"total": 0, "confirmed": 0, "human_reviewed": 0, "human_positive": 0})
    ai_used = False
    for number, row in enumerate(rows, 1):

        def value(name, row=row):
            index = indexes.get(name)
            return str(row[index]) if index is not None and index < len(row) else ""

        source, name, identity = value("Источник"), value("Группа/канал"), value("ID источника")
        key = (source, identity or f"unknown:row:{number}")
        item = counts[key]
        item.update(source=source, source_id=identity, group=name)
        item["total"] += 1
        verdict = value("Вердикт ИИ")
        ai_used |= bool(verdict)
        item["confirmed"] += int(verdict.startswith("клиент"))
    return sorted(
        counts.values(), key=lambda item: (item["confirmed"], item["total"], item["source_id"]), reverse=True
    ), ai_used


def write_stats_sheet(cfg, records, ai_used):
    spreadsheet = _get_worksheet(cfg).spreadsheet
    try:
        worksheet = spreadsheet.worksheet("Топ источников")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Топ источников", rows=max(10, len(records) + 1), cols=7)
    rows = [
        [
            "Источник",
            "ID источника",
            "Название",
            "Кандидаты",
            "Положительно ИИ",
            "Проверено человеком",
            "Положительно человеком",
        ]
    ]
    rows.extend(
        [
            [
                item[key]
                for key in (
                    "source",
                    "source_id",
                    "group",
                    "total",
                    "confirmed",
                    "human_reviewed",
                    "human_positive",
                )
            ]
            for item in records
        ]
    )
    if worksheet.row_count < len(rows):
        worksheet.add_rows(len(rows) - worksheet.row_count)
    worksheet.update(values=rows, range_name="A1", value_input_option="RAW")
    previous = worksheet.get_all_values()
    if len(previous) > len(rows):
        worksheet.batch_clear([f"A{len(rows) + 1}:G{len(previous)}"])
