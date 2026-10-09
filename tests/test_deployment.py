from __future__ import annotations

import fcntl
import hashlib
import io
import json
import os
import pwd
import sqlite3
import subprocess
import tarfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy import install_release as installer

SHA = "a" * 40
VERSION = "0.2.0"
WHEEL = f"remont_lead_parser-{VERSION}-py3-none-any.whl"


def bundle(path, *, additions=None, corrupt=None):
    files = {"requirements.txt": b"locked dependencies\n", WHEEL: b"test wheel"}
    manifest = {
        "schema_version": 1,
        "sha": SHA,
        "version": VERSION,
        "files": {name: hashlib.sha256(value).hexdigest() for name, value in files.items()},
    }
    files["manifest.json"] = json.dumps(manifest).encode()
    if corrupt:
        files[corrupt] += b"changed"
    with tarfile.open(path, "w:gz") as archive:
        for name, value in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
        for member in additions or ():
            archive.addfile(member)
    return path


class Host:
    def __init__(self, options, *, timer_active=True, fail=None):
        self.options = options
        self.timer_active = timer_active
        self.calls = []
        self.fail = fail
        self.database = options.config.parent / "parser.sqlite3"
        self.lock = options.config.parent / "parser.lock"
        self.config_before = options.config.read_bytes()
        self.needs_chromium = False
        self.schema_version = 1
        self.change_schema = False
        self.probed = None
        self.service_states = []

    def __call__(self, argv, **_kwargs):
        self.calls.append(argv)
        stage = None
        output = ""
        if argv[:2] == ["systemctl", "show"]:
            if "--property=User" in argv:
                output = f"User={pwd.getpwuid(os.getuid()).pw_name}\nGroup=\n"
            elif argv[2] == self.options.timer:
                output = "active" if self.timer_active else "inactive"
            else:
                output = self.service_states.pop(0) if self.service_states else "inactive"
        elif argv[:2] == ["systemctl", "stop"]:
            assert argv[2] == self.options.timer, "Deployment must never interrupt an active run"
            self.timer_active = False
        elif argv[:2] == ["systemctl", "start"]:
            self.timer_active = True
            stage = "start"
        elif "--require-hashes" in argv:
            stage = "pip"
        elif "--check-config" in argv:
            stage = "config"
        elif installer.CONFIG_PROBE in argv:
            output = json.dumps(
                {
                    "database": str(self.database),
                    "lock_file": str(self.lock),
                    "paths": [str(self.database), str(self.lock)],
                    "needs_chromium": self.needs_chromium,
                }
            )
        elif installer.SNAPSHOT_PROBE in argv:
            stage = "snapshot"
            self.probed = Path(argv[-1])
            assert self.probed != self.database
            assert not self.timer_active
            # The app lock must cover both snapshot validation and switching.
            with pytest.raises(installer.DeployError, match="lock"):
                with installer.exclusive_lock(self.lock):
                    pytest.fail("Application lock was not held")
            with sqlite3.connect(self.probed) as snapshot:
                snapshot.execute(f"PRAGMA user_version={self.schema_version}")
                if self.change_schema:
                    snapshot.execute("CREATE TABLE disposable_probe (value TEXT)")
                if self.database.exists():
                    snapshot.execute("UPDATE sent SET message='candidate touched only this copy'")
        code = 1 if stage and stage == self.fail else 0
        return subprocess.CompletedProcess(argv, code, stdout=output, stderr="secret API token")


@pytest.fixture
def options(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    config = runtime / "config.yaml"
    config.write_text("general: {}\n")
    return installer.DeployOptions(
        bundle(tmp_path / "release.tar.gz"),
        SHA,
        VERSION,
        root=tmp_path / "app",
        config=config,
        python=Path("/usr/bin/python3"),
        wait_seconds=0.01,
    )


def old_release(options):
    old = options.root / "releases" / "old"
    old.mkdir(parents=True)
    (options.root / "current").symlink_to(old)
    return old


def seed_database(host):
    with sqlite3.connect(host.database) as db:
        db.execute("PRAGMA user_version=1")
        db.execute("CREATE TABLE sent (message TEXT)")
        db.execute("INSERT INTO sent VALUES ('keep delivery history')")


def assert_live_preserved(host):
    assert host.options.config.read_bytes() == host.config_before
    with sqlite3.connect(host.database) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        assert db.execute("SELECT message FROM sent").fetchall() == [("keep delivery history",)]


def test_success_uses_backup_then_atomic_switch_and_preserves_runtime(options):
    previous = old_release(options)
    host = Host(options)
    seed_database(host)
    receipt = installer.deploy(options, runner=host)
    assert Path(receipt["release"]) == (options.root / "current").resolve()
    assert receipt["previous"] == str(previous)
    assert receipt["sha"] == SHA
    with sqlite3.connect(receipt["backup"]) as backup:
        assert backup.execute("PRAGMA user_version").fetchone()[0] == 1
        assert backup.execute("SELECT message FROM sent").fetchone()[0] == "keep delivery history"
    assert Path(receipt["backup"]).stat().st_mode & 0o777 == 0o600
    assert host.timer_active
    assert_live_preserved(host)
    assert host.probed and not host.probed.exists()
    assert not any("playwright" in argv for argv in host.calls)
    assert not any("--deliver-only" in argv for argv in host.calls)


def test_repeat_completed_release_is_idempotent(options):
    host = Host(options)
    receipt = installer.deploy(options, runner=host)
    host.calls.clear()
    assert installer.deploy(options, runner=host) == receipt
    assert len(host.calls) == 1  # Only systemd runtime identity is inspected.
    assert not host.database.exists()


def test_disabled_timer_stays_disabled_and_chromium_is_conditional(options):
    host = Host(options, timer_active=False)
    host.needs_chromium = True
    installer.deploy(options, runner=host)
    assert not host.timer_active
    assert not any(argv[:2] == ["systemctl", "start"] for argv in host.calls)
    assert any(argv[-4:] == ["-m", "playwright", "install", "chromium"] for argv in host.calls)


@pytest.mark.parametrize("stage", ["pip", "config", "snapshot"])
def test_pre_activation_failure_preserves_old_release_and_schedule(options, stage):
    previous = old_release(options)
    host = Host(options, fail=stage)
    seed_database(host)
    with pytest.raises(installer.DeployError) as error:
        installer.deploy(options, runner=host)
    assert "secret" not in str(error.value)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active
    assert_live_preserved(host)
    if stage in {"pip", "config"}:
        assert not any(argv[:2] == ["systemctl", "stop"] for argv in host.calls)


def test_activation_failure_stops_schedule_without_unsafe_code_or_db_rollback(options):
    previous = old_release(options)
    host = Host(options, fail="start")
    seed_database(host)
    with pytest.raises(installer.DeployError, match="candidate remains current"):
        installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() != previous
    assert not host.timer_active
    assert_live_preserved(host)
    host.fail = None
    with pytest.raises(installer.DeployError, match="already exists"):
        installer.deploy(options, runner=host)


def test_failed_preparation_kept_for_inspection_and_can_be_retried(options):
    host = Host(options, fail="pip")
    with pytest.raises(installer.DeployError):
        installer.deploy(options, runner=host)
    assert len(list((options.root / "releases").glob(".failed-*"))) == 1
    host.fail = None
    assert installer.deploy(options, runner=host)["status"] == "deployed"


@pytest.mark.parametrize("change_schema", [False, True])
def test_automatic_schema_migration_rejected_before_switch(options, change_schema):
    previous = old_release(options)
    host = Host(options)
    if change_schema:
        host.change_schema = True
    else:
        host.schema_version = 2
    seed_database(host)
    with pytest.raises(installer.DeployError, match="explicit migration"):
        installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active
    assert_live_preserved(host)


def test_backup_failure_restores_schedule(options, monkeypatch):
    previous = old_release(options)
    host = Host(options)
    seed_database(host)

    def broken_backup(*_args):
        raise installer.DeployError("Snapshot failed")

    monkeypatch.setattr(installer, "sqlite_snapshot", broken_backup)
    with pytest.raises(installer.DeployError, match="Snapshot failed"):
        installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active


def test_receipt_failure_after_switch_rolls_code_back_before_timer_starts(options, monkeypatch):
    previous = old_release(options)
    host = Host(options)

    def fail_receipt(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(installer, "write_receipt", fail_receipt)
    with pytest.raises(OSError):
        installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active


def test_active_service_timeout_keeps_old_release_and_restores_timer(options):
    previous = old_release(options)
    host = Host(options)
    host.service_states = ["activating"] * 20
    with pytest.raises(installer.DeployError, match="still running"):
        installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active


def test_manual_parser_lock_prevents_switch(options):
    previous = old_release(options)
    host = Host(options)
    with installer.exclusive_lock(host.lock):
        with pytest.raises(installer.DeployError, match="lock"):
            installer.deploy(options, runner=host)
    assert (options.root / "current").resolve() == previous
    assert host.timer_active


def test_concurrent_deployment_excluded_before_any_command(options):
    options.root.mkdir()
    host = Host(options)
    with installer.exclusive_lock(options.root / ".deploy.lock"):
        with pytest.raises(installer.DeployError, match="lock"):
            installer.deploy(options, runner=host)
    assert host.calls == []


@pytest.mark.parametrize("name", ["../escaped", "/absolute", "./manifest.json", "manifest.json"])
def test_archive_extra_duplicate_or_traversal_member_rejected(options, tmp_path, name):
    malicious = tarfile.TarInfo(name)
    archive = bundle(tmp_path / "malicious.tar.gz", additions=[malicious])
    destination = tmp_path / "extracted"
    destination.mkdir()
    with pytest.raises(installer.DeployError):
        installer.extract_bundle(archive, destination, SHA, VERSION)


def test_archive_symlink_rejected(options, tmp_path):
    archive = tmp_path / "symlink.tar.gz"
    with tarfile.open(options.archive) as source, tarfile.open(archive, "w:gz") as target:
        for member in source.getmembers():
            if member.name == "requirements.txt":
                member.type = tarfile.SYMTYPE
                member.linkname = "/etc/passwd"
                member.size = 0
                target.addfile(member)
            else:
                target.addfile(member, source.extractfile(member))
    destination = tmp_path / "extracted"
    destination.mkdir()
    with pytest.raises(installer.DeployError, match="archive member"):
        installer.extract_bundle(archive, destination, SHA, VERSION)


def test_hash_mismatch_and_wrong_manifest_identity_rejected(options, tmp_path):
    for label, archive, sha in [
        ("hash", bundle(tmp_path / "corrupt.tar.gz", corrupt=WHEEL), SHA),
        ("identity", options.archive, "b" * 40),
    ]:
        destination = tmp_path / label
        destination.mkdir()
        with pytest.raises(installer.DeployError):
            installer.extract_bundle(archive, destination, sha, VERSION)


def test_current_directory_and_release_collision_never_overwritten(options):
    current = options.root / "current"
    current.mkdir(parents=True)
    (current / "keep.txt").write_text("keep")
    with pytest.raises(installer.DeployError, match="non-symlink"):
        installer.deploy(options, runner=Host(options))
    assert (current / "keep.txt").read_text() == "keep"
    shutil_marker = options.root / "releases" / f"{VERSION}-{SHA[:12]}"
    shutil_marker.mkdir(parents=True)
    (current / "keep.txt").unlink()
    current.rmdir()
    with pytest.raises(installer.DeployError, match="already exists"):
        installer.deploy(options, runner=Host(options))


def test_python_in_home_rejected_for_systemd_sandbox(options):
    with pytest.raises(installer.DeployError, match="ProtectHome"):
        installer.deploy(replace(options, python=Path("/root/.local/bin/python")), runner=Host(options))


def test_lock_remains_at_stable_path_and_excludes_filelock_compatible_flock(tmp_path):
    path = tmp_path / "lock"
    with installer.exclusive_lock(path):
        with path.open("a") as other:
            with pytest.raises(BlockingIOError):
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert path.exists()


def test_sqlite_backup_includes_committed_wal_records(tmp_path):
    database = tmp_path / "live.sqlite3"
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(database) as live:
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("CREATE TABLE sent (message TEXT)")
        live.execute("INSERT INTO sent VALUES ('committed in WAL')")
        live.commit()
        assert Path(str(database) + "-wal").exists()
        installer.sqlite_snapshot(database, backup)
        with sqlite3.connect(backup) as copied:
            assert copied.execute("SELECT message FROM sent").fetchall() == [("committed in WAL",)]
        assert live.execute("SELECT count(*) FROM sent").fetchone()[0] == 1


@pytest.mark.skipif(os.geteuid() != 0, reason="Checking ownership across runtime/root needs root")
def test_root_readonly_backup_does_not_create_root_owned_runtime_sidecars(tmp_path):
    database = tmp_path / "live.sqlite3"
    live = sqlite3.connect(database)
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE sent (message TEXT)")
    live.commit()
    live.close()
    os.chown(database, 65534, 65534)
    database.chmod(0o600)
    before = (database.stat().st_uid, database.stat().st_gid, database.stat().st_mode)
    installer.sqlite_snapshot(database, tmp_path / "backup.sqlite3")
    assert (database.stat().st_uid, database.stat().st_gid, database.stat().st_mode) == before
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(database) + suffix)
        if sidecar.exists():
            assert sidecar.stat().st_uid == 65534
            assert sidecar.stat().st_gid == 65534
            assert sidecar.stat().st_mode & 0o777 == 0o600


def test_symlink_root_normalized_before_release_guard_and_idempotence(options, tmp_path):
    options.root.mkdir()
    alias = tmp_path / "app-alias"
    alias.symlink_to(options.root, target_is_directory=True)
    aliased = replace(options, root=alias)
    host = Host(aliased)
    receipt = installer.deploy(aliased, runner=host)
    assert Path(receipt["release"]).is_relative_to(options.root)
    assert installer.deploy(aliased, runner=host) == receipt


def test_failed_activation_with_symlink_root_keeps_current_target_intact(options, tmp_path):
    options.root.mkdir()
    alias = tmp_path / "app-alias"
    alias.symlink_to(options.root, target_is_directory=True)
    aliased = replace(options, root=alias)
    host = Host(aliased, fail="start")
    with pytest.raises(installer.DeployError, match="candidate remains current"):
        installer.deploy(aliased, runner=host)
    current = alias / "current"
    assert current.is_dir()
    assert json.loads((current / "deployment.json").read_text())["status"] == "prepared"
    assert not list((options.root / "releases").glob(".failed-*"))
    assert not host.timer_active


def test_runtime_path_inside_symlinked_release_root_is_rejected(options, tmp_path):
    options.root.mkdir()
    alias = tmp_path / "app-alias"
    alias.symlink_to(options.root, target_is_directory=True)
    aliased = replace(options, root=alias)
    host = Host(aliased)
    host.database = alias / "releases" / "runtime.sqlite3"
    with pytest.raises(installer.DeployError, match="outside release"):
        installer.deploy(aliased, runner=host)
    assert host.timer_active
    assert not (alias / "current").exists()


def test_receipt_finalization_failure_preserves_prepared_record_and_stops_timer(options, monkeypatch):
    host = Host(options)
    original = installer.write_receipt

    def fail_final(path, receipt):
        if receipt["status"] == "deployed":
            raise OSError("disk full")
        original(path, receipt)

    monkeypatch.setattr(installer, "write_receipt", fail_final)
    with pytest.raises(installer.DeployError, match="candidate remains current"):
        installer.deploy(options, runner=host)
    receipt = json.loads((options.root / "current" / "deployment.json").read_text())
    assert receipt["status"] == "prepared"
    assert not host.timer_active


def test_config_checks_use_systemd_explicit_primary_group(options, monkeypatch):
    host = Host(options)
    release = options.root / "releases" / "test"
    release.mkdir(parents=True)
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.grp, "getgrgid", lambda _gid: SimpleNamespace(gr_name="runtime-group"))
    installer.prepare_release(options, release, host, ("runtime-user", 1234, 4321))
    probes = [argv for argv in host.calls if argv[0] == "runuser"]
    assert len(probes) == 2
    assert all(argv[:6] == ["runuser", "-u", "runtime-user", "-g", "runtime-group", "--"] for argv in probes)
