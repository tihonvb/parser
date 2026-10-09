"""Отправка уведомлений о новых лидах в Telegram через Bot API.

Настройки берутся из config.yaml, раздел:

    notifications:
      telegram:
        enabled: true
        bot_token: "ТВОЙ_ТОКЕН_БОТА"
        chat_id: "ТВОЙ_CHAT_ID"          # один получатель (как раньше)
        # ИЛИ несколько получателей сразу (например, ты + заказчик):
        # chat_ids: ["ТВОЙ_CHAT_ID", "CHAT_ID_ЗАКАЗЧИКА"]

Можно указать и `chat_id`, и `chat_ids` одновременно — уйдёт всем, дубли
убираются автоматически. Если раздел отсутствует, enabled: false, или не
указано ни одного получателя — уведомления просто не отправляются,
остальной пайплайн это никак не затрагивает (ошибки тут не должны ронять
основной прогон main.py).
"""

from __future__ import annotations

import requests

from common import Lead


def _format_message(lead: Lead) -> str:
    verdict = ""
    if "ai_is_client" in lead.extra:
        mark = "клиент" if lead.extra.get("ai_is_client") else "не клиент"
        conf = lead.extra.get("ai_confidence", "")
        reason = lead.extra.get("ai_reason", "")
        verdict = f"\nИИ: {mark} ({conf}) — {reason}"

    parts = [
        f"🆕 Новый лид ({lead.source})",
        lead.source_group or "(источник не определён)",
        "",
        (lead.text or "")[:800],
        "",
        f"Телефон: {lead.phone or '—'}",
        f"Ссылка: {lead.url or '—'}",
    ]
    text = "\n".join(parts)
    if verdict:
        text += verdict
    return text


def _collect_chat_ids(tg_cfg: dict) -> list[str]:
    """Собирает список получателей из chat_id (один) и/или chat_ids (список),
    убирая дубли и пустые значения, сохраняя порядок."""
    raw: list = []
    single = tg_cfg.get("chat_id")
    if single:
        raw.append(single)
    many = tg_cfg.get("chat_ids") or []
    if isinstance(many, (list, tuple)):
        raw.extend(many)
    seen = set()
    result = []
    for cid in raw:
        cid = str(cid).strip()
        if cid and cid not in seen:
            seen.add(cid)
            result.append(cid)
    return result


def send_lead_notification(cfg: dict, lead: Lead) -> None:
    tg_cfg = (cfg.get("notifications") or {}).get("telegram") or {}
    if not tg_cfg.get("enabled"):
        return
    bot_token = tg_cfg.get("bot_token")
    chat_ids = _collect_chat_ids(tg_cfg)
    if not bot_token or not chat_ids:
        return

    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    text = _format_message(lead)
    for chat_id in chat_ids:
        try:
            resp = requests.post(
                url,
                data={"chat_id": chat_id, "text": text},
                timeout=10,
            )
            if resp.status_code != 200:
                print(f"[telegram_notify] Telegram API вернул {resp.status_code} для chat_id {chat_id}: {resp.text[:300]}")
        except Exception as exc:  # noqa: BLE001 — уведомления не должны ронять прогон
            print(f"[telegram_notify] Не удалось отправить уведомление на chat_id {chat_id}: {exc}")


def send_leads_notifications(cfg: dict, leads) -> None:
    for lead in leads:
        send_lead_notification(cfg, lead)
