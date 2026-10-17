"""Tiny read-only page: which leads were sent and who takes them. Standard library only."""

from __future__ import annotations

import hmac
import html
import time
from datetime import datetime
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.infrastructure.tracking import looks_like_bot

STATUSES = {"": "Все", "unopened": "Не открыты", "free": "Свободные", "taken": "Взятые"}
STYLE = """
:root{color-scheme:light dark;--bg:#fff;--fg:#1c1c1e;--mut:#6b6b70;--line:#e3e3e8;--ok:#1a7f37;--chip:#f2f2f5}
@media(prefers-color-scheme:dark){:root{--bg:#161618;--fg:#f2f2f5;--mut:#9a9aa1;--line:#2c2c30;--ok:#4cc96b;--chip:#232326}}
body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.45 system-ui,sans-serif}
main{max-width:860px;margin:0 auto;padding:16px}h1{font-size:20px;margin:0 0 12px}
nav a{display:inline-block;margin:0 6px 12px 0;padding:6px 12px;border-radius:99px;background:var(--chip);color:var(--fg);text-decoration:none}
nav a.on{background:var(--fg);color:var(--bg)}
article{border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin:0 0 10px}
.meta{color:var(--mut);font-size:13px}.text{margin:6px 0;white-space:pre-wrap;word-break:break-word}
.who{font-size:13px;margin-top:6px}.seen{color:var(--ok)}.unseen{color:#c0392b}.taken{color:var(--ok);font-weight:600}.skip{color:var(--mut)}
"""


def _when(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).strftime("%d.%m %H:%M")


def render(rows: list[dict], status: str, title: str, names: dict | None = None) -> str:
    names = names or {}
    cards = []
    for row in rows:
        lead = row["lead"]
        text = html.escape(lead.text[:600] + ("…" if len(lead.text) > 600 else ""))
        link = (
            f' · <a href="{html.escape(lead.url, quote=True)}" rel="noreferrer">Открыть</a>'
            if lead.url.startswith("https://")
            else ""
        )
        marks = []
        for mark in row["claims"]:
            who = html.escape(mark["user_name"] or mark["user_id"])
            if mark["status"] == "taken":
                marks.append(
                    f'<span class="taken">✅ Берёт: {who}</span> <span class="meta">{_when(mark["updated"])}</span>'
                )
            else:
                marks.append(f'<span class="skip">🚫 {who} не берёт</span>')
        for r in row["recipients"]:
            person = html.escape(str(names.get(r["chat_id"]) or r["chat_id"]))
            click = r["click"]
            if click:
                marks.insert(
                    0,
                    f'<span class="seen">👁 {person}: перешёл</span> <span class="meta">{_when(click["first_at"])}'
                    f"{', раз: ' + str(click['count']) if click['count'] > 1 else ''}</span>",
                )
            else:
                marks.insert(0, f'<span class="unseen">○ {person}: не переходил</span>')
        who_line = "<br>".join(marks) or '<span class="meta">Пока никто не взял</span>'
        cards.append(
            f'<article><div class="meta">{_when(row["first_seen"])} · {html.escape(lead.source_group or lead.source)}'
            f"{' · ' + html.escape(lead.phone) if lead.phone else ''}{link}</div>"
            f'<div class="text">{text}</div><div class="who">{who_line}</div></article>'
        )
    tabs = "".join(
        f'<a class="{"on" if value == status else ""}" href="?status={value}">{label}</a>'
        for value, label in STATUSES.items()
    )
    body = "".join(cards) or '<p class="meta">Пока ничего нет.</p>'
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head><body><main>"
        f"<h1>{html.escape(title)}</h1><nav>{tabs}</nav>{body}</main></body></html>"
    )


def make_handler(
    db_path: str,
    token: str,
    title: str,
    names: dict | None = None,
    *,
    panel: bool = True,
    on_first_click=None,
    name_of=None,
):
    if len(token) < 16:
        raise ValueError("web token must be at least 16 characters")

    class Handler(BaseHTTPRequestHandler):
        server_version = "leads"

        def log_message(self, *args) -> None:  # the token travels in the URL; never log it
            pass

        def _authorized(self, query: dict) -> bool:
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("key")
            supplied = (query.get("key") or [""])[0] or (cookie.value if cookie else "")
            return hmac.compare_digest(supplied.encode(), token.encode())

        def do_GET(self) -> None:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query)
            if parts.path.startswith("/r/"):
                return self._redirect(parts.path[3:])
            if parts.path != "/" or not panel:
                return self._send(HTTPStatus.NOT_FOUND, "not found")
            if not self._authorized(query):
                time.sleep(1)  # slows token guessing
                return self._send(HTTPStatus.UNAUTHORIZED, "Нужна ссылка с ключом")
            status = (query.get("status") or [""])[0]
            status = status if status in STATUSES else ""
            with Store(db_path) as store:
                rows = store.board(status=status)
                shown = dict(names or {})
                if name_of:
                    for chat in {r["chat_id"] for row in rows for r in row["recipients"]}:
                        shown[chat] = name_of(store, chat)
                page = render(rows, status, title, shown)
            headers = {}
            if (query.get("key") or [""])[0]:
                headers["Set-Cookie"] = f"key={token}; Path=/; HttpOnly; SameSite=Strict; Max-Age=31536000"
            self._send(HTTPStatus.OK, page, headers)

        def _redirect(self, link: str) -> None:
            """Personal tracking link: count the click (not link-preview bots) and go to the order."""
            if len(link) != 20 or not link.isalnum():
                return self._send(HTTPStatus.NOT_FOUND, "not found")
            with Store(db_path) as store:
                found = store.find_link(link, token)
                if found is None or not found[0].url.startswith("https://"):
                    return self._send(HTTPStatus.NOT_FOUND, "not found")
                lead, chat_id = found
                first_click = False
                if not looks_like_bot(self.headers.get("User-Agent", "")):
                    first_click = store.record_click(lead.dedupe_key(), chat_id)
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", lead.url)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Length", "0")
            self.end_headers()
            if first_click and on_first_click:
                try:
                    on_first_click(lead, chat_id)  # after the redirect: the visitor never waits for it
                except Exception:
                    pass

        def _send(self, code, body: str, headers: dict | None = None) -> None:
            data = body.encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

    return Handler
