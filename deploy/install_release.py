#!/usr/bin/env python3
"""Install a verified release on one Linux/systemd host without running the pipeline."""

from __future__ import annotations

import argparse
import fcntl
import grp
import hashlib
import json
import os
import pwd
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path


class DeployError(Exception):
    pass


@dataclass(frozen=True)
class DeployOptions:
    archive: Path
    sha: str
    version: str
    root: Path = Path("/opt/parser")
    config: Path = Path("/var/lib/parser/config.yaml")
    python: Path = Path("/usr/bin/python3.12")
    service: str = "parser.service"
    timer: str = "parser.timer"
    web_service: str = "parser-web.service"
    health_seconds: float = 30
    wait_seconds: float = 900


CONFIG_PROBE = """
import json, sys
from lead_parser.infrastructure.configuration import load_config, web_required
cfg = load_config(sys.argv[1])
paths = [cfg['storage'][key] for key in ('database', 'lock_file', 'seen_store')]
paths += [cfg['telegram']['session_name'], cfg['vk']['token_file'], cfg['vk']['state_file']]
paths += [cfg['output']['google_sheets']['credentials_file'], cfg['profi_mail']['oauth']['token_file']]
host = cfg['web']['host']
host = '127.0.0.1' if host in ('0.0.0.0', '') else '::1' if host == '::' else host
host = '[' + host + ']' if ':' in host else host
print(json.dumps({'database': cfg['storage']['database'], 'lock_file': cfg['storage']['lock_file'],
                  'paths': paths, 'web_required': web_required(cfg),
                  'health_url': 'http://' + host + ':' + str(cfg['web']['port']) + '/healthz',
                  'needs_chromium': bool(cfg['avito']['enabled']
                  and not cfg['avito']['cdp_endpoint'] and not cfg['avito']['dolphin']['enabled'])}))
"""

SNAPSHOT_PROBE = """
import json, sys
from lead_parser.infrastructure.persistence.schema import migrate_database
print(json.dumps(migrate_database(sys.argv[1], initialize=True)))
"""

HEALTH_PROBE = """
import json, sys, time
from urllib.request import urlopen
url, version, timeout = sys.argv[1:]
deadline = time.monotonic() + float(timeout)
while True:
    try:
        with urlopen(url, timeout=2) as response:
            data = json.load(response)
        if data == {'status': 'ok', 'schema_version': 2, 'version': version}:
            break
    except Exception:
        pass
    if time.monotonic() >= deadline:
        raise SystemExit(1)
    time.sleep(0.5)
"""


def command(runner, argv, *, env=None, allowed=(0,)):
    """Never emit child output: configuration, pip URLs and SDK errors can contain secrets."""
    try:
        result = runner(argv, check=False, capture_output=True, text=True, env=env)
    except (OSError, subprocess.SubprocessError) as error:
        raise DeployError(f"Cannot execute deployment command: {Path(argv[0]).name}") from error
    if result.returncode not in allowed:
        raise DeployError(f"Deployment command failed: {Path(argv[0]).name} (exit {result.returncode})")
    return result.stdout.strip()


def validate_options(options):
    if not re.fullmatch(r"[0-9a-f]{40}", options.sha):
        raise DeployError("SHA must be 40 lowercase hexadecimal characters")
    if not re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", options.version):
        raise DeployError("Version must be stable X.Y.Z")
    for unit, suffix in (
        (options.service, "service"),
        (options.timer, "timer"),
        (options.web_service, "service"),
    ):
        if not re.fullmatch(r"[A-Za-z0-9_-]+\." + suffix, unit):
            raise DeployError("Invalid systemd unit name")
    if options.wait_seconds <= 0 or options.health_seconds <= 0:
        raise DeployError("Wait timeout must be positive")
    if not options.config.is_absolute() or not options.root.is_absolute() or not options.python.is_absolute():
        raise DeployError("Root, config and Python paths must be absolute")
    python = options.python.resolve()
    if python.is_relative_to("/home") or python.is_relative_to("/root"):
        raise DeployError("Python must be outside home directories for ProtectHome")
    if not options.config.is_file():
        raise DeployError("Runtime config does not exist; provision the server first")


def extract_bundle(archive, destination, sha, version):
    """Extract only the three expected regular files; never use tarfile.extractall."""
    try:
        with tarfile.open(archive, "r:gz") as bundle:
            members = bundle.getmembers()
            names = [member.name for member in members]
            wheels = [name for name in names if re.fullmatch(r"remont_lead_parser-[^/]+\.whl", name)]
            if (
                len(names) != 3
                or len(wheels) != 1
                or set(names) != {"manifest.json", "requirements.txt", *wheels}
            ):
                raise DeployError("Release must contain exactly manifest, requirements and one wheel")
            for member in members:
                limit = 128 * 1024 * 1024 if member.name.endswith(".whl") else 1024 * 1024
                if not member.isfile() or member.size > limit or member.size < 0:
                    raise DeployError("Unsupported archive member")
                with bundle.extractfile(member) as source, (destination / member.name).open("xb") as target:
                    shutil.copyfileobj(source, target)
                (destination / member.name).chmod(0o644)
        manifest = json.loads((destination / "manifest.json").read_text())
        if (
            manifest.get("schema_version") != 1
            or manifest.get("sha") != sha
            or manifest.get("version") != version
        ):
            raise DeployError("Manifest does not match the requested release")
        expected = {"requirements.txt", wheels[0]}
        if not isinstance(manifest.get("files"), dict) or set(manifest["files"]) != expected:
            raise DeployError("Manifest file list is invalid")
        for name in expected:
            digest = hashlib.sha256((destination / name).read_bytes()).hexdigest()
            if manifest["files"][name] != digest:
                raise DeployError("Release file checksum mismatch")
        if not wheels[0].startswith(f"remont_lead_parser-{version}-"):
            raise DeployError("Wheel name does not match release version")
        return destination / wheels[0]
    except (OSError, tarfile.TarError, ValueError, TypeError, AttributeError) as error:
        raise DeployError("Cannot read release bundle") from error


@contextmanager
def exclusive_lock(path, *, timeout=0, identity=None):
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o700)
        if identity:
            os.chown(parent, *identity)
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if identity:
            os.fchown(descriptor, *identity)
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise DeployError("Timed out waiting for the parser/deployment lock") from None
                time.sleep(min(0.2, max(0, deadline - time.monotonic())))
        yield
    finally:
        os.close(descriptor)


def atomic_symlink(target, link):
    if link.exists() and not link.is_symlink():
        raise DeployError("Refusing to replace a non-symlink current path")
    temporary = link.with_name(f".{link.name}-{uuid.uuid4().hex}")
    try:
        temporary.symlink_to(target)
        temporary.replace(link)
    finally:
        temporary.unlink(missing_ok=True)


def write_receipt(path, receipt):
    temporary = path.with_name(f".deployment-{uuid.uuid4().hex}.json")
    try:
        temporary.write_text(json.dumps(receipt, indent=2) + "\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def service_state(runner, unit):
    state = command(runner, ["systemctl", "show", unit, "--property=ActiveState", "--value"])
    if state not in {"inactive", "failed", "active", "activating", "deactivating", "reloading"}:
        raise DeployError("Cannot determine systemd unit state")
    return state


def wait_service(runner, unit, timeout):
    deadline = time.monotonic() + timeout
    while service_state(runner, unit) not in {"inactive", "failed"}:
        if time.monotonic() >= deadline:
            raise DeployError("Parser service is still running; deployment was not activated")
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def runtime_identity(runner, unit):
    output = command(runner, ["systemctl", "show", unit, "--property=User", "--property=Group"])
    fields = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if not fields.get("User"):
        raise DeployError("Provision a systemd service with an explicit User first")
    try:
        user = pwd.getpwnam(fields["User"])
        gid = grp.getgrnam(fields["Group"]).gr_gid if fields.get("Group") else user.pw_gid
        return user.pw_name, user.pw_uid, gid
    except KeyError as error:
        raise DeployError("Systemd runtime user/group does not exist") from error


def sqlite_snapshot(database, destination):
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(destination)) as target:
                source.backup(target)
        destination.chmod(0o600)
    except sqlite3.Error as error:
        raise DeployError("Cannot back up the live SQLite database") from error


def database_schema(database):
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        return (
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema ORDER BY type,name,tbl_name,sql"
            ).fetchall(),
        )


@contextmanager
def release_attempt(release, current):
    release.mkdir(parents=True, mode=0o755)
    try:
        yield
    except BaseException:
        # Keep evidence without blocking a retry. Never move the active release or shared runtime files.
        if current.resolve() != release:
            release.rename(release.with_name(f".failed-{release.name}-{uuid.uuid4().hex}"))
        raise


def prepare_release(options, release, runner, identity):
    wheel = extract_bundle(options.archive, release, options.sha, options.version)
    command(runner, [str(options.python), "-m", "venv", str(release / ".venv")])
    python = str(release / ".venv/bin/python")
    env = {**os.environ, "PLAYWRIGHT_BROWSERS_PATH": str(release / ".browsers")}
    command(
        runner, [python, "-m", "pip", "install", "--require-hashes", "-r", str(release / "requirements.txt")]
    )
    command(runner, [python, "-m", "pip", "install", "--no-deps", str(wheel)])
    prefix = runtime_prefix(identity)
    command(
        runner,
        prefix + [python, "-m", "lead_parser", "--config", str(options.config), "--check-config"],
        env=env,
    )
    try:
        cfg = json.loads(command(runner, prefix + [python, "-c", CONFIG_PROBE, str(options.config)], env=env))
        paths = [options.config, options.config.parent / ".env", *map(Path, cfg["paths"])]
        if any(path.resolve().is_relative_to(options.root / "releases") for path in paths):
            raise DeployError("Persistent runtime paths must be outside release directories")
        if not all(Path(cfg[key]).is_absolute() for key in ("database", "lock_file")):
            raise ValueError("Expected absolute persistent paths")
    except (ValueError, KeyError, TypeError) as error:
        raise DeployError("Cannot inspect runtime storage configuration") from error
    if cfg["needs_chromium"]:
        command(runner, [python, "-m", "playwright", "install", "chromium"], env=env)
    return cfg, python, env


def runtime_prefix(identity):
    if os.geteuid() == 0 and (identity[1] != 0 or identity[2] != os.getegid()):
        return ["runuser", "-u", identity[0], "-g", grp.getgrgid(identity[2]).gr_name, "--"]
    return []


def validate_schema_transition(before, after):
    if after[0] != 2:
        raise DeployError("Candidate must use supported SQLite schema 2")
    if before is None or before == after:
        return
    if before[0] not in {0, 1}:
        raise DeployError("Unexpected schema change; perform an explicit migration first")
    old = {(row[0], row[1]): row[2:] for row in before[1]}
    new = {(row[0], row[1]): row[2:] for row in after[1]}
    allowed_names = {
        "leads",
        "deliveries",
        "sources",
        "legacy_seen",
        "human_reviews",
        "meta",
        "claims",
        "clicks",
        "sheet_slots",
        "delivery_due",
        "ai_due",
        "sqlite_sequence",
        "sqlite_autoindex_leads_1",
        "sqlite_autoindex_deliveries_1",
        "sqlite_autoindex_sources_1",
        "sqlite_autoindex_legacy_seen_1",
        "sqlite_autoindex_human_reviews_1",
        "sqlite_autoindex_meta_1",
        "sqlite_autoindex_claims_1",
        "sqlite_autoindex_clicks_1",
    }
    if any(new.get(key) != value for key, value in old.items()) or any(
        name not in allowed_names or kind not in {"table", "index"} for kind, name in new.keys() - old.keys()
    ):
        raise DeployError("Unexpected schema change; perform an explicit migration first")


def restore_database(backup, database, identity):
    """Restore only before new services have started, with both writer locks held."""
    try:
        with closing(sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True)) as source:
            with closing(sqlite3.connect(database)) as target:
                source.backup(target)
    except sqlite3.Error as error:
        raise DeployError("Database restore failed; keep all services stopped") from error
    finally:
        for suffix in ("", "-wal", "-shm", "-journal"):
            path = Path(str(database) + suffix)
            if path.exists():
                if os.geteuid() == 0:
                    os.chown(path, *identity[1:])
                path.chmod(0o600)


def deploy(options, *, runner=subprocess.run):
    validate_options(options)
    options = replace(options, root=options.root.resolve())
    root = options.root
    root.mkdir(parents=True, exist_ok=True)
    current = root / "current"
    if current.exists() and not current.is_symlink():
        raise DeployError("Refusing to replace a non-symlink current path")
    with exclusive_lock(root / ".deploy.lock"), ExitStack() as attempts:
        identity = runtime_identity(runner, options.service)
        release = root / "releases" / f"{options.version}-{options.sha[:12]}"
        previous = current.resolve(strict=True) if current.is_symlink() else None
        if release.exists():
            receipt_path = release / "deployment.json"
            if previous == release and receipt_path.is_file():
                receipt = json.loads(receipt_path.read_text())
                if (
                    receipt.get("sha") == options.sha
                    and receipt.get("version") == options.version
                    and receipt.get("status") == "deployed"
                ):
                    return receipt
            raise DeployError("Release directory already exists; inspect the previous attempt")
        attempts.enter_context(release_attempt(release, current))
        cfg, python, env = prepare_release(options, release, runner, identity)
        prefix = runtime_prefix(identity)
        web_state = command(
            runner,
            ["systemctl", "show", options.web_service, "--property=LoadState", "--value"],
            allowed=(0, 1),
        )
        if web_state not in {"loaded", "not-found"}:
            raise DeployError("Cannot determine web service installation state")
        web_present = web_state == "loaded"
        if cfg["web_required"] and not web_present:
            raise DeployError("Install parser-web.service before deploying enabled web features")
        if web_present:
            if runtime_identity(runner, options.web_service) != identity:
                raise DeployError("Parser and web services must use the same runtime user/group")
        web_active = web_present and service_state(runner, options.web_service) == "active"
        timer_active = service_state(runner, options.timer) == "active"
        stopped = switched = activation_attempted = database_touched = False
        backup = None
        database = Path(cfg["database"])
        database_existed = database.exists()

        @contextmanager
        def writer_locks():
            with exclusive_lock(Path(cfg["lock_file"]), timeout=options.wait_seconds, identity=identity[1:]):
                with exclusive_lock(
                    Path(cfg["lock_file"] + ".web"), timeout=options.wait_seconds, identity=identity[1:]
                ):
                    yield

        def stop_managed():
            failures = []
            for unit in [options.timer] + ([options.web_service] if web_present else []):
                try:
                    command(runner, ["systemctl", "stop", unit])
                except Exception as failure:
                    failures.append(failure)
            if failures:
                raise DeployError(
                    "Cannot confirm all services stopped; inspect before resuming"
                ) from failures[0]

        try:
            stopped = True
            command(runner, ["systemctl", "stop", options.timer])
            wait_service(runner, options.service, options.wait_seconds)
            if web_present:
                command(runner, ["systemctl", "stop", options.web_service])
                wait_service(runner, options.web_service, options.wait_seconds)
            with writer_locks():
                backups = root / "backups"
                backups.mkdir(mode=0o700, exist_ok=True)
                if database_existed:
                    backup = backups / f"{options.version}-{options.sha[:12]}-{uuid.uuid4().hex}.sqlite3"
                    sqlite_snapshot(database, backup)
                before = database_schema(backup) if backup else None
                with tempfile.TemporaryDirectory(prefix=".preflight-", dir=backups) as temporary:
                    snapshot = Path(temporary) / "parser.sqlite3"
                    if backup:
                        shutil.copyfile(backup, snapshot)
                    command(runner, [python, "-c", SNAPSHOT_PROBE, str(snapshot)], env=env)
                    expected = database_schema(snapshot)
                    validate_schema_transition(before, expected)
                # The same audited migration now runs on the live DB, as runtime user, after backup.
                database_touched = True
                migration = json.loads(
                    command(runner, prefix + [python, "-c", SNAPSHOT_PROBE, str(database)], env=env)
                )
                if database_schema(database) != expected:
                    raise DeployError("Live migration differs from the verified snapshot")
                receipt = {
                    "sha": options.sha,
                    "version": options.version,
                    "release": str(release),
                    "previous": str(previous) if previous else None,
                    "backup": str(backup) if backup else None,
                    "migration": migration,
                    "web_required": cfg["web_required"],
                    "status": "prepared",
                }
                atomic_symlink(release, current)
                switched = True
                write_receipt(release / "deployment.json", receipt)
            if cfg["web_required"]:
                activation_attempted = True
                command(runner, ["systemctl", "start", options.web_service])
                if service_state(runner, options.web_service) != "active":
                    raise DeployError("Web service did not start")
                command(
                    runner,
                    [
                        python,
                        "-c",
                        HEALTH_PROBE,
                        cfg["health_url"],
                        options.version,
                        str(options.health_seconds),
                    ],
                    env=env,
                )
            if timer_active:
                activation_attempted = True
                command(runner, ["systemctl", "start", options.timer])
            receipt["status"] = "deployed"
            write_receipt(release / "deployment.json", receipt)
            return receipt
        except BaseException as error:
            if activation_attempted:
                # New web callbacks or a timer may already have written business data. Never undo it.
                stop_managed()
                raise DeployError(
                    "Activation failed; candidate remains current and services are stopped; inspect before resuming"
                ) from error
            try:
                if database_touched or switched:
                    with writer_locks():
                        if database_touched:
                            if backup:
                                restore_database(backup, database, identity)
                            elif not database_existed:
                                for suffix in ("", "-wal", "-shm", "-journal"):
                                    Path(str(database) + suffix).unlink(missing_ok=True)
                        if switched:
                            if previous:
                                atomic_symlink(previous, current)
                            else:
                                current.unlink(missing_ok=True)
                if stopped:
                    if web_active:
                        command(runner, ["systemctl", "start", options.web_service])
                    if timer_active:
                        command(runner, ["systemctl", "start", options.timer])
            except BaseException as recovery_error:
                stop_managed()
                raise DeployError(
                    "Deployment failed; automatic recovery failed, keep services stopped and inspect"
                ) from recovery_error
            raise error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--sha", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--root", type=Path, default=Path("/opt/parser"))
    parser.add_argument("--config", type=Path, default=Path("/var/lib/parser/config.yaml"))
    parser.add_argument("--python", type=Path, default=Path("/usr/bin/python3.12"))
    parser.add_argument("--service", default="parser.service")
    parser.add_argument("--timer", default="parser.timer")
    parser.add_argument("--web-service", default="parser-web.service")
    parser.add_argument("--health-seconds", type=float, default=30)
    parser.add_argument("--wait-seconds", type=float, default=900)
    options = DeployOptions(**vars(parser.parse_args(argv)))
    if os.geteuid() != 0:
        print("Run this installer through sudo on the prepared host.", file=sys.stderr)
        return 1
    # sudo commonly inherits a restrictive umask; release code must remain readable by the runtime user.
    os.umask(0o022)

    def interrupted(signum, _frame):
        raise DeployError(f"Deployment interrupted by signal {signum}")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        print(json.dumps(deploy(options)))
        return 0
    except (DeployError, OSError, ValueError) as error:
        print(
            str(error) if isinstance(error, DeployError) else "Deployment failed: local filesystem error",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
