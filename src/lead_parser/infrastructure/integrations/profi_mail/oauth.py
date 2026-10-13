"""Microsoft OAuth2 for the mailbox connector (Outlook.com / Microsoft 365 IMAP, XOAUTH2).

Microsoft has disabled sign-in with a mailbox password over IMAP ("Basic authentication is disabled"); only OAuth2
is accepted. This module implements the *device code flow* for a public client, so **no password is ever handled**:

1. ``lead-parser profi-mail login`` prints a short code and an address; you open it in any browser, sign in to the
   mailbox and approve access;
2. the resulting tokens are stored in an owner-only file (atomic writes, mode 0600);
3. every run takes a valid access token — refreshed automatically shortly before it expires — and
   authenticates with ``AUTHENTICATE XOAUTH2``.

You need your own (free) app registration in Microsoft Entra / Azure: only its public *client id* goes into the
configuration (``profi_mail.oauth.client_id``); there is no client secret. See docs/profi-mail.md.

Messages never contain tokens or codes beyond the one-time user code that is meant to be shown to the user.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import requests

LOGIN_HOST = "https://login.microsoftonline.com"
DEFAULT_SCOPE = "https://outlook.office.com/IMAP.AccessAsUser.All offline_access"
REFRESH_MARGIN = 120  # seconds: refresh when the access token has less than this left
HTTP_TIMEOUT = 30


class OAuthError(RuntimeError):
    """A short, secret-free explanation (``Kind:details``)."""


@dataclass(frozen=True)
class DeviceCode:
    user_code: str
    verification_uri: str
    device_code: str
    interval: int
    expires_in: int


def _endpoint(settings: dict, name: str) -> str:
    return f"{LOGIN_HOST}/{settings['tenant']}/oauth2/v2.0/{name}"


def _post(http, url: str, data: dict) -> dict:
    try:
        response = http.post(url, data=data, timeout=HTTP_TIMEOUT)
        body = response.json()
    except (requests.RequestException, ValueError) as error:
        raise OAuthError(f"Network:{type(error).__name__}") from None
    if not isinstance(body, dict):
        raise OAuthError("BadResponse")
    return body


def _error(body: dict) -> OAuthError:
    code = str(body.get("error", "unknown"))
    description = str(body.get("error_description", "")).split("\r\n")[0][:140]
    return OAuthError(f"{code}:{description}" if description else code)


def start_device_flow(settings: dict, http=requests) -> DeviceCode:
    body = _post(
        http,
        _endpoint(settings, "devicecode"),
        {"client_id": settings["client_id"], "scope": settings["scope"]},
    )
    if "device_code" not in body:
        raise _error(body)
    return DeviceCode(
        user_code=str(body["user_code"]),
        verification_uri=str(body.get("verification_uri") or body.get("verification_url") or ""),
        device_code=str(body["device_code"]),
        interval=max(1, int(body.get("interval", 5))),
        expires_in=int(body.get("expires_in", 900)),
    )


def wait_for_tokens(
    settings: dict,
    code: DeviceCode,
    http=requests,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> dict:
    """Poll until the user approves the sign-in (or the code expires / is declined)."""
    deadline = clock() + code.expires_in
    interval = code.interval
    while clock() < deadline:
        sleep(interval)
        body = _post(
            http,
            _endpoint(settings, "token"),
            {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": settings["client_id"],
                "device_code": code.device_code,
            },
        )
        if "access_token" in body:
            return _with_expiry(body, clock())
        error = body.get("error")
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval += 5
            continue
        raise _error(body)
    raise OAuthError("expired_token:the sign-in code expired before it was approved")


def _with_expiry(body: dict, now: float) -> dict:
    return {
        "access_token": body["access_token"],
        "refresh_token": body.get("refresh_token", ""),
        "expires_at": int(now) + int(body.get("expires_in", 3600)),
        "scope": body.get("scope", ""),
    }


def refresh(
    settings: dict, refresh_token: str, http=requests, clock: Callable[[], float] = time.time
) -> dict:
    body = _post(
        http,
        _endpoint(settings, "token"),
        {
            "grant_type": "refresh_token",
            "client_id": settings["client_id"],
            "refresh_token": refresh_token,
            "scope": settings["scope"],
        },
    )
    if "access_token" not in body:
        error = _error(body)
        if str(body.get("error")) == "invalid_grant":
            raise OAuthError(
                "invalid_grant:the sign-in expired or was revoked; run `lead-parser profi-mail login`"
            )
        raise error
    tokens = _with_expiry(body, clock())
    tokens["refresh_token"] = (
        tokens["refresh_token"] or refresh_token
    )  # providers may omit an unchanged token
    return tokens


class TokenFile:
    """Owner-only token storage with atomic replacement."""

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load(self) -> dict | None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as error:
            raise OAuthError(f"TokenFileUnreadable:{type(error).__name__}") from None
        return data if isinstance(data, dict) else None

    def save(self, tokens: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(self.path.name + ".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(tokens, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)
        if os.name == "posix":
            self.path.chmod(0o600)


def access_token(
    settings: dict,
    store: TokenFile,
    http=requests,
    clock: Callable[[], float] = time.time,
) -> str:
    """A valid access token, refreshed (and the new tokens saved first) when it is about to expire."""
    tokens = store.load()
    if not tokens or not tokens.get("access_token"):
        raise OAuthError("NotLoggedIn:run `lead-parser profi-mail login`")
    if int(tokens.get("expires_at", 0)) - clock() > REFRESH_MARGIN:
        return str(tokens["access_token"])
    if not tokens.get("refresh_token"):
        raise OAuthError("NoRefreshToken:run `lead-parser profi-mail login` again")
    fresh = refresh(settings, str(tokens["refresh_token"]), http, clock)
    store.save(fresh)  # persist before use: a rotated refresh token must never be lost
    return fresh["access_token"]


def xoauth2(username: str, token: str) -> bytes:
    """SASL XOAUTH2 initial response (imaplib base64-encodes it)."""
    return f"user={username}\x01auth=Bearer {token}\x01\x01".encode()
