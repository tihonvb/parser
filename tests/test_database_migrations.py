"""Upgrade real v1 files without losing leads, reservations or unreleased feature data."""

import json
import sqlite3
from pathlib import Path

import pytest

from lead_parser.application.errors import StorageError
from lead_parser.infrastructure.persistence import schema
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.interfaces.cli.migrate_db import main


@pytest.fixture
def legacy(tmp_path):
    path = tmp_path / "parser.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parent / "fixtures/schema-v1.sql").read_text())
        db.execute(
            "INSERT INTO leads(key,payload,content_hash,first_seen,last_seen) VALUES(?,?,?,?,?)",
            (
                "vk:1",
                json.dumps({"source": "vk", "external_id": "1", "date": "", "text": "original"}),
                "hash",
                1,
                2,
            ),
        )
        db.execute(
            "INSERT INTO deliveries(id,lead_key,kind,destination,sheet_row,created) "
            "VALUES(71,'vk:1','sheets','sheet',17,1)"
        )
        db.execute("INSERT INTO sources VALUES('vk:1','{\"offset\":200}','{}',1)")
    return path


def contents(path):
    with sqlite3.connect(path) as db:
        return {
            name: db.execute(f"SELECT * FROM {name}").fetchall()
            for name in ("leads", "deliveries", "sources")
        }


def test_runtime_requires_explicit_migration_then_keeps_every_existing_record(legacy):
    before = contents(legacy)
    with pytest.raises(StorageError, match="migration"):
        Store(legacy)
    assert contents(legacy) == before
    assert schema.migrate_database(legacy) == {
        "from_version": 1,
        "to_version": 2,
        "migration": "web-claims-clicks-v2",
        "changed": True,
    }
    with Store(legacy) as store:
        assert store.lead("vk:1").text == "original"
        assert store.db.execute("PRAGMA user_version").fetchone()[0] == 2
        store.set_claim("vk:1", "10", "name", "taken")
        store.record_click("vk:1", "10")
    assert contents(legacy) == before
    assert schema.migrate_database(legacy)["changed"] is False
    with Store(legacy) as store:
        assert store.claim_of("vk:1", "10") == "taken"
        assert store.db.execute("SELECT count FROM clicks").fetchone()[0] == 1


def test_unreleased_version1_feature_tables_are_preserved(legacy):
    with sqlite3.connect(legacy) as db:
        for statement in schema.SCHEMA_SQL.split(";"):
            if statement.strip():
                db.execute(statement)
        db.execute("INSERT INTO claims VALUES('vk:1','10','name','taken',1)")
        db.execute("INSERT INTO clicks VALUES('vk:1','10',1,2,3)")
    schema.migrate_database(legacy)
    with sqlite3.connect(legacy) as db:
        assert db.execute("SELECT * FROM clicks").fetchall() == [("vk:1", "10", 1, 2, 3)]
        assert db.execute("SELECT status FROM claims").fetchone()[0] == "taken"


@pytest.mark.parametrize(
    "change",
    [
        "PRAGMA user_version=99",
        "ALTER TABLE leads ADD COLUMN surprise TEXT",
        "CREATE TABLE claims (unexpected TEXT)",
    ],
)
def test_unknown_schema_is_never_modified(legacy, change):
    with sqlite3.connect(legacy) as db:
        db.execute(change)
        before = list(db.iterdump())
    with pytest.raises(StorageError):
        schema.migrate_database(legacy)
    with sqlite3.connect(legacy) as db:
        assert list(db.iterdump()) == before


def test_failed_ddl_rolls_back_tables_and_version_together(legacy, monkeypatch):
    real_connect = sqlite3.connect

    class BrokenConnection(sqlite3.Connection):
        def execute(self, statement, *args):
            if "CREATE TABLE IF NOT EXISTS claims (" in statement:
                raise sqlite3.OperationalError("simulated disk failure")
            return super().execute(statement, *args)

    monkeypatch.setattr(
        schema.sqlite3, "connect", lambda *a, **kw: real_connect(*a, **kw, factory=BrokenConnection)
    )
    with pytest.raises(sqlite3.OperationalError):
        schema.migrate_database(legacy)
    with real_connect(legacy) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_schema WHERE name IN ('claims','clicks')").fetchall() == []


def test_cli_dry_run_backup_and_repeat_cannot_overwrite_backup(legacy, tmp_path, capsys):
    backup = tmp_path / "before.sqlite3"
    common = ["--database", str(legacy), "--lock-file", str(tmp_path / "parser.lock")]
    before = contents(legacy)
    assert main(common) == 0
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert not backup.exists()
    assert main([*common, "--apply", "--backup", str(backup)]) == 0
    assert contents(backup) == before == contents(legacy)
    assert backup.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
    with pytest.raises(SystemExit):
        main([*common, "--apply", "--backup", str(backup)])
    assert contents(backup) == before


def test_missing_database_and_missing_backup_never_create_database(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(StorageError):
        schema.migrate_database(path)
    with pytest.raises(SystemExit):
        main(["--database", str(path), "--apply"])
    assert not path.exists()


def test_orphaned_records_block_upgrade_without_losing_data(legacy):
    with sqlite3.connect(legacy) as db:
        db.execute("INSERT INTO human_reviews VALUES('vk:missing',1,1)")
    with pytest.raises(StorageError, match="orphaned"):
        schema.migrate_database(legacy)
    with sqlite3.connect(legacy) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1


@pytest.mark.parametrize("value", ["pen ding", "PENDING"])
def test_schema_validation_preserves_literal_whitespace_and_case(tmp_path, value):
    path = tmp_path / "changed.sqlite3"
    sql = (Path(__file__).parent / "fixtures/schema-v1.sql").read_text()
    with sqlite3.connect(path) as db:
        db.executescript(sql.replace("DEFAULT 'pending'", f"DEFAULT '{value}'"))
    with pytest.raises(StorageError, match="Unknown database structure"):
        schema.migrate_database(path)
