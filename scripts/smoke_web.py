"""Offline installed-wheel smoke: legacy migration, HTTP readiness and SIGTERM."""

import argparse
import json
import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from importlib.metadata import version
from pathlib import Path
from urllib.request import urlopen

import yaml


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config-template", type=Path, required=True)
    parser.add_argument("--legacy-schema", type=Path, required=True)
    args = parser.parse_args(argv)
    cfg = yaml.safe_load(args.config_template.read_text())
    assert all(
        not cfg[name]["enabled"] for name in ("telegram", "vk", "avito", "profi_mail", "ai_filter", "output")
    )
    assert not cfg["notifications"]["telegram"]["enabled"]
    with tempfile.TemporaryDirectory(prefix="parser-web-smoke-") as temporary:
        root = Path(temporary)
        database, config, backup = root / "parser.sqlite3", root / "config.yaml", root / "backup.sqlite3"
        with sqlite3.connect(database) as db:
            db.executescript(args.legacy_schema.read_text())
            db.execute(
                "INSERT INTO leads(key,payload,content_hash,first_seen,last_seen) "
                "VALUES('vk:smoke','{}','fixture',1,2)"
            )
        cfg["storage"].update(database=str(database), lock_file=str(root / "parser.lock"))
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        cfg["web"].update(host="127.0.0.1", port=port, enabled=True, token="offline-smoke-token-only")
        config.write_text(yaml.safe_dump(cfg))
        config.chmod(0o600)
        # Never inherit integration credentials or a caller's source-tree PYTHONPATH.
        child_env = {
            key: value
            for key, value in os.environ.items()
            if key not in {"PYTHONPATH", "LEAD_WEB_TOKEN"}
            and not key.startswith(("TELEGRAM_", "VK_", "PROFI_MAIL_", "OPENROUTER_", "GOOGLE_"))
        }
        cli = [sys.executable, "-m", "lead_parser"]
        result = subprocess.run(
            cli + ["migrate-db", "--config", str(config), "--apply", "--backup", str(backup)],
            env=child_env,
            capture_output=True,
            text=True,
            check=True,
        )
        assert json.loads(result.stdout)["from_version"] == 1
        with sqlite3.connect(backup) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 1
        with sqlite3.connect(database) as db:
            assert db.execute("PRAGMA user_version").fetchone()[0] == 2
            assert db.execute("SELECT count(*) FROM leads").fetchone()[0] == 1
        with (root / "web.log").open("w") as log:
            process = subprocess.Popen(
                cli + ["web", "--config", str(config)], env=child_env, stdout=log, stderr=log
            )
            try:
                deadline = time.monotonic() + 15
                while True:
                    if process.poll() is not None:
                        raise AssertionError("Web process exited before readiness")
                    try:
                        with urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1) as response:
                            health = json.load(response)
                        assert health == {
                            "status": "ok",
                            "schema_version": 2,
                            "version": version("remont-lead-parser"),
                        }
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise AssertionError("Web readiness timed out") from None
                        time.sleep(0.1)
                process.send_signal(signal.SIGTERM)
                assert process.wait(timeout=10) == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
        print(
            json.dumps({"migration": "1->2", "health": "ok", "sigterm": "clean", "network": "loopback-only"})
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
