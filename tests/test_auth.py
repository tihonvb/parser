import json
import time
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
import requests
from filelock import Timeout

from security import private_json
from vk_token import TokenError, TokenManager, main


def callback(manager, *, state=None):
    saved = manager.read(manager.state_path)
    return (
        manager.settings["redirect_uri"]
        + "?"
        + urlencode(
            {
                "code": "test-code",
                "device_id": "test-device",
                "state": saved["state"] if state is None else state,
            }
        )
    )


def test_login_callback_rotation_preserves_refresh_and_never_logs(cfg, monkeypatch, capsys):
    manager = TokenManager(cfg)
    link = manager.login()
    assert parse_qs(urlsplit(link).query)["code_challenge_method"] == ["S256"]
    assert manager.state_path.stat().st_mode & 0o777 == 0o600
    calls = []

    def request(url, *, data, timeout):
        calls.append(data)
        return SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {
                "access_token": "private-access-one" if len(calls) == 1 else "private-access-two",
                "refresh_token": "private-refresh" if len(calls) == 1 else "",
                "expires_in": 3600,
                "state": data["state"],
                "scope": "wall groups offline",
            },
        )

    monkeypatch.setattr(requests, "post", request)
    assert manager.exchange(callback(manager)) == "private-access-one"
    assert not manager.state_path.exists()
    assert manager.refresh() == "private-access-one" and len(calls) == 1
    assert manager.refresh(force=True) == "private-access-two"
    saved = manager.read(manager.path)
    assert saved["refresh_token"] == "private-refresh" and saved["device_id"] == "test-device"
    assert saved["expires_at"] > time.time() and saved["scope"] == "wall groups offline"
    assert main(["refresh", "--config", cfg["_config_path"]]) == 0
    assert "private-" not in capsys.readouterr().out


@pytest.mark.parametrize("kind", ["missing", "wrong", "expired", "legacy", "foreign", "duplicate"])
def test_reject_bad_callback_before_network(cfg, kind):
    manager = TokenManager(cfg)
    manager.login()
    url = callback(manager)
    state = manager.read(manager.state_path)
    if kind == "missing":
        url = manager.settings["redirect_uri"] + "?code=code&device_id=device"
    elif kind == "wrong":
        url = callback(manager, state="wrong")
    elif kind in {"expired", "legacy"}:
        if kind == "legacy":
            state.pop("created_at")
        else:
            state["created_at"] = time.time() - 601
        private_json(manager.state_path, state)
    elif kind == "foreign":
        url = url.replace("oauth.vk.ru", "example.test")
    else:
        url += "&state=another"
    with pytest.raises(TokenError):
        manager.exchange(url)


def test_legacy_tokens_migrate_once_then_fresh(cfg, monkeypatch):
    manager = TokenManager(cfg)
    private_json(
        manager.path,
        {
            "access_token": "legacy-access",
            "refresh_token": "legacy-refresh",
            "device_id": "legacy-device",
            "expires_in": 3600,
            "scope": "wall",
        },
    )
    calls = []

    def request(data):
        calls.append(data)
        return {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 3600}

    monkeypatch.setattr(manager, "request", request)
    assert manager.refresh() == "new-access"
    saved = json.loads(manager.path.read_text())
    assert (
        saved["device_id"] == "legacy-device"
        and saved["scope"] == "wall"
        and saved["refresh_token"] == "new-refresh"
    )
    assert manager.refresh() == "new-access" and len(calls) == 1


def test_competing_rotation_lock(cfg):
    first, second = TokenManager(cfg), TokenManager(cfg)
    with first.lock, pytest.raises(Timeout):
        second.refresh()


def test_oauth_failure_does_not_dump_remote_response(cfg, monkeypatch, capsys):
    manager = TokenManager(cfg)
    manager.login()
    monkeypatch.setattr(
        requests,
        "post",
        lambda *args, **kwargs: SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"error": "invalid_grant", "error_description": "private-secret"},
        ),
    )
    assert main(["code", callback(manager), "--config", cfg["_config_path"]]) == 1
    assert "private-secret" not in capsys.readouterr().err
