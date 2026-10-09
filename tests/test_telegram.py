import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from telethon.errors import FloodWaitError

import telegram_parser
from storage import Store
from telegram_parser import _collect_from_channel, collect_leads_async


class Client:
    def __init__(self, number=300, fail=None):
        self.number, self.fail = number, fail
        self.disconnected = False
        self.options = []

    async def get_entity(self, channel):
        if channel == "bad":
            raise RuntimeError("secret must never be logged")
        return SimpleNamespace(id=12, title="Соседи", username="neighbors")

    def iter_messages(self, entity, **options):
        self.options.append(options)

        async def messages():
            yielded = 0
            for identity in range(self.number, 0, -1):
                if identity <= options.get("min_id", 0) or identity >= options.get("max_id", self.number + 1):
                    continue
                if self.fail and yielded == 10:
                    raise self.fail
                if yielded >= options["limit"]:
                    return
                yielded += 1
                yield SimpleNamespace(
                    id=identity,
                    date=datetime.now(UTC),
                    text="Нужен ремонт" if identity in [291, 50] else "Новости",
                    sender_id=5,
                )

        return messages()

    async def connect(self):
        pass

    async def is_user_authorized(self):
        return True

    async def disconnect(self):
        self.disconnected = True


def test_deep_telegram_and_budget_resume(cfg):
    cfg["telegram"].update(max_messages_per_channel=200, messages_per_run=50)
    client = Client()
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        asyncio.run(_collect_from_channel(client, "good", cfg, store, reports))
        assert not reports[0].complete and store.cursor("telegram:12")["resume_max_id"] == 101
        reports = []
        leads = asyncio.run(_collect_from_channel(client, "good", cfg, store, reports))
        assert reports[0].complete and any(lead.external_id == "12_50" for lead in leads)
        assert store.cursor("telegram:12") == {"high_watermark": 300}
        assert not asyncio.run(_collect_from_channel(client, "good", cfg, store))


def test_flood_wait_keeps_partial_and_resume(cfg):
    client = Client(fail=FloodWaitError(request=None, capture=123))
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        leads = asyncio.run(_collect_from_channel(client, "good", cfg, store, reports))
        assert leads and store.lead("telegram:12_291")
        assert reports[0].errors == ["FloodWait:123"] and store.cursor("telegram:12")["resume_max_id"] == 291


def test_bad_channel_does_not_cancel_good_and_disconnect(cfg, monkeypatch):
    cfg["telegram"].update(enabled=True, api_id=1, api_hash="test", channels=["bad", "good"])
    client = Client()
    monkeypatch.setattr(telegram_parser, "TelegramClient", lambda *args, **kwargs: client)
    reports = []
    assert asyncio.run(collect_leads_async(cfg, reports=reports))
    assert len(reports) == 2 and any(not report.complete for report in reports) and client.disconnected


def test_connect_failure_disconnects(cfg, monkeypatch):
    cfg["telegram"].update(enabled=True, api_id=1, api_hash="test")
    client = Client()

    async def failed():
        raise RuntimeError("connect failed")

    client.connect = failed
    monkeypatch.setattr(telegram_parser, "TelegramClient", lambda *args, **kwargs: client)
    with pytest.raises(RuntimeError):
        asyncio.run(collect_leads_async(cfg))
    assert client.disconnected


def test_disconnect_failure_preserves_collected_data(cfg, monkeypatch):
    cfg["telegram"].update(enabled=True, api_id=1, api_hash="test", channels=["good"])
    client = Client()

    async def failed_cleanup():
        raise RuntimeError("disconnect failed")

    client.disconnect = failed_cleanup
    monkeypatch.setattr(telegram_parser, "TelegramClient", lambda *args, **kwargs: client)
    reports = []
    assert asyncio.run(collect_leads_async(cfg, reports=reports))
    assert reports[-1].source_id == "telegram:cleanup" and not reports[-1].complete


def test_cancellation_flushes_page_and_does_not_advance_window(cfg):
    client = Client(fail=asyncio.CancelledError())
    with Store(cfg["storage"]["database"]) as store:

        async def cancelled():
            with pytest.raises(asyncio.CancelledError):
                await _collect_from_channel(client, "good", cfg, store)

        asyncio.run(cancelled())
        assert store.lead("telegram:12_291")
        assert store.cursor("telegram:12")["resume_max_id"] == 291
