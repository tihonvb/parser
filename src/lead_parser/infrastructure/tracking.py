"""Personal tracking links: one unguessable token per (lead, recipient)."""

from __future__ import annotations

import hashlib
import hmac

BOT_MARKERS = (
    "bot", "crawl", "spider", "preview", "curl", "python", "facebookexternalhit", "slack", "whatsapp", "go-http",
)  # fmt: skip


def click_token(secret: str, lead_key: str, chat_id: str) -> str:
    return hmac.new(secret.encode(), f"{lead_key}\n{chat_id}".encode(), hashlib.sha256).hexdigest()[:20]


def tracked_url(public_url: str, secret: str, lead_key: str, chat_id: str) -> str:
    return f"{public_url.rstrip('/')}/r/{click_token(secret, lead_key, chat_id)}"


def looks_like_bot(user_agent: str) -> bool:
    agent = user_agent.lower()
    return not agent or any(marker in agent for marker in BOT_MARKERS)
