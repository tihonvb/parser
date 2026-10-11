"""Connector to a Telegram USER account (Telethon / MTProto), not a bot.

One place owns everything about talking to Telegram as an account:

* ``TelegramApiSettings`` — ``api_id`` / ``api_hash`` / session path, validated
  without any network access (``problems`` lists what is missing or malformed).
* ``TelegramAccount.new_client`` — the single constructor of ``TelegramClient``
  (timeouts, retries and flood policy are defined here, not in every caller).
* ``TelegramAccount.session`` — async context manager: connect with a timeout,
  require an authorized session, always disconnect, keep session files 0600.
* ``TelegramAccount.login`` — explicit interactive authorization (phone, code,
  optional 2FA password); only the CLI calls it.
* ``TelegramAccount.status`` — is the session authorized, which account is it
  (id, username, masked phone). Never prints ``api_hash`` or session contents.

Where to get the credentials: https://my.telegram.org -> API development tools
(see docs/telegram-account.md). Values come from ``config.yaml`` (``telegram.api_id``,
``telegram.api_hash``) or from ``TELEGRAM_API_ID`` / ``TELEGRAM_API_HASH``.

The session file is equivalent to a logged-in account: keep it outside Git, readable
only by the runtime user, and never run two processes on the same session.
"""

from __future__ import annotations

import asyncio
import os
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from telethon import TelegramClient

from lead_parser.infrastructure.security import safe_error

AUTHORIZATION_HINT = "Run lead-parser telegram-auth login interactively"
_PLACEHOLDERS = ("PUT_YOUR", "YOUR_", "CHANGE_ME", "ВСТАВЬТЕ")
_API_HASH = re.compile(r"[0-9a-fA-F]{32}")


class TelegramConfigurationError(ValueError):
    """``telegram.api_id`` / ``api_hash`` / ``session_name`` are missing or malformed."""


class TelegramAuthorizationRequired(RuntimeError):
    """The session file exists or is absent, but the account is not logged in."""


def _is_placeholder(value: Any) -> bool:
    text = str(value or "").strip()
    return not text or any(marker in text.upper() for marker in _PLACEHOLDERS)


def problems(telegram_cfg: dict, *, strict_format: bool = False) -> list[str]:
    """Human-readable configuration problems; empty list means usable. No network.

    ``strict_format`` also checks that ``api_hash`` looks like a real one (32 hex characters);
    it is used for diagnostics (``telegram-auth status``) and does not block collection."""
    found: list[str] = []
    api_id = telegram_cfg.get("api_id")
    if _is_placeholder(api_id):
        found.append("telegram.api_id is not set (or TELEGRAM_API_ID): get it at https://my.telegram.org")
    else:
        try:
            if int(str(api_id).strip()) <= 0:
                raise ValueError
        except ValueError:
            found.append("telegram.api_id must be a positive integer")
    api_hash = telegram_cfg.get("api_hash")
    if _is_placeholder(api_hash):
        found.append("telegram.api_hash is not set (or TELEGRAM_API_HASH): get it at https://my.telegram.org")
    elif strict_format and not _API_HASH.fullmatch(str(api_hash).strip()):
        found.append("telegram.api_hash must be 32 hexadecimal characters")
    if not str(telegram_cfg.get("session_name") or "").strip():
        found.append("telegram.session_name is not set")
    return found


@dataclass(frozen=True)
class TelegramApiSettings:
    api_id: int
    api_hash: str
    session: Path
    connect_timeout: float = 30.0
    authorization_timeout: float = 15.0
    disconnect_timeout: float = 15.0
    request_timeout: float = 10.0

    @classmethod
    def from_config(cls, cfg: dict) -> TelegramApiSettings:
        telegram_cfg = cfg["telegram"]
        issues = problems(telegram_cfg)
        if issues:
            raise TelegramConfigurationError("; ".join(issues))
        timeout = telegram_cfg.get("channel_timeout_seconds")
        return cls(
            api_id=int(str(telegram_cfg["api_id"]).strip()),
            api_hash=str(telegram_cfg["api_hash"]).strip(),
            session=Path(str(telegram_cfg["session_name"])),
            connect_timeout=float(timeout) if isinstance(timeout, (int, float)) and timeout > 0 else 30.0,
        )

    @property
    def session_file(self) -> Path:
        """Telethon appends ``.session`` to the session name."""
        return self.session.with_name(self.session.name + ".session")


@dataclass(frozen=True)
class AccountInfo:
    id: int
    username: str
    name: str
    phone_hint: str


@dataclass(frozen=True)
class AccountStatus:
    session_file_exists: bool
    authorized: bool
    account: AccountInfo | None = None


def mask_phone(phone: str | None) -> str:
    digits = re.sub(r"\D", "", phone or "")
    return "+" + "*" * max(len(digits) - 2, 0) + digits[-2:] if digits else ""


def _describe(user: Any) -> AccountInfo:
    name = " ".join(filter(None, [getattr(user, "first_name", ""), getattr(user, "last_name", "")])).strip()
    return AccountInfo(
        id=int(getattr(user, "id", 0) or 0),
        username=getattr(user, "username", None) or "",
        name=name,
        phone_hint=mask_phone(getattr(user, "phone", None)),
    )


class TelegramAccount:
    """Lifecycle of one Telegram user-account client."""

    def __init__(self, settings: TelegramApiSettings, client_factory: Callable[..., Any] | None = None):
        self.settings = settings
        self._factory = client_factory
        self.cleanup_error: str = ""

    @classmethod
    def from_config(cls, cfg: dict, client_factory: Callable[..., Any] | None = None) -> TelegramAccount:
        return cls(TelegramApiSettings.from_config(cfg), client_factory)

    def new_client(self, *, interactive: bool = False) -> Any:
        """The only place that builds ``TelegramClient``.

        Noninteractive clients (collection, discovery, status) never sleep on
        FloodWait and give up quickly; the interactive login keeps Telethon defaults."""
        settings = self.settings
        settings.session.parent.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {}
        if not interactive:
            options = {
                "flood_sleep_threshold": 0,
                "request_retries": 1,
                "connection_retries": 1,
                "timeout": settings.request_timeout,
            }
        factory = self._factory or TelegramClient
        return factory(str(settings.session), settings.api_id, settings.api_hash, **options)

    def _protect_session_files(self) -> None:
        if os.name != "posix":
            return
        session = self.settings.session
        for path in session.parent.glob(session.name + ".session*"):
            try:
                path.chmod(0o600)
            except OSError:
                pass

    async def _close(self, client: Any) -> None:
        try:
            await asyncio.wait_for(client.disconnect(), timeout=self.settings.disconnect_timeout)
        except Exception as error:  # cleanup must never mask the original outcome
            self.cleanup_error = safe_error(error)
        finally:
            self._protect_session_files()

    @asynccontextmanager
    async def session(self, *, require_authorization: bool = True) -> AsyncIterator[Any]:
        """Connected client; always disconnected, even on error or cancellation."""
        self.cleanup_error = ""
        client = self.new_client()
        try:
            await asyncio.wait_for(client.connect(), timeout=self.settings.connect_timeout)
            if require_authorization and not await asyncio.wait_for(
                client.is_user_authorized(), timeout=self.settings.authorization_timeout
            ):
                raise TelegramAuthorizationRequired(AUTHORIZATION_HINT)
            yield client
        finally:
            await self._close(client)

    async def login(self, **start_options: Any) -> AccountInfo:
        """Explicit interactive authorization. Telethon asks for phone, code and 2FA
        password on the console unless callbacks are given in ``start_options``."""
        client = self.new_client(interactive=True)
        try:
            await client.start(**start_options)
            return _describe(await client.get_me())
        finally:
            await self._close(client)

    async def status(self) -> AccountStatus:
        """No session file -> not authorized, without touching the network."""
        if not self.settings.session_file.exists():
            return AccountStatus(session_file_exists=False, authorized=False)
        try:
            async with self.session(require_authorization=True) as client:
                return AccountStatus(
                    True, True, _describe(await asyncio.wait_for(client.get_me(), timeout=15))
                )
        except TelegramAuthorizationRequired:
            return AccountStatus(session_file_exists=True, authorized=False)
