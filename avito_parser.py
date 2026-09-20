"""Парсинг объявлений Avito через Playwright.

Avito активно борется со скрапингом (капчи, блокировки по паттерну
поведения/IP), поэтому в отличие от telegram_parser.py и vk_parser.py
(официальные API) здесь используется управление браузером.

Если объявления не грузятся / Avito показывает капчу — используйте
антидетект-браузер Dolphin{anty}. Два варианта подключения, оба задаются
в config.yaml -> avito:

1. Автоматически (рекомендуется) — avito.dolphin.enabled: true +
   avito.dolphin.profile_id. Скрипт сам запускает нужный профиль через
   Dolphin{anty} Local API (http://127.0.0.1:3001) перед каждым прогоном
   и сам его останавливает после. Порт для CDP каждый раз выдаётся заново
   Dolphin'ом — скрипт сам его подставляет, вручную ничего копировать не
   нужно. Требуется, чтобы приложение Dolphin{anty} было запущено и вы
   были в нём залогинены.
2. Вручную — avito.cdp_endpoint: если у вас уже открыт профиль с
   известным CDP-адресом (например "http://127.0.0.1:PORT" из ответа
   Dolphin Local API), впишите его напрямую. Имеет смысл, если хотите
   сами управлять запуском/остановкой профиля.

Если ни то, ни другое не задано — просто запускается обычный headless
Chromium через Playwright (без антидетекта).

ВАЖНО: селекторы карточек объявлений (data-marker=...) видны в актуальной
вёрстке Avito на момент написания скрипта, но сайт меняет их без
предупреждения. Если парсер вернёт 0 объявлений при рабочем интернете —
скорее всего, поменялась вёрстка: откройте страницу поиска руками,
посмотрите атрибуты карточек через "Просмотреть код" и поправьте
SELECTORS ниже.
"""

from __future__ import annotations

import re
import urllib.parse
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright

from common import Lead, matches_keywords

SELECTORS = {
    "card": '[data-marker="item"]',
    "title": '[itemprop="name"]',
    "price": '[data-marker="item-price"]',
    "link": 'a[data-marker="item-title"]',
}

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def _build_url(city_slug: str, query: str) -> str:
    base = f"https://www.avito.ru/{city_slug}"
    params = urllib.parse.urlencode({"q": query})
    return f"{base}?{params}"


def _extract_id_from_link(href: str) -> str:
    match = re.search(r"_(\d+)(?:\?|$)", href)
    return match.group(1) if match else href


def _dolphin_base_url(dolphin_cfg: dict) -> str:
    return dolphin_cfg.get("local_api_url", "http://127.0.0.1:3001").rstrip("/")


def _start_dolphin_profile(dolphin_cfg: dict) -> str:
    """Запускает профиль Dolphin{anty} через Local API и возвращает CDP-адрес
    вида http://127.0.0.1:PORT (порт каждый раз новый, выдаёт сам Dolphin)."""
    base = _dolphin_base_url(dolphin_cfg)
    profile_id = dolphin_cfg["profile_id"]
    resp = requests.get(
        f"{base}/v1.0/browser_profiles/{profile_id}/start",
        params={"automation": 1},
        timeout=30,
    )
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Dolphin{{anty}} Local API отказал при запуске профиля {profile_id}: {data}")
    port = data["automation"]["port"]
    return f"http://127.0.0.1:{port}"


def _stop_dolphin_profile(dolphin_cfg: dict) -> None:
    base = _dolphin_base_url(dolphin_cfg)
    profile_id = dolphin_cfg["profile_id"]
    try:
        requests.get(f"{base}/v1.0/browser_profiles/{profile_id}/stop", timeout=10)
    except requests.RequestException:
        pass  # не критично — профиль просто останется открытым, ничего не сломается


def _get_browser(pw, avito_cfg: dict):
    dolphin_cfg = avito_cfg.get("dolphin") or {}
    if dolphin_cfg.get("enabled"):
        cdp = _start_dolphin_profile(dolphin_cfg)
        print(f"[avito] Подключаюсь к профилю Dolphin{{anty}} ({cdp})")
        browser = pw.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context

    cdp = avito_cfg.get("cdp_endpoint")
    if cdp:
        browser = pw.chromium.connect_over_cdp(cdp)
        context = browser.contexts[0] if browser.contexts else browser.new_context()
        return browser, context

    browser = pw.chromium.launch(headless=avito_cfg.get("headless", True))
    context = browser.new_context(user_agent=USER_AGENT, locale="ru-RU")
    return browser, context


def collect_leads(cfg: dict) -> list[Lead]:
    avito_cfg = cfg["avito"]
    if not avito_cfg.get("enabled", True):
        return []

    general = cfg["general"]
    keywords = general["keywords"]
    exclude = general.get("exclude_keywords")
    city_slug = avito_cfg.get("city_slug", "samara")
    max_items = avito_cfg.get("max_listings_per_query", 50)
    dolphin_cfg = avito_cfg.get("dolphin") or {}

    leads: list[Lead] = []

    with sync_playwright() as pw:
        try:
            browser, context = _get_browser(pw, avito_cfg)
        except requests.exceptions.ConnectionError:
            print(f"[avito] Не достучаться до Dolphin{{anty}} Local API ({_dolphin_base_url(dolphin_cfg)}).")
            print("[avito] Проверьте, что приложение Dolphin{anty} запущено и вы вошли в аккаунт.")
            return []
        except Exception as e:
            print(f"[avito] Не удалось запустить/подключиться к браузеру: {e}")
            return []

        try:
            page = context.new_page()

            for query in avito_cfg.get("search_queries", []):
                url = _build_url(city_slug, query)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_selector(SELECTORS["card"], timeout=15000)
                except Exception as e:
                    print(f"[avito] Не удалось загрузить выдачу по запросу '{query}': {e}")
                    print("[avito] Возможно, Avito показал капчу — попробуйте включить avito.dolphin в конфиге.")
                    continue

                cards = page.query_selector_all(SELECTORS["card"])
                for card in cards[:max_items]:
                    title_el = card.query_selector(SELECTORS["title"])
                    price_el = card.query_selector(SELECTORS["price"])
                    link_el = card.query_selector(SELECTORS["link"])
                    if not title_el or not link_el:
                        continue

                    title = title_el.inner_text().strip()
                    if not matches_keywords(title, keywords, exclude):
                        continue

                    href = link_el.get_attribute("href") or ""
                    full_url = href if href.startswith("http") else f"https://www.avito.ru{href}"
                    price = price_el.inner_text().strip() if price_el else ""

                    leads.append(
                        Lead(
                            source="avito",
                            external_id=_extract_id_from_link(href),
                            date=datetime.now(timezone.utc).isoformat(),
                            author="",
                            text=title,
                            phone="",
                            price=price,
                            url=full_url,
                        )
                    )
        finally:
            context.close()
            browser.close()
            if dolphin_cfg.get("enabled"):
                _stop_dolphin_profile(dolphin_cfg)

    return leads
