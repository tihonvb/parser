"""Button presses of the recipients: who takes which lead. Long polling, no public webhook needed."""

from __future__ import annotations

import logging

import requests

from lead_parser.infrastructure.integrations.telegram.notifications import _collect_chat_ids, keyboard
from lead_parser.infrastructure.persistence.sqlite import Store

log = logging.getLogger(__name__)
OFFSET_KEY = "telegram_updates_offset"
ANSWERS = {"taken": "Вы взяли заказ", "skipped": "Отмечено: не берёте", None: "Отметка снята"}


def _api(cfg: dict, method: str, payload: dict, *, timeout: float = 15) -> dict:
    url = f"https://api.telegram.org/bot{cfg['notifications']['telegram']['bot_token']}/{method}"
    response = requests.post(url, json=payload, timeout=(10, timeout))
    data = response.json()
    if not isinstance(data, dict) or data.get("ok") is not True:
        raise RuntimeError(f"Telegram {method} failed: {response.status_code}")
    return data


def display_name(user: dict) -> str:
    name = " ".join(part for part in (user.get("first_name"), user.get("last_name")) if part)
    return name or (f"@{user['username']}" if user.get("username") else str(user.get("id", "")))


def handle_callback(cfg: dict, store: Store, query: dict, api=_api) -> bool:
    """Record one press. Only the configured recipients may mark leads. Returns True when a mark was stored."""
    user = query.get("from") or {}
    user_id = str(user.get("id", ""))
    action, _, token = str(query.get("data", "")).partition(":")
    if user_id not in set(_collect_chat_ids(cfg["notifications"]["telegram"])) or action not in {"t", "s"}:
        api(cfg, "answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Нет доступа"})
        return False
    key = store.lead_key_by_token(token)
    if key is None:
        api(cfg, "answerCallbackQuery", {"callback_query_id": query.get("id"), "text": "Заказ не найден"})
        return False
    status = store.set_claim(key, user_id, display_name(user), "taken" if action == "t" else "skipped")
    api(cfg, "answerCallbackQuery", {"callback_query_id": query.get("id"), "text": ANSWERS[status]})
    message = query.get("message") or {}
    if message.get("message_id") and (message.get("chat") or {}).get("id"):
        try:
            api(
                cfg,
                "editMessageReplyMarkup",
                {
                    "chat_id": message["chat"]["id"],
                    "message_id": message["message_id"],
                    "reply_markup": keyboard(token, status),
                },
            )
        except RuntimeError:
            log.warning("could not update the buttons of a message")  # e.g. the same markup, harmless
    return True


def poll_once(cfg: dict, store: Store, *, wait: int = 25, api=_api) -> int:
    row = store.db.execute("SELECT value FROM meta WHERE key=?", (OFFSET_KEY,)).fetchone()
    offset = int(row["value"]) if row else 0
    data = api(
        cfg,
        "getUpdates",
        {"offset": offset, "timeout": wait, "allowed_updates": ["callback_query"]},
        timeout=wait + 10,
    )
    handled = 0
    for update in data.get("result", []):
        offset = max(offset, int(update["update_id"]) + 1)
        if isinstance(update.get("callback_query"), dict):
            try:
                handled += handle_callback(cfg, store, update["callback_query"], api)
            except Exception:
                log.exception("callback failed")  # never block the queue on one bad press
        store.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (OFFSET_KEY, str(offset)),
        )
    return handled


def person_name(cfg: dict, store: Store, chat_id: str, api=_api) -> str:
    """Readable name of a recipient: configured name, else the Telegram @nick / first name (cached), else the id."""
    configured = cfg["notifications"]["telegram"]["names"].get(str(chat_id))
    if configured:
        return str(configured)
    key = f"name:{chat_id}"
    row = store.db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row:
        return row["value"]
    try:
        chat = api(cfg, "getChat", {"chat_id": chat_id}, timeout=5).get("result") or {}
    except Exception:
        return str(chat_id)  # not cached: try again next time (e.g. the person has not started the bot yet)
    name = f"@{chat['username']}" if chat.get("username") else display_name({"id": chat_id, **chat})
    if name != str(chat_id):
        store.db.execute(
            "INSERT INTO meta VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, name)
        )
    return name
