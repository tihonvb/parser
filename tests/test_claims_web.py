"""Who takes which lead: Telegram buttons, storage and the small web page."""

import copy
import socket
import threading
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from lead_parser.core.models import Lead
from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, validate_config
from lead_parser.infrastructure.integrations.telegram import claims, notifications
from lead_parser.infrastructure.persistence.sqlite import Store, claim_token
from lead_parser.interfaces.web.app import make_handler

TOKEN = "t" * 24
REAL_CONNECT = socket.socket.connect  # captured before the network guard replaces it


def cfg():
    value = copy.deepcopy(DEFAULTS)
    value["notifications"]["telegram"].update(enabled=True, bot_token="x", chat_ids=["10", "20"])
    return value


def lead(n=1, **kw):
    return Lead(source="profi", external_id=str(n), date="2026-10-10", text=f"Нужен ремонт {n}", **kw)


@pytest.fixture
def store(tmp_path):
    with Store(tmp_path / "db.sqlite3") as value:
        value.ingest([lead(1), lead(2, url="https://b.profi.ru/abcdefghijklmnop")])
        yield value


def mark_sent(store, key):
    store.db.execute(
        "INSERT INTO deliveries(lead_key,kind,destination,state,created) VALUES (?,?,?,?,0)",
        (key, "telegram", "10", "done"),
    )


class FakeApi:
    def __init__(self):
        self.calls = []

    def __call__(self, cfg, method, payload, **kw):
        self.calls.append((method, payload))
        return {"ok": True, "result": []}


def press(user, data, message=True):
    query = {"id": "q", "from": {"id": user, "first_name": "Иван"}, "data": data}
    if message:
        query["message"] = {"message_id": 5, "chat": {"id": user}}
    return query


def test_buttons_are_added_only_when_enabled(monkeypatch):
    sent = []
    monkeypatch.setattr(
        notifications.requests,
        "post",
        lambda url, json, timeout: (
            sent.append(json) or type("R", (), {"status_code": 200, "json": lambda s: {"ok": True}})()
        ),
    )
    conf = cfg()
    notifications.send_to(conf, lead(), "10")
    assert "reply_markup" not in sent[-1]
    conf["notifications"]["telegram"]["claim_buttons"] = True
    notifications.send_to(conf, lead(), "10")
    data = [b["callback_data"] for b in sent[-1]["reply_markup"]["inline_keyboard"][0]]
    assert data == [f"t:{claim_token('profi:1')}", f"s:{claim_token('profi:1')}"]
    assert all(len(item.encode()) <= 64 for item in data)


def test_press_records_toggles_and_switches(store):
    api = FakeApi()
    token = claim_token("profi:1")
    assert claims.handle_callback(cfg(), store, press(10, f"t:{token}"), api)
    assert store.claim_of("profi:1", "10") == "taken"
    claims.handle_callback(cfg(), store, press(10, f"s:{token}"), api)
    assert store.claim_of("profi:1", "10") == "skipped"
    claims.handle_callback(cfg(), store, press(10, f"s:{token}"), api)
    assert store.claim_of("profi:1", "10") is None  # same button again clears the mark
    assert [m for m, _ in api.calls].count("editMessageReplyMarkup") == 3


def test_strangers_and_unknown_leads_are_refused(store):
    api = FakeApi()
    assert not claims.handle_callback(cfg(), store, press(99, f"t:{claim_token('profi:1')}"), api)
    assert not claims.handle_callback(cfg(), store, press(10, "t:" + "0" * 16), api)
    assert not claims.handle_callback(cfg(), store, press(10, "x:whatever"), api)
    assert store.db.execute("SELECT count(*) FROM claims").fetchone()[0] == 0
    assert all(method == "answerCallbackQuery" for method, _ in api.calls)


def test_two_users_can_both_take_and_polling_offset_is_kept(store):
    token = claim_token("profi:2")

    class Api(FakeApi):
        def __call__(self, cfg, method, payload, **kw):
            super().__call__(cfg, method, payload, **kw)
            if method == "getUpdates":
                return {
                    "ok": True,
                    "result": [
                        {"update_id": 7, "callback_query": press(10, f"t:{token}")},
                        {"update_id": 8, "callback_query": press(20, f"t:{token}")},
                        {"update_id": 9, "message": {"text": "hi"}},
                    ],
                }
            return {"ok": True}

    api = Api()
    assert claims.poll_once(cfg(), store, wait=0, api=api) == 2
    assert {c["user_id"] for c in _claims(store)} == {"10", "20"}
    assert store.db.execute("SELECT value FROM meta WHERE key=?", (claims.OFFSET_KEY,)).fetchone()[0] == "10"
    assert api.calls[0][1]["offset"] == 0


def _claims(store):
    return store.db.execute("SELECT * FROM claims").fetchall()


def test_board_filters(store):
    for key in ("profi:1", "profi:2"):
        mark_sent(store, key)
    store.ingest([lead(3)])  # never sent to Telegram: not on the board
    store.set_claim("profi:1", "10", "Иван", "taken")
    store.set_claim("profi:2", "10", "Иван", "skipped")
    assert {r["key"] for r in store.board()} == {"profi:1", "profi:2"}
    assert [r["key"] for r in store.board(status="taken")] == ["profi:1"]
    assert [r["key"] for r in store.board(status="free")] == ["profi:2"]


@pytest.fixture
def server(store, monkeypatch):
    def loopback_only(sock, address):
        assert address[0] == "127.0.0.1", "only the local test server may be contacted"
        return REAL_CONNECT(sock, address)

    monkeypatch.setattr(socket.socket, "connect", loopback_only)
    mark_sent(store, "profi:1")
    mark_sent(store, "profi:2")
    store.set_claim("profi:1", "10", "<b>Иван</b>", "taken")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(str(store.path), TOKEN, "Заявки"))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()
    httpd.server_close()


def get(url, **headers):
    return urlopen(Request(url, headers=headers), timeout=5)


def test_page_requires_the_key_and_escapes_content(server):
    with pytest.raises(HTTPError) as error:
        get(server + "/")
    assert error.value.code == 401
    with pytest.raises(HTTPError) as error:
        get(server + "/?key=wrong")
    assert error.value.code == 401
    page = get(f"{server}/?key={TOKEN}")
    body = page.read().decode()
    assert "Берёт: &lt;b&gt;Иван&lt;/b&gt;" in body and "<b>Иван</b>" not in body
    assert "Нужен ремонт 1" in body and "https://b.profi.ru/abcdefghijklmnop" in body
    assert "HttpOnly" in page.headers["Set-Cookie"] and page.headers["Cache-Control"] == "no-store"
    assert "Нужен ремонт 2" in get(server + "/", Cookie=f"key={TOKEN}").read().decode()


def test_page_status_filter(server):
    body = get(f"{server}/?key={TOKEN}&status=taken").read().decode()
    assert "Нужен ремонт 1" in body and "Нужен ремонт 2" not in body


def test_unknown_path_and_short_token(server):
    with pytest.raises(HTTPError) as error:
        get(f"{server}/admin?key={TOKEN}")
    assert error.value.code == 404
    with pytest.raises(ValueError):
        make_handler("db", "short", "t")


def test_config_validation():
    conf = cfg()
    conf["output"]["enabled"] = True
    conf["notifications"]["telegram"]["claim_buttons"] = "yes"
    with pytest.raises(ConfigError, match="claim_buttons"):
        validate_config(conf, require_access=False)
    conf = cfg()
    conf["output"]["enabled"] = True
    conf["web"]["port"] = 0
    with pytest.raises(ConfigError, match="web"):
        validate_config(conf, require_access=False)
