"""Economics distinguish missing billing, unique deliveries and current lead cohorts."""

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from lead_parser.application.analytics import Period, build_report, money, summarize_usage
from lead_parser.core.models import Lead
from lead_parser.infrastructure.persistence.analytics import read_snapshot, read_usage
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.interfaces.cli.analytics import main


def stamp(day, hour=12):
    return datetime(2026, 10, day, hour, tzinfo=UTC).timestamp()


@pytest.fixture
def snapshot(tmp_path):
    database = tmp_path / "snapshot.sqlite3"
    with Store(database) as store:

        def add(key, seen, state="accepted", group="vk:12", name="Соседи", human=None):
            lead = Lead(
                source="vk",
                date="2026-10-02T12:00:00+00:00",
                external_id=key,
                text="private text",
                phone="+79990000000",
                author="private author",
                url=f"https://vk.com/wall{key}",
                source_group_id=group,
                source_group=name,
                extra={"ai_is_client": state == "accepted", "ai_reason": "Ремонт"},
            )
            store.db.execute(
                "INSERT INTO leads (key,payload,content_hash,ai_state,first_seen,last_seen) VALUES(?,?,?,?,?,?)",
                (lead.dedupe_key(), json.dumps(asdict(lead)), "hash", state, seen, seen),
            )
            if human is not None:
                store.db.execute(
                    "INSERT INTO human_reviews VALUES (?,?,?)", (lead.dedupe_key(), int(human), stamp(9))
                )

        def deliver(key, kind, destination, when, state="done"):
            store.db.execute(
                "INSERT INTO deliveries (lead_key,kind,destination,state,created,delivered) VALUES (?,?,?,?,?,?)",
                (f"vk:{key}", kind, destination, state, stamp(1), when),
            )

        add("old", stamp(1))
        add("already", stamp(1))
        add("new", stamp(2), human=True)
        add("other", stamp(2), state="rejected", group="vk:13", name="Соседи", human=False)
        add("pending", stamp(2), state="pending")
        add("review", stamp(2), state="review")
        add("future", stamp(3))
        deliver("old", "sheets", "sheet", stamp(2))
        deliver("already", "sheets", "sheet", stamp(1))
        deliver("already", "telegram", "chat", stamp(2))
        deliver("new", "sheets", "sheet", stamp(2))
        deliver("new", "telegram", "chat1", stamp(2))
        deliver("new", "telegram", "chat2", stamp(2))
        deliver("new", "telegram", "chat3", None, "pending")
        deliver("future", "sheets", "sheet", stamp(3))
    return database


def event(request_id="1", event_type="finished", **kwargs):
    return {
        "schema_version": 1,
        "event": event_type,
        "request_id": request_id,
        "started_at": "2026-10-02T12:00:00+00:00",
        "occurred_at": "2026-10-02T12:01:00+00:00",
        "purpose": "pipeline",
        "provider": "openrouter",
        "requested_model": "model",
        "batch_size": 2,
        "outcome": "success",
        "prompt_tokens": 10,
        "completion_tokens": 5,
        "cost_usd": "0.03",
        **kwargs,
    }


def test_period_midnight_timezone_and_exclusive_end():
    period = Period.dates("2026-10-02", "2026-10-03", "Europe/Moscow")
    assert period.contains(stamp(1, 21))
    assert not period.contains(stamp(1, 20))
    assert period.contains(stamp(2, 20))
    assert not period.contains(stamp(2, 21))
    dst = Period.dates("2026-10-25", "2026-10-26", "Europe/Berlin")
    assert dst.end.timestamp() - dst.start.timestamp() == 25 * 3600


@pytest.mark.parametrize(
    "start,end,zone",
    [
        ("20261002", "2026-10-03", "UTC"),
        ("2026-10-02", "2026-10-02", "UTC"),
        ("2026-10-02", "2026-10-03", "Moon"),
    ],
)
def test_invalid_period_rejected(start, end, zone):
    with pytest.raises(ValueError):
        Period.dates(start, end, zone)


def test_snapshot_report_keeps_cohort_delivery_jobs_and_first_deliveries_separate(snapshot):
    period = Period.dates("2026-10-02", "2026-10-03")
    leads, deliveries = read_snapshot(snapshot, period)
    report = build_report(
        leads,
        deliveries,
        [],
        period,
        ai_cost_usd=Decimal("1.25"),
        fixed_cost_usd=Decimal("2.75"),
        include_leads=True,
    )
    assert report["cohort"]["total"] == 4
    assert {state: report["cohort"][state] for state in ("accepted", "rejected", "pending", "review")} == {
        "accepted": 1,
        "rejected": 1,
        "pending": 1,
        "review": 1,
    }
    assert report["cohort"]["human_positive_rate"] == 0.5
    assert report["cohort"]["delivered_any_channel"] == 1
    assert report["cohort"]["delivered_by_channel"] == {"sheets": 1, "telegram": 1}
    assert report["cohort"]["delivery_jobs_by_current_state"] == {"done": 3, "pending": 1}
    assert report["period_deliveries"] == {
        "unique_leads_any_channel": 3,
        "unique_leads_first_delivered": 2,
        "unique_leads_by_channel": {"sheets": 2, "telegram": 2},
    }
    assert report["economics"]["total_cost_usd"] == "4.00"
    assert report["economics"]["operational_cost_per_delivered_lead_usd"] == "2.00"
    assert len(report["sources"]) == 2  # Same name, distinct group IDs.
    assert report["accepted_leads"][0]["url"] == "https://vk.com/wallnew"
    encoded = json.dumps(report)
    assert "private text" not in encoded and "private author" not in encoded and "+79990000000" not in encoded


def test_snapshot_never_creates_or_changes_files(snapshot, tmp_path):
    period = Period.dates("2026-10-02", "2026-10-03")
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    read_snapshot(snapshot, period)
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir()}
    assert before == after
    missing = tmp_path / "missing.sqlite3"
    with pytest.raises(ValueError, match="does not exist"):
        read_snapshot(missing, period)
    assert not missing.exists()


def test_live_wal_and_wrong_schema_rejected(snapshot, tmp_path):
    period = Period.dates("2026-10-02", "2026-10-03")
    wal = tmp_path / (snapshot.name + "-wal")
    wal.write_bytes(b"active WAL")
    with pytest.raises(ValueError, match="standalone SQLite backup"):
        read_snapshot(snapshot, period)
    wal.unlink()
    with sqlite3.connect(snapshot) as db:
        db.execute("PRAGMA user_version=999")
    db.close()
    with pytest.raises(ValueError, match="expected version 1"):
        read_snapshot(snapshot, period)


def test_usage_deduplicates_attempts_excludes_evaluation_and_counts_unknowns():
    period = Period.dates("2026-10-02", "2026-10-03")
    finished = event()
    events = [
        event(event_type="started"),
        finished,
        finished,
        event("crash", "started"),
        event("fail", outcome="error", cost_usd=None, prompt_tokens=None, completion_tokens=None),
        event("eval", purpose="evaluation", cost_usd="999"),
        event("zero", cost_usd="0"),
        event("previous", started_at="2026-10-01T12:00:00+00:00"),
        event("boundary", started_at="2026-10-03T00:00:00+00:00"),
    ]
    usage = summarize_usage(events, period, available=True)
    assert usage["requests"] == 4
    assert usage["known_cost_usd"] == "0.03"
    assert usage["requests_with_known_cost"] == 2
    assert usage["requests_with_unknown_cost"] == 2
    assert usage["unfinished"] == 1
    assert usage["errors"] == 1
    assert usage["prompt_tokens"] == 20
    assert usage["requests_with_incomplete_tokens"] == 2


def test_conflicting_duplicate_cost_never_silently_accepted():
    period = Period.dates("2026-10-02", "2026-10-03")
    usage = summarize_usage([event(), event(cost_usd="99")], period, available=True)
    assert usage["conflicting_requests"] == 1
    assert usage["requests"] == 1
    assert usage["known_cost_usd"] == "0"
    assert usage["requests_with_unknown_cost"] == 1


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1", True, None, [], "invalid"])
def test_nonfinite_negative_and_missing_costs_never_become_zero(bad):
    with pytest.raises(ValueError):
        money(bad)
    usage = summarize_usage([event(cost_usd=bad)], Period.dates("2026-10-02", "2026-10-03"), available=True)
    assert usage["requests_with_unknown_cost"] == 1
    assert usage["requests_with_known_cost"] == 0


def test_unknown_coverage_prevents_total_even_with_complete_logged_requests():
    period = Period.dates("2026-10-02", "2026-10-03")
    report = build_report([], [], [event()], period, usage_available=True, fixed_cost_usd=Decimal(0))
    assert report["ai_usage"]["known_cost_usd"] == "0.03"
    assert report["economics"]["ai_cost_usd"] is None
    assert report["economics"]["total_complete"] is False
    assert report["economics"]["total_cost_usd"] is None
    override = build_report(
        [], [], [event()], period, usage_available=True, ai_cost_usd=Decimal("2"), fixed_cost_usd=Decimal(0)
    )
    assert override["economics"]["total_cost_usd"] == "2"
    assert override["economics"]["operational_cost_per_delivered_lead_usd"] is None
    missing_fixed = build_report([], [], [], period, ai_cost_usd=Decimal("2"))
    assert missing_fixed["economics"]["total_cost_usd"] is None


def test_malformed_journal_warns_and_reading_missing_does_not_create(tmp_path):
    path = tmp_path / "usage.jsonl"
    assert read_usage(path) == ([], False, 0)
    assert not path.exists()
    path.write_text(json.dumps(event()) + '\n{"truncated"\n42\n', encoding="utf-8")
    events, available, invalid = read_usage(path)
    report = build_report(
        [],
        [],
        events,
        Period.dates("2026-10-02", "2026-10-03"),
        usage_available=available,
        invalid_usage_lines=invalid,
    )
    assert report["ai_usage"]["invalid_lines"] == 2
    assert any("некорректные" in warning for warning in report["warnings"])


def test_cli_has_no_config_and_auto_usage_path(snapshot, capsys):
    usage_path = snapshot.with_name(snapshot.name + ".usage.jsonl")
    usage_path.write_text(json.dumps(event()) + "\n", encoding="utf-8")
    common = ["--database", str(snapshot), "--from", "2026-10-02", "--to", "2026-10-03"]
    assert main([*common, "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ai_usage"]["requests"] == 1
    assert "accepted_leads" not in report
    assert report["period"]["timezone"] == "UTC"
    assert main([*common, "--ai-cost-usd", "0", "--fixed-cost-usd", "0", "--include-leads"]) == 0
    output = capsys.readouterr().out
    assert "https://vk.com/wallnew" in output
    assert "private text" not in output and "+79990000000" not in output
    with pytest.raises(SystemExit) as error:
        main([*common, "--ai-cost-usd", "NaN"])
    assert error.value.code == 2
