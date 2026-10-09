"""Single locked VK ID manager: PKCE, strict callback state, atomic token rotation."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import sys
import time
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import requests
from filelock import FileLock

from configuration import CONFIG_PATH, load_config
from security import private_json, safe_error

AUTHORIZE_URL = "https://id.vk.ru/authorize"
TOKEN_URL = "https://id.vk.ru/oauth2/auth"
REFRESH_IF_LESS_THAN_SEC = 1500
STATE_TTL = 600


class TokenError(RuntimeError):
    pass


class TokenManager:
    def __init__(self, cfg):
        self.settings = cfg["vk"]
        self.path = Path(self.settings["token_file"])
        self.state_path = Path(self.settings["state_file"])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.path) + ".lock", timeout=0)

    @staticmethod
    def read(path):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict):
                raise ValueError
            return value
        except (OSError, ValueError) as error:
            raise TokenError("Token/state file unavailable or invalid; authorize again") from error

    def login(self):
        with self.lock:
            verifier = secrets.token_urlsafe(64)
            challenge = (
                base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
                .decode("ascii")
                .rstrip("=")
            )
            state = secrets.token_urlsafe(32)
            private_json(
                self.state_path, {"code_verifier": verifier, "state": state, "created_at": time.time()}
            )
            return (
                AUTHORIZE_URL
                + "?"
                + urlencode(
                    {
                        "response_type": "code",
                        "client_id": self.settings["client_id"],
                        "redirect_uri": self.settings["redirect_uri"],
                        "code_challenge": challenge,
                        "code_challenge_method": "S256",
                        "state": state,
                        "scope": "wall groups offline",
                    }
                )
            )

    def request(self, data):
        response = requests.post(
            TOKEN_URL,
            data={
                **data,
                "client_id": self.settings["client_id"],
                "redirect_uri": self.settings["redirect_uri"],
            },
            timeout=(10, 30),
        )
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("access_token"), str)
            or not payload["access_token"]
        ):
            raise TokenError("VK ID exchange rejected; authorize again if refresh grant expired")
        if "state" in payload and payload["state"] != data["state"]:
            raise TokenError("VK ID response state mismatch")
        return payload

    def save(self, payload, device_id, old=None):
        old = old or {}
        expires_in = payload.get("expires_in")
        if type(expires_in) not in {int, float} or not 0 < expires_in < 10**10:
            raise TokenError("VK ID response has invalid expiry")
        value = {
            "access_token": payload["access_token"],
            "refresh_token": payload.get("refresh_token") or old.get("refresh_token"),
            "device_id": device_id,
            "user_id": payload.get("user_id", old.get("user_id")),
            "scope": payload.get("scope", old.get("scope")),
            "expires_at": time.time() + expires_in,
            "updated_at": time.time(),
            "schema_version": 1,
        }
        if not value["refresh_token"]:
            raise TokenError("Missing refresh token; repeat authorization with offline permission")
        private_json(self.path, value)
        return value

    def exchange(self, callback):
        with self.lock:
            state = self.read(self.state_path)
            created = state.get("created_at")
            if type(created) not in {int, float} or not 0 <= time.time() - created <= STATE_TTL:
                raise TokenError("PKCE state expired or legacy state has no timestamp; run login again")
            actual, expected = urlsplit(callback.strip()), urlsplit(self.settings["redirect_uri"])
            if (actual.scheme, actual.netloc, actual.path) != (
                expected.scheme,
                expected.netloc,
                expected.path,
            ):
                raise TokenError("Unexpected callback origin or path")
            params = {}
            for part in (actual.query, actual.fragment):
                for key, values in parse_qs(part).items():
                    if len(values) != 1 or key in params:
                        raise TokenError("Ambiguous callback parameters")
                    params[key] = values[0]
            if "payload" in params:
                try:
                    payload = json.loads(params["payload"])
                    if not isinstance(payload, dict) or set(payload) & set(params):
                        raise ValueError
                    params.update(payload)
                except (TypeError, ValueError) as error:
                    raise TokenError("Invalid callback payload") from error
            received = params.get("state")
            if (
                not isinstance(received, str)
                or not isinstance(state.get("state"), str)
                or not secrets.compare_digest(received, state["state"])
            ):
                raise TokenError("Missing or mismatched callback state")
            if not all(isinstance(params.get(key), str) and params[key] for key in ("code", "device_id")):
                raise TokenError("Callback requires code and device_id")
            payload = self.request(
                {
                    "grant_type": "authorization_code",
                    "code": params["code"],
                    "device_id": params["device_id"],
                    "state": received,
                    "code_verifier": state["code_verifier"],
                }
            )
            value = self.save(payload, params["device_id"])
            self.state_path.unlink()
            return value["access_token"]

    def refresh(self, force=False):
        with self.lock:
            token = self.read(self.path)
            if not all(token.get(key) for key in ("refresh_token", "device_id")):
                raise TokenError("Incomplete saved token; run login again")
            # Legacy files have no reliable issuance timestamp: force one exchange, retain refresh/device/scope.
            expiry = token.get("expires_at", 0)
            if type(expiry) not in {int, float}:
                raise TokenError("Invalid token expiry")
            if not force and expiry - time.time() > REFRESH_IF_LESS_THAN_SEC and token.get("access_token"):
                private_json(self.path, token)  # migrate permissions without changing credentials
                return token["access_token"]
            payload = self.request(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": token["refresh_token"],
                    "device_id": token["device_id"],
                    "state": secrets.token_urlsafe(32),
                }
            )
            return self.save(payload, token["device_id"], token)["access_token"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "code", "refresh"])
    parser.add_argument("callback", nargs="?")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        manager = TokenManager(load_config(args.config, require_access=False))
        if args.command == "login":
            print(manager.login())
            print("Run vk_token.py code with the complete callback within 10 minutes.")
        elif args.command == "code":
            if not args.callback:
                raise TokenError("Complete callback URL required")
            manager.exchange(args.callback)
            print("VK credentials saved privately. Parser reads the token file in vk_id mode.")
        else:
            manager.refresh(args.force)
            print("VK credentials ready.")
        return 0
    except Exception as error:
        print(
            "VK authorization failed: "
            + (str(error) if isinstance(error, TokenError) else safe_error(error)),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
