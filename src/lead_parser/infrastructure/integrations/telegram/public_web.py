"""Collector for PUBLIC Telegram channels without an account or API keys.

Reads the public web preview ``https://t.me/s/<channel>`` (what any browser sees) and implements
the same contract as the account collector: durable pages, the same cursor shape
(``high_watermark`` / ``resume_max_id``) and ``ScanResult`` reports. Enable it with
``telegram.transport: public_web``; ``api_id`` / ``api_hash`` are then not needed.

Limits of the public preview, reported honestly instead of hidden:

* only public channels with the web preview enabled (no private chats, invite links or numeric ids);
* about 20 messages per page, newest first by ``?before=<id>`` paging; the per-run budget is
  ``telegram.public_web.max_pages_per_channel``. If it is exhausted, the unread part stays in the
  cursor and the report says ``complete=False`` (a lag), exactly like the account collector;
* no sender, no replies thread, no media; some channels disable the preview entirely;
* the channel has no numeric id here: its stable identity is the lower-cased username
  (``telegram:web:<username>``), so switching transport later re-reads old posts as new.

It is polite by design: one request at a time, a pause between pages, a clear User-Agent and a stop
on HTTP 429 (the rest of that channel continues next run). Nothing is posted or joined.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from html.parser import HTMLParser

import requests

from lead_parser.application.models import ScanResult
from lead_parser.application.ports import CollectionState
from lead_parser.core.models import Lead
from lead_parser.core.policies import extract_phone, keyword_decision
from lead_parser.infrastructure.security import safe_error

BASE_URL = "https://t.me/s/"
USER_AGENT = "Mozilla/5.0 (compatible; lead-parser/0.2; +https://github.com/tihonvb/parser)"
_USERNAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{3,31}")
_REF = re.compile(r"^(?:https?://)?(?:t(?:elegram)?\.me/)?(?:s/)?@?([A-Za-z0-9_+]+)/?(?:\d+)?/?(?:\?.*)?$")


class UnsupportedChannel(ValueError):
    """The reference cannot be read through the public preview."""


def channel_username(ref: str) -> str:
    """``@name`` / ``name`` / ``https://t.me/name`` / ``t.me/s/name/123`` -> ``name``."""
    match = _REF.match(str(ref).strip())
    if not match:
        raise UnsupportedChannel(f"cannot parse channel reference {ref!r}")
    name = match.group(1)
    if name.startswith("+") or name.lower() == "joinchat" or name.isdigit():
        raise UnsupportedChannel("invite links and numeric ids need transport: account")
    if not _USERNAME.fullmatch(name):
        raise UnsupportedChannel(f"{name!r} is not a public channel username")
    return name


# ---------------------------------------------------------------- HTML


@dataclass
class WebMessage:
    channel: str
    id: int
    date: datetime | None
    text: str


@dataclass
class ParsedPage:
    title: str = ""
    has_history: bool = False
    messages: list[WebMessage] = field(default_factory=list)


class _PreviewParser(HTMLParser):
    """Extracts messages from the preview. Reply quotes reuse the text class, so only the block
    marked ``js-message_text`` (and not ``js-message_reply_text``) is the message body."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.page = ParsedPage()
        self._message: dict | None = None
        self._depth = 0  # div depth inside the current message
        self._text_depth = 0  # >0 while inside the body block
        self._in_date_link = 0

    @staticmethod
    def _classes(attrs: dict) -> set[str]:
        return set((attrs.get("class") or "").split())

    def handle_starttag(self, tag: str, attrs_list) -> None:
        attrs = dict(attrs_list)
        classes = self._classes(attrs)
        if tag == "meta" and attrs.get("property") == "og:title" and not self.page.title:
            self.page.title = attrs.get("content") or ""
        if tag == "div" and "tgme_channel_history" in classes:
            self.page.has_history = True
        if self._message is None:
            if tag == "div" and "tgme_widget_message" in classes and attrs.get("data-post"):
                channel, _, number = attrs["data-post"].rpartition("/")
                if number.isdigit():
                    self._message = {"channel": channel, "id": int(number), "date": None, "chunks": []}
                    self._depth = 1
            return
        if tag == "div":
            self._depth += 1
            if self._text_depth:
                self._text_depth += 1
            elif "js-message_text" in classes and "js-message_reply_text" not in classes:
                self._text_depth = 1
        elif tag == "br" and self._text_depth:
            self._message["chunks"].append("\n")
        elif tag == "a" and "tgme_widget_message_date" in classes:
            self._in_date_link += 1
        elif tag == "time" and self._in_date_link and self._message["date"] is None:
            stamp = attrs.get("datetime") or ""
            try:
                parsed = datetime.fromisoformat(stamp)
                self._message["date"] = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
            except ValueError:
                pass

    def handle_endtag(self, tag: str) -> None:
        if self._message is None:
            return
        if tag == "a" and self._in_date_link:
            self._in_date_link -= 1
        if tag == "div":
            if self._text_depth:
                self._text_depth -= 1
            self._depth -= 1
            if self._depth == 0:
                message = self._message
                self._message = None
                text = "".join(message["chunks"])
                text = "\n".join(re.sub(r"[ \t ]+", " ", line).strip() for line in text.split("\n")).strip()
                text = re.sub(r"\n{3,}", "\n\n", text)
                self.page.messages.append(
                    WebMessage(message["channel"], message["id"], message["date"], text)
                )

    def handle_data(self, data: str) -> None:
        if self._message is not None and self._text_depth:
            self._message["chunks"].append(data)


def parse_page(html: str) -> ParsedPage:
    parser = _PreviewParser()
    parser.feed(html)
    parser.close()
    parser.page.messages.sort(key=lambda m: m.id, reverse=True)  # newest first, like the account collector
    return parser.page


# ---------------------------------------------------------------- collection


class _Unavailable(Exception):
    pass


class _RateLimited(Exception):
    def __init__(self, retry_after: str):
        super().__init__(retry_after)
        self.retry_after = retry_after


def fetch_page(session, username: str, before: int | None, timeout: float) -> ParsedPage:
    params = {"before": before} if before else None
    response = session.get(BASE_URL + username, params=params, timeout=timeout, allow_redirects=True)
    status = response.status_code
    if status == 429:
        raise _RateLimited(response.headers.get("Retry-After", "?"))
    if status in (404, 410):
        raise _Unavailable("channel not found")
    if status >= 400:
        raise requests.HTTPError(f"HTTP {status}")
    # A channel without web preview redirects to the plain ``t.me/<name>`` page.
    if "/s/" not in str(getattr(response, "url", BASE_URL)):
        raise _Unavailable("web preview is disabled or the channel is not public")
    page = parse_page(response.text)
    if not page.has_history and not page.messages:
        raise _Unavailable("not a channel preview page")
    return page


def _settings(cfg: dict) -> dict:
    return cfg["telegram"]["public_web"]


def _final_cursor(result: ScanResult, cursor: dict, previous: dict, oldest: int | None) -> dict:
    if result.complete and not result.errors:
        return {"high_watermark": max(cursor.get("min_id", 0), cursor.get("scan_upper", 0))}
    if oldest is None:
        return dict(previous)  # nothing was read: keep the old cursor (and its lookback cutoff) as is
    return {**cursor, "resume_max_id": oldest}


def _collect_channel(
    session,
    ref,
    cfg: dict,
    store: CollectionState | None,
    reports: list[ScanResult] | None,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> list[Lead]:
    settings, web = cfg["telegram"], _settings(cfg)
    leads: list[Lead] = []
    try:
        username = channel_username(ref)
    except UnsupportedChannel as error:
        result = ScanResult(f"telegram:ref:{ref}")
        result.fail(f"PublicWebUnsupported:{error}")
        if reports is not None:
            reports.append(result)
        if store:
            store.checkpoint(result)
        return leads

    identity = username.lower()
    result = ScanResult(f"telegram:web:{identity}")
    page_leads: list[Lead] = []
    overrides = settings.get("channel_overrides", {})
    override = overrides.get(identity, overrides.get(str(ref), overrides.get(username, {})))
    previous = store.cursor(result.source_id) if store else {}
    cursor = (
        previous
        if previous.get("resume_max_id")
        else {
            "min_id": previous.get("high_watermark", 0),
            "cutoff": clock() - override.get("lookback_hours", settings["lookback_hours"]) * 3600
            if not previous
            else 0,
            "scan_upper": 0,
        }
    )
    result.cursor = cursor.copy()
    result.window = {"cutoff": cursor.get("cutoff", 0), "min_message_id": cursor.get("min_id", 0)}
    oldest = cursor.get("resume_max_id")
    before = cursor.get("resume_max_id") or None
    max_pages = web["max_pages_per_channel"]
    deadline = clock() + settings["channel_timeout_seconds"]
    title = ""

    def commit_page() -> None:
        if store:
            store.ingest(page_leads)
            result.cursor = {**cursor, "resume_max_id": oldest}
            store.checkpoint(result)
        page_leads.clear()

    try:
        pages = 0
        while True:
            if pages >= max_pages or clock() > deadline:
                result.complete = False
                break
            try:
                parsed = fetch_page(session, username, before, web["request_timeout_seconds"])
            except _Unavailable as error:
                result.fail(f"Unavailable:{error}")
                break
            except _RateLimited as error:
                result.fail(f"RateLimited:retry_after={error.retry_after}")
                break
            pages += 1
            title = title or parsed.title or username
            if not parsed.messages:
                break  # the start of the channel history
            reached = False
            for message in parsed.messages:
                stamp = message.date.timestamp() if message.date else None
                if message.id <= cursor.get("min_id", 0) or (
                    stamp is not None and stamp < cursor.get("cutoff", 0)
                ):
                    reached = True
                    break
                cursor["scan_upper"] = max(cursor.get("scan_upper", 0), message.id)
                oldest = message.id
                result.scanned += 1
                passed, reason = keyword_decision(
                    message.text, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
                )
                result.counted(reason)
                if passed:
                    lead = Lead(
                        source="telegram",
                        external_id=f"{identity}_{message.id}",
                        date=message.date.isoformat() if message.date else "",
                        text=message.text,
                        phone=extract_phone(message.text) or "",
                        source_group=title,
                        source_group_id=result.source_id,
                        url=f"https://t.me/{username}/{message.id}",
                        extra={
                            "known_city": cfg["general"]["city"],
                            "source_ref": str(ref),
                            "transport": "public_web",
                        },
                    )
                    leads.append(lead)
                    page_leads.append(lead)
                    result.candidates += 1
            result.complete = False  # mid-run checkpoints must look unfinished
            commit_page()
            result.complete = True
            if reached or min(m.id for m in parsed.messages) <= 1:
                break
            next_before = min(m.id for m in parsed.messages)
            if before is not None and next_before >= before:
                result.fail("PagingStalled:the preview did not move to older messages")
                break
            before = next_before
            sleep(web["request_delay_seconds"])
        commit_page()
        result.cursor = _final_cursor(result, cursor, previous, oldest)
    except Exception as error:  # one channel must never cancel the others
        result.fail(safe_error(error))
        commit_page()
        result.cursor = _final_cursor(result, cursor, previous, oldest)
    finally:
        if store:
            store.checkpoint(result)
    if reports is not None:
        reports.append(result)
    return leads


def collect_leads(
    cfg: dict,
    store: CollectionState | None = None,
    reports: list[ScanResult] | None = None,
    *,
    session=None,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.time,
) -> list[Lead]:
    settings = cfg["telegram"]
    if not settings["enabled"]:
        return []
    owned = session is None
    session = session or requests.Session()
    if owned:
        session.headers["User-Agent"] = USER_AGENT
    leads: list[Lead] = []
    try:
        for ref in settings["channels"]:
            leads.extend(_collect_channel(session, ref, cfg, store, reports, sleep, clock))
            sleep(_settings(cfg)["request_delay_seconds"])
    finally:
        if owned:
            session.close()
    return leads
