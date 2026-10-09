"""RAW writes to durable, preallocated rows. Never append after an uncertain response."""

from __future__ import annotations

from collections.abc import Callable

import gspread
from google.oauth2.service_account import Credentials

from lead_parser.application.errors import DeliveryError
from lead_parser.application.models import DeliveryJob
from lead_parser.application.ports import RowReservations
from lead_parser.core.models import Lead
from lead_parser.infrastructure.integrations.google_sheets.schema import HEADER, lead_to_row
from lead_parser.infrastructure.security import delivery_error

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive.file"]


class SchemaConflict(DeliveryError):
    def __init__(self, message: str):
        super().__init__(message, permanent=True)


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
            raise SchemaConflict(
                "Sheet schema differs; run lead-parser migrate-sheet dry-run and backed-up migration"
            )
        self.minimum_row = len(rows) + 1
        self.existing: dict[str, int] = {}
        for index, row in enumerate(rows[1:], 2):
            key = row[9] if len(row) > 9 else ""
            if key:
                if key in self.existing:
                    raise SchemaConflict("Duplicate lead IDs in sheet; resolve before delivery")
                self.existing[key] = index

    def find_existing_row(self, key: str) -> int | None:
        return self.existing.get(key)

    def write_row(self, lead: Lead, row: int) -> None:
        key = lead.dedupe_key()
        current = self.ws.get(f"A{row}:M{row}")
        current = current[0] if current else []
        if any(current) and (len(current) < 10 or current[9] != key):
            raise SchemaConflict("Reserved row contains another record; restore sheet order before retry")
        if row > self.ws.row_count:
            self.ws.add_rows(row - self.ws.row_count)
        values = lead_to_row(lead)
        # Sheets limits cells to 50,000 characters; the complete original remains in SQLite.
        values[4] = values[4][:49950] + ("\n[Полный текст в SQLite]" if len(values[4]) > 49950 else "")
        self.ws.update(values=[values], range_name=f"A{row}:M{row}", value_input_option="RAW")
        self.existing[key] = row
        self.minimum_row = max(self.minimum_row, row + 1)


class SheetsGateway:
    """Deliver through a reserved, restart-stable row without knowing SQLite."""

    def __init__(self, writer_factory: Callable[[], SheetsWriter], reservations: RowReservations):
        self.writer_factory = writer_factory
        self.reservations = reservations
        self.writer: SheetsWriter | None = None

    def deliver(self, lead: Lead, job: DeliveryJob) -> None:
        try:
            if self.writer is None:
                self.writer = self.writer_factory()
            row = self.reservations.reserve_row(
                job,
                self.writer.minimum_row,
                existing_row=self.writer.find_existing_row(lead.dedupe_key()),
            )
            self.writer.write_row(lead, row)
        except DeliveryError:
            raise
        except Exception as error:
            raise delivery_error(error) from error
