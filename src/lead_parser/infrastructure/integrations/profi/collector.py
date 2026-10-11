"""Order collector for the profi.ru specialist cabinet (read-only, through an authorised browser session).

What it does: opens the configured order-list pages with an existing session, reads the visible cards,
applies the same keyword pre-filter as every other source and stores the matches as leads. What it never
does: log in, type credentials, click "respond", pay, paginate behind the user's back or bypass a captcha.

Coverage is honest: each list page is a snapshot (``coverage="current_listing"``); cards beyond
``max_orders_per_page`` are reported as ``BudgetExhausted``; an unrecognised page, a login screen or a block
is a failed ``ScanResult`` with a precise reason — never an empty successful run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

from lead_parser.application.models import ScanResult
from lead_parser.application.ports import CollectionState
from lead_parser.core.models import Lead
from lead_parser.core.policies import extract_phone, keyword_decision
from lead_parser.infrastructure.integrations.profi.browser import ProfiBrowser
from lead_parser.infrastructure.integrations.profi.pages import ProfiError, check_page, read_card
from lead_parser.infrastructure.security import safe_error


def _message(error: Exception) -> str:
    return str(error) if isinstance(error, ProfiError) else safe_error(error)


def source_id(url: str) -> str:
    parts = urlsplit(url)
    return "profi:list:" + (parts.path.strip("/") or "root") + (("?" + parts.query) if parts.query else "")


def _wait_for_content(page, selectors: dict) -> None:
    wanted = [
        selectors[k] for k in ("card", "empty", "logged_in", "login_form") if selectors.get(k, "").strip()
    ]
    try:
        page.wait_for_selector(", ".join(wanted), state="attached")
    except Exception as error:
        raise ProfiError("PageLayoutChangedOrTimedOut") from error


def _lead(order, cfg: dict, list_url: str) -> tuple[Lead | None, str]:
    """Pre-filter an order: (lead or None, reason). ``None`` = not a request for the configured work."""
    settings = cfg["profi"]
    text = "\n".join(part for part in (order.title, order.description) if part)
    passed, reason = keyword_decision(text, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"])
    if not passed:
        return None, reason
    city = cfg["general"]["city"]
    if settings["require_city_match"] and city.casefold() not in order.city.casefold():
        return None, "other_city" if order.city else "no_city"
    return (
        Lead(
            source="profi",
            external_id=order.order_id,
            date=order.published,
            text=text,
            phone=extract_phone(text) or "",
            price=order.price,
            url=order.url,
            source_group="Профи.ру",
            source_group_id="profi:cabinet",
            extra={"known_city": order.city or city, "list_url": list_url},
        ),
        reason,
    )


def _scan_list(page, url: str, cfg: dict, store: CollectionState | None) -> tuple[ScanResult, list[Lead]]:
    settings = cfg["profi"]
    selectors = settings["selectors"]
    result = ScanResult(source_id(url), coverage="current_listing")
    leads: list[Lead] = []
    try:
        response = page.goto(
            url, wait_until="domcontentloaded", timeout=settings["request_timeout_seconds"] * 1000
        )
        check_page(page, response, selectors)
        _wait_for_content(page, selectors)
        check_page(page, response, selectors)  # a captcha or login can appear after the first paint
        logged_in = selectors.get("logged_in", "").strip()
        if logged_in and not page.query_selector(logged_in):
            raise ProfiError("AuthRequired:the cabinet marker is missing; the session may have expired")
        cards = page.query_selector_all(selectors["card"])
        if not cards:
            empty = selectors.get("empty", "").strip()
            if empty and page.query_selector(empty):
                result.counted("empty_list")
            else:
                raise ProfiError("NoCardsOrLayoutChanged:set profi.selectors.empty to confirm an empty list")
        limit = settings["max_orders_per_page"]
        if len(cards) > limit:
            result.fail(f"BudgetExhausted:only the first {limit} of {len(cards)} orders were read")
        broken = 0
        for card in cards[:limit]:
            result.scanned += 1
            try:
                order = read_card(card, selectors, settings["order_id_pattern"])
                lead, reason = _lead(order, cfg, url)
                result.counted(reason)
                if lead is not None:
                    leads.append(lead)
                    result.candidates += 1
                    if store:
                        store.ingest([lead])
            except Exception as error:
                broken += 1
                if broken <= 3:
                    result.fail(_message(error))
        if cards and broken == min(len(cards), limit):
            result.fail(
                "MarkupChanged:no card could be read; recalibrate with `lead-parser profi-auth inspect`"
            )
    except Exception as error:
        result.fail(_message(error))
    return result, leads


def collect_leads(
    cfg: dict,
    store: CollectionState | None = None,
    reports: list[ScanResult] | None = None,
    *,
    playwright_factory: Callable = sync_playwright,
    sleep: Callable[[float], None] = time.sleep,
) -> list[Lead]:
    reports = reports if reports is not None else []
    settings = cfg["profi"]
    if not settings["enabled"]:
        return []
    leads: list[Lead] = []
    cleanup_errors: list[str] = []
    with playwright_factory() as pw:
        browser = ProfiBrowser(pw, settings)
        try:
            browser.open()
            healthy = False
            for index, url in enumerate(settings["urls"]):
                if index:
                    sleep(settings["page_delay_seconds"])
                page = browser.page()
                try:
                    result, found = _scan_list(page, url, cfg, store)
                finally:
                    browser.close_page(page)
                leads.extend(found)
                healthy = healthy or not result.errors
                reports.append(result)
                if store:
                    store.checkpoint(result)
            if healthy:
                try:
                    browser.save_session()  # keep refreshed cookies alive between runs
                except Exception as error:
                    result = ScanResult("profi:session")
                    result.fail("SessionSave:" + safe_error(error))
                    reports.append(result)
        except Exception as error:
            result = ScanResult("profi:browser")
            result.fail(_message(error))
            reports.append(result)
            if store:
                store.checkpoint(result)
        finally:
            cleanup_errors = browser.close()
    if cleanup_errors:
        result = ScanResult("profi:cleanup")
        for error in cleanup_errors:
            result.fail(error)
        reports.append(result)
        if store:
            store.checkpoint(result)
    return leads
