#!/usr/bin/env python3
"""
Одноразовое получение VK access_token + refresh_token через новый
протокол VK ID (OAuth 2.1 + PKCE), который заменил старый response_type=token.

step1  -> печатает ссылку авторизации (открыть в туннельном Chrome)
step2  -> обменивает code+device_id на токены, сохраняет vk_tokens.json
refresh -> обновляет access_token через refresh_token (для cron/main.py)
"""
import sys, json, base64, hashlib, os, secrets, urllib.request, urllib.parse

CLIENT_ID = "54797818"
REDIRECT_URI = "https://oauth.vk.ru/blank.html"
SCOPE = "wall,groups,offline"
HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(HERE, "vk_pkce_state.json")
TOKENS_FILE = os.path.join(HERE, "vk_tokens.json")

def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

def step1():
    verifier = b64url(secrets.token_bytes(64))
    challenge = b64url(hashlib.sha256(verifier.encode()).digest())
    state = b64url(secrets.token_bytes(16))
    with open(STATE_FILE, "w") as f:
        json.dump({"code_verifier": verifier, "state": state}, f)
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = "https://id.vk.ru/authorize?" + urllib.parse.urlencode(params)
    print("\nОткрой эту ссылку В ТУННЕЛЬНОМ Chrome:\n")
    print(url)
    print("\nПосле разрешения доступа скопируй из адресной строки code= и device_id= и запусти:")
    print(f"  python3 {os.path.basename(__file__)} step2 <code> <device_id>")

def step2(code, device_id):
    with open(STATE_FILE) as f:
        st = json.load(f)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": st["code_verifier"],
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "device_id": device_id,
        "state": st["state"],
    }
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request("https://id.vk.ru/oauth2/auth", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode())
        return
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "access_token" in result:
        result["device_id"] = device_id
        with open(TOKENS_FILE, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"\nСохранено в {TOKENS_FILE}")
    else:
        print("\nОшибка обмена кода на токен — см. JSON выше.")

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
    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
    except urllib.error.HTTPError as e:
        print("HTTP", e.code, e.read().decode())
        return None
    if "access_token" in result:
        result["device_id"] = tok["device_id"]
        with open(TOKENS_FILE, "w") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print("Токен обновлён.")
        return result["access_token"]
    print("Ошибка обновления токена:", result)
    return None

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "step1":
        step1()
    elif cmd == "step2":
        step2(sys.argv[2], sys.argv[3])
    elif cmd == "refresh":
        refresh()
    else:
        print(__doc__)
