from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest

import avito_parser
from avito_parser import (
    SELECTORS,
    BrowserResources,
    SourceError,
    _extract_id_from_link,
    _listing,
    _page_status,
)


class HTML(HTMLParser):
    def __init__(self, content):
        super().__init__()
        self.nodes = []
        self.current = None
        self.feed(content)

    def handle_starttag(self, tag, attrs):
        self.current = SimpleNamespace(tag=tag, attrs=dict(attrs), text="")
        self.nodes.append(self.current)

    def handle_data(self, data):
        if self.current:
            self.current.text += data

    def query_selector(self, selector):
        for node in self.nodes:
            attributes = node.attrs
            matches = {
                SELECTORS["title"]: attributes.get("itemprop") == "name",
                SELECTORS["price"]: attributes.get("data-marker") == "item-price",
                SELECTORS["link"]: attributes.get("data-marker") == "item-title",
                SELECTORS["description"]: attributes.get("data-marker") == "item-view/item-description",
                SELECTORS["published"]: node.tag == "time" and "datetime" in attributes,
            }
            if matches.get(selector):
                return SimpleNamespace(
                    inner_text=lambda node=node: node.text,
                    get_attribute=lambda key, attrs=attributes: attrs.get(key),
                )
        return None

    def goto(self, *args, **kwargs):
        return SimpleNamespace(status=200)

    def wait_for_selector(self, *args, **kwargs):
        return None


def test_html_full_description_date_phone_id(cfg):
    card = HTML(Path("fixtures/avito-card.html").read_text())
    detail = HTML(Path("fixtures/avito-detail.html").read_text())
    lead = _listing(card, detail, cfg)
    assert lead.external_id == "123" and "Ищу бригаду" in lead.text and lead.phone == "+79990000000"
    assert lead.date == "2026-10-01T12:00:00+03:00" and "?" not in lead.url and "#" not in lead.url
    assert _extract_id_from_link("/samara/offer_123#contact") == "123"
    detail.nodes = [node for node in detail.nodes if node.tag != "time"]
    lead = _listing(card, detail, cfg)
    assert lead.date == "" and lead.observed_at and lead.extra["publication_date_known"] is False


@pytest.mark.parametrize("href", ["", "/", "https://example.test/ad_123", "/samara/ad"])
def test_missing_invalid_url_never_creates_lead(href):
    with pytest.raises(ValueError):
        _extract_id_from_link(href)


def test_block_and_changed_layout_are_distinct():
    page = HTML("<html></html>")
    with pytest.raises(SourceError, match="SourceBlocked"):
        _page_status(page, SimpleNamespace(status=403))
    with pytest.raises(SourceError, match="DetailLayoutChanged"):
        _page_status(page, SimpleNamespace(status=200), listing=True)


def test_borrowed_cdp_only_owned_pages_closed(cfg):
    calls = []
    context = SimpleNamespace(close=lambda: calls.append("context"))
    browser = SimpleNamespace(contexts=[context], close=lambda: calls.append("browser"))
    cfg["avito"]["cdp_endpoint"] = "http://localhost:9222"
    pw = SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=lambda *args, **kwargs: browser))
    resources = BrowserResources(pw, cfg["avito"]).open()
    resources.pages.append(SimpleNamespace(close=lambda: calls.append("own-page")))
    assert resources.close() == [] and calls == ["own-page"]


def test_dolphin_connect_failure_stops_profile(cfg, monkeypatch):
    cfg["avito"]["dolphin"]["enabled"] = True
    calls = []
    monkeypatch.setattr(avito_parser, "_start_dolphin_profile", lambda *args: "http://localhost:9222")
    monkeypatch.setattr(avito_parser, "_stop_dolphin_profile", lambda *args: calls.append("stop"))

    def failed(*args, **kwargs):
        raise RuntimeError("connect")

    resources = BrowserResources(
        SimpleNamespace(chromium=SimpleNamespace(connect_over_cdp=failed)), cfg["avito"]
    )
    with pytest.raises(RuntimeError):
        resources.open()
    assert resources.close() == [] and calls == ["stop"]


def test_independent_cleanup_after_context_failure(cfg, monkeypatch):
    calls = []

    def failed():
        calls.append("context")
        raise RuntimeError("close")

    resources = BrowserResources(None, cfg["avito"])
    resources.owns_context = resources.owns_browser = resources.dolphin_started = True
    resources.context = SimpleNamespace(close=failed)
    resources.browser = SimpleNamespace(close=lambda: calls.append("browser"))
    resources.pages = [SimpleNamespace(close=lambda: calls.append("page"))]
    monkeypatch.setattr(avito_parser, "_stop_dolphin_profile", lambda *args: calls.append("stop"))
    assert resources.close() == ["Cleanup:context:RuntimeError"]
    assert calls == ["page", "context", "browser", "stop"]


def test_partial_card_and_cleanup_failure_keep_good_data(cfg, monkeypatch):
    from contextlib import nullcontext

    from storage import Store

    cfg["avito"].update(enabled=True, search_queries=["ремонт"])
    good = HTML(Path("fixtures/avito-card.html").read_text())
    broken = HTML('<div data-marker="item"><span itemprop="name">broken</span></div>')
    search = HTML("<html></html>")
    search.query_selector_all = lambda *args: [good, broken]
    detail = HTML(Path("fixtures/avito-detail.html").read_text())
    pages = iter([search, detail])
    closed = []
    for page in (search, detail):
        page.set_default_timeout = lambda *args: None
        page.close = lambda: closed.append("page")

    def failed_context_close():
        closed.append("context")
        raise RuntimeError("cleanup")

    context = SimpleNamespace(new_page=lambda: next(pages), close=failed_context_close)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: closed.append("browser"))
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setattr(avito_parser, "sync_playwright", lambda: nullcontext(pw))
    with Store(cfg["storage"]["database"]) as store:
        reports = []
        leads = avito_parser.collect_leads(cfg, store, reports)
        assert len(leads) == 1 and store.lead("avito:123")
        assert reports[0].complete is False and reports[0].scanned == 2
        assert reports[-1].source_id == "avito:cleanup" and closed == ["page", "page", "context", "browser"]
