"""Общие утилиты: загрузка конфига, модель лида, фильтрация по ключевым словам."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config(path: Path | str = CONFIG_PATH) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    return text.lower()


def matches_keywords(text: str, keywords: list[str], exclude_keywords: list[str] | None = None) -> bool:
    """True, если текст содержит хотя бы одно ключевое слово и ни одного стоп-слова."""
    norm = _normalize(text)
    if exclude_keywords:
        for stop in exclude_keywords:
            if _normalize(stop) in norm:
                return False
    for kw in keywords:
        if _normalize(kw) in norm:
            return True
    return False


def extract_phone(text: str) -> str | None:
    """Достаёт первый похожий на российский номер телефона фрагмент, если есть."""
    if not text:
        return None
    match = re.search(r"(?:\+7|7|8)[\s\-\(]*\d{3}[\s\-\)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}", text)
    return match.group(0).strip() if match else None


@dataclass
class Lead:
    source: str            # "telegram" | "vk" | "avito"
    external_id: str       # уникальный id внутри источника, для дедупликации
    date: str               # ISO-строка даты публикации
    author: str = ""
    text: str = ""
    phone: str = ""
    price: str = ""
    url: str = ""
    source_group: str = ""  # название паблика/канала, откуда взят лид (для статистики по источникам)
    extra: dict = field(default_factory=dict)

    def dedupe_key(self) -> str:
        return f"{self.source}:{self.external_id}"

    def as_row(self) -> list[str]:
        """Порядок колонок должен совпадать с sheets_writer.HEADER."""
        ai_verdict = ""
        if "ai_is_client" in self.extra:
            mark = "клиент" if self.extra.get("ai_is_client") else "не клиент"
            conf = self.extra.get("ai_confidence", "")
            reason = self.extra.get("ai_reason", "")
            ai_verdict = f"{mark} ({conf}): {reason}".strip()

        return [
            self.date,
            self.source,
            self.source_group,
            self.author,
            self.text[:500],
            self.phone,
            self.price,
            self.url,
            ai_verdict,
        ]
