"""YAML, environment overrides, validation and paths anchored to the config."""

from __future__ import annotations

import copy
import math
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

# Checkout commands retain their historical default even when invoked from elsewhere.
# An installed wheel uses the caller's directory, with --config available explicitly.
_CHECKOUT = Path(__file__).resolve().parents[3]
CONFIG_PATH = (
    _CHECKOUT
    if (_CHECKOUT / "src" / "lead_parser").is_dir() and (_CHECKOUT / "pyproject.toml").is_file()
    else Path.cwd()
) / "config.yaml"


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
    "profi_mail": {
        # Заказы Профи.ру из уведомлений на почту специалиста (IMAP, только чтение; docs/profi-mail.md).
        "enabled": False,
        "host": "",
        "port": 993,
        "username": "",
        "password": "",
        "auth": "password",
        "oauth": {
            "client_id": "",
            "tenant": "consumers",
            "scope": "https://outlook.office.com/IMAP.AccessAsUser.All offline_access",
            "token_file": ".data/profi_mail_oauth.json",
        },
        "folder": "INBOX",
        "sender_domains": ["profi.ru"],
        "subject_regex": "",
        "exclude_subject_regex": "",
        "keyword_filter": True,
        "link_mode": "direct",
        "button_regex": "посмотреть заказ|открыть заказ|смотреть заказ",
        "lookback_days": 3,
        "max_messages_per_run": 200,
        "request_timeout_seconds": 30,
        "link_pattern": r"https://(?:[\w-]+\.)?profi\.ru/[^\s\x22\x27<>)\]]+",
        "order_id_pattern": r"(\d{5,})",
        "body_start_regex": "",
        "body_end_regex": "",
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
        "telegram": {
            "enabled": False,
            "bot_token": "",
            "chat_id": "",
            "chat_ids": [],
            "timeout_seconds": 10,
            "claim_buttons": False,
        }
    },
    "web": {"host": "127.0.0.1", "port": 8080, "token": "", "title": "Заявки"},
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
    "LEAD_WEB_TOKEN": ("web", "token"),
    "PROFI_MAIL_PASSWORD": ("profi_mail", "password"),
    "PROFI_MAIL_OAUTH_CLIENT_ID": ("profi_mail", "oauth", "client_id"),
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
    for section in ("telegram", "vk", "avito", "profi_mail", "ai_filter", "output"):
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
        "profi_mail": ["port", "lookback_days", "max_messages_per_run", "request_timeout_seconds"],
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
    if type(tg.get("claim_buttons")) is not bool:
        raise ConfigError("notifications.telegram.claim_buttons: expected true or false")
    web = cfg["web"]
    if (
        not isinstance(web, dict)
        or not isinstance(web.get("host"), str)
        or type(web.get("port")) is not int
        or not 1 <= web["port"] <= 65535
    ):
        raise ConfigError("web: expected host (text) and port (1-65535)")
    _validate_profi_mail_static(cfg["profi_mail"])
    if not require_access:
        return
    _validate_profi_mail_enabled(cfg["profi_mail"])
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


def _compile(label: str, pattern: str, *, groups: int | None = None) -> None:
    try:
        compiled = re.compile(pattern)
    except re.error as error:
        raise ConfigError(f"profi_mail.{label}: invalid regular expression") from error
    if groups is not None and compiled.groups != groups:
        raise ConfigError(f"profi_mail.{label}: exactly {groups} capturing group expected")


def _validate_profi_mail_static(mail: dict) -> None:
    """Shape checks that never need the connector to be enabled."""
    domains = mail["sender_domains"]
    if not isinstance(domains, list) or any(
        not isinstance(d, str) or not re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", d.lower()) for d in domains
    ):
        raise ConfigError(
            "profi_mail.sender_domains: expected a list of domains like profi.ru (empty = any sender)"
        )
    if mail["link_mode"] not in {"direct", "button"}:
        raise ConfigError("profi_mail.link_mode: expected 'direct' or 'button'")
    _compile("button_regex", mail["button_regex"])
    if type(mail["keyword_filter"]) is not bool:
        raise ConfigError("profi_mail.keyword_filter: expected boolean")
    for key in ("subject_regex", "exclude_subject_regex", "body_start_regex", "body_end_regex"):
        if not isinstance(mail[key], str):
            raise ConfigError(f"profi_mail.{key}: expected string")
        _compile(key, mail[key])
    if mail["auth"] not in {"password", "oauth2"}:
        raise ConfigError("profi_mail.auth: expected 'password' or 'oauth2'")
    if not re.fullmatch(r"consumers|common|organizations|[A-Za-z0-9.-]+", str(mail["oauth"]["tenant"])):
        raise ConfigError("profi_mail.oauth.tenant: expected consumers, common, organizations or a tenant id")
    for key in ("scope", "token_file"):
        if not isinstance(mail["oauth"][key], str) or not mail["oauth"][key].strip():
            raise ConfigError(f"profi_mail.oauth.{key}: must not be empty")
    _compile("link_pattern", mail["link_pattern"])
    _compile("order_id_pattern", mail["order_id_pattern"], groups=1)
    if not 1 <= mail["port"] <= 65535 or mail["lookback_days"] > 60 or mail["max_messages_per_run"] > 5000:
        raise ConfigError(
            "profi_mail: port, lookback_days (<=60) or max_messages_per_run (<=5000) out of range"
        )


def _validate_profi_mail_enabled(mail: dict) -> None:
    if not mail["enabled"]:
        return
    required = ["host", "username", "folder"] + (["password"] if mail["auth"] == "password" else [])
    for key in required:
        if not _filled(mail[key]):
            raise ConfigError(f"profi_mail.{key}: required when enabled (password: PROFI_MAIL_PASSWORD)")
    if mail["auth"] == "oauth2" and not _filled(mail["oauth"]["client_id"]):
        raise ConfigError("profi_mail.oauth.client_id: required for auth: oauth2 (docs/profi-mail.md)")


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
    # several recipients: TELEGRAM_CHAT_IDS="111,222 333" is added to YAML chat_ids / TELEGRAM_CHAT_ID
    extra_ids = [
        item for item in re.split(r"[,\s;]+", str(environment.get("TELEGRAM_CHAT_IDS") or "")) if item
    ]
    if extra_ids:
        tg = cfg["notifications"]["telegram"]
        tg["chat_ids"] = [*tg["chat_ids"], *extra_ids]
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
    token_file = Path(cfg["profi_mail"]["oauth"]["token_file"]).expanduser()
    cfg["profi_mail"]["oauth"]["token_file"] = str(
        token_file if token_file.is_absolute() else path.parent / token_file
    )
    creds = Path(cfg["output"]["google_sheets"]["credentials_file"]).expanduser()
    cfg["output"]["google_sheets"]["credentials_file"] = str(
        creds if creds.is_absolute() else path.parent / creds
    )
    cfg["_config_path"] = str(path)
    validate_config(cfg, require_access=require_access)
    return cfg
