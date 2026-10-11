"""profi.ru cabinet connector. No network, no browser, no account: pages and Playwright are faked."""

import contextlib
import copy
import json
import os
import stat

import pytest

from lead_parser.application.models import ScanResult
from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, validate_config
from lead_parser.infrastructure.integrations.profi import collector as profi
from lead_parser.infrastructure.integrations.profi.browser import ProfiBrowser, write_state
from lead_parser.infrastructure.integrations.profi.pages import (
    CAPTCHA_SELECTOR,
    ProfiError,
    canonical_url,
    check_page,
    mask_html,
    read_card,
)
from lead_parser.interfaces.cli import profi_auth

SEL = {
    "card": ".card",
    "link": "a.l",
    "title": ".t",
    "description": ".d",
    "price": ".p",
    "city": ".c",
    "date": "time",
    "logged_in": ".me",
    "login_form": "input[type=password]",
    "empty": ".empty",
}
URL = "https://profi.ru/backoffice/n.php"


# ---------------------------------------------------------------- fakes


class El:
    def __init__(self, text="", **attrs):
        self.text, self.attrs = text, attrs

    def inner_text(self):
        return self.text

    def get_attribute(self, name):
        return self.attrs.get(name)


class Card:
    def __init__(
        self,
        title="Нужен ремонт квартиры",
        desc="Ищу бригаду",
        href="/order/12345",
        price="30 000 ₽",
        city="Самара",
        date="2026-10-11T10:00:00+03:00",
    ):
        self.map = {
            SEL["link"]: El(href=href) if href is not None else None,
            SEL["title"]: El(title),
            SEL["description"]: El(desc),
            SEL["price"]: El(price),
            SEL["city"]: El(city),
            SEL["date"]: El(datetime=date) if date else None,
        }

    def query_selector(self, selector):
        return self.map.get(selector)


class Response:
    def __init__(self, status=200):
        self.status = status


class Page:
    def __init__(self, cards=(), markers=(".me",), status=200, url=None, wait_error=False, goto_error=None):
        self.cards, self.markers, self.status = list(cards), set(markers), status
        self.url, self.wait_error, self.goto_error = url, wait_error, goto_error
        self.closed, self.visited = False, []

    def set_default_timeout(self, ms):
        pass

    def goto(self, url, wait_until=None, timeout=None):
        self.visited.append(url)
        if self.goto_error:
            raise self.goto_error
        self.url = self.url or url
        return Response(self.status)

    def wait_for_selector(self, selector, state=None):
        if self.wait_error:
            raise TimeoutError("nothing appeared")

    def query_selector(self, selector):
        return El() if selector in self.markers else None

    def query_selector_all(self, selector):
        return self.cards if selector == SEL["card"] else []

    def content(self):
        return "<html><body><div class='card'>Иван +7 999 123-45-67</div></body></html>"

    def wait_for_timeout(self, ms):
        pass

    def close(self):
        self.closed = True


class Context:
    def __init__(self, pages, close_error=None):
        self.pages, self.close_error, self.closed, self.opened = list(pages), close_error, False, []

    def new_page(self):
        page = self.pages.pop(0) if self.pages else Page()
        self.opened.append(page)
        return page

    def storage_state(self):
        return {"cookies": [{"name": "session", "value": "fresh"}], "origins": []}

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class Browser:
    def __init__(self, context, contexts=()):
        self.context, self.contexts, self.closed, self.new_context_kwargs = (
            context,
            list(contexts),
            False,
            None,
        )

    def new_context(self, **kwargs):
        self.new_context_kwargs = kwargs
        return self.context

    def close(self):
        self.closed = True


class Chromium:
    def __init__(self, browser, connect_error=None):
        self.browser, self.connect_error, self.launched, self.cdp = browser, connect_error, False, []

    def launch(self, headless=True):
        self.launched = True
        return self.browser

    def connect_over_cdp(self, endpoint, timeout=None):
        self.cdp.append(endpoint)
        if self.connect_error:
            raise self.connect_error
        return self.browser


def factory(chromium):
    @contextlib.contextmanager
    def make():
        yield type("PW", (), {"chromium": chromium})()

    return make


class Store:
    def __init__(self):
        self.leads, self.checkpoints, self.events = [], [], []

    def ingest(self, leads):
        self.events.append("ingest")
        self.leads.extend(leads)
        return len(leads)

    def cursor(self, source_id):
        return {}

    def checkpoint(self, result):
        self.events.append("checkpoint")
        self.checkpoints.append(result)


def config(tmp_path, **override):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"]["keywords"] = ["нужен ремонт", "ищу бригаду"]
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    cfg["profi"].update(
        enabled=True, urls=[URL], storage_state=str(state), selectors=dict(SEL), page_delay_seconds=3
    )
    cfg["profi"].update(override)
    return cfg


def run(cfg, pages, *, store=None, contexts=(), browser_kwargs=None, close_error=None):
    context = Context(pages, close_error=close_error)
    browser = Browser(context, contexts=contexts)
    chromium = Chromium(browser, **(browser_kwargs or {}))
    reports, sleeps = [], []
    leads = profi.collect_leads(
        cfg, store, reports, playwright_factory=factory(chromium), sleep=sleeps.append
    )
    return leads, reports, sleeps, context, browser, chromium


# ---------------------------------------------------------------- pages


@pytest.mark.parametrize(
    "href,expected",
    [
        ("/order/123456?utm=x#top", "https://profi.ru/order/123456"),
        ("https://profi.ru/order/1", "https://profi.ru/order/1"),
        ("https://www.profi.ru/order/1", "https://www.profi.ru/order/1"),
        ("http://profi.ru/order/1", ""),
        ("https://profi.ru.evil.com/order/1", ""),
        ("https://evil.com/order/1", ""),
        ("#", ""),
        ("javascript:void(0)", ""),
        ("", ""),
    ],
)
def test_canonical_url(href, expected):
    assert canonical_url(href) == expected


def test_read_card_fields_and_id_from_path_only():
    order = read_card(Card(href="/order/987654?ref=55555#x"), SEL, r"(\d{5,})")
    assert order.order_id == "987654" and order.url == "https://profi.ru/order/987654"
    assert (order.title, order.price, order.city) == ("Нужен ремонт квартиры", "30 000 ₽", "Самара")
    assert order.published == "2026-10-11T10:00:00+03:00"


@pytest.mark.parametrize("href", [None, "", "/order/abc", "https://evil.com/order/12345"])
def test_card_without_valid_link_or_id_is_an_error(href):
    with pytest.raises(ProfiError):
        read_card(Card(href=href), SEL, r"(\d{5,})")


@pytest.mark.parametrize(
    "page,status,fragment",
    [
        (Page(markers={CAPTCHA_SELECTOR}), 200, "Blocked:captcha"),
        (Page(), 429, "Blocked:HTTP 429"),
        (Page(), 403, "Blocked:HTTP 403"),
        (Page(url="https://profi.ru/login"), 200, "AuthRequired"),
        (Page(markers={SEL["login_form"]}), 200, "AuthRequired"),
    ],
)
def test_block_and_login_screens_are_detected_not_bypassed(page, status, fragment):
    with pytest.raises(ProfiError, match=fragment):
        check_page(page, Response(status), SEL)


def test_normal_page_passes_the_check():
    check_page(Page(url="https://profi.ru/backoffice/n.php"), Response(200), SEL)


# ---------------------------------------------------------------- anonymising skeleton


def test_skeleton_keeps_structure_and_hides_content():
    html = """<html><head><title>Иван Петров</title><script>var token='SECRET123'</script></head>
    <body><div class="order-card item" id="o123" data-user="Ivan42" aria-label="Иван">
    <a class="link" href="https://profi.ru/specialists/ivan-petrov/12345?token=SECRET#x">Ремонт кухни Самара</a>
    <span class="price">30 000 ₽</span><time datetime="2026-10-11T10:00:00+03:00">сегодня</time>
    <input type="text" value="+7 999 123-45-67" name="phone" placeholder="Телефон">
    <img src="https://cdn/avatar-ivan.png" alt="Иван"><svg><path d="M0 0"/></svg>
    <style>.x{color:red}</style></div></body></html>"""
    out = mask_html(html)
    for secret in (
        "Иван",
        "Петров",
        "SECRET",
        "123-45",
        "Ремонт",
        "Самара",
        "avatar",
        "ivan",
        "cdn",
        "color",
        "Телефон",
    ):
        assert secret not in out, secret
    assert 'class="order-card item"' in out and 'id="o999"' in out and 'name="phone"' in out
    assert 'data-user="xxxx99"' in out
    assert 'href="/xxxxxxxxxxx/xxxx-xxxxxx/99999"' in out
    assert 'datetime="9999-99-99x99:99:99+99:99"' in out
    assert "<span" in out and "xxxxxx xxxxx xxxxxx" in out and "<script" not in out and "<svg" not in out


def test_skeleton_survives_garbage():
    assert isinstance(mask_html("<<<>>> <div"), str)


# ---------------------------------------------------------------- browser resources


def test_missing_session_file_is_reported_without_launching_a_browser(tmp_path):
    cfg = config(tmp_path, storage_state=str(tmp_path / "absent.json"))
    leads, reports, _, _, browser, chromium = run(cfg, [Page()])
    assert leads == [] and not chromium.launched
    assert reports[0].source_id == "profi:browser" and "AuthRequired" in reports[0].errors[0]


def test_launch_uses_saved_session_and_closes_what_it_owns(tmp_path):
    cfg = config(tmp_path)
    page = Page([Card()])
    leads, reports, _, context, browser, chromium = run(cfg, [page])
    assert browser.new_context_kwargs["storage_state"] == cfg["profi"]["storage_state"]
    assert page.closed and context.closed and browser.closed and len(leads) == 1


def test_cdp_keeps_the_users_context_and_only_closes_own_page(tmp_path):
    cfg = config(tmp_path, cdp_endpoint="http://127.0.0.1:9222")
    own = Page([Card()])
    users_context = Context([own])
    browser = Browser(None, contexts=[users_context])
    chromium = Chromium(browser)
    leads = profi.collect_leads(cfg, None, [], playwright_factory=factory(chromium), sleep=lambda s: None)
    assert len(leads) == 1 and own.closed and not users_context.closed
    assert chromium.cdp == ["http://127.0.0.1:9222"] and not chromium.launched


def test_cleanup_failure_is_reported_and_does_not_hide_leads(tmp_path):
    leads, reports, *_ = run(config(tmp_path), [Page([Card()])], close_error=RuntimeError("boom"))
    assert len(leads) == 1 and reports[-1].source_id == "profi:cleanup"
    assert "Cleanup:context" in reports[-1].errors[0]


def test_connect_failure_is_a_reported_failure_not_a_crash(tmp_path):
    cfg = config(tmp_path, cdp_endpoint="http://127.0.0.1:9222")
    leads, reports, *_ = run(cfg, [Page()], browser_kwargs={"connect_error": ConnectionError("refused")})
    assert leads == [] and reports[0].source_id == "profi:browser" and not reports[0].complete


def test_session_file_written_atomically_owner_only(tmp_path):
    path = tmp_path / "deep" / "state.json"
    write_state(path, {"cookies": []})
    assert json.loads(path.read_text()) == {"cookies": []} and not list(path.parent.glob("*.tmp"))
    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_healthy_run_refreshes_session_but_blocked_run_does_not(tmp_path):
    cfg = config(tmp_path)
    state = tmp_path / "state.json"
    before = state.read_text()
    run(cfg, [Page(markers={CAPTCHA_SELECTOR, ".me"})])
    assert state.read_text() == before  # nothing refreshed after a block
    run(cfg, [Page([Card()])])
    assert json.loads(state.read_text())["cookies"][0]["value"] == "fresh"


# ---------------------------------------------------------------- collection


def test_orders_become_leads_with_source_fields_and_are_stored_before_checkpoint(tmp_path):
    store = Store()
    cards = [Card(href="/order/11111"), Card(title="Продам диван", desc="дёшево", href="/order/22222")]
    leads, reports, *_ = run(config(tmp_path), [Page(cards)], store=store)
    (lead,) = leads
    assert (lead.source, lead.external_id, lead.price) == ("profi", "11111", "30 000 ₽")
    assert lead.url == "https://profi.ru/order/11111" and lead.source_group_id == "profi:cabinet"
    assert lead.extra["known_city"] == "Самара" and lead.extra["list_url"] == URL
    assert store.leads == leads and store.events.index("ingest") < store.events.index("checkpoint")
    report = reports[0]
    assert (
        report.coverage == "current_listing"
        and report.scanned == 2
        and report.candidates == 1
        and report.complete
    )


def test_city_match_is_optional_and_explicit(tmp_path):
    cards = [Card(href="/order/11111", city="Москва"), Card(href="/order/22222", city="Самара")]
    leads, reports, *_ = run(config(tmp_path, require_city_match=True), [Page(cards)])
    assert [lead.external_id for lead in leads] == ["22222"] and reports[0].prefilter["other_city"] == 1
    leads, *_ = run(config(tmp_path), [Page(cards)])
    assert len(leads) == 2


def test_zero_cards_is_only_ok_when_the_empty_marker_confirms_it(tmp_path):
    _, reports, *_ = run(config(tmp_path), [Page([], markers={".me", ".empty"})])
    assert reports[0].complete and reports[0].prefilter["empty_list"] == 1
    _, reports, *_ = run(config(tmp_path), [Page([], markers={".me"})])
    assert not reports[0].complete and "NoCardsOrLayoutChanged" in reports[0].errors[0]


def test_expired_session_is_reported(tmp_path):
    _, reports, *_ = run(config(tmp_path), [Page([Card()], markers=set())])
    assert not reports[0].complete and "AuthRequired" in reports[0].errors[0]


@pytest.mark.parametrize(
    "page,fragment",
    [
        (Page(markers={CAPTCHA_SELECTOR}), "Blocked:captcha"),
        (Page(status=429), "Blocked:HTTP 429"),
        (Page(url="https://profi.ru/login"), "AuthRequired"),
        (Page(wait_error=True), "PageLayoutChangedOrTimedOut"),
        (Page(goto_error=TimeoutError("slow")), "TimeoutError"),
    ],
)
def test_problem_pages_are_failures_with_reasons_and_never_empty_success(tmp_path, page, fragment):
    leads, reports, *_ = run(config(tmp_path), [page])
    assert leads == [] and not reports[0].complete and fragment in reports[0].errors[0]


def test_one_bad_page_does_not_stop_the_other_urls_and_pauses_between_them(tmp_path):
    cfg = config(tmp_path, urls=[URL, "https://profi.ru/other"])
    leads, reports, sleeps, *_ = run(
        cfg, [Page(markers={CAPTCHA_SELECTOR}), Page([Card(href="/order/33333")])]
    )
    assert [lead.external_id for lead in leads] == ["33333"]
    assert [r.complete for r in reports] == [False, True] and sleeps == [3]


def test_budget_is_reported_not_hidden(tmp_path):
    cards = [Card(href=f"/order/{10000 + i}") for i in range(5)]
    leads, reports, *_ = run(config(tmp_path, max_orders_per_page=2), [Page(cards)])
    assert len(leads) == 2 and not reports[0].complete and "BudgetExhausted" in reports[0].errors[0]


def test_broken_cards_are_isolated_but_total_breakage_means_markup_changed(tmp_path):
    mixed = [Card(href="/order/11111"), Card(href=None), Card(href="/order/22222")]
    leads, reports, *_ = run(config(tmp_path), [Page(mixed)])
    assert len(leads) == 2 and any("CardWithoutLink" in e for e in reports[0].errors)
    leads, reports, *_ = run(config(tmp_path), [Page([Card(href=None), Card(href="")])])
    assert leads == [] and any("MarkupChanged" in e for e in reports[0].errors)


def test_disabled_source_does_nothing(tmp_path):
    cfg = config(tmp_path, enabled=False)
    leads, reports, _, _, _, chromium = run(cfg, [Page([Card()])])
    assert leads == [] and reports == [] and not chromium.launched


def test_connector_is_read_only(tmp_path):
    page = Page([Card()])
    run(config(tmp_path), [page])
    assert not hasattr(page, "click") and page.visited == [URL]  # one navigation, no interaction


# ---------------------------------------------------------------- configuration and wiring


def valid(**profi_override):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"]["keywords"] = ["нужен ремонт"]
    cfg["profi"].update(profi_override)
    return cfg


def enabled(**override):
    values = {"enabled": True, "urls": [URL], "selectors": dict(SEL)}
    values.update(override)
    return valid(**values)


def test_default_config_is_valid_and_disabled():
    validate_config(valid(), require_access=False)


def test_enabled_connector_needs_calibration_not_guessing():
    validate_config(enabled(), require_access=True)
    for key in ("card", "link", "logged_in"):
        cfg = enabled()
        cfg["profi"]["selectors"][key] = ""
        with pytest.raises(ConfigError, match=key):
            validate_config(cfg, require_access=True)
    with pytest.raises(ConfigError, match="urls"):
        validate_config(valid(enabled=True, selectors=dict(SEL)), require_access=True)


@pytest.mark.parametrize(
    "url",
    [
        "http://profi.ru/x",
        "https://evil.com/x",
        "https://profi.ru.evil.com/x",
        "ftp://profi.ru/x",
        "profi.ru/x",
        5,
    ],
)
def test_only_https_profi_hosts_are_allowed(url):
    with pytest.raises(ConfigError, match="profi.urls"):
        validate_config(enabled(urls=[url]), require_access=True)


@pytest.mark.parametrize("pattern", ["(", r"\d+", r"(\d+)(\d)"])
def test_order_id_pattern_must_be_a_regex_with_one_group(pattern):
    with pytest.raises(ConfigError, match="order_id_pattern"):
        validate_config(valid(order_id_pattern=pattern), require_access=False)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_orders_per_page", 0),
        ("max_orders_per_page", 1.5),
        ("request_timeout_seconds", -1),
        ("page_delay_seconds", 0),
        ("page_delay_seconds", float("nan")),
        ("headless", "yes"),
    ],
)
def test_numbers_and_flags_are_validated(key, value):
    with pytest.raises(ConfigError):
        validate_config(valid(**{key: value}), require_access=False)


def test_bootstrap_registers_the_source_only_when_enabled():
    from lead_parser import bootstrap

    assert "profi" not in [s.name for s in bootstrap.build_sources(valid())]
    (source,) = [s for s in bootstrap.build_sources(enabled()) if s.name == "profi"]
    assert source.collector == profi.collect_leads


def test_state_path_is_resolved_next_to_the_config(tmp_path):
    import yaml

    from lead_parser.infrastructure.configuration import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"general": {"keywords": ["нужен ремонт"]}}), encoding="utf-8")
    assert load_config(path, require_access=False)["profi"]["storage_state"] == str(
        tmp_path / ".data" / "profi_state.json"
    )


# ---------------------------------------------------------------- CLI


def test_cli_login_saves_the_session_without_asking_for_credentials(tmp_path, capsys):
    context = Context([Page()])
    browser = Browser(context)
    out = tmp_path / "saved" / "state.json"
    code = profi_auth.login({}, out, playwright_factory=factory(Chromium(browser)), wait=lambda prompt: "")
    assert code == 0 and browser.closed
    assert json.loads(out.read_text())["cookies"][0]["name"] == "session"
    assert "Upload it to the server" in capsys.readouterr().out


def test_cli_status_reports_cards_and_never_prints_page_content(tmp_path, capsys):
    cfg = config(tmp_path)
    page = Page([Card(), Card()])
    code = profi_auth.status(cfg, playwright_factory=factory(Chromium(Browser(Context([page])))))
    out = capsys.readouterr().out
    assert code == 0 and "cards found on the page: 2" in out and "Нужен ремонт" not in out


@pytest.mark.parametrize(
    "page,fragment", [(Page(markers=set()), "cabinet marker"), (Page(status=429), "Blocked")]
)
def test_cli_status_not_ready_cases(tmp_path, capsys, page, fragment):
    code = profi_auth.status(config(tmp_path), playwright_factory=factory(Chromium(Browser(Context([page])))))
    assert code == 2 and fragment in capsys.readouterr().out


def test_cli_inspect_writes_only_a_masked_skeleton(tmp_path, capsys):
    out = tmp_path / "skeleton.html"
    code = profi_auth.inspect(
        config(tmp_path), None, out, playwright_factory=factory(Chromium(Browser(Context([Page()]))))
    )
    text = out.read_text(encoding="utf-8")
    assert (
        code == 0
        and 'class="card"' in text
        and "Иван" not in text
        and "+7" not in text
        and "123-45" not in text
    )


def test_cli_without_a_page_to_open(tmp_path, capsys):
    cfg = config(tmp_path, urls=[])
    assert profi_auth.status(cfg, playwright_factory=factory(Chromium(Browser(Context([]))))) == 2
    assert "profi.urls" in capsys.readouterr().out


def test_scan_result_import_is_the_project_model():
    assert ScanResult("profi:x").complete and ProfiBrowser
