"""Aggregate lead predictions, human labels and source coverage through a read port."""

from lead_parser.application.ports import StatisticsRepository


def from_store(store: StatisticsRepository):
    records = {}
    ai_used = False
    for row in store.statistics_rows():
        lead = row.lead
        identity = lead.source_group_id or "unknown:" + lead.dedupe_key()
        item = records.setdefault(
            (lead.source, identity),
            {
                "source": lead.source,
                "source_id": lead.source_group_id,
                "group": lead.source_group,
                "total": 0,
                "confirmed": 0,
                "human_reviewed": 0,
                "human_positive": 0,
                "accepted": 0,
                "rejected": 0,
                "pending": 0,
            },
        )
        item["group"] = lead.source_group
        item["total"] += 1
        ai_used |= "ai_is_client" in lead.extra
        item["confirmed"] += int(lead.extra.get("ai_is_client") is True)
        item[row.ai_state if row.ai_state in {"accepted", "rejected"} else "pending"] += 1
        if row.human_is_client is not None:
            item["human_reviewed"] += 1
            item["human_positive"] += int(row.human_is_client)
    coverage = {item["id"]: item for item in store.summary()["sources"]}
    for (_, identity), item in records.items():
        scan = coverage.get(identity, {})
        item["last_scan"] = scan
        item["prefilter_candidate_rate_last_scan"] = (
            scan.get("candidates", 0) / scan["scanned"] if scan.get("scanned") else None
        )
        item["human_positive_rate"] = (
            item["human_positive"] / item["human_reviewed"] if item["human_reviewed"] else None
        )
    return sorted(
        records.values(), key=lambda item: (item["accepted"], item["total"], item["source_id"]), reverse=True
    ), ai_used


def overrides(records, top=5):
    result = {"vk": {"group_overrides": {}}, "telegram": {"channel_overrides": {}}}
    for item in records[:top]:
        identity = item["source_id"]
        if item["source"] == "vk" and identity.startswith("vk:") and identity[3:].isdigit():
            result["vk"]["group_overrides"][identity[3:]] = {"max_pages": 100}
        elif item["source"] == "telegram" and identity.startswith("telegram:") and identity[9:].isdigit():
            result["telegram"]["channel_overrides"][identity[9:]] = {"max_messages_per_channel": 10000}
    return result
