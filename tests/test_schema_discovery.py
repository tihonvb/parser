from copy import deepcopy
from types import SimpleNamespace

import pytest

from lead_parser.application.statistics import from_store, overrides
from lead_parser.infrastructure.configuration import ConfigError
from lead_parser.infrastructure.integrations.google_sheets.gateway import SchemaConflict
from lead_parser.infrastructure.integrations.google_sheets.migration import (
    infer_identity,
    migrate,
    migrate_rows,
)
from lead_parser.infrastructure.integrations.google_sheets.reporting import aggregate
from lead_parser.infrastructure.integrations.google_sheets.schema import HEADER, LEGACY_HEADER, lead_to_row
from lead_parser.infrastructure.integrations.vk.assessment import _evaluate
from lead_parser.infrastructure.integrations.vk.discovery import _get_city_id, find_candidates
from lead_parser.infrastructure.persistence.sqlite import Store


def test_migrate_legacy_preserves_all_rows_and_extra_columns():
    row = [
        "date",
        "vk",
        "same name",
        "author",
        "=text",
        "+70000000000",
        "",
        "https://vk.com/wall-12_9",
        "",
        "user note",
    ]
    migrated, unresolved = migrate_rows([LEGACY_HEADER + ["Notes"], row])
    assert migrated[0] == HEADER + ["Notes"] and migrated[1][:9] == row[:9]
    assert migrated[1][9:11] == ["vk:-12_9", "vk:12"] and migrated[1][-1] == "user note" and unresolved == 0
    assert infer_identity("telegram", "https://t.me/username/1") == ("", "")
    with pytest.raises(SchemaConflict):
        migrate_rows([["unknown columns"]])


def test_migration_dry_run_and_backup_precedes_write():
    calls = []
    ws = SimpleNamespace(
        title="Leads",
        id=1,
        col_count=9,
        get_all_values=lambda: [LEGACY_HEADER],
        spreadsheet=SimpleNamespace(duplicate_sheet=lambda *args, **kwargs: calls.append("backup")),
        add_cols=lambda *args: calls.append("resize"),
        update=lambda **kwargs: calls.append("write"),
        freeze=lambda **kwargs: calls.append("freeze"),
    )
    assert migrate(ws)["changed"] and calls == []
    migrate(ws, apply=True)
    assert calls == ["backup", "resize", "write", "freeze"]


def test_stable_ids_disambiguate_same_name_and_track_renames(cfg, lead):
    second = deepcopy(lead)
    second.external_id = "-13_9"
    second.source_group_id = "vk:13"
    renamed = deepcopy(lead)
    renamed.external_id = "-12_10"
    renamed.source_group = "New name"
    records, _ = aggregate([lead_to_row(lead), lead_to_row(second), lead_to_row(renamed)])
    assert len(records) == 2 and {r["source_id"]: r["total"] for r in records} == {"vk:12": 2, "vk:13": 1}
    assert set(overrides(records)["vk"]["group_overrides"]) == {"12", "13"}
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead, second, renamed])
        store.human_label(lead.dedupe_key(), True)
        stats, _ = from_store(store)
        assert sum(item["human_reviewed"] for item in stats) == 1
        assert sum(item["confirmed"] for item in stats) == 0


def test_unknown_source_ids_are_not_merged_by_name(lead):
    lead.source_group_id = ""
    rows, _ = aggregate([lead_to_row(lead), lead_to_row(lead)])
    assert len(rows) == 2


def test_unknown_city_does_not_fall_back_national(cfg):
    client = SimpleNamespace(
        get_api=lambda: SimpleNamespace(database=SimpleNamespace(getCities=lambda **kwargs: {"items": []}))
    )
    with pytest.raises(ConfigError):
        find_candidates(cfg, client)
    ambiguous = SimpleNamespace(
        database=SimpleNamespace(
            getCities=lambda **kwargs: {"items": [{"title": "Самара", "id": 1}, {"title": "Самара", "id": 2}]}
        )
    )
    with pytest.raises(ConfigError):
        _get_city_id(ambiguous, "Самара")


def test_discovery_requests_members_keeps_unknown_city_scoped(cfg):
    cfg["discovery"]["city_id"] = 123
    calls = []

    def search(**params):
        calls.append(params)
        return {"items": [{"id": 12, "name": "group"}]}

    def details(**params):
        assert params["fields"] == "members_count,city,wall"
        return {"groups": [{"id": 12, "name": "group", "city": {"id": 123}}]}

    api = SimpleNamespace(groups=SimpleNamespace(search=search, getById=details))
    result = find_candidates(cfg, SimpleNamespace(get_api=lambda: api))
    assert result[0]["members_count"] is None and all(
        item["city_id"] == 123 and "Самара" in item["q"] for item in calls
    )


def test_discovery_reports_actual_depth(cfg):
    import time

    cfg["discovery"]["max_pages"] = 1
    client = SimpleNamespace(
        method=lambda *args: {"items": [{"date": int(time.time()), "text": "Нужен ремонт"}] * 100}
    )
    result = _evaluate(client, [{"id": 12}], cfg)
    assert result[0]["posts_in_window"] == 100 and result[0]["complete"] is False
