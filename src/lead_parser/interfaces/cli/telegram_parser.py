"""Telegram user-account setup: interactive login and a status check.

    lead-parser telegram-auth status   # configuration + session + which account (no secrets printed)
    lead-parser telegram-auth login    # interactive: phone, code, optional 2FA password

``api_id`` / ``api_hash`` come from https://my.telegram.org (see docs/telegram-account.md) and are
set in ``telegram.api_id`` / ``telegram.api_hash`` or in TELEGRAM_API_ID / TELEGRAM_API_HASH.
"""

import argparse
import asyncio

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.telegram.client import (
    TelegramAccount,
    TelegramApiSettings,
    TelegramConfigurationError,
    problems,
)
from lead_parser.infrastructure.security import safe_error

EXIT_OK = 0
EXIT_NOT_READY = 2


def _print_status(cfg: dict) -> int:
    issues = problems(cfg["telegram"], strict_format=True)
    if issues:
        print("Telegram API is not configured:")
        for issue in issues:
            print(f"  - {issue}")
        return EXIT_NOT_READY
    settings = TelegramApiSettings.from_config(cfg)
    print(f"api_id: set; api_hash: set; session: {settings.session_file}")
    try:
        status = asyncio.run(TelegramAccount.from_config(cfg).status())
    except Exception as error:
        print(f"Could not check the session: {safe_error(error)}")
        return EXIT_NOT_READY
    if not status.session_file_exists:
        print("Session file not found: run `lead-parser telegram-auth login`.")
        return EXIT_NOT_READY
    if not status.authorized or status.account is None:
        print("Session exists but is not authorized: run `lead-parser telegram-auth login`.")
        return EXIT_NOT_READY
    account = status.account
    username = f"@{account.username}" if account.username else "(no username)"
    print(
        f"Authorized as {account.name or '-'} {username}, id {account.id}, phone {account.phone_hint or '-'}"
    )
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["login", "status"])
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    if args.command == "status":
        return _print_status(cfg)
    try:
        account = asyncio.run(TelegramAccount.from_config(cfg).login())
    except TelegramConfigurationError as error:
        print(f"Telegram API is not configured: {error}")
        return EXIT_NOT_READY
    username = f"@{account.username}" if account.username else "(no username)"
    print(f"Authorized as {account.name or '-'} {username}, id {account.id}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
