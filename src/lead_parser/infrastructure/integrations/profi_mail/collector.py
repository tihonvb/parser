"""Order notifications from profi.ru read through the specialist's own mailbox (IMAP, read-only).

Profi.ru e-mails a specialist about new orders. This collector reads those e-mails and turns the ones that
pass the keyword pre-filter into leads (source ``profi``). No website login, no browser, no captcha.

Safety and honesty:

* TLS only (IMAP4_SSL); the mailbox is opened ``readonly`` and messages are fetched with ``BODY.PEEK``:
  nothing is marked as read, moved or deleted;
* only mail from ``profi_mail.sender_domains`` (an empty list accepts any sender — for a mailbox that only
  receives Profi.ru notifications); mail whose ``Authentication-Results`` reports a failed
  DKIM/SPF/DMARC is rejected (a forged sender cannot inject leads when the provider reports it);
* progress is a UID cursor (``uidvalidity`` + ``last_uid``) advanced only after the leads of a batch are stored;
  if ``max_messages_per_run`` is reached the report says ``complete=False`` and the next run continues;
* a failed message is isolated and counted; login problems, TLS/timeouts and a changed mailbox are reported
  as a precise ``ScanResult`` error, never as an empty successful run.
"""

from __future__ import annotations

import imaplib
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import requests

from lead_parser.application.models import ScanResult
from lead_parser.application.ports import CollectionState
from lead_parser.core.models import Lead
from lead_parser.core.policies import extract_phone, keyword_decision
from lead_parser.infrastructure.integrations.profi_mail import oauth
from lead_parser.infrastructure.integrations.profi_mail.message import (
    Mail,
    domain_allowed,
    fallback_id,
    lead_text,
    order_link,
    parse_mail,
)
from lead_parser.infrastructure.security import safe_error

MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
BATCH = 25


class MailError(RuntimeError):
    """A mailbox-level problem with a short, secret-free message."""


def imap_date(moment: datetime) -> str:
    """IMAP SINCE date (``11-Oct-2026``), independent of the process locale."""
    return f"{moment.day:02d}-{MONTHS[moment.month - 1]}-{moment.year}"


def _uids(data) -> list[int]:
    raw = b" ".join(item for item in data if isinstance(item, bytes)) if data else b""
    return sorted(int(token) for token in raw.split() if token.isdigit())


def _uidvalidity(imap) -> int:
    """imaplib's ``response()`` CLEARS the value it returns, so this must be read exactly once per SELECT
    (use ``select_folder``); a second call would report 0 and silently disable the UID cursor."""
    try:
        _, values = imap.response("UIDVALIDITY")
        return int(values[0]) if values and values[0] else 0
    except (TypeError, ValueError, IndexError):
        return 0


def select_folder(imap, folder: str) -> int:
    """Open the folder read-only and return its UIDVALIDITY (read once, see ``_uidvalidity``)."""
    status, _ = imap.select(folder, readonly=True)
    if status != "OK":
        raise MailError("FolderNotFound")
    return _uidvalidity(imap)


def _make_lead(mail: Mail, uid: int, cfg: dict) -> tuple[Lead | None, str]:
    settings = cfg["profi_mail"]
    text = lead_text(mail, settings["body_start_regex"], settings["body_end_regex"])
    if settings["keyword_filter"]:
        passed, reason = keyword_decision(
            text, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
        )
        if not passed:
            return None, reason
    else:
        reason = "unfiltered"  # every accepted mail becomes a lead; relevance is left to the later stages
    order_id, url = order_link(mail.links, settings["link_pattern"], settings["order_id_pattern"])
    if not order_id:
        order_id, reason = fallback_id(mail), "no_order_link"
    return (
        Lead(
            source="profi",
            external_id=order_id,
            date=mail.date,
            text=text,
            phone=extract_phone(text) or "",
            url=url,
            source_group="Профи.ру",
            source_group_id="profi:orders",
            extra={"known_city": cfg["general"]["city"], "transport": "email", "message_uid": uid},
        ),
        reason,
    )


def _select_uids(imap, cursor: dict, settings: dict, now: datetime, validity: int) -> list[int]:
    if cursor.get("uidvalidity") == validity and cursor.get("last_uid"):
        start = int(cursor["last_uid"]) + 1
        status, data = imap.uid("SEARCH", None, "UID", f"{start}:*")
        found = [uid for uid in _uids(data) if uid >= start]  # ``n:*`` always returns the last message
    else:
        since = imap_date(now - timedelta(days=settings["lookback_days"]))
        status, data = imap.uid("SEARCH", None, "SINCE", since)
        found = _uids(data)
    if status != "OK":
        raise MailError("SearchFailed")
    return found


def _scan(imap, cfg: dict, store: CollectionState | None, result: ScanResult, now: datetime) -> list[Lead]:
    settings = cfg["profi_mail"]
    validity = select_folder(imap, settings["folder"])
    previous = dict(
        result.cursor
    )  # seeded from the store before connecting, so a failure never resets progress
    uids = _select_uids(imap, previous, settings, now, validity)
    subject_filter = (
        re.compile(settings["subject_regex"], re.IGNORECASE) if settings["subject_regex"] else None
    )
    subject_excluded = (
        re.compile(settings["exclude_subject_regex"], re.IGNORECASE)
        if settings["exclude_subject_regex"]
        else None
    )
    budget = settings["max_messages_per_run"]
    if len(uids) > budget:
        result.fail(f"BudgetExhausted:{len(uids) - budget} messages remain for the next run")
        uids = uids[:budget]
    result.window = {"lookback_days": settings["lookback_days"], "first_uid": uids[0] if uids else 0}

    leads: list[Lead] = []
    last_uid = previous.get("last_uid", 0) if previous.get("uidvalidity") == validity else 0
    for start in range(0, len(uids), BATCH):
        page: list[Lead] = []
        for uid in uids[start : start + BATCH]:
            result.scanned += 1
            try:
                status, data = imap.uid("FETCH", str(uid), "(BODY.PEEK[])")
                raw = next((part[1] for part in data if isinstance(part, tuple)), None)
                if status != "OK" or not raw:
                    raise MailError("FetchFailed")
                mail = parse_mail(raw)
                if not domain_allowed(mail.sender_domain, settings["sender_domains"]):
                    result.counted("other_sender")
                elif mail.authentication_failed:
                    result.counted("failed_authentication")
                elif subject_filter and not subject_filter.search(mail.subject):
                    result.counted("subject_filtered")
                elif subject_excluded and subject_excluded.search(mail.subject):
                    result.counted("subject_excluded")
                else:
                    lead, reason = _make_lead(mail, uid, cfg)
                    result.counted(reason)
                    if lead is not None:
                        page.append(lead)
                        result.candidates += 1
            except Exception as error:
                result.fail(
                    f"Message uid={uid}:"
                    + (str(error) if isinstance(error, MailError) else safe_error(error))
                )
            last_uid = max(last_uid, uid)
        leads.extend(page)
        if store:
            store.ingest(page)  # the cursor moves only after the page is stored
            result.cursor = {"uidvalidity": validity, "last_uid": last_uid}
            store.checkpoint(result)
    result.cursor = {"uidvalidity": validity, "last_uid": last_uid or previous.get("last_uid", 0)}
    return leads


def connect(settings: dict, imap_factory: Callable = imaplib.IMAP4_SSL, oauth_http=requests):
    """Open and authenticate an IMAP connection: password (app password) or OAuth2 (XOAUTH2).

    Used by the collector and by the CLI so both authenticate identically. Raises ``MailError`` with a precise,
    secret-free reason; never leaves a half-open connection behind."""
    imap = imap_factory(settings["host"], settings["port"], timeout=settings["request_timeout_seconds"])
    try:
        if settings["auth"] == "oauth2":
            options = settings["oauth"]
            try:
                token = oauth.access_token(options, oauth.TokenFile(options["token_file"]), oauth_http)
            except oauth.OAuthError as error:
                raise MailError(f"AuthFailed:{error}") from None
            imap.authenticate("XOAUTH2", lambda _challenge: oauth.xoauth2(settings["username"], token))
        else:
            imap.login(settings["username"], settings["password"])
    except imaplib.IMAP4.error as error:
        _quiet_logout(imap)
        hint = (
            "the server rejected the OAuth2 token: run `lead-parser profi-mail login` and check the app permissions"
            if settings["auth"] == "oauth2"
            else "check username, app password and that IMAP is enabled"
        )
        raise MailError(f"AuthFailed:{hint}") from error
    except MailError:
        _quiet_logout(imap)
        raise
    return imap


def _quiet_logout(imap) -> None:
    try:
        imap.logout()
    except Exception:
        pass


def collect_leads(
    cfg: dict,
    store: CollectionState | None = None,
    reports: list[ScanResult] | None = None,
    *,
    imap_factory: Callable = imaplib.IMAP4_SSL,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    oauth_http=requests,
) -> list[Lead]:
    settings = cfg["profi_mail"]
    if not settings["enabled"]:
        return []
    result = ScanResult(f"profi:mail:{settings['folder']}", coverage="uid_cursor")
    result.cursor = dict(store.cursor(result.source_id)) if store else {}
    leads: list[Lead] = []
    imap = None
    try:
        imap = connect(settings, imap_factory, oauth_http)
        leads = _scan(imap, cfg, store, result, clock())
    except Exception as error:
        result.fail(str(error) if isinstance(error, MailError) else safe_error(error))
    finally:
        if imap is not None:
            for name in ("close", "logout"):  # close() on a read-only mailbox does not expunge
                try:
                    getattr(imap, name)()
                except Exception as error:
                    if name == "logout":
                        result.fail(f"Cleanup:{name}:{safe_error(error)}")
        if reports is not None:
            reports.append(result)
        if store:
            store.checkpoint(result)
    return leads
