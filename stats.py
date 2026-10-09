"""Stable source IDs, AI predictions and independent human review statistics."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict

import gspread
import yaml

from configuration import CONFIG_PATH, load_config
from sheets_writer import HEADER, _get_worksheet
from storage import Store


def aggregate(rows, header=HEADER):
    indexes = {name: index for index, name in enumerate(header)}
    counts = defaultdict(lambda: {"total": 0, "confirmed": 0, "human_reviewed": 0, "human_positive": 0})
    ai_used = False
    for number, row in enumerate(rows, 1):

        def value(name, row=row):
            index = indexes.get(name)
            return str(row[index]) if index is not None and index < len(row) else ""

        source, name, identity = value("Источник"), value("Группа/канал"), value("ID источника")
        key = (source, identity or f"unknown:row:{number}")
        item = counts[key]
        item.update(source=source, source_id=identity, group=name)
        item["total"] += 1
        verdict = value("Вердикт ИИ")
        ai_used |= bool(verdict)
        item["confirmed"] += int(verdict.startswith("клиент"))
    return sorted(
        counts.values(), key=lambda item: (item["confirmed"], item["total"], item["source_id"]), reverse=True
    ), ai_used


def from_store(store):
    from common import Lead

    records = {}
    ai_used = False
    for row in store.db.execute(
        "SELECT l.key,l.payload,l.ai_state,h.is_client FROM leads l LEFT JOIN human_reviews h ON h.lead_key=l.key ORDER BY l.first_seen,l.key"
    ):
        lead = Lead.from_dict(json.loads(row["payload"]))
        identity = lead.source_group_id or "unknown:" + row["key"]
        item = records.setdefault(
            identity,
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
        item[row["ai_state"] if row["ai_state"] in {"accepted", "rejected"} else "pending"] += 1
        if row["is_client"] is not None:
            item["human_reviewed"] += 1
            item["human_positive"] += int(row["is_client"])
    coverage = {item["id"]: item for item in store.summary()["sources"]}
    for identity, item in records.items():
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


def write_stats_sheet(cfg, records, ai_used):
    spreadsheet = _get_worksheet(cfg).spreadsheet
    try:
        worksheet = spreadsheet.worksheet("Топ источников")
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title="Топ источников", rows=max(10, len(records) + 1), cols=7)
    rows = [
        [
            "Источник",
            "ID источника",
            "Название",
            "Кандидаты",
            "Положительно ИИ",
            "Проверено человеком",
            "Положительно человеком",
        ]
    ]
    rows.extend(
        [
            [
                item[key]
                for key in (
                    "source",
                    "source_id",
                    "group",
                    "total",
                    "confirmed",
                    "human_reviewed",
                    "human_positive",
                )
            ]
            for item in records
        ]
    )
    if worksheet.row_count < len(rows):
        worksheet.add_rows(len(rows) - worksheet.row_count)
    worksheet.update(values=rows, range_name="A1", value_input_option="RAW")
    previous = worksheet.get_all_values()
    if len(previous) > len(rows):
        worksheet.batch_clear([f"A{len(rows) + 1}:G{len(previous)}"])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--write", action="store_true", help="Explicitly update the statistics worksheet")
    parser.add_argument("--human-label", metavar="SOURCE:ID")
    parser.add_argument("--is-client", choices=["yes", "no"])
    args = parser.parse_args(argv)
    if args.top <= 0 or bool(args.human_label) != bool(args.is_client):
        parser.error("positive --top; human label requires --is-client")
    cfg = load_config(args.config)
    from pathlib import Path

    from filelock import FileLock

    Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
    with FileLock(cfg["storage"]["lock_file"], timeout=0), Store(cfg["storage"]["database"]) as store:
        if args.human_label:
            store.human_label(args.human_label, args.is_client == "yes")
        records, ai_used = from_store(store)
        report = {"sources": records[: args.top], "coverage": store.summary()["sources"]}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(yaml.safe_dump(overrides(records, args.top), allow_unicode=True))
        if args.write:
            write_stats_sheet(cfg, records, ai_used)


if __name__ == "__main__":
    main()
