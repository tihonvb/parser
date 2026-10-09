#!/usr/bin/env python3
"""
Перед каждым запуском main.py: обновляет VK access_token через refresh_token
(без браузера) и записывает свежий токен в config.yaml.
Использование в cron: python3 vk_refresh_and_sync.py && python3 main.py
"""
import json, re, os, sys, urllib.request, urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
TOKENS_FILE = os.path.join(HERE, "vk_tokens.json")
CONFIG_FILE = os.path.join(HERE, "config.yaml")
CLIENT_ID = "54797818"

def refresh():
    with open(TOKENS_FILE) as f:
        tok = json.load(f)
    data = {
        "grant_type": "refresh_token",
        "refresh_token": tok["refresh_token"],
        "client_id": CLIENT_ID,
        "device_id": tok["device_id"],
    }
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://id.vk.ru/oauth2/auth", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(req) as resp:
        result = json.load(resp)
    if "access_token" not in result:
        print("Ошибка обновления VK токена:", result, file=sys.stderr)
        sys.exit(1)
    result["device_id"] = tok["device_id"]
    with open(TOKENS_FILE, "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    return result["access_token"]

def patch_config(new_token):
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        content = f.read()
    new_content, n = re.subn(
        r'^(\s*access_token:\s*).*$',
        lambda m: m.group(1) + '"' + new_token + '"',
        content,
        count=1,
        flags=re.MULTILINE,
    )
    if n == 0:
        print("Не нашёл строку access_token: в config.yaml — обнови вручную!", file=sys.stderr)
        sys.exit(1)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        f.write(new_content)

if __name__ == "__main__":
    token = refresh()
    patch_config(token)
    print("VK токен обновлён и записан в config.yaml")
