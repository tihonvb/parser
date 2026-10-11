"""Public-channel collector without an account (t.me/s/<channel>). No network: pages are rendered locally."""

import copy
import html as htmllib
from datetime import UTC

import pytest

from lead_parser.application.models import ScanResult
from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, validate_config
from lead_parser.infrastructure.integrations.telegram import public_web as web
from lead_parser.infrastructure.integrations.telegram.public_web import (
    UnsupportedChannel,
    channel_username,
    collect_leads,
    parse_page,
)


def default_config():
    return copy.deepcopy(DEFAULTS)


NOW = 1_800_000_000.0
PAGE = 20


def message_html(channel, number, text, stamp, *, reply=None):
    reply_block = (
        f'<a class="tgme_widget_message_reply" href="https://t.me/{channel}/1">'
        f'<div class="tgme_widget_message_text js-message_reply_text">{htmllib.escape(reply)}</div></a>'
        if reply
        else ""
    )
    body = (
        f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>'
        if text is not None
        else ""
    )
    return (
        f'<div class="tgme_widget_message_wrap js-widget_message_wrap">'
        f'<div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{channel}/{number}">'
        f'<div class="tgme_widget_message_bubble">{reply_block}{body}'
        f'<div class="tgme_widget_message_footer"><a class="tgme_widget_message_date" href="https://t.me/{channel}/{number}">'
        f'<time datetime="{stamp}" class="time">12:00</time></a></div></div></div></div>'
    )


def render(channel, messages, title="Тестовый канал"):
    """messages: [(id, text_html, iso_stamp)] in ascending order, as the preview shows them."""
    items = "".join(message_html(channel, i, t, s) for i, t, s in messages)
    return (
        f'<html><head><meta property="og:title" content="{title}"></head><body>'
        f'<div class="tgme_channel_history js-message_history">{items}</div></body></html>'
    )


def iso(seconds_ago):
    from datetime import datetime

    return datetime.fromtimestamp(NOW - seconds_ago, tz=UTC).isoformat()


class FakeResponse:
    def __init__(self, text="", status=200, url="https://t.me/s/chan", headers=None):
        self.text, self.status_code, self.url, self.headers = text, status, url, headers or {}


class FakeChannel:
    """A channel with ``total`` messages; newest id == total. Serves 20 per page, ?before=<id> paging."""

    def __init__(self, name, total, hits=None, age_step=60):
        self.name, self.total, self.hits, self.age_step = name, total, hits or {}, age_step
        self.requests: list[int | None] = []
        self.fail_before: dict[int | None, FakeResponse | Exception] = {}

    def text(self, number):
        return self.hits.get(number, f"болтовня номер {number}")

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        before = (params or {}).get("before")
        self.requests.append(before)
        planned = self.fail_before.get(before)
        if isinstance(planned, Exception):
            raise planned
        if planned is not None:
            return planned
        top = (before - 1) if before else self.total
        ids = list(range(max(1, top - PAGE + 1), top + 1))
        rows = [(i, htmllib.escape(self.text(i)), iso((self.total - i) * self.age_step + 30)) for i in ids]
        return FakeResponse(render(self.name, rows), url=f"https://t.me/s/{self.name}")


class Session:
    def __init__(self, *channels):
        self.channels = {c.name.lower(): c for c in channels}
        self.headers = {}

    def get(self, url, params=None, timeout=None, allow_redirects=True):
        name = url.rsplit("/", 1)[-1].lower()
        return self.channels[name].get(url, params, timeout, allow_redirects)

    def close(self):
        pass


class Store:
    def __init__(self):
        self.cursors, self.leads, self.events = {}, [], []

    def ingest(self, leads):
        self.events.append(("ingest", len(leads)))
        self.leads.extend(leads)
        return len(leads)

    def cursor(self, source_id):
        return dict(self.cursors.get(source_id, {}))

    def checkpoint(self, result):
        self.events.append(("checkpoint", result.complete))
        self.cursors[result.source_id] = dict(result.cursor)


def cfg(channels=("chan",), **web_override):
    config = default_config()
    config["general"]["keywords"] = ["ищу бригаду", "нужен ремонт"]
    config["telegram"].update(
        enabled=True,
        transport="public_web",
        channels=list(channels),
        lookback_hours=48,
        channel_timeout_seconds=600,
    )
    config["telegram"]["public_web"].update(web_override)
    return config


def run(config, *channels, store=None, reports=None):
    sleeps = []
    leads = collect_leads(
        config, store, reports, session=Session(*channels), sleep=sleeps.append, clock=lambda: NOW
    )
    return leads, sleeps


# ---------- references ----------


@pytest.mark.parametrize(
    "ref",
    [
        "durov",
        "@durov",
        "https://t.me/durov",
        "t.me/durov",
        "https://t.me/s/durov",
        "https://t.me/durov/123",
        "telegram.me/durov",
    ],
)
def test_reference_forms(ref):
    assert channel_username(ref) == "durov"


@pytest.mark.parametrize(
    "ref",
    ["https://t.me/+AbCdEf123", "https://t.me/joinchat/XYZ", "-1001234567890", "12345", "ab", "", "a b"],
)
def test_private_or_invalid_references_are_rejected_with_reason(ref):
    with pytest.raises(UnsupportedChannel):
        channel_username(ref)


# ---------- HTML parsing ----------


def test_parse_text_with_markup_breaks_and_emoji():
    html = render(
        "chan",
        [
            (
                5,
                'Привет <b>мир</b><br/>вторая&nbsp;строка <a href="x">ссылка</a><tg-emoji><i class="emoji"><b>🩵</b></i></tg-emoji>',
                iso(10),
            )
        ],
    )
    (message,) = parse_page(html).messages
    assert message.id == 5 and message.channel == "chan"
    assert message.text == "Привет мир\nвторая строка ссылка🩵"
    assert message.date.isoformat() == iso(10)


def test_reply_quote_is_not_the_message_body():
    items = message_html("chan", 7, "Ищу бригаду на ремонт", iso(5), reply="цитата другого поста")
    page = parse_page(f'<div class="tgme_channel_history">{items}</div>')
    assert page.messages[0].text == "Ищу бригаду на ремонт" and "цитата" not in page.messages[0].text


def test_media_only_message_has_empty_text_and_pages_are_newest_first():
    html = render("chan", [(1, "первое", iso(30)), (2, None, iso(20)), (3, "третье", iso(10))])
    page = parse_page(html)
    assert [m.id for m in page.messages] == [3, 2, 1]
    assert page.messages[1].text == "" and page.has_history and page.title == "Тестовый канал"


def test_nested_divs_in_text_do_not_cut_the_message():
    html = render("chan", [(1, 'до <div class="x">внутри</div> после', iso(5))])
    assert "внутри" in parse_page(html).messages[0].text and "после" in parse_page(html).messages[0].text


def test_garbage_html_does_not_crash():
    assert parse_page("<<<>>> <div").messages == []


# ---------- collection ----------


def test_leads_have_stable_ids_and_fields():
    channel = FakeChannel("Chan", 30, hits={25: "Ищу бригаду, телефон +7 999 123-45-67"})
    leads, _ = run(cfg(channels=["@Chan"]), channel)
    (lead,) = leads
    assert lead.source == "telegram" and lead.external_id == "chan_25"
    assert lead.source_group_id == "telegram:web:chan" and lead.source_group == "Тестовый канал"
    assert lead.url == "https://t.me/Chan/25" and lead.phone and lead.extra["transport"] == "public_web"
    assert lead.extra["source_ref"] == "@Chan"


def test_pagination_reaches_older_pages_and_pauses_between_requests():
    channel = FakeChannel("chan", 55, hits={3: "Нужен ремонт кухни"})
    leads, sleeps = run(cfg(), channel)
    assert [lead.external_id for lead in leads] == ["chan_3"]
    assert channel.requests == [None, 36, 16]  # three pages, each with ?before=<oldest id>
    assert sleeps and all(s == 1.0 for s in sleeps)


def test_lookback_window_stops_the_scan():
    channel = FakeChannel("chan", 500, age_step=3600)  # one message per hour: 48h window = ~48 messages
    store, reports = Store(), []
    run(cfg(max_pages_per_channel=50), channel, store=store, reports=reports)
    assert reports[0].complete and reports[0].scanned <= 50 and len(channel.requests) <= 4


def test_budget_exhaustion_is_incomplete_then_resumes_without_gaps():
    hits = {35: "Ищу бригаду первая", 5: "Нужен ремонт вторая"}
    channel = FakeChannel("chan", 40, hits=hits)
    store = Store()
    config = cfg(max_pages_per_channel=1)
    found, reports = set(), []
    for _ in range(5):
        reports.clear()
        leads, _ = run(config, channel, store=store, reports=reports)
        found |= {lead.external_id for lead in leads}
        if reports[0].complete:
            break
    assert found == {"chan_35", "chan_5"}
    assert store.cursors["telegram:web:chan"].get("high_watermark") == 40


def test_incomplete_run_reports_lag():
    channel = FakeChannel("chan", 100)
    reports: list[ScanResult] = []
    run(cfg(max_pages_per_channel=2), channel, store=Store(), reports=reports)
    assert reports[0].complete is False and reports[0].cursor["resume_max_id"] == 61


def test_steady_state_reads_only_the_newest_page():
    channel = FakeChannel("chan", 30)
    store = Store()
    run(cfg(), channel, store=store)
    channel.total = 33
    channel.hits = {33: "Ищу бригаду срочно"}
    channel.requests.clear()
    leads, _ = run(cfg(), channel, store=store)
    assert [lead.external_id for lead in leads] == ["chan_33"] and channel.requests == [None]


def test_each_page_is_stored_before_its_checkpoint():
    channel = FakeChannel("chan", 45, hits={40: "Ищу бригаду"})
    store = Store()
    run(cfg(), channel, store=store)
    events = store.events
    assert events.index(("ingest", 1)) < events.index(("checkpoint", False))


@pytest.mark.parametrize(
    "response,expected",
    [
        (FakeResponse(status=404), "Unavailable"),
        (
            FakeResponse("<html>no preview</html>", url="https://t.me/chan"),
            "Unavailable",
        ),  # redirected away from /s/
        (FakeResponse("<html><body>hi</body></html>"), "Unavailable"),
        (FakeResponse(status=429, headers={"Retry-After": "30"}), "RateLimited"),
        (FakeResponse(status=503), "HTTPError"),
    ],
)
def test_unavailable_limited_and_broken_pages_are_reported(response, expected):
    channel = FakeChannel("chan", 30)
    channel.fail_before[None] = response
    reports = []
    leads, _ = run(cfg(), channel, reports=reports)
    assert leads == [] and not reports[0].complete and expected in reports[0].errors[0]


def test_first_page_failure_keeps_old_cursor_and_lookback_cutoff():
    channel = FakeChannel("chan", 30)
    channel.fail_before[None] = FakeResponse(status=503)
    store, reports = Store(), []
    run(cfg(), channel, store=store, reports=reports)
    assert store.cursors["telegram:web:chan"] == {}  # next run still applies the lookback cutoff


def test_rate_limit_midway_keeps_what_was_read_and_resumes():
    channel = FakeChannel("chan", 60, hits={50: "Ищу бригаду раз", 5: "Нужен ремонт два"})
    channel.fail_before[41] = FakeResponse(status=429, headers={"Retry-After": "60"})
    store, reports = Store(), []
    leads, _ = run(cfg(), channel, store=store, reports=reports)
    assert [lead.external_id for lead in leads] == ["chan_50"] and not reports[0].complete
    assert store.cursors["telegram:web:chan"]["resume_max_id"] == 41
    del channel.fail_before[41]
    leads2, _ = run(cfg(), channel, store=store, reports=[])
    assert [lead.external_id for lead in leads2] == ["chan_5"]


def test_network_error_is_isolated_per_channel():
    bad, good = FakeChannel("bad", 30), FakeChannel("good", 30, hits={28: "Ищу бригаду"})
    bad.fail_before[None] = TimeoutError("slow")
    reports = []
    leads, _ = run(cfg(channels=["bad", "good"]), bad, good, reports=reports)
    assert [lead.external_id for lead in leads] == ["good_28"]
    assert [r.complete for r in reports] == [False, True]


def test_unsupported_refs_reported_not_fatal():
    good = FakeChannel("good", 25, hits={24: "Ищу бригаду"})
    reports = []
    leads, _ = run(cfg(channels=["https://t.me/+secretinvite", "good"]), good, reports=reports)
    assert len(leads) == 1 and "PublicWebUnsupported" in reports[0].errors[0]


def test_stalled_paging_does_not_loop_forever():
    class Stuck(FakeChannel):
        def get(self, url, params=None, *args, **kw):
            self.requests.append((params or {}).get("before"))
            rows = [(i, "текст", iso(1000 - i)) for i in range(20, 40)]  # the same ids whatever ?before says
            return FakeResponse(render(self.name, rows), url=f"https://t.me/s/{self.name}")

    channel = Stuck("chan", 100)
    reports = []
    run(cfg(max_pages_per_channel=50), channel, reports=reports)
    assert "PagingStalled" in reports[0].errors[0] and len(channel.requests) <= 3


def test_disabled_telegram_does_nothing():
    config = cfg()
    config["telegram"]["enabled"] = False
    assert run(config, FakeChannel("chan", 10))[0] == []


# ---------- configuration and wiring ----------


def valid_config(**telegram):
    config = default_config()
    config["general"]["keywords"] = ["ищу бригаду"]
    config["telegram"].update(telegram)
    return config


def test_public_web_needs_no_api_keys():
    validate_config(
        valid_config(enabled=True, transport="public_web", channels=["durov"]), require_access=True
    )


def test_account_transport_still_requires_keys():
    with pytest.raises(ConfigError, match="api_id"):
        validate_config(
            valid_config(enabled=True, transport="account", channels=["durov"]), require_access=True
        )


@pytest.mark.parametrize("value", ["bot", "", None, 1])
def test_unknown_transport_rejected(value):
    with pytest.raises(ConfigError):
        validate_config(valid_config(transport=value), require_access=False)


@pytest.mark.parametrize(
    "key,value",
    [
        ("max_pages_per_channel", 0),
        ("max_pages_per_channel", 501),
        ("max_pages_per_channel", 1.5),
        ("request_delay_seconds", 0),
        ("request_delay_seconds", float("nan")),
        ("request_timeout_seconds", -1),
        ("request_timeout_seconds", True),
    ],
)
def test_public_web_numbers_validated(key, value):
    config = valid_config()
    config["telegram"]["public_web"][key] = value
    with pytest.raises(ConfigError):
        validate_config(config, require_access=False)


def test_bootstrap_selects_collector_by_transport():
    from lead_parser import bootstrap

    for transport, module in (("public_web", web), ("account", bootstrap.telegram)):
        config = valid_config(enabled=True, transport=transport, channels=["durov"])
        sources = bootstrap.build_sources(config)
        (telegram_source,) = [s for s in sources if s.name == "telegram"]
        assert telegram_source.collector == module.collect_leads
