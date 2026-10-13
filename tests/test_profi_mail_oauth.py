"""Outlook / Microsoft 365 OAuth2 for the mailbox connector. No network: the Microsoft endpoints are faked."""

import base64
import copy
import imaplib
import json
import os
import stat
from datetime import UTC, datetime

import pytest
import requests

from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, validate_config
from lead_parser.infrastructure.integrations.profi_mail import collector as mail_collector
from lead_parser.infrastructure.integrations.profi_mail import oauth
from lead_parser.infrastructure.integrations.profi_mail.oauth import OAuthError, TokenFile
from lead_parser.interfaces.cli import profi_mail as cli

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
CLIENT = "11111111-2222-3333-4444-555555555555"
SECRET_ACCESS, SECRET_REFRESH = "ACCESSTOKENVALUE_aaaaaaaa", "REFRESHTOKENVALUE_bbbbbbbb"
NEW_ACCESS, NEW_REFRESH = "ACCESSTOKENVALUE_cccccccc", "REFRESHTOKENVALUE_dddddddd"


class Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status

    def json(self):
        if isinstance(self._body, Exception):
            raise self._body
        return self._body


class Http:
    """Scripted Microsoft endpoints: ``plan`` maps endpoint name -> list of responses (last one repeats)."""

    def __init__(self, **plan):
        self.plan, self.calls = plan, []

    def post(self, url, data=None, timeout=None):
        name = url.rsplit("/", 1)[-1]
        self.calls.append((name, dict(data or {}), url))
        queue = self.plan[name]
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return Resp(item)


def settings(tmp_path, **override):
    options = {
        "client_id": CLIENT,
        "tenant": "consumers",
        "scope": oauth.DEFAULT_SCOPE,
        "token_file": str(tmp_path / "oauth.json"),
    }
    options.update(override)
    return options


DEVICE = {
    "device_code": "DEVCODE",
    "user_code": "ABCD-1234",
    "verification_uri": "https://microsoft.com/devicelogin",
    "interval": 1,
    "expires_in": 900,
}
TOKENS = {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_in": 3600, "scope": "IMAP"}


# ---------------------------------------------------------------- device flow


def test_device_flow_requests_the_right_endpoints_and_scope(tmp_path):
    http = Http(devicecode=[DEVICE])
    code = oauth.start_device_flow(settings(tmp_path), http)
    name, data, url = http.calls[0]
    assert url == "https://login.microsoftonline.com/consumers/oauth2/v2.0/devicecode"
    assert data == {"client_id": CLIENT, "scope": oauth.DEFAULT_SCOPE}
    assert (code.user_code, code.verification_uri, code.interval) == (
        "ABCD-1234",
        "https://microsoft.com/devicelogin",
        1,
    )


def test_waiting_until_the_user_approves_respects_the_interval_and_slow_down():
    opts = {"client_id": CLIENT, "tenant": "consumers", "scope": "s"}
    http = Http(token=[{"error": "authorization_pending"}, {"error": "slow_down"}, TOKENS])
    sleeps, clock = [], iter(range(0, 10_000, 10))
    code = oauth.DeviceCode("U", "https://x", "DEVCODE", 5, 900)
    tokens = oauth.wait_for_tokens(opts, code, http, sleeps.append, lambda: float(next(clock)))
    assert sleeps == [5, 5, 10]  # slow_down lengthens the polling interval
    assert tokens["access_token"] == SECRET_ACCESS and tokens["refresh_token"] == SECRET_REFRESH
    assert http.calls[0][1]["grant_type"] == "urn:ietf:params:oauth:grant-type:device_code"
    assert http.calls[0][1]["device_code"] == "DEVCODE" and "client_secret" not in http.calls[0][1]


@pytest.mark.parametrize(
    "error,expected",
    [
        ("authorization_declined", "authorization_declined"),
        ("expired_token", "expired_token"),
        ("invalid_client", "invalid_client"),
    ],
)
def test_declined_or_failed_sign_in_is_a_clear_error(error, expected):
    http = Http(token=[{"error": error, "error_description": "AADSTS70000: details\r\nTrace ID: abc"}])
    with pytest.raises(OAuthError, match=expected) as caught:
        oauth.wait_for_tokens(
            {"client_id": CLIENT, "tenant": "consumers", "scope": "s"},
            oauth.DeviceCode("U", "https://x", "D", 1, 900),
            http,
            lambda s: None,
            lambda: 0.0,
        )
    assert "Trace ID" not in str(caught.value)  # only the first line of the description


def test_code_that_is_never_approved_expires():
    http = Http(token=[{"error": "authorization_pending"}])
    ticks = iter([0.0, 0.0, 400.0, 800.0, 1200.0])
    with pytest.raises(OAuthError, match="expired_token"):
        oauth.wait_for_tokens(
            {"client_id": CLIENT, "tenant": "consumers", "scope": "s"},
            oauth.DeviceCode("U", "https://x", "D", 1, 900),
            http,
            lambda s: None,
            lambda: next(ticks),
        )


def test_network_failures_are_reported_without_details():
    http = Http(devicecode=[requests.ConnectionError("https://secret-host/path?token=ACCESSTOKENVALUE_zzzz")])
    with pytest.raises(OAuthError) as caught:
        oauth.start_device_flow({"client_id": CLIENT, "tenant": "consumers", "scope": "s"}, http)
    assert str(caught.value) == "Network:ConnectionError"


def test_unknown_app_registration_is_reported():
    http = Http(
        devicecode=[{"error": "unauthorized_client", "error_description": "AADSTS700016: app not found"}]
    )
    with pytest.raises(OAuthError, match="unauthorized_client"):
        oauth.start_device_flow({"client_id": "wrong", "tenant": "consumers", "scope": "s"}, http)


# ---------------------------------------------------------------- tokens


def test_token_file_is_atomic_and_owner_only(tmp_path):
    store = TokenFile(tmp_path / "deep" / "oauth.json")
    store.save({"access_token": "a"})
    assert store.load() == {"access_token": "a"} and not list((tmp_path / "deep").glob("*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert TokenFile(tmp_path / "absent.json").load() is None


def test_corrupt_token_file_is_an_error_not_a_crash(tmp_path):
    path = tmp_path / "oauth.json"
    path.write_text("{broken")
    with pytest.raises(OAuthError, match="TokenFileUnreadable"):
        TokenFile(path).load()


def test_valid_token_is_used_without_any_request(tmp_path):
    store = TokenFile(tmp_path / "oauth.json")
    store.save({"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_at": 10_000})
    http = Http(token=[TOKENS])
    assert oauth.access_token(settings(tmp_path), store, http, lambda: 5_000.0) == SECRET_ACCESS
    assert http.calls == []


def test_expiring_token_is_refreshed_and_saved_before_use(tmp_path):
    store = TokenFile(tmp_path / "oauth.json")
    store.save({"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_at": 10_050})
    http = Http(token=[{"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 3600}])
    token = oauth.access_token(settings(tmp_path), store, http, lambda: 10_000.0)  # < 120 s left
    assert token == NEW_ACCESS
    saved = store.load()
    assert (
        saved["refresh_token"] == NEW_REFRESH and saved["expires_at"] == 13_600
    )  # rotated token is not lost
    assert (
        http.calls[0][1]["grant_type"] == "refresh_token"
        and http.calls[0][1]["refresh_token"] == SECRET_REFRESH
    )


def test_unchanged_refresh_token_is_kept_when_the_server_omits_it(tmp_path):
    store = TokenFile(tmp_path / "oauth.json")
    store.save({"access_token": "old", "refresh_token": SECRET_REFRESH, "expires_at": 0})
    http = Http(token=[{"access_token": NEW_ACCESS, "expires_in": 3600}])
    oauth.access_token(settings(tmp_path), store, http, lambda: 10.0)
    assert store.load()["refresh_token"] == SECRET_REFRESH


def test_revoked_sign_in_asks_to_log_in_again(tmp_path):
    store = TokenFile(tmp_path / "oauth.json")
    store.save({"access_token": "old", "refresh_token": SECRET_REFRESH, "expires_at": 0})
    http = Http(token=[{"error": "invalid_grant", "error_description": "AADSTS70008: expired"}])
    with pytest.raises(OAuthError, match="profi-mail login"):
        oauth.access_token(settings(tmp_path), store, http, lambda: 10.0)
    assert store.load()["refresh_token"] == SECRET_REFRESH  # a failed refresh does not wipe the file


@pytest.mark.parametrize(
    "tokens,fragment", [(None, "NotLoggedIn"), ({"access_token": "a", "expires_at": 0}, "NoRefreshToken")]
)
def test_missing_login_is_explained(tmp_path, tokens, fragment):
    store = TokenFile(tmp_path / "oauth.json")
    if tokens:
        store.save(tokens)
    with pytest.raises(OAuthError, match=fragment):
        oauth.access_token(settings(tmp_path), store, Http(token=[TOKENS]), lambda: 10.0)


def test_xoauth2_string_format():
    raw = oauth.xoauth2("user@outlook.com", "TOKEN")
    assert raw == b"user=user@outlook.com\x01auth=Bearer TOKEN\x01\x01"
    assert base64.b64decode(base64.b64encode(raw)) == raw


# ---------------------------------------------------------------- collector with OAuth2


class FakeIMAP:
    def __init__(self, *, reject_token=False):
        self.auth_calls, self.reject_token, self.logged_out, self.closed = [], reject_token, False, False
        self.password_login_attempted = False

    def login(self, user, password):
        self.password_login_attempted = True
        raise imaplib.IMAP4.error("LOGIN failed")

    def authenticate(self, mechanism, callback):
        self.auth_calls.append((mechanism, callback(b"")))
        if self.reject_token:
            raise imaplib.IMAP4.error("AUTHENTICATE failed")
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        self._untagged = {"UIDVALIDITY": [b"7"]}
        return "OK", [b"0"]

    def response(self, code):
        return code, self._untagged.pop(code, [None])  # like imaplib: the value is cleared when read

    def uid(self, command, *args):
        return "OK", [b""]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


def config(tmp_path, **mail):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"].update(keywords=["нужен ремонт"], city="Самара")
    cfg["profi_mail"].update(
        enabled=True, host="outlook.office365.com", username="user@outlook.com", auth="oauth2"
    )
    cfg["profi_mail"]["oauth"].update(client_id=CLIENT, token_file=str(tmp_path / "oauth.json"))
    cfg["profi_mail"].update(mail)
    return cfg


def logged_in(tmp_path, expires_at=10**10):
    TokenFile(tmp_path / "oauth.json").save(
        {"access_token": SECRET_ACCESS, "refresh_token": SECRET_REFRESH, "expires_at": expires_at}
    )


def run(cfg, imap, http=None):
    reports = []
    leads = mail_collector.collect_leads(
        cfg,
        None,
        reports,
        imap_factory=lambda *a, **k: imap,
        clock=lambda: NOW,
        oauth_http=http or Http(token=[TOKENS]),
    )
    return leads, reports[0]


def test_collector_authenticates_with_xoauth2_not_a_password(tmp_path):
    logged_in(tmp_path)
    imap = FakeIMAP()
    leads, report = run(config(tmp_path), imap)
    assert imap.auth_calls == [("XOAUTH2", oauth.xoauth2("user@outlook.com", SECRET_ACCESS))]
    assert not imap.password_login_attempted and report.complete and imap.logged_out and imap.closed


def test_collector_refreshes_an_expired_token_before_connecting(tmp_path):
    logged_in(tmp_path, expires_at=0)
    http = Http(token=[{"access_token": NEW_ACCESS, "refresh_token": NEW_REFRESH, "expires_in": 3600}])
    imap = FakeIMAP()
    run(config(tmp_path), imap, http)
    assert imap.auth_calls[0][1] == oauth.xoauth2("user@outlook.com", NEW_ACCESS)
    assert TokenFile(tmp_path / "oauth.json").load()["refresh_token"] == NEW_REFRESH


def test_not_logged_in_is_reported_and_the_connection_is_closed(tmp_path):
    imap = FakeIMAP()
    _, report = run(config(tmp_path), imap)
    assert (
        not report.complete and "NotLoggedIn" in report.errors[0] and imap.logged_out and not imap.auth_calls
    )


def test_server_rejecting_the_token_gives_actionable_error_without_leaking(tmp_path):
    logged_in(tmp_path)
    imap = FakeIMAP(reject_token=True)
    _, report = run(config(tmp_path), imap)
    text = " ".join(report.errors)
    assert (
        "profi-mail login" in text
        and SECRET_ACCESS not in text
        and SECRET_REFRESH not in text
        and imap.logged_out
    )


def test_password_mode_is_unchanged(tmp_path):
    class PasswordIMAP(FakeIMAP):
        def login(self, user, password):
            self.password_login_attempted = password == "app-pass"
            return "OK", [b"ok"]

    imap = PasswordIMAP()
    cfg = config(tmp_path, auth="password", password="app-pass")
    _, report = run(cfg, imap)
    assert imap.password_login_attempted and not imap.auth_calls and report.complete


# ---------------------------------------------------------------- CLI login


def test_cli_login_shows_code_saves_tokens_and_prints_no_secrets(tmp_path):
    lines = []
    http = Http(devicecode=[DEVICE], token=[{"error": "authorization_pending"}, TOKENS])
    code = cli.login(
        config(tmp_path),
        http,
        sleep=lambda s: None,
        clock=iter(range(0, 10_000, 10)).__next__,
        out=lines.append,
    )
    printed = "\n".join(lines)
    assert code == 0 and "ABCD-1234" in printed and "https://microsoft.com/devicelogin" in printed
    assert SECRET_ACCESS not in printed and SECRET_REFRESH not in printed and "DEVCODE" not in printed
    saved = json.loads((tmp_path / "oauth.json").read_text())
    assert saved["access_token"] == SECRET_ACCESS and saved["refresh_token"] == SECRET_REFRESH
    if os.name == "posix":
        assert stat.S_IMODE((tmp_path / "oauth.json").stat().st_mode) == 0o600


def test_cli_login_without_client_id_explains_what_to_do(tmp_path):
    lines = []
    cfg = config(tmp_path)
    cfg["profi_mail"]["oauth"]["client_id"] = ""
    assert cli.login(cfg, Http(), out=lines.append) == 2 and "client_id" in lines[0]


def test_cli_login_declined_is_a_clean_failure(tmp_path):
    lines = []
    http = Http(devicecode=[DEVICE], token=[{"error": "authorization_declined"}])
    assert (
        cli.login(
            config(tmp_path),
            http,
            sleep=lambda s: None,
            clock=iter(range(0, 10_000, 10)).__next__,
            out=lines.append,
        )
        == 2
    )
    assert "authorization_declined" in lines[-1] and not (tmp_path / "oauth.json").exists()


def test_cli_status_uses_oauth(tmp_path, capsys):
    logged_in(tmp_path)
    imap = FakeIMAP()
    assert (
        cli.status(config(tmp_path), imap_factory=lambda *a, **k: imap, oauth_http=Http(token=[TOKENS])) == 0
    )
    out = capsys.readouterr().out
    assert "Mailbox OK" in out and SECRET_ACCESS not in out and imap.auth_calls


# ---------------------------------------------------------------- configuration


def base(**mail):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"]["keywords"] = ["нужен ремонт"]
    cfg["profi_mail"].update(mail)
    return cfg


def test_oauth_mode_needs_a_client_id_but_no_password():
    validate_config(
        base(
            enabled=True,
            host="h",
            username="u",
            auth="oauth2",
            oauth={**DEFAULTS["profi_mail"]["oauth"], "client_id": CLIENT},
        ),
        require_access=True,
    )
    with pytest.raises(ConfigError, match="client_id"):
        validate_config(base(enabled=True, host="h", username="u", auth="oauth2"), require_access=True)
    with pytest.raises(ConfigError, match="password"):
        validate_config(base(enabled=True, host="h", username="u", auth="password"), require_access=True)


@pytest.mark.parametrize(
    "override",
    [
        {"auth": "kerberos"},
        {"oauth": {**DEFAULTS["profi_mail"]["oauth"], "tenant": "bad tenant!"}},
        {"oauth": {**DEFAULTS["profi_mail"]["oauth"], "scope": ""}},
        {"oauth": {**DEFAULTS["profi_mail"]["oauth"], "token_file": " "}},
    ],
)
def test_invalid_oauth_settings_rejected(override):
    with pytest.raises(ConfigError):
        validate_config(base(**override), require_access=False)


def test_token_file_is_resolved_next_to_the_config_and_client_id_comes_from_env(tmp_path, monkeypatch):
    import yaml

    from lead_parser.infrastructure.configuration import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"general": {"keywords": ["нужен ремонт"]}}), encoding="utf-8")
    monkeypatch.setenv("PROFI_MAIL_OAUTH_CLIENT_ID", CLIENT)
    loaded = load_config(path, require_access=False)["profi_mail"]["oauth"]
    assert loaded["client_id"] == CLIENT and loaded["token_file"] == str(
        tmp_path / ".data" / "profi_mail_oauth.json"
    )


def test_defaults_are_backward_compatible():
    assert DEFAULTS["profi_mail"]["auth"] == "password" and DEFAULTS["profi_mail"]["enabled"] is False
