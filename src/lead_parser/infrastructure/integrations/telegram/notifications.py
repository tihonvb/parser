"""Telegram Bot API transport. Durable scheduling belongs to delivery.py."""

from __future__ import annotations

import requests

from lead_parser.application.errors import DeliveryError
from lead_parser.application.models import DeliveryJob
from lead_parser.core.models import Lead
from lead_parser.infrastructure.security import delivery_error


class NotificationError(DeliveryError):
    def __init__(self, code: int, *, retry_after: float = 0):
        super().__init__(
            f"Telegram API error {code}",
            permanent=code in {400, 401, 403, 404},
            retry_after=retry_after,
        )


def _format_message(lead: Lead) -> str:
    return "\n".join(
        [
            f"Новый лид ({lead.source})",
            lead.source_group,
            lead.text[:2400],
            f"Телефон: {lead.phone or '—'}",
            f"Ссылка: {lead.url or '—'}",
            f"ИИ: {lead.extra.get('ai_confidence', 'отключён')} {lead.extra.get('ai_reason', '')[:400]}",
        ]
    )[:4096]


def _collect_chat_ids(settings: dict) -> list[str]:
    raw = [settings.get("chat_id")] + settings.get("chat_ids", [])
    return list(dict.fromkeys(str(item).strip() for item in raw if item is not None and str(item).strip()))


def send_to(cfg: dict, lead: Lead, chat_id: str) -> None:
    settings = cfg["notifications"]["telegram"]
    response = requests.post(
        f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage",
        json={"chat_id": chat_id, "text": _format_message(lead), "disable_web_page_preview": True},
        timeout=(10, settings["timeout_seconds"]),
    )
    try:
        payload = response.json()
    except ValueError as error:
        raise NotificationError(response.status_code or 502) from error
    if not isinstance(payload, dict) or response.status_code != 200 or payload.get("ok") is not True:
        code = (
            payload.get("error_code", response.status_code)
            if isinstance(payload, dict)
            else response.status_code
        )
        delay = payload.get("parameters", {}).get("retry_after", 0) if isinstance(payload, dict) else 0
        raise NotificationError(int(code or 502), retry_after=float(delay or 0))


class TelegramGateway:
    """One recipient per delivery job; retry policy stays in the application."""

    def __init__(self, cfg: dict):
        self.cfg = cfg

    def deliver(self, lead: Lead, job: DeliveryJob) -> None:
        try:
            send_to(self.cfg, lead, job["destination"])
        except DeliveryError:
            raise
        except Exception as error:
            raise delivery_error(error) from error
