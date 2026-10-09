import json
from types import SimpleNamespace

import pytest
import requests

from common import Lead, unique_leads
from delivery import drain
from sheets_writer import HEADER, SchemaConflict, SheetsWriter, destination
from storage import StorageError, Store
from telegram_notify import NotificationError, send_to


class Worksheet:
    def __init__(self):
        self.rows = [HEADER.copy()]
        self.row_count = 1000
        self.calls = []
        self.lose_response = False

    def get_all_values(self):
        return [row.copy() for row in self.rows]

    def get(self, address):
        number = int(address.split(":")[0][1:])
        return [self.rows[number - 1]] if number <= len(self.rows) else []

    def update(self, *, values, range_name, value_input_option):
        assert value_input_option == "RAW"
        number = int(range_name.split(":")[0][1:])
        while len(self.rows) < number:
            self.rows.append([])
        self.rows[number - 1] = values[0].copy()
        self.calls.append(range_name)
        if self.lose_response:
            self.lose_response = False
            raise requests.Timeout("simulated token=must-not-be-logged")


def enable_output(cfg):
    cfg["output"]["enabled"] = True
    cfg["output"]["google_sheets"]["spreadsheet_id"] = "test-sheet"
    cfg["notifications"]["telegram"].update(enabled=True, bot_token="test-secret", chat_ids=["1", "2"])


def test_merge_one_key_rich_metadata(lead):
    second = Lead.from_dict(lead.to_dict())
    second.text += " под ключ"
    second.url = "https://vk.com/wall-12_9"
    second.phone = ""
    merged = unique_leads([lead, second])
    assert len(merged) == 1 and merged[0].phone == lead.phone and merged[0].url == second.url
    assert merged[0].text == second.text and lead.text != second.text


def test_restart_inbox_verdict_and_legacy_no_invention(cfg, lead, tmp_path):
    legacy = tmp_path / "seen.json"
    legacy.write_text(json.dumps({"seen": ["vk:-1_2"]}))
    with Store(cfg["storage"]["database"]) as store:
        assert store.import_legacy(legacy) == 1
        assert store.import_legacy(legacy) == 0
        assert store.ingest([lead, lead]) == 1
        store.classification(lead, "accepted", verdict={"is_client": True})
    with Store(cfg["storage"]["database"]) as store:
        assert store.pending_ai() == []
        assert store.lead(lead.dedupe_key()).text == lead.text
        assert not store.db.execute("SELECT 1 FROM leads WHERE key='vk:-1_2'").fetchone()
        corrupt = tmp_path / "bad.json"
        corrupt.write_text("{")
        with pytest.raises(StorageError):
            store.import_legacy(corrupt)


def test_pending_backoff_and_review(cfg, lead):
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        store.classification(lead, "pending", now=100, retry_seconds=60, max_attempts=2)
        assert not store.pending_ai(now=159)
        assert store.pending_ai(now=160)
        store.classification(lead, "pending", now=160, max_attempts=2)
        assert store.summary()["leads"] == {"review": 1}
        store.retry_failed()
        assert store.summary()["leads"] == {"pending": 1}


def test_claim_concurrent_expiry_and_dependency(cfg, lead):
    with Store(cfg["storage"]["database"]) as first, Store(cfg["storage"]["database"]) as second:
        first.ingest([lead])
        first.classification(lead, "accepted")
        first.enqueue(lead.dedupe_key(), "sheet", ["one"])
        assert not first.claim("telegram", ["one"], now=100)
        job = first.claim("sheets", ["sheet"], now=100, lease_seconds=10)
        assert not second.claim("sheets", ["sheet"], now=109)
        renewed = second.claim("sheets", ["sheet"], now=111)
        assert renewed["lease_owner"] != job["lease_owner"]
        with pytest.raises(StorageError):
            first.delivered(job)
        second.delivered(renewed)
        assert first.claim("telegram", ["one"], now=112)


def test_timeout_after_sheet_commit_reuses_row_and_verdict(cfg, lead):
    enable_output(cfg)
    ws = Worksheet()
    ws.lose_response = True
    lead.text = '=IMPORTXML("https://example.test","x")'
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        store.classification(lead, "accepted", verdict={"cached": True})
        calls = []

        def factory(config):
            return SheetsWriter(config, ws)

        assert (
            drain(cfg, store, writer_factory=factory, sender=lambda *args: calls.append(args[-1]))["errors"]
            == 1
        )
        assert calls == [] and len(ws.rows) == 2
        store.db.execute("UPDATE deliveries SET next_at=0")
        result = drain(cfg, store, writer_factory=factory, sender=lambda *args: calls.append(args[-1]))
        assert result == {"sheets": 1, "telegram": 2, "errors": 0}
        assert len(ws.rows) == 2 and ws.calls == ["A2:M2", "A2:M2"]
        assert ws.rows[1][4] == lead.text and ws.rows[1][5].startswith("+7")
        assert store.db.execute("SELECT ai_attempts FROM leads").fetchone()[0] == 1
        assert (
            drain(cfg, store, writer_factory=factory, sender=lambda *args: calls.append(args[-1]))["sheets"]
            == 0
        )


def test_recipient_partial_retry_and_reclassification_no_redelivery(cfg, lead):
    enable_output(cfg)
    ws = Worksheet()
    calls = []
    failure = [True]

    def sender(config, item, recipient):
        calls.append(recipient)
        if recipient == "2" and failure[0]:
            failure[0] = False
            raise NotificationError(429, retry_after=120)

    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        store.classification(lead, "accepted")
        drain(cfg, store, writer_factory=lambda config: SheetsWriter(config, ws), sender=sender)
        assert calls == ["1", "2"]
        store.db.execute("UPDATE deliveries SET next_at=0")
        drain(cfg, store, writer_factory=lambda config: SheetsWriter(config, ws), sender=sender)
        assert calls == ["1", "2", "2"]
        store.reclassify(lead.dedupe_key())
        store.classification(lead, "accepted")
        drain(cfg, store, writer_factory=lambda config: SheetsWriter(config, ws), sender=sender)
        assert calls == ["1", "2", "2"] and len(ws.rows) == 2


def test_conflicting_reserved_row_is_never_overwritten(cfg, lead):
    enable_output(cfg)
    ws = Worksheet()
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        store.classification(lead, "accepted")
        store.enqueue(lead.dedupe_key(), destination(cfg), [])
        job = store.claim("sheets", [destination(cfg)])
        store.reserve_row(job, 2)
        ws.rows.append(["personal note"])
        with pytest.raises(SchemaConflict):
            SheetsWriter(cfg, ws).deliver(store, job, lead)
        assert ws.rows[1] == ["personal note"]


def test_backup_snapshot_private(cfg, lead, tmp_path):
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        backup = tmp_path / "backup.sqlite3"
        store.backup(backup)
        assert backup.stat().st_mode & 0o777 == 0o600
        with Store(backup) as restored:
            assert restored.lead(lead.dedupe_key()).text == lead.text
        with pytest.raises(StorageError):
            store.backup(backup)


@pytest.mark.parametrize(
    "status,payload",
    [(200, {"ok": False, "error_code": 403}), (429, {"ok": False, "parameters": {"retry_after": 91}})],
)
def test_telegram_requires_api_ok(cfg, lead, monkeypatch, status, payload):
    enable_output(cfg)
    monkeypatch.setattr(
        requests, "post", lambda *args, **kwargs: SimpleNamespace(status_code=status, json=lambda: payload)
    )
    with pytest.raises(NotificationError) as captured:
        send_to(cfg, lead, "1")
    assert captured.value.permanent == (status == 200)
    if status == 429:
        assert captured.value.retry_after == 91


def test_adding_recipient_does_not_replay_historical_leads(cfg, lead):
    enable_output(cfg)
    ws = Worksheet()
    calls = []
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
        store.classification(lead, "accepted")
        drain(
            cfg,
            store,
            writer_factory=lambda config: SheetsWriter(config, ws),
            sender=lambda *args: calls.append(args[-1]),
        )
        cfg["notifications"]["telegram"]["chat_ids"].append("new-customer")
        cfg["output"]["google_sheets"]["spreadsheet_id"] = "new-destination"
        drain(
            cfg,
            store,
            writer_factory=lambda config: SheetsWriter(config, ws),
            sender=lambda *args: calls.append(args[-1]),
        )
        assert calls == ["1", "2"] and len(ws.rows) == 2
