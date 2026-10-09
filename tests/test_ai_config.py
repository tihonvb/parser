import copy
import json
import math
from pathlib import Path

import pytest
import requests
import yaml

import lead_parser.infrastructure.integrations.openrouter.classifier as ai_filter
from lead_parser.bootstrap import run_once
from lead_parser.core.policies import matches_keywords
from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, load_config
from lead_parser.infrastructure.integrations.openrouter.classifier import (
    InvalidVerdict,
    _build_user_prompt,
    filter_leads,
    validate_verdicts,
)
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.infrastructure.security import private_json, safe_error
from lead_parser.interfaces.cli.main import main


@pytest.mark.parametrize(
    "patch",
    [
        {"is_client": "false"},
        {"index": True},
        {"index": -1},
        {"index": 1},
        {"confidence": "0.9"},
        {"confidence": math.nan},
        {"confidence": math.inf},
        {"confidence": -0.1},
        {"confidence": 1.1},
        {"confidence": True},
        {"reason": None},
    ],
)
def test_schema_rejects_ambiguous_verdict(patch):
    verdict = {"index": 0, "is_client": True, "confidence": 0.9, "reason": "request"}
    with pytest.raises(InvalidVerdict):
        validate_verdicts([{**verdict, **patch}], 1)


def test_duplicate_indices_and_missing_are_retryable(cfg, lead, monkeypatch):
    verdict = {"index": 0, "is_client": True, "confidence": 0.9, "reason": "request"}
    with pytest.raises(InvalidVerdict):
        validate_verdicts([verdict, verdict], 1)
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="test")
    monkeypatch.setattr(ai_filter, "_classify_batch", lambda *args: {})
    assert not filter_leads(cfg, [lead])
    assert lead.extra["ai_pending"] is True


def test_enabled_missing_key_blocks_no_delivery(cfg, lead):
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="")
    with pytest.raises(ConfigError):
        filter_leads(cfg, [lead])


def test_technical_failure_persists_pending_original(cfg, lead, monkeypatch):
    cfg["ai_filter"].update(enabled=True, openrouter_api_key="test")

    def failed(*args):
        raise requests.Timeout("Authorization: secret-token")

    monkeypatch.setattr(ai_filter, "_classify_batch", failed)
    with Store(cfg["storage"]["database"]) as store:
        store.ingest([lead])
    code, report = run_once(cfg, deliver_only=True)
    assert code == 1 and report["leads"] == {"pending": 1}
    with Store(cfg["storage"]["database"]) as store:
        payload = store.lead(lead.dedupe_key())
        assert payload.extra["ai_pending"] and "secret-token" not in json.dumps(payload.extra)


def test_prompt_keeps_tail_original_and_dynamic_context(cfg, lead):
    cfg["general"].update(city="Казань", work_type="отделка офисов")
    cfg["ai_filter"]["max_text_chars"] = 1000
    lead.text = "Начало " + "x" * 10000 + " Нужна отделка офиса в Казани"
    before = lead.text
    item = json.loads(_build_user_prompt([lead], cfg))[0]
    assert len(item["text"]) <= 1000 and item["text"].endswith("в Казани")
    assert item["text_truncated"] and item["source_group_id"] == "vk:12" and lead.text == before
    assert "Казань" in ai_filter.SYSTEM_PROMPT.format(**cfg["general"])


@pytest.mark.parametrize(
    "text",
    [
        "НУЖЕН\nРЕМОНТ",
        "нужен  ремонт",
        "нужен\u00a0ремонт",
        "Ищу хорошего мастера для ремонта",
        "Посоветуйте бригаду для отделки квартиры",
        "Требуются мастера под ключ",
    ],
)
def test_normalization_and_natural_requests(text):
    assert matches_keywords(text, ["нужен ремонт"], [])


@pytest.mark.parametrize(
    "text", ["Ремонт квартир недорого, наши услуги", "Продаю материалы для ремонта", "Ремонт завершён вчера"]
)
def test_prefilter_does_not_require_every_post_to_reach_ai(text):
    assert not matches_keywords(text, ["нужен ремонт"], [])


def test_env_precedence_paths_and_offline_check(cfg, tmp_path, monkeypatch):
    config = Path(cfg["_config_path"])
    (tmp_path / ".env").write_text("VK_ACCESS_TOKEN=file-value\nTELEGRAM_API_ID=123\n")
    monkeypatch.setenv("VK_ACCESS_TOKEN", "environment-value")
    loaded = load_config(config)
    assert loaded["vk"]["access_token"] == "environment-value"
    assert loaded["telegram"]["api_id"] == "123"
    assert loaded["storage"]["database"].startswith(str(tmp_path))
    monkeypatch.chdir("/")
    assert main(["--config", str(config), "--check-config"]) == 0
    assert not Path(loaded["storage"]["database"]).exists()


@pytest.mark.parametrize(
    "patch",
    [
        {"output": 5},
        {"telegram": {"enabled": "false"}},
        {"storage": {"database": []}},
        {"ai_filter": {"min_confidence": math.nan}},
        {"vk": {"unknown": 1}},
        {"vk": {"group_overrides": {12: {"max_pages": 0}}}},
        {"telegram": {"channels": [None]}},
        {"notifications": {"telegram": {"enabled": True}}},
    ],
)
def test_invalid_configuration_fails_before_network(tmp_path, patch):
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(patch))
    with pytest.raises(ConfigError):
        load_config(config)


def test_private_state_atomic_replacement_and_safe_logs(tmp_path):
    path = tmp_path / "state.json"
    private_json(path, {"token": "first"})
    private_json(path, {"token": "second"})
    assert json.loads(path.read_text())["token"] == "second"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob("*.tmp"))
    assert safe_error(requests.Timeout("https://api.test/token-secret")) == "Timeout"


def test_all_defaults_are_isolated(cfg):
    cfg["general"]["keywords"].append("new")
    assert "new" not in copy.deepcopy(DEFAULTS)["general"]["keywords"]
