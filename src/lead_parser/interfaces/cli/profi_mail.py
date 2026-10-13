"""Check and calibrate the Profi.ru mailbox connector (read-only).

    lead-parser profi-mail login                       # Outlook / Microsoft 365: sign in via browser (OAuth2), no password
    lead-parser profi-mail status                      # can we log in; how many Profi.ru mails in the window
    lead-parser profi-mail inspect [--count 1] [--out FILE] [--raw-out FILE]
                                                       # masked structure of the latest matching mails
                                                       # (letters -> x, digits -> 9) for safe sharing;
                                                       # --raw-out keeps the readable text for YOUR eyes only

The mailbox is opened read-only and messages are read with BODY.PEEK: nothing is marked read or changed.
The password is taken from PROFI_MAIL_PASSWORD or config.yaml and is never printed. See docs/profi-mail.md.
"""

import argparse
import imaplib
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.profi_mail import oauth
from lead_parser.infrastructure.integrations.profi_mail.collector import (
    MailError,
    _select_uids,
    connect,
    select_folder,
)
from lead_parser.infrastructure.integrations.profi_mail.message import (
    domain_allowed,
    lead_text,
    order_link,
    parse_mail,
    shape,
)
from lead_parser.infrastructure.security import safe_error

EXIT_OK, EXIT_NOT_READY = 0, 2


def _open(cfg: dict, imap_factory, oauth_http=requests):
    settings = cfg["profi_mail"]
    credentials = settings["password"] if settings["auth"] == "password" else settings["oauth"]["client_id"]
    if not (settings["host"] and settings["username"] and credentials):
        raise MailError(
            "NotConfigured:set profi_mail.host, username and "
            + ("PROFI_MAIL_PASSWORD" if settings["auth"] == "password" else "oauth.client_id")
        )
    imap = connect(settings, imap_factory, oauth_http)
    try:
        validity = select_folder(imap, settings["folder"])
    except MailError:
        imap.logout()
        raise MailError("FolderNotFound:" + settings["folder"]) from None
    return imap, validity


def _profi_uids(imap, cfg: dict, validity: int) -> list[int]:
    """UIDs of mails from the configured sender domains in the lookback window (headers only)."""
    settings = cfg["profi_mail"]
    found = []
    for uid in _select_uids(imap, {}, settings, datetime.now(UTC), validity):
        _, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        raw = next((part[1] for part in parts if isinstance(part, tuple)), b"")
        if domain_allowed(parse_mail(raw + b"\r\n").sender_domain, settings["sender_domains"]):
            found.append(uid)
    return found


def login(cfg: dict, http=requests, sleep=time.sleep, clock=time.time, out=print) -> int:
    """Device-code sign-in for Outlook / Microsoft 365: you approve in a browser, we store only tokens."""
    settings = cfg["profi_mail"]
    options = settings["oauth"]
    if not options["client_id"].strip():
        out(
            "Not ready: set profi_mail.oauth.client_id (or PROFI_MAIL_OAUTH_CLIENT_ID); see docs/profi-mail.md."
        )
        return EXIT_NOT_READY
    try:
        code = oauth.start_device_flow(options, http)
        out(f"Open {code.verification_uri} in any browser and enter the code: {code.user_code}")
        out(
            f"Sign in to {settings['username'] or 'your mailbox'} and approve access. Waiting up to {code.expires_in // 60} min..."
        )
        tokens = oauth.wait_for_tokens(options, code, http, sleep, clock)
        oauth.TokenFile(options["token_file"]).save(tokens)
    except oauth.OAuthError as error:
        out(f"Not ready: {error}")
        return EXIT_NOT_READY
    out(
        f"Signed in. Tokens saved: {options['token_file']} (owner-only). Set profi_mail.auth: oauth2 and enabled: true."
    )
    return EXIT_OK


def status(cfg: dict, imap_factory=imaplib.IMAP4_SSL, oauth_http=requests) -> int:
    try:
        imap, validity = _open(cfg, imap_factory, oauth_http)
    except (MailError, OSError) as error:
        print(f"Not ready: {error if isinstance(error, MailError) else safe_error(error)}")
        return EXIT_NOT_READY
    try:
        settings = cfg["profi_mail"]
        matching = _profi_uids(imap, cfg, validity)
        print(f"Mailbox OK: {settings['folder']}, UIDVALIDITY {validity}.")
        print(
            f"Mail from {', '.join(settings['sender_domains'])} in the last {settings['lookback_days']} days: {len(matching)}."
        )
        if not matching:
            print(
                "None found: enable e-mail notifications about new orders in the Profi.ru specialist settings."
            )
        return EXIT_OK
    finally:
        for name in ("close", "logout"):
            try:
                getattr(imap, name)()
            except Exception:
                pass


def inspect(
    cfg: dict,
    count: int,
    out: Path,
    raw_out: Path | None,
    imap_factory=imaplib.IMAP4_SSL,
    oauth_http=requests,
) -> int:
    try:
        imap, validity = _open(cfg, imap_factory, oauth_http)
    except (MailError, OSError) as error:
        print(f"Not ready: {error if isinstance(error, MailError) else safe_error(error)}")
        return EXIT_NOT_READY
    settings = cfg["profi_mail"]
    masked, raw = [], []
    try:
        for uid in _profi_uids(imap, cfg, validity)[-count:]:
            _, parts = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
            mail = parse_mail(next(p[1] for p in parts if isinstance(p, tuple)))
            order_id, url = order_link(mail.links, settings["link_pattern"], settings["order_id_pattern"])
            body = lead_text(mail, settings["body_start_regex"], settings["body_end_regex"])
            masked.append(
                "--- mail ---\n"
                f"sender domain: {mail.sender_domain}\nsubject shape: {shape(mail.subject)}\n"
                f"order link found: {bool(url)} (id length {len(order_id)}); link shape: {shape(url) if url else '-'}\n"
                f"other link shapes: {[shape(link)[:80] for link in mail.links[:6]]}\n"
                f"authentication failed: {mail.authentication_failed}\n--- body shape ---\n{shape(body)}\n"
            )
            raw.append(
                f"--- mail ---\nSubject: {mail.subject}\nDate: {mail.date}\nLinks: {mail.links}\n\n{body}\n"
            )
    finally:
        for name in ("close", "logout"):
            try:
                getattr(imap, name)()
            except Exception:
                pass
    if not masked:
        print("No matching mails in the window: nothing to inspect.")
        return EXIT_NOT_READY
    out.write_text("\n".join(masked), encoding="utf-8")
    print(f"Masked structure of {len(masked)} mail(s) saved: {out}")
    if raw_out is not None:
        descriptor = os.open(raw_out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(raw))
        print(f"Readable text saved for your eyes only (mode 0600): {raw_out} — do not share it.")
    return EXIT_OK


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", choices=["login", "status", "inspect"])
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--count", type=int, default=1, help="inspect: how many latest mails")
    parser.add_argument("--out", default="profi-mail-skeleton.txt")
    parser.add_argument("--raw-out", help="inspect: also save the readable text here (owner-only)")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=False)
    if args.command == "login":
        return login(cfg)
    if args.command == "status":
        return status(cfg)
    return inspect(cfg, max(1, args.count), Path(args.out), Path(args.raw_out) if args.raw_out else None)


if __name__ == "__main__":
    sys.exit(main())
