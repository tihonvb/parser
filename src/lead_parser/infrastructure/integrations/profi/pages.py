"""Page-level logic for the profi.ru specialist cabinet: status detection, card reading and an
anonymising HTML "skeleton" used to calibrate selectors without exposing personal data.

Nothing here knows the real markup: every selector comes from ``profi.selectors`` in the configuration,
so a layout change is a configuration change and an unrecognised page is reported, never guessed.
"""

from __future__ import annotations

import html as htmllib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

CAPTCHA_SELECTOR = "iframe[src*='captcha'], [class*='captcha'], #captcha, [id*='captcha']"
BLOCKED_STATUSES = {403, 429}


class ProfiError(RuntimeError):
    """A page-level problem with a short, secret-free message (``Kind:details``)."""


@dataclass(frozen=True)
class Order:
    order_id: str
    url: str
    title: str
    description: str
    price: str
    city: str
    published: str


def _text(element) -> str:
    return " ".join((element.inner_text() or "").split()) if element is not None else ""


def canonical_url(href: str) -> str:
    """Absolute https://profi.ru/... address without query and fragment; ``""`` for anything else."""
    href = (href or "").strip()
    if not href or href.startswith(("#", "javascript:")):
        return ""
    parts = urlsplit(urljoin("https://profi.ru/", href))
    host = parts.hostname or ""
    if parts.scheme != "https" or not (host == "profi.ru" or host.endswith(".profi.ru")):
        return ""
    return f"https://{host}{parts.path}"


def check_page(page, response, selectors: dict) -> None:
    """Raise ``ProfiError`` when the page is a block, a captcha or a login screen.

    Neither a captcha nor a login is bypassed: the run reports it and stops for that page."""
    status = getattr(response, "status", 200)
    if status in BLOCKED_STATUSES:
        raise ProfiError(f"Blocked:HTTP {status}")
    if page.query_selector(CAPTCHA_SELECTOR):
        raise ProfiError("Blocked:captcha")
    login_form = selectors.get("login_form", "").strip()
    url = str(getattr(page, "url", "") or "")
    if re.search(r"/(?:login|auth|signin)\b", urlsplit(url).path) or (
        login_form and page.query_selector(login_form)
    ):
        raise ProfiError("AuthRequired:run `lead-parser profi-auth login` and upload the session file")


def read_card(card, selectors: dict, id_pattern: str) -> Order:
    """Read one order card. Raises ``ProfiError`` when the card has no usable link / order id."""
    link = card.query_selector(selectors["link"])
    if link is None:
        raise ProfiError("CardWithoutLink")
    href = link.get_attribute("href") or ""
    url = canonical_url(href)
    match = re.search(id_pattern, urlsplit(url).path) if url else None
    if not url or not match:
        raise ProfiError("CardWithoutOrderId")

    def field(name: str) -> str:
        selector = selectors.get(name, "").strip()
        return _text(card.query_selector(selector)) if selector else ""

    date_selector = selectors.get("date", "").strip()
    date_element = card.query_selector(date_selector) if date_selector else None
    return Order(
        order_id=match.group(1),
        url=url,
        title=field("title"),
        description=field("description"),
        price=field("price"),
        city=field("city"),
        published=(date_element.get_attribute("datetime") or "").strip() if date_element is not None else "",
    )


# ------------------------------------------------------------------ anonymising skeleton

_STRUCTURAL = {"class", "id", "role", "type", "name", "itemprop", "itemtype", "rel", "tabindex", "target"}
_DROPPED = {"script", "style", "noscript", "svg", "iframe", "head", "template"}
_VOID = {"br", "hr", "img", "input", "meta", "link", "source", "area", "base", "col", "embed", "track", "wbr"}


def _shape(value: str) -> str:
    """Keep the form of a value, lose its content: letters -> x, digits -> 9."""
    return re.sub(r"\d", "9", re.sub(r"[^\W\d_]", "x", value))


class _Skeleton(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip = 0

    def _attrs(self, attrs) -> str:
        rendered = []
        for name, value in attrs:
            value = value or ""
            if name in _STRUCTURAL:
                safe = re.sub(r"\d", "9", value)  # class / id / role names are markup, not personal data
            elif name == "href":
                safe = _shape(urlsplit(value).path)  # only the form of the path: x-letters, 9-digits
            elif name.startswith(("data-", "aria-")) or name == "datetime":
                safe = _shape(value)
            else:
                continue  # value, src, alt, title, content, placeholder ...: dropped
            rendered.append(f'{name}="{htmllib.escape(safe, quote=True)}"')
        return (" " + " ".join(rendered)) if rendered else ""

    def handle_starttag(self, tag, attrs) -> None:
        if self._skip:
            self._skip += tag not in _VOID
            return
        if tag in _DROPPED:
            self._skip = 1
            return
        self.out.append(f"<{tag}{self._attrs(attrs)}>\n")

    def handle_startendtag(self, tag, attrs) -> None:
        if not self._skip and tag not in _DROPPED:
            self.out.append(f"<{tag}{self._attrs(attrs)}>\n")

    def handle_endtag(self, tag) -> None:
        if self._skip:
            self._skip -= 1
            return
        if tag not in _VOID:
            self.out.append(f"</{tag}>\n")

    def handle_data(self, data) -> None:
        if not self._skip and data.strip():
            self.out.append(_shape(" ".join(data.split())) + "\n")


def mask_html(html: str) -> str:
    """Return the page structure (tags, classes, ids, data-* shapes) with every text, number, address
    and attribute value reduced to its shape. Safe to share for selector calibration."""
    parser = _Skeleton()
    parser.feed(html)
    parser.close()
    return "".join(parser.out)
