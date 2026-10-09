"""Самообновляющийся VK-токен через VK ID (OAuth 2.1 + PKCE) — для сервера.

Заменяет старую пару vk_oauth_pkce.py + vk_refresh_and_sync.py. Главное
исправление: VK ID при каждом обновлении выдаёт НОВЫЙ refresh_token, а
старый сразу перестаёт работать. Старый скрипт новый refresh_token не
сохранял — поэтому после первого же обновления цепочка рвалась
("invalid_grant: refresh_token is missing or invalid"). Здесь новый
refresh_token записывается в vk_tokens.json сразу после получения,
атомарно (через временный файл), до любых других действий.

Кроме того, обновление делается только когда access_token скоро истечёт
(меньше 25 минут), а не на каждом запуске — так refresh_token тратится
реже, и меньше шансов что-то сломать.

Использование (из папки проекта на сервере):

  1) Один раз — авторизация (нужен браузер, ЛЮБОЙ, туннель не нужен):
       venv/bin/python vk_token.py login
     Скрипт напечатает ссылку. Открой её, войди в VK, разреши доступ.
     Тебя перекинет на пустую страницу oauth.vk.ru/blank.html?... —
     скопируй ВЕСЬ адрес из адресной строки и в течение 10 минут выполни:
       venv/bin/python vk_token.py code "СЮДА_ВЕСЬ_АДРЕС"

  2) Перед каждым запуском парсера (это делает cron):
       venv/bin/python vk_token.py refresh
     Если токен ещё свежий — ничего не делает. Если скоро истечёт —
     обновляет, сохраняет новый refresh_token и вписывает новый
     access_token в config.yaml (строка vk.access_token).
     Код выхода 0 — в config.yaml рабочий токен, можно запускать main.py;
     1 — токена нет/не удалось обновить (main.py по && не запустится).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import requests

CLIENT_ID = "54797818"
REDIRECT_URI = "https://oauth.vk.ru/blank.html"
SCOPE = "wall groups offline"

AUTHORIZE_URL = "https://id.vk.ru/authorize"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "vk_pkce_state.json"
TOKENS_FILE = BASE_DIR / "vk_tokens.json"
CONFIG_FILE = BASE_DIR / "config.yaml"

REFRESH_IF_LESS_THAN_SEC = 25 * 60  # обновлять, если до истечения меньше 25 минут


# ---------- файлы ----------

def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _read_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _patch_config(access_token: str) -> None:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    new_content, n = re.subn(
        r'^(\s*access_token:\s*).*$',
        lambda m: m.group(1) + '"' + access_token + '"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        raise RuntimeError("в config.yaml не найдена строка access_token: — проверь блок vk")
    if new_content != content:
        tmp = CONFIG_FILE.with_suffix(".yaml.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(new_content)
        os.replace(tmp, CONFIG_FILE)


def _save_tokens(resp: dict, device_id: str, old: dict | None = None) -> dict:
    """Сохраняет ответ VK ID. Если VK не прислал новый refresh_token —
    оставляем прежний (на случай, если ротация не произошла)."""
    old = old or {}
    data = {
        "access_token": resp["access_token"],
        "refresh_token": resp.get("refresh_token") or old.get("refresh_token"),
        "device_id": device_id,
        "user_id": resp.get("user_id", old.get("user_id")),
        "scope": resp.get("scope", old.get("scope")),
        "expires_at": int(time.time()) + int(resp.get("expires_in", 3600)),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _write_json_atomic(TOKENS_FILE, data)
    return data


# ---------- шаги ----------

def cmd_login() -> int:
    code_verifier = secrets.token_urlsafe(64)[:96]
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    state = secrets.token_urlsafe(32)

    _write_json_atomic(STATE_FILE, {"code_verifier": code_verifier, "state": state, "created_at": int(time.time())})

    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": SCOPE,
    }
    print("Открой ссылку в браузере (в любом, туннель не нужен), войди в VK и разреши доступ:\n")
    print(f"{AUTHORIZE_URL}?{urlencode(params)}\n")
    print("Потом скопируй ВЕСЬ адрес пустой страницы oauth.vk.ru/blank.html?... и выполни (в течение 10 минут):")
    print('  venv/bin/python vk_token.py code "ВЕСЬ_АДРЕС"')
    return 0


def _parse_redirect(url: str) -> dict:
    parsed = urlparse(url.strip())
    params: dict[str, str] = {}
    for part in (parsed.query, parsed.fragment):
        for k, v in parse_qs(part).items():
            params[k] = v[0]
    # Иногда VK ID кладёт всё в JSON-параметр payload
    if "payload" in params and "code" not in params:
        try:
            payload = json.loads(params["payload"])
            for k in ("code", "device_id", "state"):
                if k in payload:
                    params[k] = str(payload[k])
        except json.JSONDecodeError:
            pass
    return params


def cmd_code(redirect_url: str) -> int:
    st = _read_json(STATE_FILE)
    if not st:
        print("Нет vk_pkce_state.json — сначала выполни: venv/bin/python vk_token.py login")
        return 1

    params = _parse_redirect(redirect_url)
    code = params.get("code")
    device_id = params.get("device_id")
    if not code or not device_id:
        print(f"В адресе не нашлось code и/или device_id. Нашлось: {sorted(params)}")
        print("Нужен адрес страницы oauth.vk.ru/blank.html?code=...&device_id=... целиком.")
        return 1
    if params.get("state") and params["state"] != st["state"]:
        print("state в адресе не совпадает с сохранённым — похоже, это адрес от другой попытки входа. Сделай login заново.")
        return 1

    resp = requests.post(
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code_verifier": st["code_verifier"],
            "redirect_uri": REDIRECT_URI,
            "code": code,
            "client_id": CLIENT_ID,
            "device_id": device_id,
            "state": st["state"],
        },
        timeout=20,
    ).json()

    if "access_token" not in resp:
        print(f"Не удалось получить токен: {resp}")
        return 1

    data = _save_tokens(resp, device_id)
    _patch_config(data["access_token"])
    try:
        STATE_FILE.unlink()
    except OSError:
        pass
    print(f"Готово. Токен получен (scope: {data.get('scope')}), сохранён в vk_tokens.json и вписан в config.yaml.")
    print("Дальше он будет обновляться сам: venv/bin/python vk_token.py refresh")
    return 0


def cmd_refresh(force: bool = False) -> int:
    tok = _read_json(TOKENS_FILE)
    if not tok or not tok.get("refresh_token") or not tok.get("device_id"):
        print("[vk_token] Нет сохранённого токена (или в нём нет refresh_token/device_id). "
              "Нужна разовая авторизация: venv/bin/python vk_token.py login")
        return 1

    left = int(tok.get("expires_at", 0)) - int(time.time())
    if not force and left > REFRESH_IF_LESS_THAN_SEC:
        # Токен свежий — на всякий случай убеждаемся, что именно он стоит в config.yaml.
        _patch_config(tok["access_token"])
        print(f"[vk_token] Токен ещё действует ~{left // 60} мин — обновление не нужно.")
        return 0

    try:
        resp = requests.post(
            TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "client_id": CLIENT_ID,
                "device_id": tok["device_id"],
                "state": secrets.token_urlsafe(32),
            },
            timeout=20,
        ).json()
    except (requests.RequestException, ValueError) as e:
        print(f"[vk_token] Сетевая ошибка при обновлении: {e}")
        return 0 if left > 60 else 1  # старый токен ещё жив — пусть парсер отработает

    if "access_token" not in resp:
        print(f"[vk_token] Ошибка обновления: {resp}")
        if resp.get("error") == "invalid_grant":
            print("[vk_token] refresh_token больше не принимается — нужна разовая авторизация заново: "
                  "venv/bin/python vk_token.py login")
        return 0 if left > 60 else 1

    data = _save_tokens(resp, tok["device_id"], old=tok)  # СНАЧАЛА сохраняем новый refresh_token
    _patch_config(data["access_token"])
    print("[vk_token] Токен обновлён, новый refresh_token сохранён, config.yaml обновлён.")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    cmd = sys.argv[1]
    if cmd == "login":
        return cmd_login()
    if cmd == "code":
        if len(sys.argv) < 3:
            print('Использование: venv/bin/python vk_token.py code "ВЕСЬ_АДРЕС_СТРАНИЦЫ"')
            return 1
        return cmd_code(sys.argv[2])
    if cmd == "refresh":
        return cmd_refresh(force="--force" in sys.argv)
    print(f"Неизвестная команда {cmd!r}. Доступно: login, code, refresh")
    return 1


if __name__ == "__main__":
    sys.exit(main())
