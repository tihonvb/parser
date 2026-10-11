"""Telegram user-account connector: settings, session lifecycle, login, status. No network, no account."""

import asyncio
import os
import stat

import pytest

from lead_parser.infrastructure.integrations.telegram import client as tg
from lead_parser.infrastructure.integrations.telegram.client import (
    TelegramAccount,
    TelegramApiSettings,
    TelegramAuthorizationRequired,
    TelegramConfigurationError,
    mask_phone,
    problems,
)

API_HASH = "0123456789abcdef0123456789abcdef"


def cfg(tmp_path, **override):
    telegram = {
        "api_id": "123456",
        "api_hash": API_HASH,
        "session_name": str(tmp_path / ".data" / "telegram"),
    }
    telegram.update(override)
    return {"telegram": telegram}


class FakeUser:
    id = 42
    username = "tester"
    first_name = "Ivan"
    last_name = "P"
    phone = "79991234567"


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs
        self.events: list[str] = []
        self.authorized = True
        self.connect_delay = 0.0
        self.disconnect_error: Exception | None = None

    async def connect(self):
        await asyncio.sleep(self.connect_delay)
        self.events.append("connect")

    async def is_user_authorized(self):
        return self.authorized

    async def get_me(self):
        return FakeUser()

    async def start(self, **kwargs):
        self.events.append("start")

    async def disconnect(self):
        self.events.append("disconnect")
        if self.disconnect_error:
            raise self.disconnect_error


def account(tmp_path, client: FakeClient | None = None, **override) -> tuple[TelegramAccount, FakeClient]:
    fake = client or FakeClient()
    acc = TelegramAccount.from_config(
        cfg(tmp_path, **override), client_factory=lambda *a, **k: _capture(fake, a, k)
    )
    return acc, fake


def _capture(fake, args, kwargs):
    fake.args, fake.kwargs = args, kwargs
    return fake


def run(coro):
    return asyncio.run(coro)


# ---------- settings ----------


def test_settings_from_config(tmp_path):
    s = TelegramApiSettings.from_config(cfg(tmp_path))
    assert (s.api_id, s.api_hash) == (123456, API_HASH)
    assert s.session_file.name == "telegram.session"


@pytest.mark.parametrize(
    "override, fragment",
    [
        ({"api_id": ""}, "api_id is not set"),
        ({"api_id": None}, "api_id is not set"),
        ({"api_id": "PUT_YOUR_API_ID"}, "api_id is not set"),
        ({"api_id": "abc"}, "positive integer"),
        ({"api_id": "-5"}, "positive integer"),
        ({"api_hash": ""}, "api_hash is not set"),
        ({"api_hash": "PUT_YOUR_API_HASH"}, "api_hash is not set"),
        ({"session_name": ""}, "session_name"),
    ],
)
def test_bad_settings_rejected_with_clear_message(tmp_path, override, fragment):
    with pytest.raises(TelegramConfigurationError, match=fragment):
        TelegramApiSettings.from_config(cfg(tmp_path, **override))


def test_hash_format_is_diagnostic_only():
    base = {"api_id": "1", "api_hash": "not-hex", "session_name": "s"}
    assert problems(base) == []  # collection is not blocked by the format check
    assert any("32 hexadecimal" in p for p in problems(base, strict_format=True))
    assert problems({**base, "api_hash": API_HASH}, strict_format=True) == []


def test_mask_phone_hides_all_but_last_two_digits():
    assert mask_phone("+7 (999) 123-45-67") == "+*********67"
    assert mask_phone(None) == "" and mask_phone("") == ""


# ---------- single client constructor ----------


def test_new_client_uses_noninteractive_policy_and_creates_session_dir(tmp_path):
    acc, fake = account(tmp_path)
    acc.new_client()
    assert fake.args == (str(tmp_path / ".data" / "telegram"), 123456, API_HASH)
    assert fake.kwargs == {
        "flood_sleep_threshold": 0,
        "request_retries": 1,
        "connection_retries": 1,
        "timeout": 10.0,
    }
    assert (tmp_path / ".data").is_dir()
    acc.new_client(interactive=True)
    assert fake.kwargs == {}  # interactive login keeps Telethon defaults


# ---------- session lifecycle ----------


def test_session_connects_checks_authorization_and_always_disconnects(tmp_path):
    acc, fake = account(tmp_path)

    async def scenario():
        async with acc.session() as client:
            assert client is fake
            assert fake.events == ["connect"]

    run(scenario())
    assert fake.events == ["connect", "disconnect"]


def test_unauthorized_session_raises_and_still_disconnects(tmp_path):
    fake = FakeClient()
    fake.authorized = False
    acc, _ = account(tmp_path, fake)

    async def scenario():
        async with acc.session():
            raise AssertionError("body must not run")

    with pytest.raises(TelegramAuthorizationRequired, match="telegram-auth login"):
        run(scenario())
    assert fake.events == ["connect", "disconnect"]


def test_unauthorized_allowed_when_not_required(tmp_path):
    fake = FakeClient()
    fake.authorized = False
    acc, _ = account(tmp_path, fake)

    async def scenario():
        async with acc.session(require_authorization=False) as client:
            return client

    assert run(scenario()) is fake


def test_error_in_body_still_disconnects(tmp_path):
    acc, fake = account(tmp_path)

    async def scenario():
        async with acc.session():
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        run(scenario())
    assert fake.events[-1] == "disconnect"


def test_connect_timeout_disconnects(tmp_path):
    fake = FakeClient()
    fake.connect_delay = 5
    acc, _ = account(tmp_path, fake, channel_timeout_seconds=0.05)

    async def scenario():
        async with acc.session():
            pass

    with pytest.raises(asyncio.TimeoutError):
        run(scenario())
    assert fake.events == ["disconnect"]


def test_cancellation_disconnects(tmp_path):
    acc, fake = account(tmp_path)

    async def scenario():
        async def work():
            async with acc.session():
                await asyncio.sleep(30)

        task = asyncio.create_task(work())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert fake.events[-1] == "disconnect"


def test_cleanup_error_is_recorded_not_raised_and_does_not_mask_result(tmp_path):
    fake = FakeClient()
    fake.disconnect_error = RuntimeError("disconnect failed token=SECRETSECRET123456")
    acc, _ = account(tmp_path, fake)

    async def scenario():
        async with acc.session():
            return "ok"

    run(scenario())
    assert acc.cleanup_error == "RuntimeError"  # safe_error keeps only the exception class
    assert "SECRETSECRET123456" not in acc.cleanup_error


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
def test_session_files_become_owner_only(tmp_path):
    acc, fake = account(tmp_path)
    session_dir = tmp_path / ".data"
    session_dir.mkdir()
    for name in ("telegram.session", "telegram.session-journal"):
        (session_dir / name).write_text("x")
        (session_dir / name).chmod(0o644)

    async def scenario():
        async with acc.session():
            pass

    run(scenario())
    for name in ("telegram.session", "telegram.session-journal"):
        assert stat.S_IMODE((session_dir / name).stat().st_mode) == 0o600


# ---------- login and status ----------


def test_login_runs_interactive_start_and_describes_account(tmp_path):
    acc, fake = account(tmp_path)
    info = run(acc.login())
    assert fake.events == ["start", "disconnect"]
    assert fake.kwargs == {}  # interactive client
    assert (info.id, info.username, info.name, info.phone_hint) == (42, "tester", "Ivan P", "+*********67")


def test_status_without_session_file_does_not_touch_network(tmp_path):
    acc, fake = account(tmp_path)
    status = run(acc.status())
    assert (status.session_file_exists, status.authorized, status.account) == (False, False, None)
    assert fake.events == []


def test_status_authorized_returns_masked_account(tmp_path):
    acc, fake = account(tmp_path)
    (tmp_path / ".data").mkdir()
    (tmp_path / ".data" / "telegram.session").write_text("x")
    status = run(acc.status())
    assert status.authorized and status.account.username == "tester"
    assert "9991234567" not in repr(status)  # the full phone number never leaves the connector
    assert fake.events == ["connect", "disconnect"]


def test_status_not_authorized(tmp_path):
    fake = FakeClient()
    fake.authorized = False
    acc, _ = account(tmp_path, fake)
    (tmp_path / ".data").mkdir()
    (tmp_path / ".data" / "telegram.session").write_text("x")
    status = run(acc.status())
    assert status.session_file_exists and not status.authorized and status.account is None
    assert fake.events[-1] == "disconnect"


# ---------- CLI ----------


def write_config(tmp_path, **telegram_override):
    import yaml

    from lead_parser.infrastructure.configuration import load_config  # noqa: F401  (validates import path)

    telegram = {
        "enabled": False,
        "api_id": "123456",
        "api_hash": API_HASH,
        "session_name": str(tmp_path / ".data" / "telegram"),
    }
    telegram.update(telegram_override)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"telegram": telegram}), encoding="utf-8")
    return path


def test_cli_status_reports_missing_configuration_without_secrets(tmp_path, capsys, monkeypatch):
    from lead_parser.interfaces.cli import telegram_parser as cli

    monkeypatch.setattr(
        cli, "load_config", lambda path: {"telegram": {"api_id": "", "api_hash": "", "session_name": "s"}}
    )
    assert cli.main(["status"]) == 2
    out = capsys.readouterr().out
    assert "api_id is not set" in out and "api_hash is not set" in out


def test_cli_status_prints_account_not_secrets(tmp_path, capsys, monkeypatch):
    from lead_parser.interfaces.cli import telegram_parser as cli

    config = cfg(tmp_path)
    (tmp_path / ".data").mkdir()
    (tmp_path / ".data" / "telegram.session").write_text("x")
    fake = FakeClient()
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(tg, "TelegramClient", lambda *a, **k: fake)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "@tester" in out and "+*********67" in out
    assert API_HASH not in out and "9991234567" not in out


def test_cli_status_without_session_asks_for_login(tmp_path, capsys, monkeypatch):
    from lead_parser.interfaces.cli import telegram_parser as cli

    monkeypatch.setattr(cli, "load_config", lambda path: cfg(tmp_path))
    assert cli.main(["status"]) == 2
    assert "telegram-auth login" in capsys.readouterr().out


def test_collector_and_discovery_use_the_connector_factory():
    """Single constructor: collector/discovery no longer build TelegramClient with their own kwargs."""
    import inspect

    from lead_parser.infrastructure.integrations.telegram import collector, discovery

    assert "TelegramAccount" in inspect.getsource(collector)
    assert "TelegramAccount" in inspect.getsource(discovery)
    assert "flood_sleep_threshold" not in inspect.getsource(collector)
    assert "flood_sleep_threshold" not in inspect.getsource(discovery)
    assert collector.TelegramAuthorizationRequired is tg.TelegramAuthorizationRequired
