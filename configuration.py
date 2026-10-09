"""YAML, environment overrides, validation and paths anchored to the config."""

from __future__ import annotations

import copy
import math
import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

CONFIG_PATH = Path(__file__).parent / "config.yaml"


class ConfigError(ValueError):
    pass


DEFAULTS = {
    "general": {
        "city": "Самара",
        "work_type": "ремонт квартир под ключ",
        "keywords": ["нужен ремонт", "ищу бригаду"],
        "exclude_keywords": [],
    },
    "telegram": {
        "enabled": False,
        "api_id": "",
        "api_hash": "",
        "session_name": ".data/telegram",
        "channels": [],
        "messages_per_run": 200,
        "lookback_hours": 48,
        "max_concurrent_channels": 5,
        "max_messages_per_channel": 5000,
        "channel_timeout_seconds": 180,
        "channel_overrides": {},
    },
    "vk": {
        "enabled": False,
        "access_token": "",
        "token_mode": "static",
        "token_file": "vk_tokens.json",
        "state_file": "vk_pkce_state.json",
        "client_id": "54797818",
        "redirect_uri": "https://oauth.vk.ru/blank.html",
        "api_version": "5.199",
        "group_ids": [],
        "search_token": "",
        "use_global_newsfeed_search": False,
        "posts_per_run": 100,
        "max_pages_per_query": 50,
        "max_age_hours": 48,
        "overlap_seconds": 300,
        "group_overrides": {},
        "request_timeout_seconds": 30,
    },
    "avito": {
        "enabled": False,
        "city_slug": "samara",
        "search_queries": [],
        "headless": True,
        "cdp_endpoint": "",
        "max_listings_per_query": 50,
        "detail_limit": 50,
        "request_timeout_seconds": 30,
        "dolphin": {"enabled": False, "profile_id": "", "local_api_url": "http://127.0.0.1:3001"},
    },
    "ai_filter": {
        "enabled": False,
        "openrouter_api_key": "",
        "model": "openai/gpt-4o-mini",
        "batch_size": 8,
        "min_confidence": 0.75,
        "max_text_chars": 12000,
        "timeout_seconds": 60,
        "max_attempts": 5,
    },
    "output": {
        "enabled": False,
        "mode": "google_sheets",
        "google_sheets": {
            "credentials_file": "service_account.json",
            "spreadsheet_id": "",
            "worksheet_name": "Лиды",
            "timeout_seconds": 30,
        },
    },
    "notifications": {
        "telegram": {"enabled": False, "bot_token": "", "chat_id": "", "chat_ids": [], "timeout_seconds": 10}
    },
    "schedule": {"interval_minutes": 60},
    "storage": {
        "database": ".data/parser.sqlite3",
        "seen_store": "seen_leads.json",
        "lock_file": ".data/parser.lock",
    },
    "delivery": {
        "max_attempts": 8,
        "retry_base_seconds": 60,
        "retry_max_seconds": 3600,
        "lease_seconds": 300,
        "jobs_per_run": 100,
    },
    "discovery": {"city_id": None, "max_groups": 2000, "max_pages": 50, "days": 7},
}

ENV_MAP = {
    "TELEGRAM_API_ID": ("telegram", "api_id"),
    "TELEGRAM_API_HASH": ("telegram", "api_hash"),
    "VK_ACCESS_TOKEN": ("vk", "access_token"),
    "VK_SEARCH_TOKEN": ("vk", "search_token"),
    "OPENROUTER_API_KEY": ("ai_filter", "openrouter_api_key"),
    "GOOGLE_SERVICE_ACCOUNT_FILE": ("output", "google_sheets", "credentials_file"),
    "GOOGLE_SPREADSHEET_ID": ("output", "google_sheets", "spreadsheet_id"),
    "TELEGRAM_BOT_TOKEN": ("notifications", "telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("notifications", "telegram", "chat_id"),
}


def _merge(base: dict, values: dict) -> dict:
    for key, value in values.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value
    return base


def _filled(value: Any) -> bool:
    return (
        isinstance(value, (str, int))
        and bool(str(value).strip())
        and not any(marker in str(value).upper() for marker in ("PUT_YOUR", "ВСТАВЬТЕ", "ТВОЙ_"))
    )


def _number(
    cfg: dict,
    section: str,
    key: str,
    *,
    low: float = 0,
    high: float | None = None,
    integer: bool = False,
    allow_zero: bool = False,
) -> None:
    value = cfg[section][key]
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ConfigError(f"{section}.{key}: expected a finite number")
    if integer and not isinstance(value, int):
        raise ConfigError(f"{section}.{key}: expected an integer")
    if value < low or (not allow_zero and value == low) or (high is not None and value > high):
        raise ConfigError(f"{section}.{key}: outside allowed range")


def validate_config(cfg: dict, *, require_access: bool = True) -> None:
    def shape(values, defaults, prefix=""):
        if not isinstance(values, dict):
            raise ConfigError(prefix + ": expected mapping")
        for key in values:
            if key.startswith("_") and not prefix:
                continue
            if key not in defaults:
                raise ConfigError(prefix + key + ": unknown setting")
        for key, default in defaults.items():
            value = values[key]
            label = prefix + key
            if isinstance(default, dict) and default:
                shape(value, default, label + ".")
            elif isinstance(default, dict) and not isinstance(value, dict):
                raise ConfigError(label + ": expected mapping")
            elif type(default) is bool and type(value) is not bool:
                raise ConfigError(label + ": expected boolean")
            elif isinstance(default, str) and not isinstance(value, str):
                if key not in {"api_id", "chat_id", "client_id"} or type(value) is not int:
                    raise ConfigError(label + ": expected string")
            elif isinstance(default, list) and not isinstance(value, list):
                raise ConfigError(label + ": expected list")

    shape(cfg, DEFAULTS)
    for section in DEFAULTS:
        if not isinstance(cfg.get(section), dict):
            raise ConfigError(f"{section}: expected a mapping")
    for section in ("telegram", "vk", "avito", "ai_filter", "output"):
        if type(cfg[section].get("enabled")) is not bool:
            raise ConfigError(f"{section}.enabled: expected boolean")
    general = cfg["general"]
    if not _filled(general.get("city")) or not _filled(general.get("work_type")):
        raise ConfigError("general.city and general.work_type must be filled")
    for key in ("keywords", "exclude_keywords"):
        if not isinstance(general[key], list) or any(
            not isinstance(v, str) or not v.strip() for v in general[key]
        ):
            raise ConfigError(f"general.{key}: expected non-empty strings in a list")
    for section, keys in {
        "schedule": ["interval_minutes"],
        "telegram": [
            "messages_per_run",
            "lookback_hours",
            "max_concurrent_channels",
            "max_messages_per_channel",
            "channel_timeout_seconds",
        ],
        "vk": ["posts_per_run", "max_pages_per_query", "request_timeout_seconds"],
        "avito": ["max_listings_per_query", "detail_limit", "request_timeout_seconds"],
        "ai_filter": ["batch_size", "max_text_chars", "timeout_seconds", "max_attempts"],
        "delivery": [
            "max_attempts",
            "retry_base_seconds",
            "retry_max_seconds",
            "lease_seconds",
            "jobs_per_run",
        ],
        "discovery": ["max_groups", "max_pages", "days"],
    }.items():
        for key in keys:
            _number(cfg, section, key, integer=key not in {"interval_minutes", "lookback_hours"})
    _number(cfg, "ai_filter", "min_confidence", high=1, allow_zero=True)
    _number(cfg, "vk", "max_age_hours", allow_zero=True)
    _number(cfg, "vk", "overlap_seconds", allow_zero=True)
    if cfg["vk"]["token_mode"] not in {"static", "vk_id"}:
        raise ConfigError("vk.token_mode must be static or vk_id")
    if cfg["output"]["mode"] != "google_sheets":
        raise ConfigError("output.mode supports google_sheets only")
    for section, key in (("telegram", "channels"), ("vk", "group_ids"), ("avito", "search_queries")):
        if not isinstance(cfg[section][key], list):
            raise ConfigError(f"{section}.{key}: expected list")
    if not isinstance(cfg["telegram"]["channel_overrides"], dict) or not isinstance(
        cfg["vk"]["group_overrides"], dict
    ):
        raise ConfigError("source overrides must be mappings")
    for section, key, allowed in (
        ("telegram", "channel_overrides", {"messages_per_run", "lookback_hours", "max_messages_per_channel"}),
        ("vk", "group_overrides", {"max_pages"}),
    ):
        for identity, override in cfg[section][key].items():
            if (
                not isinstance(identity, (str, int))
                or not isinstance(override, dict)
                or set(override) - allowed
            ):
                raise ConfigError(section + "." + key + ": invalid override")
            for name, number in override.items():
                if (
                    type(number) not in {int, float}
                    or not math.isfinite(number)
                    or number <= 0
                    or (name != "lookback_hours" and type(number) is not int)
                ):
                    raise ConfigError(section + "." + key + ": invalid limit")
    for section, key in (("telegram", "channels"), ("vk", "group_ids"), ("avito", "search_queries")):
        if any(type(item) not in {str, int} or not str(item).strip() for item in cfg[section][key]):
            raise ConfigError(section + "." + key + ": invalid source reference")
    for nested in (cfg["output"]["google_sheets"], cfg["notifications"]["telegram"]):
        timeout = nested["timeout_seconds"]
        if type(timeout) not in {int, float} or not math.isfinite(timeout) or timeout <= 0:
            raise ConfigError("Output/notification timeout must be positive")
    if cfg["ai_filter"]["max_text_chars"] < 256:
        raise ConfigError("ai_filter.max_text_chars must be at least 256")
    if (
        cfg["delivery"]["lease_seconds"]
        < max(
            cfg["output"]["google_sheets"]["timeout_seconds"],
            cfg["notifications"]["telegram"]["timeout_seconds"],
        )
        + 30
    ):
        raise ConfigError("delivery.lease_seconds must exceed transport timeout by 30 seconds")
    if cfg["discovery"]["city_id"] is not None and (
        type(cfg["discovery"]["city_id"]) is not int or cfg["discovery"]["city_id"] <= 0
    ):
        raise ConfigError("discovery.city_id must be a positive integer or null")
    tg = cfg["notifications"]["telegram"]
    if (
        not isinstance(tg, dict)
        or type(tg.get("enabled")) is not bool
        or not isinstance(tg.get("chat_ids"), list)
    ):
        raise ConfigError("notifications.telegram: invalid settings")
    if any(type(item) not in {str, int} or not str(item).strip() for item in tg["chat_ids"]):
        raise ConfigError("notifications.telegram.chat_ids: invalid recipients")
    if tg["enabled"] and not cfg["output"]["enabled"]:
        raise ConfigError("Notifications require Sheets delivery to be enabled")
    if not require_access:
        return
    for section, key in (
        ("telegram", "api_id"),
        ("telegram", "api_hash"),
        ("ai_filter", "openrouter_api_key"),
    ):
        if cfg[section]["enabled"] and not _filled(cfg[section][key]):
            raise ConfigError(f"{section}.{key}: required when enabled")
    if cfg["telegram"]["enabled"]:
        if not cfg["telegram"]["channels"]:
            raise ConfigError("telegram.channels: required when enabled")
        try:
            if int(cfg["telegram"]["api_id"]) <= 0:
                raise ValueError
        except (TypeError, ValueError) as error:
            raise ConfigError("telegram.api_id: expected positive integer") from error
    if (
        cfg["vk"]["enabled"]
        and cfg["vk"]["token_mode"] == "static"
        and not _filled(cfg["vk"]["access_token"])
    ):
        raise ConfigError("vk.access_token: required for enabled static mode")
    if cfg["vk"]["enabled"] and not (cfg["vk"]["group_ids"] or cfg["vk"]["use_global_newsfeed_search"]):
        raise ConfigError("vk: configure group_ids or global search when enabled")
    if cfg["avito"]["enabled"] and not cfg["avito"]["search_queries"]:
        raise ConfigError("avito.search_queries: required when enabled")
    if cfg["avito"]["dolphin"]["enabled"] and not _filled(cfg["avito"]["dolphin"]["profile_id"]):
        raise ConfigError("avito.dolphin.profile_id: required when enabled")
    if cfg["output"]["enabled"] and not _filled(cfg["output"]["google_sheets"]["spreadsheet_id"]):
        raise ConfigError("output.google_sheets.spreadsheet_id: required when enabled")
    if cfg["output"]["enabled"] and not Path(cfg["output"]["google_sheets"]["credentials_file"]).is_file():
        raise ConfigError("output.google_sheets.credentials_file: credentials file required")
    if tg["enabled"] and (not _filled(tg["bot_token"]) or not (tg["chat_id"] or tg["chat_ids"])):
        raise ConfigError("notifications.telegram: bot_token and recipients required")


def load_config(path: Path | str = CONFIG_PATH, *, require_access: bool = True) -> dict[str, Any]:
    path = Path(path).resolve()
    try:
        with path.open(encoding="utf-8") as stream:
            value = yaml.safe_load(stream)
    except (OSError, yaml.YAMLError) as error:
        raise ConfigError("Cannot read configuration; check path and YAML syntax") from error
    if not isinstance(value, dict):
        raise ConfigError("Configuration must be a YAML mapping")
    if any(not isinstance(key, str) for key in value):
        raise ConfigError("Configuration section names must be strings")
    unknown = set(value) - set(DEFAULTS)
    if unknown:
        raise ConfigError("Unknown configuration sections: " + ", ".join(sorted(unknown)))
    cfg = _merge(copy.deepcopy(DEFAULTS), value)
    if isinstance(value.get("output"), dict) and "enabled" not in value["output"]:
        cfg["output"]["enabled"] = True
    validate_config(cfg, require_access=False)
    environment = {**dotenv_values(path.parent / ".env"), **os.environ}
    if environment.get("VK_TOKEN") and not environment.get("VK_ACCESS_TOKEN"):
        environment["VK_ACCESS_TOKEN"] = environment["VK_TOKEN"]
    for name, keys in ENV_MAP.items():
        if environment.get(name):
            target = cfg
            for key in keys[:-1]:
                target = target[key]
            target[keys[-1]] = environment[name]
    for section, keys in {
        "storage": ["database", "seen_store", "lock_file"],
        "telegram": ["session_name"],
        "vk": ["token_file", "state_file"],
    }.items():
        for key in keys:
            if not cfg[section][key].strip():
                raise ConfigError(section + "." + key + ": empty path")
            candidate = Path(cfg[section][key]).expanduser()
            cfg[section][key] = str(candidate if candidate.is_absolute() else path.parent / candidate)
    creds = Path(cfg["output"]["google_sheets"]["credentials_file"]).expanduser()
    cfg["output"]["google_sheets"]["credentials_file"] = str(
        creds if creds.is_absolute() else path.parent / creds
    )
    cfg["_config_path"] = str(path)
    validate_config(cfg, require_access=require_access)
    return cfg
