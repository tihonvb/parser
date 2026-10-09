"""Avito public detail extraction with explicit resource ownership and diagnostics."""

from __future__ import annotations

import re
from datetime import datetime
from urllib.parse import urlencode, urljoin, urlsplit, urlunsplit

import requests
from playwright.sync_api import TimeoutError as BrowserTimeout
from playwright.sync_api import sync_playwright

from common import Lead, ScanResult, extract_phone, matches_keywords
from security import safe_error

SELECTORS = {
    "card": '[data-marker="item"]',
    "title": '[itemprop="name"]',
    "price": '[data-marker="item-price"]',
    "link": 'a[data-marker="item-title"]',
    "description": '[data-marker="item-view/item-description"], [itemprop="description"]',
    "published": 'time[datetime], meta[itemprop="datePosted"]',
}


class SourceError(RuntimeError):
    """A fixed diagnostic code, never remote response text."""


def _build_url(city_slug, query):
    return f"https://www.avito.ru/{city_slug}?" + urlencode({"q": query})


def canonical_url(href):
    if not href:
        raise ValueError("Missing listing URL")
    parts = urlsplit(urljoin("https://www.avito.ru", href))
    if parts.hostname not in {"avito.ru", "www.avito.ru"} or parts.scheme not in {"http", "https"}:
        raise ValueError("Invalid listing origin")
    return urlunsplit(("https", "www.avito.ru", parts.path.rstrip("/"), "", ""))


def _extract_id_from_link(href):
    url = canonical_url(href)
    match = re.search(r"_(\d+)$", urlsplit(url).path)
    if not match:
        raise ValueError("Missing Avito listing ID")
    return match[1]


def _start_dolphin_profile(settings):
    response = requests.get(
        settings["local_api_url"].rstrip("/") + f"/v1.0/browser_profiles/{settings['profile_id']}/start",
        params={"automation": 1},
        timeout=(5, 30),
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("success") is not True:
        raise RuntimeError("DolphinStartFailed")
    return f"http://127.0.0.1:{int(payload['automation']['port'])}"


def _stop_dolphin_profile(settings):
    response = requests.get(
        settings["local_api_url"].rstrip("/") + f"/v1.0/browser_profiles/{settings['profile_id']}/stop",
        timeout=(5, 10),
    )
    response.raise_for_status()
    if response.json().get("success") is not True:
        raise RuntimeError("DolphinStopFailed")


class BrowserResources:
    def __init__(self, pw, settings):
        self.pw, self.settings = pw, settings
        self.browser = self.context = None
        self.owns_browser = self.owns_context = self.dolphin_started = False
        self.pages = []

    def open(self):
        endpoint = self.settings["cdp_endpoint"]
        dolphin = self.settings["dolphin"]
        if dolphin["enabled"]:
            # Stop is attempted even if the start request succeeded remotely but its response was lost.
            self.dolphin_started = True
            endpoint = _start_dolphin_profile(dolphin)
        if endpoint:
            self.browser = self.pw.chromium.connect_over_cdp(
                endpoint, timeout=self.settings["request_timeout_seconds"] * 1000
            )
            if self.browser.contexts:
                self.context = self.browser.contexts[0]
            else:
                self.context = self.browser.new_context()
                self.owns_context = True
        else:
            self.browser = self.pw.chromium.launch(headless=self.settings["headless"])
            self.owns_browser = True
            self.context = self.browser.new_context(locale="ru-RU")
            self.owns_context = True
        return self

    def page(self):
        page = self.context.new_page()
        page.set_default_timeout(self.settings["request_timeout_seconds"] * 1000)
        self.pages.append(page)
        return page

    def close(self) -> list[str]:
        errors = []
        actions = [("page", page.close) for page in self.pages]
        if self.owns_context and self.context:
            actions.append(("context", self.context.close))
        if self.owns_browser and self.browser:
            actions.append(("browser", self.browser.close))
        if self.dolphin_started:
            actions.append(("dolphin", lambda: _stop_dolphin_profile(self.settings["dolphin"])))
        for name, action in actions:
            try:
                action()
            except Exception as error:
                errors.append(f"Cleanup:{name}:{safe_error(error)}")
        return errors


def _text(element):
    return element.inner_text().strip() if element else ""


def _page_status(page, response, *, listing=False):
    if response is None:
        raise SourceError("MissingHTTPResponse")
    if response.status in {403, 429} or page.query_selector('[data-marker="captcha"], #captcha, .captcha'):
        raise SourceError("SourceBlocked")
    if response.status >= 400:
        raise SourceError(f"HTTP:{response.status}")
    if listing and not page.query_selector(SELECTORS["description"]):
        raise SourceError("DetailLayoutChanged")


def _wait_content(page, selector, diagnostic):
    try:
        page.wait_for_selector(selector + ', [data-marker="captcha"], #captcha, .captcha', state="attached")
    except BrowserTimeout as error:
        raise SourceError(diagnostic) from error


def _listing(card, detail, cfg):
    link = card.query_selector(SELECTORS["link"])
    title = _text(card.query_selector(SELECTORS["title"]))
    url = canonical_url(link.get_attribute("href") if link else "")
    identity = _extract_id_from_link(url)
    response = detail.goto(
        url, wait_until="domcontentloaded", timeout=cfg["avito"]["request_timeout_seconds"] * 1000
    )
    _page_status(detail, response)
    _wait_content(detail, SELECTORS["description"], "DetailLayoutChangedOrTimedOut")
    _page_status(detail, response, listing=True)
    text = title + "\n\n" + _text(detail.query_selector(SELECTORS["description"]))
    if not matches_keywords(text, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]):
        return None
    published = detail.query_selector(SELECTORS["published"])
    raw_date = (
        (published.get_attribute("datetime") or published.get_attribute("content")) if published else None
    )
    date = ""
    if raw_date:
        try:
            value = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
            date = value.isoformat()
        except ValueError:
            pass
    return Lead(
        source="avito",
        external_id=identity,
        date=date,
        text=text,
        url=url,
        price=_text(card.query_selector(SELECTORS["price"])),
        phone=extract_phone(text) or "",
        source_group="Avito " + cfg["general"]["city"],
        source_group_id="avito:" + cfg["avito"]["city_slug"],
        extra={"title": title, "publication_date_known": bool(date), "known_city": cfg["general"]["city"]},
    )


def collect_leads(cfg, store=None, reports=None):
    reports = reports if reports is not None else []
    settings = cfg["avito"]
    if not settings["enabled"]:
        return []
    leads = []
    cleanup_errors = []
    with sync_playwright() as pw:
        resources = BrowserResources(pw, settings)
        try:
            resources.open()
            page, detail = resources.page(), resources.page()
            for query in settings["search_queries"]:
                result = ScanResult("avito:search:" + settings["city_slug"] + ":" + query)
                try:
                    response = page.goto(
                        _build_url(settings["city_slug"], query),
                        wait_until="domcontentloaded",
                        timeout=settings["request_timeout_seconds"] * 1000,
                    )
                    _page_status(page, response)
                    _wait_content(
                        page,
                        SELECTORS["card"] + ', [data-marker="search-results/no-results"]',
                        "SearchLayoutChangedOrTimedOut",
                    )
                    _page_status(page, response)
                    cards = page.query_selector_all(SELECTORS["card"])
                    if not cards and not page.query_selector('[data-marker="search-results/no-results"]'):
                        raise SourceError("SearchLayoutChangedOrUnrecognizedBlock")
                    if not cards:
                        result.counted("empty_results")
                    limit = min(settings["max_listings_per_query"], settings["detail_limit"])
                    if len(cards) > limit:
                        result.fail("BudgetExhausted: listing coverage is best effort")
                    for card in cards[:limit]:
                        result.scanned += 1
                        try:
                            lead = _listing(card, detail, cfg)
                            if lead:
                                result.counted("candidate")
                                leads.append(lead)
                                result.candidates += 1
                                if store:
                                    store.ingest([lead])
                            else:
                                result.counted("prefilter_rejected")
                        except Exception as error:
                            result.fail(str(error) if isinstance(error, SourceError) else safe_error(error))
                        if store:
                            store.checkpoint(result)
                except Exception as error:
                    result.fail(str(error) if isinstance(error, SourceError) else safe_error(error))
                reports.append(result)
                if store:
                    store.checkpoint(result)
        except Exception as error:
            result = ScanResult("avito:browser")
            result.fail(str(error) if isinstance(error, SourceError) else safe_error(error))
            reports.append(result)
            if store:
                store.checkpoint(result)
        finally:
            cleanup_errors = resources.close()
    if cleanup_errors:
        result = ScanResult("avito:cleanup")
        for error in cleanup_errors:
            result.fail(error)
        reports.append(result)
        if store:
            store.checkpoint(result)
    return leads
