"""Domain objects and inexpensive candidate filtering; no network or storage."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from configuration import CONFIG_PATH as CONFIG_PATH
from configuration import load_config as load_config


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return " ".join(text.split())


REQUEST_RE = re.compile(
    r"\b(?:ищу|ищем|нуж(?:ен|на|ны|но)|требу(?:ется|ются)|посовет\w*|подскаж\w*|порекоменд\w*|кто (?:может|делал|сделает)|помогите найти)\b"
)
TOPIC_RE = re.compile(r"\b(?:ремонт\w*|отдел\w*|бригад\w*|мастер\w*|под ключ)\b")


def keyword_decision(
    text: str, keywords: list[str], exclude_keywords: list[str] | None = None
) -> tuple[bool, str]:
    norm = _normalize(text)
    if not norm:
        return False, "empty"
    if any(_normalize(stop) in norm for stop in (exclude_keywords or []) if stop.strip()):
        return False, "excluded_phrase"
    if any(_normalize(word) in norm for word in keywords if word.strip()):
        return True, "configured_phrase"
    if REQUEST_RE.search(norm) and TOPIC_RE.search(norm):
        return True, "request_and_topic"
    return False, "no_request_or_keyword"


def matches_keywords(text: str, keywords: list[str], exclude_keywords: list[str] | None = None) -> bool:
    return keyword_decision(text, keywords, exclude_keywords)[0]


def extract_phone(text: str) -> str | None:
    match = re.search(
        r"(?<!\d)(?:\+7|7|8)[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)", text or ""
    )
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group())
    return "+7" + digits[1:]


@dataclass
class Lead:
    source: str
    external_id: str
    date: str  # publication time; empty means unknown
    author: str = ""
    text: str = ""
    phone: str = ""
    price: str = ""
    url: str = ""
    source_group: str = ""
    source_group_id: str = ""
    observed_at: str = field(default_factory=utc_now)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.source not in {"telegram", "vk", "avito"} or not str(self.external_id).strip():
            raise ValueError("Lead requires a supported source and non-empty external_id")
        self.external_id = str(self.external_id)
        self.source_group_id = str(self.source_group_id)

    def dedupe_key(self) -> str:
        return f"{self.source}:{self.external_id}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Lead:
        return cls(**{k: v for k, v in value.items() if k in cls.__dataclass_fields__})

    def as_row(self) -> list[str]:
        verdict = ""
        if "ai_is_client" in self.extra:
            verdict = f"{'клиент' if self.extra['ai_is_client'] else 'не клиент'} ({self.extra.get('ai_confidence', '')}): {self.extra.get('ai_reason', '')}"
        return [
            self.date,
            self.source,
            self.source_group,
            self.author,
            self.text,
            self.phone,
            self.price,
            self.url,
            verdict,
            self.dedupe_key(),
            self.source_group_id,
            self.observed_at,
            self.extra.get("prompt_version", ""),
        ]


def unique_leads(leads: list[Lead]) -> list[Lead]:
    selected: dict[str, Lead] = {}
    for lead in leads:
        key = lead.dedupe_key()
        if key not in selected:
            selected[key] = Lead.from_dict(lead.to_dict())
            continue
        current = selected[key]
        for name in ("author", "phone", "price", "url", "source_group", "source_group_id", "date"):
            if not getattr(current, name) and getattr(lead, name):
                setattr(current, name, getattr(lead, name))
        if len(lead.text) > len(current.text):
            current.text = lead.text
        current.extra.update({k: v for k, v in lead.extra.items() if k not in current.extra})
    return list(selected.values())


@dataclass
class ScanResult:
    source_id: str
    scanned: int = 0
    candidates: int = 0
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    prefilter: dict[str, int] = field(default_factory=dict)

    def counted(self, reason: str) -> None:
        self.prefilter[reason] = self.prefilter.get(reason, 0) + 1

    def fail(self, message: str) -> None:
        self.complete = False
        self.errors.append(message)
