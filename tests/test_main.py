import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from filelock import FileLock, Timeout

import ai_filter
import delivery
import main
from common import Lead, ScanResult
from sheets_writer import SheetsWriter
from storage import Store
from tests.test_storage_delivery import Worksheet


def test_full_pipeline_partial_sources_and_restart_without_reclassification(cfg, lead, monkeypatch):
    cfg["vk"].update(enabled=True, access_token="test", group_ids=[12])
    cfg["telegram"].update(enabled=True, api_id=1, api_hash="test", channels=["bad"])
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="test")
    cfg["output"]["enabled"] = True
    cfg["output"]["google_sheets"]["spreadsheet_id"] = "test"
    cfg["notifications"]["telegram"].update(enabled=True, bot_token="test", chat_ids=["recipient"])

    def collected(config, store, reports):
        store.ingest([lead, lead])
        result = ScanResult("vk:12", scanned=2, candidates=2)
        store.checkpoint(result)
        reports.append(result)
        return [lead, lead]

    def failed(*args, **kwargs):
        raise RuntimeError("token should never be printed")

    monkeypatch.setattr(
        main.importlib,
        "import_module",
        lambda name: SimpleNamespace(collect_leads=collected if name == "vk_parser" else failed),
    )
    calls = []

    def classify(config, batch):
        calls.append(len(batch))
        return {0: {"is_client": True, "confidence": 0.9, "reason": "request"}}

    monkeypatch.setattr(ai_filter, "_classify_batch", classify)
    ws = Worksheet()
    sent = []
    monkeypatch.setattr(
        main,
        "drain",
        lambda config, store: delivery.drain(
            config,
            store,
            writer_factory=lambda config: SheetsWriter(config, ws),
            sender=lambda *args: sent.append(args[-1]),
        ),
    )
    code, report = main.run_once(cfg)
    assert code == 1 and report["leads"] == {"accepted": 1} and len(ws.rows) == 2 and sent == ["recipient"]
    assert "token should" not in json.dumps(report)
    code, _ = main.run_once(cfg, deliver_only=True)
    assert code == 0 and calls == [1] and sent == ["recipient"] and len(ws.rows) == 2


def test_process_lock_blocks_overlapping_runs(cfg):
    Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True)
    with FileLock(cfg["storage"]["lock_file"]), pytest.raises(Timeout):
        main.run_once(cfg)
    assert not Path(cfg["storage"]["database"]).exists()


def test_ai_pending_survives_source_window_and_is_processed_from_database(cfg, monkeypatch):
    item = Lead(source="vk", external_id="-12_1", date="2000-01-01", text="Нужен ремонт")
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([item])
        store.classification(item, "pending", now=1)
    code, report = main.run_once(cfg, deliver_only=True)
    assert code == 0 and report["leads"] == {"accepted": 1}


def test_loop_reloads_configuration_and_refreshes_vk_each_cycle(cfg, monkeypatch):
    import yaml

    cfg["vk"].update(enabled=True, token_mode="vk_id", group_ids=[12])
    path = Path(cfg["_config_path"])
    path.write_text(yaml.safe_dump({key: value for key, value in cfg.items() if not key.startswith("_")}))
    refreshes, seen = [], []

    class Manager:
        def __init__(self, config):
            pass

        def refresh(self):
            refreshes.append(True)
            return f"token-{len(refreshes)}"

    monkeypatch.setattr(main, "TokenManager", Manager)

    def collector(config, **kwargs):
        seen.append((config["vk"]["access_token"], config["general"]["city"]))
        return []

    monkeypatch.setattr(
        main.importlib, "import_module", lambda name: SimpleNamespace(collect_leads=collector)
    )

    def next_cycle(*args):
        if len(refreshes) == 1:
            cfg["general"]["city"] = "Казань"
            path.write_text(
                yaml.safe_dump({key: value for key, value in cfg.items() if not key.startswith("_")})
            )
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr(main.time, "sleep", next_cycle)
    assert main.main(["--config", str(path), "--loop"]) == 0
    assert seen == [("token-1", "Самара"), ("token-2", "Казань")]


def test_status_can_inspect_inbox_without_expired_api_credentials(cfg):
    import yaml

    cfg["ai_filter"].update(enabled=True, openrouter_api_key="")
    path = Path(cfg["_config_path"])
    path.write_text(yaml.safe_dump({key: value for key, value in cfg.items() if not key.startswith("_")}))
    assert main.main(["--config", str(path), "--status"]) == 0
    assert main.main(["--config", str(path), "--check-config"]) == 2
