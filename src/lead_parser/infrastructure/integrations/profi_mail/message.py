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
import html as htmllib
import re
from dataclasses import dataclass, field
from email import policy
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urlsplit

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
        body = htmllib.unescape(
            plain.get_content()
        )  # the plain part may carry literal HTML entities (&#178;)
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


_ID_PARAMS = ("o", "id", "order", "order_id", "orderid", "oid")  # "o" is Profi.ru's own order link parameter
_TRACKER_PREFIXES = ("/api/",)  # click trackers: their "id" is a click number and the link signs the user in


def _raw_pair(query: str, name: str) -> str:
    """The query pair ``name=...`` exactly as written (encoding preserved)."""
    for pair in query.split("&"):
        if pair.split("=", 1)[0] == name:
            return pair
    return ""


def order_link(links: list[str], link_pattern: str, id_pattern: str) -> tuple[str, str]:
    """(order id, canonical https URL) of the first profi.ru link that points at an order.

    * Click trackers (``/api/...``) are never used: their ``id`` is a click number, and following one signs
      the user in (it redirects with a login token), so it must not be published either.
    * The order number is looked for in the path first, then in the query string: preferred parameters
      (``o``, ``id``, ``order`` ...) first. Profi.ru's own link is ``/backoffice/n.php?o=<number>|||<kind>``:
      for the ``o`` parameter the number is the digits at the start of the value.
    * The canonical link keeps ONLY the parameter that carries the number, with its original encoding: the
      other parameters are tracking or sign-in tokens and must never be copied to Telegram or the sheet."""
    wanted = re.compile(link_pattern)
    pattern = re.compile(id_pattern)
    for link in links:
        if not wanted.fullmatch(link) and not wanted.match(link):
            continue
        parts = urlsplit(link)
        host = parts.hostname or ""
        if parts.scheme != "https" or not (host == "profi.ru" or host.endswith(".profi.ru")):
            continue
        if parts.path.startswith(_TRACKER_PREFIXES):
            continue
        base = f"https://{host}{parts.path}"
        found = pattern.search(parts.path)
        if found:
            return found.group(1), base
        params = sorted(parse_qsl(parts.query), key=lambda item: _rank(item[0]))
        for name, value in params:
            if name.lower() not in _ID_PARAMS:
                matched = pattern.fullmatch(value)  # unknown parameter: the whole value must be the number
            else:
                matched = pattern.match(value)  # known parameter: the number may be followed by a suffix
            if matched:
                raw = _raw_pair(parts.query, name) or urlencode({name: value})
                return matched.group(1), f"{base}?{raw}"
    return "", ""


def _rank(name: str) -> int:
    name = name.lower()
    return _ID_PARAMS.index(name) if name in _ID_PARAMS else len(_ID_PARAMS)


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
