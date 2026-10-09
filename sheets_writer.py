"""RAW writes to durable, preallocated rows. Never append after an uncertain response."""

from __future__ import annotations

import gspread
from google.oauth2.service_account import Credentials

from common import Lead

LEGACY_HEADER = [
    "Дата",
    "Источник",
    "Группа/канал",
    "Автор",
    "Текст",
    "Телефон",
    "Цена",
    "Ссылка",
    "Вердикт ИИ",
]
HEADER = LEGACY_HEADER + ["ID лида", "ID источника", "Обнаружено", "Версия промпта"]
SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]


class SchemaConflict(RuntimeError):
    pass


def destination(cfg: dict) -> str:
    settings = cfg["output"]["google_sheets"]
    return settings["spreadsheet_id"] + ":" + settings["worksheet_name"]


def _get_worksheet(cfg: dict, *, create=True):
    settings = cfg["output"]["google_sheets"]
    credentials = Credentials.from_service_account_file(settings["credentials_file"], scopes=SCOPES)
    client = gspread.authorize(credentials)
    client.set_timeout((10, settings["timeout_seconds"]))
    spreadsheet = client.open_by_key(settings["spreadsheet_id"])
    try:
        worksheet = spreadsheet.worksheet(settings["worksheet_name"])
    except gspread.WorksheetNotFound:
        if not create:
            raise
        worksheet = spreadsheet.add_worksheet(title=settings["worksheet_name"], rows=1000, cols=len(HEADER))
    return worksheet


class SheetsWriter:
    def __init__(self, cfg: dict, worksheet=None):
        self.ws = worksheet if worksheet is not None else _get_worksheet(cfg)
        rows = self.ws.get_all_values()
        if not rows:
            if self.ws.col_count < len(HEADER):
                self.ws.add_cols(len(HEADER) - self.ws.col_count)
            self.ws.update(values=[HEADER], range_name="A1:M1", value_input_option="RAW")
            rows = [HEADER]
        if rows[0][: len(HEADER)] != HEADER:
            raise SchemaConflict("Sheet schema differs; run fix_sheet.py dry-run and backed-up migration")
        self.minimum_row = len(rows) + 1
        self.existing: dict[str, int] = {}
        for index, row in enumerate(rows[1:], 2):
            key = row[9] if len(row) > 9 else ""
            if key:
                if key in self.existing:
                    raise SchemaConflict("Duplicate lead IDs in sheet; resolve before delivery")
                self.existing[key] = index

    def deliver(self, store, job: dict, lead: Lead) -> None:
        key = lead.dedupe_key()
        existing = self.existing.get(key)
        row = store.reserve_row(job, self.minimum_row, existing_row=existing)
        current = self.ws.get(f"A{row}:M{row}")
        current = current[0] if current else []
        if any(current) and (len(current) < 10 or current[9] != key):
            raise SchemaConflict("Reserved row contains another record; restore sheet order before retry")
        if row > self.ws.row_count:
            self.ws.add_rows(row - self.ws.row_count)
        values = lead.as_row()
        # Sheets limits cells to 50,000 characters; the complete original remains in SQLite.
        values[4] = values[4][:49950] + ("\n[Полный текст в SQLite]" if len(values[4]) > 49950 else "")
        self.ws.update(values=[values], range_name=f"A{row}:M{row}", value_input_option="RAW")
        self.existing[key] = row
        self.minimum_row = max(self.minimum_row, row + 1)


def append_leads(cfg: dict, leads) -> int:
    """Compatibility entry point using the same durable outbox as main."""
    from delivery import drain
    from storage import Store

    with Store(cfg["storage"]["database"]) as store:
        leads = list(leads)
        store.ingest(leads)
        for lead in leads:
            store.classification(lead, "accepted")
            store.enqueue(lead.dedupe_key(), destination(cfg), [])
        return drain(cfg, store).get("sheets", 0)
