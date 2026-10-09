"""Google Sheets representation of a lead; the domain does not know this schema."""

from lead_parser.core.models import Lead

LEGACY_HEADER = [
    "Дата",
    "Источник",
    "Группа/канал",
    "Автор",
    "Текст",
    "Телефон",
    "Цена",
    "Ссылка",
    "Вердикт ИИ",
]
HEADER = LEGACY_HEADER + ["ID лида", "ID источника", "Обнаружено", "Версия промпта"]


def lead_to_row(lead: Lead) -> list[str]:
    verdict = ""
    if "ai_is_client" in lead.extra:
        verdict = (
            f"{'клиент' if lead.extra['ai_is_client'] else 'не клиент'} "
            f"({lead.extra.get('ai_confidence', '')}): {lead.extra.get('ai_reason', '')}"
        )
    return [
        lead.date,
        lead.source,
        lead.source_group,
        lead.author,
        lead.text,
        lead.phone,
        lead.price,
        lead.url,
        verdict,
        lead.dedupe_key(),
        lead.source_group_id,
        lead.observed_at,
        lead.extra.get("prompt_version", ""),
    ]
