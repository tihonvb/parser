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
        self.anchors: list[tuple[str, str]] = []  # (visible text of the link, href): the buttons of the mail
        self._skip = 0
        self._anchor: list | None = None

    def handle_starttag(self, tag, attrs) -> None:
        if tag in _SKIPPED_TAGS:
            self._skip += 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(href)
            self._anchor = [href or "", ""]

    def handle_endtag(self, tag) -> None:
        if tag == "a" and self._anchor is not None:
            text = " ".join(self._anchor[1].split())
            if self._anchor[0]:
                self.anchors.append((text, self._anchor[0]))
            self._anchor = None
        if tag in _SKIPPED_TAGS and self._skip:
            self._skip -= 1
        if tag in _BLOCK_TAGS:
            self.chunks.append("\n")

    def handle_data(self, data) -> None:
        if not self._skip:
            self.chunks.append(data)
            if self._anchor is not None:
                self._anchor[1] += data


def html_to_text(html: str) -> tuple[str, list[str]]:
    text, links, _ = html_parts(html)
    return text, links


def html_parts(html: str) -> tuple[str, list[str], list[tuple[str, str]]]:
    parser = _HtmlText()
    parser.feed(html)
    parser.close()
    return "".join(parser.chunks), parser.links, parser.anchors


@dataclass
class Mail:
    sender_domain: str
    subject: str
    date: str
    message_id: str
    text: str
    links: list[str] = field(default_factory=list)
    authentication_failed: bool = False
    buttons: list[tuple[str, str]] = field(
        default_factory=list
    )  # (caption, href) of the links of the HTML part


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
    buttons: list[tuple[str, str]] = []
    if html is not None:
        html_text, hrefs, buttons = html_parts(html.get_content())
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
        buttons=buttons,
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


# Parameters that look like secrets or tracking are never copied into a lead; routing parameters
# (sub1/sub2/... fromSection) are kept because the Profi.ru app needs them to open the order.
_SECRET_NAME = re.compile(r"(?i)tok|tkn|sig|hash|hsh|hmac|auth|pass|secret|key|sess|code|^utm|^_")


def _clean_query(query: str, keep_name: str) -> str:
    """The query exactly as written (encoding preserved), without secret-looking or tracking parameters."""
    kept = []
    for pair in query.split("&"):
        name = pair.split("=", 1)[0]
        if name == keep_name or (name and not _SECRET_NAME.search(name)):
            kept.append(pair)
    return "&".join(kept)


def order_link(links: list[str], link_pattern: str, id_pattern: str) -> tuple[str, str]:
    """(order id, canonical https URL) of the first profi.ru link that points at an order.

    * Click trackers (``/api/...``) are never used: their ``id`` is a click number, and following one signs
      the user in (it redirects with a login token), so it must not be published either.
    * The order number is looked for in the path first, then in the query string: preferred parameters
      (``o``, ``id``, ``order`` ...) first. Profi.ru's own link is ``/backoffice/n.php?o=<number>|||<kind>``:
      for the ``o`` parameter the number is the digits at the start of the value.
    * The canonical link keeps the parameters of the link exactly as written, except those whose names look
      like secrets (token, sig, hash, auth ...) or tracking (utm_*, _*): those must never be copied to Telegram or
      the sheet. Routing parameters such as ``sub1=order&sub2=<number>`` are kept — without them the Profi.ru
      app opens the cabinet but cannot load the order."""
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
                query = _clean_query(parts.query, name) or urlencode({name: value})
                return matched.group(1), f"{base}?{query}"
    return "", ""


def _rank(name: str) -> int:
    name = name.lower()
    return _ID_PARAMS.index(name) if name in _ID_PARAMS else len(_ID_PARAMS)


def button_link(buttons: list[tuple[str, str]], caption_regex: str) -> str:
    """The https profi.ru address behind the mail button whose caption matches (e.g. «Посмотреть заказ»).

    CAUTION: such a button usually opens the order AND signs the user in (the redirect carries a login token),
    so publishing it gives whoever reads the Telegram chat / sheet access to the specialist's cabinet."""
    wanted = re.compile(caption_regex, re.IGNORECASE)
    for caption, href in buttons:
        if not wanted.search(caption):
            continue
        parts = urlsplit(href.strip())
        host = parts.hostname or ""
        if parts.scheme == "https" and (host == "profi.ru" or host.endswith(".profi.ru")):
            return href.strip()
    return ""


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
