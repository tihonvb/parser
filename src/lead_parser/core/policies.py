"""Pure candidate, merge, and qualification rules shared by all integrations."""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Iterable
from copy import deepcopy

from lead_parser.core.models import ClassificationDecision, ClassificationState, Lead, Verdict

REQUEST_RE = re.compile(
    r"\b(?:ищу|ищем|нуж(?:ен|на|ны|но)|требу(?:ется|ются)|посовет\w*|подскаж\w*|порекоменд\w*|кто (?:может|делал|сделает)|помогите найти)\b"
)
TOPIC_RE = re.compile(r"\b(?:ремонт\w*|отдел\w*|бригад\w*|мастер\w*|под ключ)\b")


def normalize_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").casefold().replace("ё", "е")
    return " ".join(text.split())


def keyword_decision(
    text: str, keywords: list[str], exclude_keywords: list[str] | None = None
) -> tuple[bool, str]:
    norm = normalize_text(text)
    if not norm:
        return False, "empty"
    if any(normalize_text(stop) in norm for stop in (exclude_keywords or []) if stop.strip()):
        return False, "excluded_phrase"
    if any(normalize_text(word) in norm for word in keywords if word.strip()):
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


def unique_leads(leads: Iterable[Lead]) -> list[Lead]:
    """Merge duplicate identities in encounter order without mutating callers.

    Keep the first nonempty metadata value and longest text. Extra provider
    metadata is copied recursively, including fields added by later duplicates.
    """
    selected: dict[str, Lead] = {}
    for lead in leads:
        key = lead.dedupe_key()
        if key not in selected:
            selected[key] = deepcopy(lead)
            continue
        current = selected[key]
        for name in ("author", "phone", "price", "url", "source_group", "source_group_id", "date"):
            if not getattr(current, name) and getattr(lead, name):
                setattr(current, name, getattr(lead, name))
        if len(lead.text) > len(current.text):
            current.text = lead.text
        current.extra.update({k: deepcopy(v) for k, v in lead.extra.items() if k not in current.extra})
    return list(selected.values())


def decide_classification(verdict: Verdict, min_confidence: float = 0.75) -> ClassificationDecision:
    """Accept a client request only when its confidence reaches the threshold."""
    if (
        type(min_confidence) not in {int, float}
        or not math.isfinite(min_confidence)
        or not 0 <= min_confidence <= 1
    ):
        raise ValueError("min_confidence must be finite and between zero and one")
    state = (
        ClassificationState.ACCEPTED
        if verdict.is_client and verdict.confidence >= min_confidence
        else ClassificationState.REJECTED
    )
    return ClassificationDecision(state=state, verdict=verdict)
