"""Declaration of every connector and its credentials — the single source of truth.

Add an integration by adding one ``ConnectorSpec`` here (and, for a lead source, one entry in
``bootstrap.SOURCE_MODULES``). The environment mapping, ``lead-parser connectors`` and the credential template
follow automatically; nothing else needs to know how a connector reads its settings.

Credentials are loaded from the process environment or a ``.env`` file next to ``config.yaml`` (priority:
process > ``.env`` > YAML). Values are never printed by any command; only names and paths are.
"""

from __future__ import annotations

from pathlib import Path

from lead_parser.application.connectors import ConnectorSpec, Credential, dig


def file_exists(path: str) -> bool:
    """The I/O half of the contract: does a declared file credential exist on disk?"""
    return Path(path).is_file()


TELEGRAM = ConnectorSpec(
    name="telegram",
    title="Telegram: каналы и чаты (аккаунт, MTProto)",
    kind="source",
    section="telegram",
    docs="README.md#источники",
    credentials=(
        Credential(
            "telegram.api_id", "api_id приложения с my.telegram.org", env="TELEGRAM_API_ID", secret=False
        ),
        Credential("telegram.api_hash", "api_hash приложения с my.telegram.org", env="TELEGRAM_API_HASH"),
        Credential(
            "telegram.session_name",
            "файл сессии аккаунта; создаётся один раз: lead-parser telegram-auth login",
            kind="file",
            suffix=".session",
            secret=True,
        ),
    ),
)

VK = ConnectorSpec(
    name="vk",
    title="VK: стены групп и поиск",
    kind="source",
    section="vk",
    docs="README.md#источники",
    credentials=(
        Credential(
            "vk.access_token",
            "токен VK (режим token_mode: static)",
            env="VK_ACCESS_TOKEN",
            applies=lambda cfg: dig(cfg, "vk.token_mode") == "static",
        ),
        Credential(
            "vk.token_file",
            "файл токенов VK ID; создаётся: lead-parser vk-auth login (режим token_mode: vk_id)",
            kind="file",
            applies=lambda cfg: dig(cfg, "vk.token_mode") == "vk_id",
        ),
        Credential(
            "vk.search_token",
            "пользовательский токен для глобального поиска",
            env="VK_SEARCH_TOKEN",
            required=False,
            applies=lambda cfg: bool(dig(cfg, "vk.use_global_newsfeed_search")),
        ),
    ),
)

AVITO = ConnectorSpec(
    name="avito",
    title="Avito: объявления через браузер",
    kind="source",
    section="avito",
    docs="README.md#источники",
)

OPENROUTER = ConnectorSpec(
    name="ai_filter",
    title="ИИ-фильтр (OpenRouter)",
    kind="classifier",
    section="ai_filter",
    credentials=(Credential("ai_filter.openrouter_api_key", "ключ OpenRouter", env="OPENROUTER_API_KEY"),),
)

SHEETS = ConnectorSpec(
    name="google_sheets",
    title="Google Sheets: таблица лидов",
    kind="delivery",
    section="output",
    credentials=(
        Credential(
            "output.google_sheets.credentials_file",
            "JSON-ключ сервисного аккаунта Google",
            env="GOOGLE_SERVICE_ACCOUNT_FILE",
            kind="file",
        ),
        Credential(
            "output.google_sheets.spreadsheet_id",
            "ID таблицы из её адреса",
            env="GOOGLE_SPREADSHEET_ID",
            secret=False,
        ),
    ),
)

TELEGRAM_BOT = ConnectorSpec(
    name="telegram_bot",
    title="Telegram-бот: уведомления о лидах",
    kind="delivery",
    section="notifications.telegram",
    credentials=(
        Credential("notifications.telegram.bot_token", "токен бота от @BotFather", env="TELEGRAM_BOT_TOKEN"),
        Credential(
            "notifications.telegram.chat_id",
            "id получателя (или список chat_ids в config.yaml)",
            env="TELEGRAM_CHAT_ID",
            secret=False,
            applies=lambda cfg: not dig(cfg, "notifications.telegram.chat_ids"),
        ),
    ),
)

SPECS: tuple[ConnectorSpec, ...] = (TELEGRAM, VK, AVITO, OPENROUTER, SHEETS, TELEGRAM_BOT)

ENV_MAP: dict[str, tuple[str, ...]] = {
    credential.env: credential.keys for spec in SPECS for credential in spec.credentials if credential.env
}

if len({spec.name for spec in SPECS}) != len(SPECS):  # fail at import, not at runtime
    raise RuntimeError("connector names must be unique")
if len(ENV_MAP) != sum(1 for spec in SPECS for c in spec.credentials if c.env):
    raise RuntimeError("an environment variable is declared by two credentials")
