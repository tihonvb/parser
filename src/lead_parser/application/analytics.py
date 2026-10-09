"""Pure snapshot reporting: current lead cohorts and period delivery economics."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


@dataclass(frozen=True)
class Period:
    start: datetime
    end: datetime
    timezone: str

    @classmethod
    def dates(cls, start: str, end: str, timezone: str = "UTC") -> Period:
        try:
            zone = ZoneInfo(timezone)
            first, last = date.fromisoformat(start), date.fromisoformat(end)
            if first.isoformat() != start or last.isoformat() != end:
                raise ValueError("Use YYYY-MM-DD dates")
            result = cls(
                datetime.combine(first, time(), zone), datetime.combine(last, time(), zone), timezone
            )
        except (ValueError, ZoneInfoNotFoundError) as error:
            raise ValueError("Use valid YYYY-MM-DD dates and an IANA timezone") from error
        if result.start >= result.end:
            raise ValueError("--to must be later than --from (end is exclusive)")
        return result

    def contains(self, timestamp: float) -> bool:
        return self.start.timestamp() <= timestamp < self.end.timestamp()


def money(value: object) -> Decimal:
    """Accept finite nonnegative dollar amounts, including exact JSON strings."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise ValueError("Cost must be a finite nonnegative number in USD")
    try:
        amount = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError("Cost must be a finite nonnegative number in USD") from error
    if not amount.is_finite() or amount < 0:
        raise ValueError("Cost must be a finite nonnegative number in USD")
    return amount


def _amount(value: Decimal | None) -> str | None:
    return format(value, "f") if value is not None else None


@dataclass(frozen=True)
class AnalyticsLead:
    key: str
    payload: Mapping[str, Any]
    state: str
    first_seen: float
    human_is_client: bool | None


@dataclass(frozen=True)
class AnalyticsDelivery:
    lead_key: str
    kind: str
    state: str
    delivered: float | None
    first_delivered: float | None = None


def _timestamp(value: object) -> float:
    if not isinstance(value, str):
        raise ValueError("Invalid event timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Event timestamps require a timezone")
    return parsed.timestamp()


def summarize_usage(
    events: Iterable[dict], period: Period, *, available: bool, invalid_lines: int = 0
) -> dict:
    """Deduplicate request events and keep unknown billing separate from zero."""
    requests: dict[str, dict[str, dict]] = {}
    conflicts: set[str] = set()
    malformed = invalid_lines
    for event in events:
        try:
            if not isinstance(event, dict) or event.get("schema_version") != 1:
                raise ValueError("Unsupported usage event")
            if event.get("purpose") == "evaluation":
                continue
            if event.get("purpose") != "pipeline" or event.get("provider") != "openrouter":
                raise ValueError("Unknown usage purpose/provider")
            event_type, request_id = event.get("event"), event.get("request_id")
            if event_type not in {"started", "finished"} or not isinstance(request_id, str) or not request_id:
                raise ValueError("Invalid request identity")
            _timestamp(event.get("started_at"))
            _timestamp(event.get("occurred_at"))
            if event_type == "finished" and event.get("outcome") not in {"success", "partial", "error"}:
                raise ValueError("Invalid request outcome")
            records = requests.setdefault(request_id, {})
            if event_type in records and records[event_type] != event:
                conflicts.add(request_id)
            records.setdefault(event_type, event)
        except (TypeError, ValueError, OverflowError):
            malformed += 1

    result = {
        "available": available,
        "historical_coverage": "unknown",
        "attribution": "request_started_at",
        "requests": 0,
        "finished": 0,
        "unfinished": 0,
        "errors": 0,
        "partial": 0,
        "requests_with_known_cost": 0,
        "requests_with_unknown_cost": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "requests_with_incomplete_tokens": 0,
        "invalid_lines": malformed,
        "conflicting_requests": len(conflicts),
    }
    cost = Decimal(0)
    for request_id, records in requests.items():
        first = records.get("started") or records["finished"]
        if not period.contains(_timestamp(first["started_at"])):
            continue
        result["requests"] += 1
        finished = records.get("finished")
        conflict = request_id in conflicts or any(
            record["started_at"] != first["started_at"] for record in records.values()
        )
        if conflict and request_id not in conflicts:
            result["conflicting_requests"] += 1
        if finished:
            result["finished"] += 1
            result["errors"] += int(finished["outcome"] == "error")
            result["partial"] += int(finished["outcome"] == "partial")
        else:
            result["unfinished"] += 1
        try:
            request_cost = money(finished.get("cost_usd")) if finished and not conflict else None
        except ValueError:
            request_cost = None
        if request_cost is None:
            result["requests_with_unknown_cost"] += 1
        else:
            result["requests_with_known_cost"] += 1
            cost += request_cost
        incomplete = False
        for field in ("prompt_tokens", "completion_tokens"):
            tokens = finished.get(field) if finished and not conflict else None
            if type(tokens) is int and tokens >= 0:
                result[field] += tokens
            else:
                incomplete = True
        result["requests_with_incomplete_tokens"] += int(incomplete)
    result["known_cost_usd"] = _amount(cost) if available else None
    return result


def _counts() -> dict:
    return {
        "total": 0,
        "accepted": 0,
        "rejected": 0,
        "pending": 0,
        "review": 0,
        "unknown_state": 0,
        "ai_positive": 0,
        "human_reviewed": 0,
        "human_positive": 0,
    }


def build_report(
    leads: Iterable[AnalyticsLead],
    deliveries: Iterable[AnalyticsDelivery],
    usage_events: Iterable[dict],
    period: Period,
    *,
    usage_available: bool = False,
    invalid_usage_lines: int = 0,
    ai_cost_usd: Decimal | None = None,
    fixed_cost_usd: Decimal | None = None,
    include_leads: bool = False,
) -> dict:
    """Costs are operational period ratios, not attributed cohort acquisition costs."""
    ai_cost_usd = money(ai_cost_usd) if ai_cost_usd is not None else None
    fixed_cost_usd = money(fixed_cost_usd) if fixed_cost_usd is not None else None
    cohort = _counts()
    sources: dict[tuple[str, str], dict] = {}
    lead_sources: dict[str, tuple[str, str]] = {}
    accepted = []
    for lead in leads:
        if not period.contains(lead.first_seen) or lead.key in lead_sources:
            continue
        payload = lead.payload
        source = str(payload.get("source") or lead.key.split(":", 1)[0])
        source_id = str(payload.get("source_group_id") or "unknown:" + lead.key)
        identity = (source, source_id)
        lead_sources[lead.key] = identity
        item = sources.setdefault(identity, {"source": source, "source_id": source_id, **_counts()})
        item["group"] = str(payload.get("source_group") or "")
        extra = payload.get("extra") or {}
        if not isinstance(extra, dict):
            extra = {}
        for bucket in (cohort, item):
            bucket["total"] += 1
            bucket[
                lead.state if lead.state in {"accepted", "rejected", "pending", "review"} else "unknown_state"
            ] += 1
            bucket["ai_positive"] += int(extra.get("ai_is_client") is True)
            bucket["human_reviewed"] += int(lead.human_is_client is not None)
            bucket["human_positive"] += int(lead.human_is_client is True)
        if include_leads and lead.state == "accepted":
            accepted.append(
                {
                    "key": lead.key,
                    "source": source,
                    "source_id": source_id,
                    "group": item["group"],
                    "url": str(payload.get("url") or ""),
                    "first_seen": datetime.fromtimestamp(lead.first_seen, UTC).isoformat(),
                    "ai_is_client": extra.get("ai_is_client"),
                    "ai_confidence": extra.get("ai_confidence"),
                    "ai_reason": extra.get("ai_reason"),
                    "human_is_client": lead.human_is_client,
                }
            )
    cohort_delivered: dict[str, set] = {}
    period_delivered: dict[str, set] = {}
    first_delivered_in_period: set[str] = set()
    delivery_queue = Counter()
    for delivery in deliveries:
        if delivery.lead_key in lead_sources:
            delivery_queue[delivery.state] += 1
        if delivery.state != "done" or delivery.delivered is None:
            continue
        if delivery.lead_key in lead_sources:
            cohort_delivered.setdefault(delivery.kind, set()).add(delivery.lead_key)
        if delivery.first_delivered is not None and period.contains(delivery.first_delivered):
            first_delivered_in_period.add(delivery.lead_key)
        if period.contains(delivery.delivered):
            period_delivered.setdefault(delivery.kind, set()).add(delivery.lead_key)
    cohort_any = set().union(*cohort_delivered.values())
    period_any = set().union(*period_delivered.values())
    delivered_per_source = Counter(lead_sources[key] for key in cohort_any)
    for identity, item in sources.items():
        item["delivered_any_channel"] = delivered_per_source[identity]
        item["human_positive_rate"] = (
            item["human_positive"] / item["human_reviewed"] if item["human_reviewed"] else None
        )
    cohort.update(
        human_positive_rate=cohort["human_positive"] / cohort["human_reviewed"]
        if cohort["human_reviewed"]
        else None,
        delivered_any_channel=len(cohort_any),
        delivered_by_channel={kind: len(keys) for kind, keys in sorted(cohort_delivered.items())},
        delivery_jobs_by_current_state=dict(sorted(delivery_queue.items())),
    )
    usage = summarize_usage(
        usage_events, period, available=usage_available, invalid_lines=invalid_usage_lines
    )
    complete = ai_cost_usd is not None and fixed_cost_usd is not None
    total = ai_cost_usd + fixed_cost_usd if complete else None
    warnings = [
        "Когорта: first_seen за период; статусы и ручные оценки отражают состояние снимка, не конец периода.",
        "Уникальность — source:external_id, а не уникальные люди или клиенты.",
        "AI-positive и accepted не означают подтверждённую продажу; ручные оценки могут быть нерепрезентативны.",
        "Доставки за период включают старые лиды; CPL делит затраты на впервые доставленные за период лиды, а не на first_seen-когорту.",
        "Известные расходы журнала — начисления OpenRouter; отдельные счета BYOK-провайдеров в эту сумму не входят.",
        "Покрытие журнала расходов за прошлые периоды неизвестно; известная сумма не равна полному счёту провайдера.",
        "Выручка, сделки и прибыль не сохраняются: CAC и ROI не рассчитываются.",
    ]
    if not usage_available:
        warnings.append("Журнал запросов не найден: исторические расходы ИИ неизвестны.")
    if usage["invalid_lines"] or usage["conflicting_requests"]:
        warnings.append(
            "Журнал содержит некорректные/конфликтующие события; часть запросов или расходов может отсутствовать."
        )
    if usage["requests_with_unknown_cost"]:
        warnings.append(
            "Для части запросов цена неизвестна, включая незавершённые запросы; это не нулевые расходы."
        )
    if ai_cost_usd is None:
        warnings.append("Для полного расчёта укажите расходы ИИ по счёту за этот период (--ai-cost-usd).")
    if fixed_cost_usd is None:
        warnings.append("Доля остальных расходов за период неизвестна (--fixed-cost-usd); явный 0 допустим.")
    report = {
        "period": {
            "from": period.start.isoformat(),
            "to_exclusive": period.end.isoformat(),
            "timezone": period.timezone,
        },
        "cohort": cohort,
        "sources": sorted(
            sources.values(),
            key=lambda row: (-row["accepted"], -row["total"], row["source"], row["source_id"]),
        ),
        "period_deliveries": {
            "unique_leads_first_delivered": len(first_delivered_in_period),
            "unique_leads_any_channel": len(period_any),
            "unique_leads_by_channel": {kind: len(keys) for kind, keys in sorted(period_delivered.items())},
        },
        "ai_usage": usage,
        "economics": {
            "currency": "USD",
            "ai_cost_source": "manual_period_invoice" if ai_cost_usd is not None else "unknown",
            "ai_cost_usd": _amount(ai_cost_usd),
            "fixed_cost_usd": _amount(fixed_cost_usd),
            "total_complete": complete,
            "total_cost_usd": _amount(total),
            "cpl_denominator": "unique_leads_first_delivered_in_period",
            "operational_cost_per_delivered_lead_usd": _amount(total / len(first_delivered_in_period))
            if total is not None and first_delivered_in_period
            else None,
        },
        "warnings": warnings,
    }
    if include_leads:
        report["accepted_leads"] = accepted
    return report


def _cell(value: object) -> str:
    if value is None:
        return "неизвестно"
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def render_markdown(report: dict) -> str:
    period, cohort, economics, usage = (report[key] for key in ("period", "cohort", "economics", "ai_usage"))
    lines = [
        "# Аналитика лидов",
        "",
        f"Период: {period['from']} — {period['to_exclusive']} (правая граница не включена).",
        "",
        "## Затраты и доставки за период",
        "",
        "| Показатель | Значение |",
        "| --- | ---: |",
    ]
    metrics = [
        (
            "Впервые доставленных уникальных лидов (знаменатель CPL)",
            report["period_deliveries"]["unique_leads_first_delivered"],
        ),
        (
            "Уникальных лидов с любой успешной доставкой за период",
            report["period_deliveries"]["unique_leads_any_channel"],
        ),
        ("Расходы ИИ по счёту, USD", economics["ai_cost_usd"]),
        ("Остальные расходы за период, USD", economics["fixed_cost_usd"]),
        ("Полные расходы, USD", economics["total_cost_usd"]),
        (
            "Операционная стоимость доставленного лида, USD",
            economics["operational_cost_per_delivered_lead_usd"],
        ),
        ("Известный подытог расходов из журнала, USD (покрытие неизвестно)", usage["known_cost_usd"]),
        ("Запросов ИИ в журнале", usage["requests"]),
        ("Запросов без известной цены", usage["requests_with_unknown_cost"]),
        (
            "Незавершённых / ошибочных / частично обработанных запросов",
            f"{usage['unfinished']} / {usage['errors']} / {usage['partial']}",
        ),
        ("Известные входные / выходные токены", f"{usage['prompt_tokens']} / {usage['completion_tokens']}"),
        ("Запросов с неполными данными о токенах", usage["requests_with_incomplete_tokens"]),
        (
            "Некорректных строк / конфликтующих запросов",
            f"{usage['invalid_lines']} / {usage['conflicting_requests']}",
        ),
    ]
    lines.extend(f"| {name} | {_cell(value)} |" for name, value in metrics)
    lines.extend(
        ["", "## Новые лиды: текущее состояние когорты", "", "| Показатель | Количество |", "| --- | ---: |"]
    )
    for label, key in [
        ("Всего", "total"),
        ("Принято", "accepted"),
        ("Отклонено", "rejected"),
        ("Ожидают ИИ", "pending"),
        ("Требуют проверки", "review"),
        ("Неизвестный статус", "unknown_state"),
        ("AI-positive", "ai_positive"),
        ("Оценены человеком", "human_reviewed"),
        ("Положительные ручные оценки", "human_positive"),
        ("Доставлены хотя бы в один канал к моменту снимка", "delivered_any_channel"),
    ]:
        lines.append(f"| {label} | {cohort[key]} |")
    lines.extend(
        [
            "",
            "## Доставки по каналам (уникальные лиды)",
            "",
            "| Канал | Доставлено за период | Из новой когорты к моменту снимка |",
            "| --- | ---: | ---: |",
        ]
    )
    period_channels = report["period_deliveries"]["unique_leads_by_channel"]
    cohort_channels = cohort["delivered_by_channel"]
    for kind in sorted(set(period_channels) | set(cohort_channels)):
        lines.append(f"| {_cell(kind)} | {period_channels.get(kind, 0)} | {cohort_channels.get(kind, 0)} |")
    lines.extend(
        [
            "",
            "## Источники новой когорты",
            "",
            "| Источник | ID | Группа | Всего | Принято | Доставлено | Ручные + / проверены |",
            "| --- | --- | --- | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in report["sources"]:
        values = [
            row["source"],
            row["source_id"],
            row["group"],
            row["total"],
            row["accepted"],
            row["delivered_any_channel"],
            f"{row['human_positive']} / {row['human_reviewed']}",
        ]
        lines.append("| " + " | ".join(map(_cell, values)) + " |")
    if "accepted_leads" in report:
        lines.extend(
            [
                "",
                "## Принятые лиды новой когорты",
                "",
                "| ID | Группа | Ссылка | Вердикт ИИ | Причина | Ручная оценка |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for row in report["accepted_leads"]:
            values = [
                row["key"],
                row["group"],
                row["url"],
                row["ai_is_client"],
                row["ai_reason"],
                row["human_is_client"],
            ]
            lines.append("| " + " | ".join(map(_cell, values)) + " |")
    lines.extend(["", "## Ограничения", ""])
    lines.extend(f"- {warning}" for warning in report["warnings"])
    return "\n".join(lines) + "\n"
