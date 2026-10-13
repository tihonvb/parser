"""profi.ru e-mail connector. No network and no mailbox: IMAP is faked, mails are built locally."""

import copy
import imaplib
from datetime import UTC, datetime
from email.message import EmailMessage

import pytest

from lead_parser.infrastructure.configuration import DEFAULTS, ConfigError, validate_config
from lead_parser.infrastructure.integrations.profi_mail import collector as mail_collector
from lead_parser.infrastructure.integrations.profi_mail.collector import imap_date
from lead_parser.infrastructure.integrations.profi_mail.message import (
    domain_allowed,
    fallback_id,
    lead_text,
    order_link,
    parse_mail,
    shape,
)
from lead_parser.interfaces.cli import profi_mail as cli

NOW = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
LINK_RE = DEFAULTS["profi_mail"]["link_pattern"]
ID_RE = DEFAULTS["profi_mail"]["order_id_pattern"]


def build(
    subject="Новый заказ: ремонт квартиры",
    body="Здравствуйте!\nКлиент ищет бригаду: нужен ремонт квартиры под ключ.\nБюджет 300 000 ₽\n"
    "https://profi.ru/backoffice/order/456789/?utm=mail\nС уважением, Профи.ру",
    sender="Профи.ру <noreply@mail.profi.ru>",
    html=None,
    message_id="<m1@profi.ru>",
    auth=None,
    date="Sat, 10 Oct 2026 09:30:00 +0300",
) -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"], msg["Date"], msg["Message-ID"] = (
        sender,
        "me@example.com",
        subject,
        date,
        message_id,
    )
    if auth:
        msg["Authentication-Results"] = auth
    msg.set_content(body)
    if html:
        msg.add_alternative(html, subtype="html")
    return msg.as_bytes()


# ---------------------------------------------------------------- message parsing


def test_plain_mail_fields():
    mail = parse_mail(build())
    assert mail.sender_domain == "mail.profi.ru" and mail.subject.startswith("Новый заказ")
    assert mail.date.startswith("2026-10-10T09:30:00") and mail.message_id == "<m1@profi.ru>"
    assert order_link(mail.links, LINK_RE, ID_RE) == ("456789", "https://profi.ru/backoffice/order/456789/")


def test_html_only_mail_is_converted_and_links_are_taken_from_hrefs():
    html = (
        "<html><head><style>.a{color:red}</style><title>T</title></head><body><p>Новый <b>заказ</b></p>"
        "<div>Нужен ремонт кухни</div><a href='https://profi.ru/o/778899?x=1'>Открыть заказ</a>"
        "<script>alert(1)</script></body></html>"
    )
    msg = EmailMessage()
    msg["From"], msg["Subject"] = "noreply@profi.ru", "Заказ"
    msg.set_content(html, subtype="html")
    mail = parse_mail(msg.as_bytes())
    assert "Нужен ремонт кухни" in mail.text and "alert" not in mail.text and "color" not in mail.text
    assert order_link(mail.links, LINK_RE, ID_RE)[0] == "778899"


@pytest.mark.parametrize(
    "domain,allowed,expected",
    [
        ("profi.ru", ["profi.ru"], True),
        ("mail.profi.ru", ["profi.ru"], True),
        ("PROFI.RU.", ["profi.ru"], True),
        ("profi.ru.evil.com", ["profi.ru"], False),
        ("evilprofi.ru", ["profi.ru"], False),
        ("", ["profi.ru"], False),
    ],
)
def test_sender_domain_matching_is_strict(domain, allowed, expected):
    assert domain_allowed(domain, allowed) is expected


@pytest.mark.parametrize(
    "header,failed",
    [
        ("mx.example; dkim=pass header.d=profi.ru; spf=pass", False),
        ("mx.example; dkim=fail header.d=profi.ru", True),
        ("mx.example; spf=softfail", True),
        ("mx.example; dmarc=fail", True),
        (None, False),
    ],
)
def test_failed_authentication_is_detected(header, failed):
    assert parse_mail(build(auth=header)).authentication_failed is failed


@pytest.mark.parametrize(
    "links,expected",
    [
        (["https://profi.ru/order/12345"], ("12345", "https://profi.ru/order/12345")),
        (["https://click.evil.com/profi.ru/order/12345"], ("", "")),
        (["http://profi.ru/order/12345"], ("", "")),
        (
            ["https://profi.ru/about", "https://www.profi.ru/o/99999?a=1#b"],
            ("99999", "https://www.profi.ru/o/99999"),
        ),
        (["https://profi.ru.evil.com/order/12345"], ("", "")),
        ([], ("", "")),
    ],
)
def test_order_link_selection(links, expected):
    assert order_link(links, LINK_RE, ID_RE) == expected


def test_lead_text_trims_with_markers_and_drops_urls():
    mail = parse_mail(
        build(body="Приветствие\nЗАКАЗ:\nНужен ремонт под ключ https://profi.ru/o/12345\nОтписаться\nподвал")
    )
    assert lead_text(mail, r"ЗАКАЗ:", r"Отписаться") == "Новый заказ: ремонт квартиры\nНужен ремонт под ключ"
    assert "https" not in lead_text(mail, "", "") and "Приветствие" in lead_text(mail, "", "")
    assert lead_text(mail, "нет такого", "нет такого").startswith(
        "Новый заказ"
    )  # unmatched markers change nothing


def test_fallback_id_is_stable_and_distinguishes_messages():
    a, b = parse_mail(build(message_id="<a@x>")), parse_mail(build(message_id="<b@x>"))
    assert fallback_id(a) == fallback_id(parse_mail(build(message_id="<a@x>"))) != fallback_id(b)


def test_shape_hides_content():
    assert shape("Иван +7 999 12-34 profi.ru/o/456789") == "xxxx +9 999 99-99 xxxxx.xx/x/999999"


def test_imap_date_is_locale_independent():
    assert imap_date(NOW) == "11-Oct-2026"


# ---------------------------------------------------------------- fake IMAP


class FakeIMAP:
    """Mailbox: {uid: raw bytes}. Records every command to prove read-only access."""

    instances: list = []

    def __init__(
        self,
        host="h",
        port=993,
        timeout=None,
        *,
        mails=None,
        uidvalidity=7,
        login_error=None,
        fetch_errors=(),
    ):
        self.mails, self.uidvalidity = dict(mails or {}), uidvalidity
        self.login_error, self.fetch_errors = login_error, set(fetch_errors)
        self.log, self.closed, self.logged_out, self.readonly = [], False, False, None
        FakeIMAP.instances.append(self)

    def login(self, user, password):
        self.log.append("LOGIN")
        if self.login_error:
            raise imaplib.IMAP4.error(self.login_error)
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        self.readonly = readonly
        self.log.append(f"SELECT {folder}")
        self._untagged = {"UIDVALIDITY": [str(self.uidvalidity).encode()]}
        return ("OK" if folder != "NOPE" else "NO"), [b"1"]

    def response(self, code):
        """Like imaplib: the returned value is CLEARED, so a second read gives [None]."""
        return code, self._untagged.pop(code, [None])

    def uid(self, command, *args):
        self.log.append(f"UID {command} {args}")
        if command == "SEARCH":
            if "UID" in args:
                start = int(args[args.index("UID") + 1].split(":")[0])
                uids = [u for u in self.mails if u >= start] or [max(self.mails, default=0)]  # ``n:*`` quirk
            else:
                uids = list(self.mails)
            return "OK", [" ".join(map(str, sorted(uids))).encode()]
        uid = int(args[0])
        if uid in self.fetch_errors:
            return "NO", [None]
        raw = self.mails[uid]
        if "HEADER.FIELDS" in args[1]:
            raw = raw.split(b"\n\n", 1)[0] + b"\n\n" if b"\n\n" in raw else raw
        return "OK", [(b"%d (UID %d BODY[] {%d}" % (uid, uid, len(raw)), raw), b")"]

    def close(self):
        self.closed = True

    def logout(self):
        self.logged_out = True


class Store:
    def __init__(self):
        self.leads, self.cursors, self.events, self.reports = [], {}, [], []

    def ingest(self, leads):
        self.events.append(("ingest", len(leads)))
        self.leads.extend(leads)
        return len(leads)

    def cursor(self, source_id):
        return dict(self.cursors.get(source_id, {}))

    def checkpoint(self, result):
        self.events.append("checkpoint")
        self.cursors[result.source_id] = dict(result.cursor)
        self.reports.append(result)


def config(**override):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"].update(keywords=["нужен ремонт", "ищу бригаду"], city="Самара")
    cfg["profi_mail"].update(
        enabled=True, host="imap.example.com", username="me@example.com", password="app-pass-1234"
    )
    cfg["profi_mail"].update(override)
    return cfg


def run(cfg, mails=None, store=None, reports=None, **imap_options):
    imap = FakeIMAP(mails=mails, **imap_options)
    leads = mail_collector.collect_leads(
        cfg, store, reports, imap_factory=lambda *a, **k: imap, clock=lambda: NOW
    )
    return leads, imap


def mail(n, **kw):
    kw.setdefault("message_id", f"<m{n}@profi.ru>")
    kw.setdefault("body", f"Клиент: нужен ремонт квартиры.\nhttps://profi.ru/order/{100000 + n}")
    return build(**kw)


# ---------------------------------------------------------------- collection


def test_leads_from_notification_mails(tmp_path):
    reports = []
    leads, imap = run(
        config(), {1: mail(1), 2: mail(2, subject="Новости сервиса", body="Скидка на тариф")}, reports=reports
    )
    (lead,) = leads
    assert (lead.source, lead.external_id) == ("profi", "100001")
    assert lead.url == "https://profi.ru/order/100001" and lead.source_group_id == "profi:mail"
    assert (
        lead.date.startswith("2026-10-10")
        and "нужен ремонт" in lead.text.lower()
        and "https" not in lead.text
    )
    assert lead.extra == {"known_city": "Самара", "transport": "email", "message_uid": 1}
    assert reports[0].scanned == 2 and reports[0].candidates == 1 and reports[0].complete


def test_connector_is_read_only():
    _, imap = run(config(), {1: mail(1)})
    assert imap.readonly is True
    commands = " ".join(imap.log)
    assert "BODY.PEEK" in commands and "STORE" not in commands and "EXPUNGE" not in commands
    assert "(BODY[])" not in commands and imap.closed and imap.logged_out


def test_other_senders_and_forged_mail_are_rejected_and_counted():
    mails = {
        1: mail(1, sender="Мошенник <promo@profi.ru.evil.com>"),
        2: mail(2, auth="mx; dkim=fail header.d=profi.ru"),
        3: mail(3, sender="someone@gmail.com"),
        4: mail(4),
    }
    reports = []
    leads, _ = run(config(), mails, reports=reports)
    assert [lead.external_id for lead in leads] == ["100004"]
    assert reports[0].prefilter["other_sender"] == 2 and reports[0].prefilter["failed_authentication"] == 1


def test_subject_filter_is_optional_and_applied():
    mails = {1: mail(1), 2: mail(2, subject="Реклама тарифа")}
    leads, _ = run(config(subject_regex="заказ"), mails)
    assert [lead.external_id for lead in leads] == ["100001"]


def test_mail_without_order_link_gets_a_stable_fallback_id():
    leads, _ = run(config(), {1: mail(1, body="Нужен ремонт квартиры, ссылки нет")})
    assert leads[0].external_id.startswith("mail-") and leads[0].url == ""
    again, _ = run(config(), {1: mail(1, body="Нужен ремонт квартиры, ссылки нет")})
    assert again[0].external_id == leads[0].external_id


def test_cursor_makes_next_run_read_only_new_mail_and_never_rereads():
    store = Store()
    first, _ = run(config(), {1: mail(1), 2: mail(2)}, store=store)
    assert len(first) == 2 and store.cursors["profi:mail:INBOX"] == {"uidvalidity": 7, "last_uid": 2}
    second, imap = run(config(), {1: mail(1), 2: mail(2), 3: mail(3)}, store=store)
    assert [lead.external_id for lead in second] == ["100003"]
    fetched = [entry for entry in imap.log if "FETCH" in entry]
    assert len(fetched) == 1 and "'3'" in fetched[0]  # old mails are not fetched again
    nothing, imap = run(config(), {1: mail(1), 2: mail(2), 3: mail(3)}, store=store)
    assert nothing == [] and not [entry for entry in imap.log if "FETCH" in entry]


def test_leads_are_stored_before_the_cursor_moves():
    store = Store()
    run(config(), {1: mail(1)}, store=store)
    assert store.events.index(("ingest", 1)) < store.events.index("checkpoint")


def test_uidvalidity_is_read_once_per_select_like_real_imaplib():
    store = Store()
    run(config(), {1: mail(1)}, store=store)
    assert (
        store.cursors["profi:mail:INBOX"]["uidvalidity"] == 7
    )  # not 0: the second read would have returned 0
    _, imap = run(config(), {1: mail(1), 2: mail(2)}, store=store)
    assert not any(
        "SINCE" in entry for entry in imap.log
    )  # the cursor was honoured, the window was not re-read


def test_uidvalidity_change_resets_the_cursor_to_the_lookback_window():
    store = Store()
    run(config(), {1: mail(1), 2: mail(2)}, store=store)
    leads, imap = run(config(), {1: mail(11), 2: mail(12)}, store=store, uidvalidity=99)
    assert len(leads) == 2 and any("SINCE" in entry for entry in imap.log)
    assert store.cursors["profi:mail:INBOX"]["uidvalidity"] == 99


def test_budget_exhaustion_is_reported_and_resumed():
    store, reports = Store(), []
    mails = {n: mail(n) for n in range(1, 6)}
    found = set()
    for _ in range(4):
        reports.clear()
        leads, _ = run(config(max_messages_per_run=2), mails, store=store, reports=reports)
        found |= {lead.external_id for lead in leads}
        if reports[0].complete:
            break
    assert found == {str(100000 + n) for n in range(1, 6)}


def test_incomplete_run_is_visible():
    reports = []
    run(config(max_messages_per_run=1), {1: mail(1), 2: mail(2)}, reports=reports)
    assert not reports[0].complete and "BudgetExhausted" in reports[0].errors[0]


def test_one_bad_message_does_not_stop_the_batch_or_block_progress():
    store, reports = Store(), []
    leads, _ = run(
        config(), {1: mail(1), 2: mail(2), 3: mail(3)}, store=store, reports=reports, fetch_errors={2}
    )
    assert [lead.external_id for lead in leads] == ["100001", "100003"]
    assert not reports[0].complete and "uid=2" in reports[0].errors[0]
    assert store.cursors["profi:mail:INBOX"]["last_uid"] == 3


def test_login_failure_is_reported_without_secrets_and_keeps_the_cursor():
    store = Store()
    store.cursors["profi:mail:INBOX"] = {"uidvalidity": 7, "last_uid": 41}
    reports = []
    leads, imap = run(
        config(), {1: mail(1)}, store=store, reports=reports, login_error="[AUTHENTICATIONFAILED] bad"
    )
    assert leads == [] and not reports[0].complete
    assert "AuthFailed" in reports[0].errors[0] and "app-pass-1234" not in " ".join(reports[0].errors)
    assert store.cursors["profi:mail:INBOX"] == {"uidvalidity": 7, "last_uid": 41}  # progress is not reset
    assert not any("SELECT" in entry for entry in imap.log)


def test_missing_folder_and_network_errors_are_reported():
    reports = []
    run(config(folder="NOPE"), {1: mail(1)}, reports=reports)
    assert "FolderNotFound" in reports[0].errors[0]

    def broken(*args, **kwargs):
        raise TimeoutError("timed out")

    reports = []
    leads = mail_collector.collect_leads(config(), None, reports, imap_factory=broken, clock=lambda: NOW)
    assert leads == [] and reports[0].errors == ["TimeoutError"]


def test_disabled_does_nothing():
    leads, imap = run(config(enabled=False), {1: mail(1)})
    assert leads == [] and imap.log == []


def test_connection_uses_tls_factory_by_default():
    assert mail_collector.imaplib.IMAP4_SSL is imaplib.IMAP4_SSL


# ---------------------------------------------------------------- configuration and wiring


def base(**override):
    cfg = copy.deepcopy(DEFAULTS)
    cfg["general"]["keywords"] = ["нужен ремонт"]
    cfg["profi_mail"].update(override)
    return cfg


def test_default_is_valid_and_disabled():
    validate_config(base(), require_access=False)
    assert DEFAULTS["profi_mail"]["enabled"] is False


def test_enabled_requires_credentials_but_checks_run_only_with_access():
    validate_config(base(enabled=True), require_access=False)
    for key in ("host", "username", "password"):
        with pytest.raises(ConfigError, match=key):
            validate_config(
                base(enabled=True, **{"host": "h", "username": "u", "password": "p", key: ""}),
                require_access=True,
            )
    validate_config(base(enabled=True, host="h", username="u", password="p"), require_access=True)


@pytest.mark.parametrize(
    "override",
    [
        {"sender_domains": ["not a domain"]},
        {"sender_domains": "profi.ru"},
        {"link_pattern": "("},
        {"order_id_pattern": r"\d+"},
        {"order_id_pattern": r"(\d)(\d)"},
        {"subject_regex": "("},
        {"body_start_regex": "["},
        {"port": 0},
        {"port": 70000},
        {"lookback_days": 0},
        {"lookback_days": 100},
        {"max_messages_per_run": 0},
        {"max_messages_per_run": 10**6},
        {"request_timeout_seconds": -1},
    ],
)
def test_invalid_settings_are_rejected(override):
    with pytest.raises(ConfigError):
        validate_config(base(**override), require_access=False)


def test_password_comes_from_the_environment(tmp_path, monkeypatch):
    import yaml

    from lead_parser.infrastructure.configuration import load_config

    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"general": {"keywords": ["нужен ремонт"]}}), encoding="utf-8")
    monkeypatch.setenv("PROFI_MAIL_PASSWORD", "from-env-secret")
    assert load_config(path, require_access=False)["profi_mail"]["password"] == "from-env-secret"


def test_bootstrap_registers_the_source_only_when_enabled():
    from lead_parser import bootstrap

    assert "profi_mail" not in [s.name for s in bootstrap.build_sources(base())]
    sources = bootstrap.build_sources(base(enabled=True, host="h", username="u", password="p"))
    (source,) = [s for s in sources if s.name == "profi_mail"]
    assert source.collector == mail_collector.collect_leads


# ---------------------------------------------------------------- CLI


def test_cli_status_counts_profi_mail_without_printing_content(capsys):
    imap = FakeIMAP(mails={1: mail(1), 2: mail(2, sender="x@gmail.com")})
    code = cli.status(config(), imap_factory=lambda *a, **k: imap)
    out = capsys.readouterr().out
    assert (
        code == 0 and "Mailbox OK" in out and ": 1." in out and "ремонт" not in out and "app-pass" not in out
    )
    assert imap.readonly is True and imap.logged_out


def test_cli_status_reports_problems(capsys):
    code = cli.status(config(), imap_factory=lambda *a, **k: FakeIMAP(login_error="no"))
    assert code == 2 and "AuthFailed" in capsys.readouterr().out
    code = cli.status(config(password=""), imap_factory=lambda *a, **k: FakeIMAP())
    assert code == 2 and "NotConfigured" in capsys.readouterr().out


def test_cli_inspect_writes_masked_structure_and_private_raw_copy(tmp_path, capsys):
    out, raw = tmp_path / "skeleton.txt", tmp_path / "raw.txt"
    imap = FakeIMAP(mails={1: mail(1)})
    code = cli.inspect(config(), 1, out, raw, imap_factory=lambda *a, **k: imap)
    masked, private = out.read_text(encoding="utf-8"), raw.read_text(encoding="utf-8")
    assert code == 0 and "order link found: True" in masked and "sender domain: mail.profi.ru" in masked
    for secret in ("Клиент", "ремонт", "300", "100001", "profi.ru/order"):
        assert secret not in masked
    assert "нужен ремонт" in private.lower()
    import os
    import stat

    if os.name == "posix":
        assert stat.S_IMODE(raw.stat().st_mode) == 0o600


def test_cli_inspect_without_matching_mail(tmp_path, capsys):
    code = cli.inspect(config(), 1, tmp_path / "s.txt", None, imap_factory=lambda *a, **k: FakeIMAP(mails={}))
    assert code == 2 and "nothing to inspect" in capsys.readouterr().out


# ---------------------------------------------------------------- "everything goes through" mode


def passthrough(**override):
    values = {"sender_domains": [], "keyword_filter": False}
    values.update(override)
    return config(**values)


def test_empty_sender_list_accepts_any_sender_but_still_rejects_failed_authentication():
    assert domain_allowed("anything.example", [])
    mails = {
        1: mail(1, sender="Кто-то <someone@gmail.com>", body="Просто письмо без ключевых слов"),
        2: mail(2, sender="forged@evil.com", auth="mx; dkim=fail", body="Просто письмо"),
    }
    reports = []
    leads, _ = run(passthrough(), mails, reports=reports)
    assert [lead.extra["message_uid"] for lead in leads] == [1]
    assert reports[0].prefilter["failed_authentication"] == 1


def test_keyword_filter_off_turns_every_accepted_mail_into_a_lead():
    mails = {1: mail(1, subject="Новый заказ", body="Текст без слов про ремонт https://profi.ru/order/55555")}
    assert run(config(), mails)[0] == []  # default: the keyword pre-filter drops it
    reports = []
    leads, _ = run(passthrough(), mails, reports=reports)
    assert [lead.external_id for lead in leads] == ["55555"]
    assert reports[0].prefilter == {"unfiltered": 1}


def test_unfiltered_mail_without_link_still_gets_a_stable_id():
    leads, _ = run(passthrough(), {1: mail(1, body="Без ссылки")})
    assert leads[0].external_id.startswith("mail-") and leads[0].url == ""


def test_subject_exclusion_removes_confirmations_and_newsletters():
    mails = {
        1: mail(1, subject="Подтвердите адрес электронной почты"),
        2: mail(2, subject="Новый заказ: ремонт"),
        3: mail(3, subject="Рассылка: скидки"),
    }
    reports = []
    leads, _ = run(passthrough(exclude_subject_regex="подтверд|рассылк"), mails, reports=reports)
    assert [lead.extra["message_uid"] for lead in leads] == [2]
    assert reports[0].prefilter["subject_excluded"] == 2


def test_default_behaviour_is_unchanged():
    assert DEFAULTS["profi_mail"]["keyword_filter"] is True
    assert DEFAULTS["profi_mail"]["sender_domains"] == ["profi.ru"]
    assert DEFAULTS["profi_mail"]["exclude_subject_regex"] == ""


def test_passthrough_settings_validate_but_bad_values_do_not():
    validate_config(
        base(sender_domains=[], keyword_filter=False, exclude_subject_regex="подтверд"), require_access=False
    )
    for bad in (
        {"keyword_filter": "no"},
        {"exclude_subject_regex": "("},
        {"sender_domains": ["not a domain"]},
        {"sender_domains": "x"},
    ):
        with pytest.raises(ConfigError):
            validate_config(base(**bad), require_access=False)


def test_cli_status_counts_all_mail_when_any_sender_is_allowed(capsys):
    imap = FakeIMAP(mails={1: mail(1), 2: mail(2, sender="x@gmail.com")})
    assert cli.status(passthrough(), imap_factory=lambda *a, **k: imap) == 0
    assert ": 2." in capsys.readouterr().out
