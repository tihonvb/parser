"""Turn one raw notification e-mail from profi.ru into the pieces a lead needs.

Pure functions over bytes: no network, no mailbox, easy to test. The parser does not need to know the
exact template of Profi.ru e-mails: the lead text is the subject plus the readable body, the order link is
the first profi.ru link whose path contains an order number, and optional ``body_start_regex`` /
``body_end_regex`` cut greetings and footers. Everything else (relevance) is decided downstream by the
keyword pre-filter and the AI classifier.
"""

from __future__ import annotations

import email
import hashlib
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

_BLOCK_TAGS = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}
_SKIPPED_TAGS = {"script", "style", "head", "title"}
_URL = re.compile(r"https?://\S+")
MAX_TEXT = 5000


class _HtmlText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.chunks: list[str] = []
        self.links: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)

    def handle_endtag(self, tag) -> None:
        if tag in _SKIPPED_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data) -> None:
        if not self._skip:
            self.chunks.append(data)


def html_to_text(html: str) -> tuple[str, list[str]]:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return "".join(parser.chunks), parser.links


@dataclass
class Mail:
    sender_domain: str
    subject: str
    date: str
    message_id: str
    text: str
    links: list[str] = field(default_factory=list)
    authentication_failed: bool = False


def _clean(text: str) -> str:
    lines = [re.sub(r"[ \t ​]+", " ", line).strip() for line in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_mail(raw: bytes) -> Mail:
    message = email.message_from_bytes(raw, policy=policy.default)
    assert isinstance(message, EmailMessage)
    address = parseaddr(str(message.get("From", "")))[1].lower()
    domain = address.rsplit("@", 1)[-1] if "@" in address else ""
    results = " ".join(str(value).lower() for value in message.get_all("Authentication-Results", []))
    try:
        stamp = parsedate_to_datetime(str(message.get("Date")))
        date = stamp.isoformat() if stamp else ""
    except (TypeError, ValueError):
        date = ""

    plain = message.get_body(preferencelist=("plain",))
    html = message.get_body(preferencelist=("html",))
    links: list[str] = []
    body = ""
    if plain is not None:
        body = plain.get_content()
        links.extend(_URL.findall(body))
    if html is not None:
        html_text, hrefs = html_to_text(html.get_content())
        links = hrefs + links
        if not body.strip():
            body = html_text
    return Mail(
        sender_domain=domain,
        subject=_clean(str(message.get("Subject", ""))),
        date=date,
        message_id=str(message.get("Message-ID", "")).strip(),
        text=_clean(body),
        links=[link.rstrip(".,;)>]\"'") for link in links],
        authentication_failed=bool(re.search(r"\b(?:dkim|spf|dmarc)=(?:fail|softfail|permerror)\b", results)),
    )


def domain_allowed(domain: str, allowed: list[str]) -> bool:
    """Is the sender's domain accepted? An EMPTY list means any sender (a mailbox that only receives Profi.ru)."""
    if not allowed:
        return True
    domain = domain.lower().rstrip(".")
    return any(domain == item.lower() or domain.endswith("." + item.lower()) for item in allowed)


def order_link(links: list[str], link_pattern: str, id_pattern: str) -> tuple[str, str]:
    """(order id, canonical https URL) of the first profi.ru link with an order number in its path."""
    wanted = re.compile(link_pattern)
    pattern = re.compile(id_pattern)
    for link in links:
        if not wanted.fullmatch(link) and not wanted.match(link):
            continue
        parts = urlsplit(link)
        host = parts.hostname or ""
        if parts.scheme != "https" or not (host == "profi.ru" or host.endswith(".profi.ru")):
            continue
        found = pattern.search(parts.path)
        if found:
            return found.group(1), f"https://{host}{parts.path}"
    return "", ""


def lead_text(mail: Mail, start_regex: str, end_regex: str) -> str:
    """Subject plus the part of the body between the optional markers, without raw URLs."""
    body = mail.text
    if start_regex:
        found = re.search(start_regex, body, flags=re.IGNORECASE | re.MULTILINE)
        if found:
            body = body[found.end() :]
    if end_regex:
        found = re.search(end_regex, body, flags=re.IGNORECASE | re.MULTILINE)
        if found:
            body = body[: found.start()]
    body = _clean(_URL.sub("", body))
    text = f"{mail.subject}\n{body}".strip() if mail.subject else body
    return text[:MAX_TEXT]


def fallback_id(mail: Mail) -> str:
    """Stable id for a mail without an order link: derived from Message-ID (or subject+date)."""
    basis = mail.message_id or f"{mail.subject}|{mail.date}"
    return "mail-" + hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def shape(value: str) -> str:
    """Keep the form, lose the content: letters -> x, digits -> 9."""
    return re.sub(r"\d", "9", re.sub(r"[^\W\d_]", "x", value))
