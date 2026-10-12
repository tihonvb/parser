"""Connector contract, credential registry and the `lead-parser connectors` command."""

import copy
import json
import os
import re
import stat
from pathlib import Path

import pytest

from lead_parser import bootstrap
from lead_parser.application.connectors import (
    ConnectorSpec,
    Credential,
    blocked,
    dig,
    is_filled,
    report,
)
from lead_parser.infrastructure import credentials
from lead_parser.infrastructure.configuration import DEFAULTS, ENV_MAP, load_config
from lead_parser.infrastructure.credentials import SPECS, file_exists
from lead_parser.interfaces.cli import connectors as cli

ROOT = Path(__file__).resolve().parent.parent


def cfg(**sections):
    config = copy.deepcopy(DEFAULTS)
    for path, value in sections.items():
        node = config
        *parents, last = path.split("__")
        for key in parents:
            node = node[key]
        node[last] = value
    return config


def status_of(name, config):
    return next(s for s in report(config, SPECS, file_exists) if s.name == name)


# ---------------------------------------------------------------- contract


def test_is_filled_rejects_empty_and_placeholders():
    for bad in ("", "   ", None, True, [], "PUT_YOUR_KEY", "ВСТАВЬТЕ_ТОКЕН", "ТВОЙ_ТОКЕН"):
        assert not is_filled(bad)
    assert is_filled("abc") and is_filled(123456)


def test_dig_walks_dotted_paths_and_tolerates_missing_parts():
    assert dig({"a": {"b": 1}}, "a.b") == 1
    assert dig({"a": {"b": 1}}, "a.c") is None and dig({"a": 5}, "a.b") is None and dig({}, "x") is None


def test_spec_validates_kind_and_duplicate_paths():
    with pytest.raises(ValueError, match="kind"):
        ConnectorSpec("x", "X", "oracle", "x")
    with pytest.raises(ValueError, match="duplicate"):
        ConnectorSpec("x", "X", "source", "x", (Credential("x.a", "d"), Credential("x.a", "d")))


def test_file_credential_needs_an_existing_file_with_suffix(tmp_path):
    session = tmp_path / "telegram"
    credential = Credential("t.session", "d", kind="file", suffix=".session")
    config = {"t": {"session": str(session)}}
    assert not credential.present(config, file_exists)
    (tmp_path / "telegram.session").write_text("x")
    assert credential.present(config, file_exists)
    assert credential.present(
        {"t": {"session": str(tmp_path / "other")}}
    )  # without an injected check: filled is enough


def test_credential_applies_predicate_controls_what_is_required():
    spec = ConnectorSpec(
        "x", "X", "source", "x", (Credential("x.k", "d", applies=lambda c: c["x"]["mode"] == "strict"),)
    )
    assert spec.status({"x": {"enabled": True, "mode": "strict", "k": ""}}).missing
    assert not spec.status({"x": {"enabled": True, "mode": "lax", "k": ""}}).missing


# ---------------------------------------------------------------- registry


def test_every_source_has_a_module_and_a_declaration():
    declared = {s.name for s in SPECS if s.kind == "source"}
    assert declared == set(bootstrap.SOURCE_MODULES)
    for module in bootstrap.SOURCE_MODULES.values():
        assert callable(module.collect_leads)


def test_every_declared_section_exists_in_the_defaults():
    for spec in SPECS:
        assert isinstance(dig(DEFAULTS, spec.section), dict), spec.name
        assert "enabled" in dig(DEFAULTS, spec.section), spec.name
        for credential in spec.credentials:
            assert dig(DEFAULTS, credential.path) is not None, credential.path


def test_env_map_is_derived_from_declarations_with_no_collisions():
    expected = {c.env: c.keys for spec in SPECS for c in spec.credentials if c.env}
    assert ENV_MAP == expected == credentials.ENV_MAP
    assert len(ENV_MAP) == len({c.env for s in SPECS for c in s.credentials if c.env})
    assert {"TELEGRAM_API_ID", "TELEGRAM_API_HASH", "VK_ACCESS_TOKEN", "OPENROUTER_API_KEY"} <= set(ENV_MAP)


def test_names_are_unique_and_kinds_valid():
    assert len({s.name for s in SPECS}) == len(SPECS)
    assert {s.kind for s in SPECS} == {"source", "classifier", "delivery"}


def test_env_example_lists_exactly_the_declared_variables():
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    listed = set(re.findall(r"^([A-Z][A-Z0-9_]+)=", text, flags=re.M))
    assert listed == set(ENV_MAP), listed ^ set(ENV_MAP)


# ---------------------------------------------------------------- status logic


def test_disabled_connectors_are_not_blocked_even_without_credentials():
    statuses = report(cfg(), SPECS, file_exists)
    assert not any(s.enabled for s in statuses) and blocked(statuses) == []


def test_enabled_connector_without_credentials_is_blocked_with_names_only():
    config = cfg(**{"telegram__enabled": True})
    status = status_of("telegram", config)
    assert status.enabled and not status.ready
    assert {c.path for c in status.missing} == {
        "telegram.api_id",
        "telegram.api_hash",
        "telegram.session_name",
    }
    assert [s.name for s in blocked(report(config, SPECS, file_exists))] == ["telegram"]


def test_vk_credentials_depend_on_the_token_mode(tmp_path):
    static = status_of("vk", cfg(**{"vk__enabled": True}))
    assert [c.path for c in static.missing] == ["vk.access_token"]
    vk_id = status_of("vk", cfg(**{"vk__enabled": True, "vk__token_mode": "vk_id"}))
    assert [c.path for c in vk_id.missing] == ["vk.token_file"]
    ready = status_of("vk", cfg(**{"vk__enabled": True, "vk__access_token": "real-token-1"}))
    assert ready.ready and [c.path for c in ready.optional] == []


def test_optional_credential_is_listed_only_when_it_applies():
    config = cfg(**{"vk__enabled": True, "vk__access_token": "t", "vk__use_global_newsfeed_search": True})
    assert [c.path for c in status_of("vk", config).optional] == ["vk.search_token"]


def test_avito_needs_no_credentials():
    status = status_of("avito", cfg(**{"avito__enabled": True}))
    assert status.ready and not status.missing


def test_bot_recipient_is_satisfied_by_either_chat_id_or_chat_ids():
    base = {
        "notifications__telegram": {
            **DEFAULTS["notifications"]["telegram"],
            "enabled": True,
            "bot_token": "123:abc",
        }
    }
    assert [c.path for c in status_of("telegram_bot", cfg(**base)).missing] == [
        "notifications.telegram.chat_id"
    ]
    with_list = {"notifications__telegram": {**base["notifications__telegram"], "chat_ids": ["1", "2"]}}
    assert status_of("telegram_bot", cfg(**with_list)).ready


def test_sheets_requires_file_and_id(tmp_path):
    key = tmp_path / "service_account.json"
    config = cfg(
        **{
            "output__enabled": True,
            "output__google_sheets": {"credentials_file": str(key), "spreadsheet_id": ""},
        }
    )
    assert {c.path for c in status_of("google_sheets", config).missing} == {
        "output.google_sheets.credentials_file",
        "output.google_sheets.spreadsheet_id",
    }
    key.write_text("{}")
    config["output"]["google_sheets"]["spreadsheet_id"] = "abc"
    assert status_of("google_sheets", config).ready


def test_placeholders_do_not_count_as_credentials():
    assert status_of(
        "ai_filter", cfg(**{"ai_filter__enabled": True, "ai_filter__openrouter_api_key": "PUT_YOUR_KEY"})
    ).missing


# ---------------------------------------------------------------- loading from the environment


def write_config(tmp_path, body="general: {keywords: [нужен ремонт]}\n"):
    path = tmp_path / "config.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_credentials_load_from_dotenv_and_process_env_with_priority(tmp_path, monkeypatch):
    path = write_config(tmp_path)
    (tmp_path / ".env").write_text(
        "TELEGRAM_API_ID=111\nTELEGRAM_API_HASH=fromdotenv\nOPENROUTER_API_KEY=dotenvkey\n"
    )
    monkeypatch.setenv("TELEGRAM_API_HASH", "fromprocess")
    loaded = load_config(path, require_access=False)
    assert loaded["telegram"]["api_id"] == "111" and loaded["ai_filter"]["openrouter_api_key"] == "dotenvkey"
    assert loaded["telegram"]["api_hash"] == "fromprocess"  # process beats .env


def test_loading_credentials_later_turns_a_blocked_connector_ready(tmp_path):
    path = write_config(tmp_path, "general: {keywords: [нужен ремонт]}\nai_filter: {enabled: true}\n")
    before = status_of("ai_filter", load_config(path, require_access=False))
    assert before.missing
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=sk-real-key-123\n")
    after = status_of("ai_filter", load_config(path, require_access=False))
    assert after.ready


# ---------------------------------------------------------------- CLI


def test_status_prints_names_never_values(tmp_path, capsys, monkeypatch):
    path = write_config(
        tmp_path,
        "general: {keywords: [нужен ремонт]}\ntelegram: {enabled: true, api_hash: SUPERSECRETHASH123}\n",
    )
    assert cli.main(["status", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "telegram" in out and "TELEGRAM_API_ID" in out and "НЕТ КРЕДОВ" in out
    assert "SUPERSECRETHASH123" not in out


def test_status_json_is_structured_and_secret_free(tmp_path, capsys):
    path = write_config(
        tmp_path, "general: {keywords: [нужен ремонт]}\nvk: {enabled: true, access_token: REALTOKENVALUE99}\n"
    )
    assert cli.main(["status", "--json", "--config", str(path)]) == 0
    raw = capsys.readouterr().out
    data = json.loads(raw)
    vk = next(item for item in data if item["name"] == "vk")
    assert vk["enabled"] and vk["ready"] and vk["missing"] == []
    assert "REALTOKENVALUE99" not in raw


def test_check_exit_codes(tmp_path, capsys):
    ok = write_config(tmp_path)
    assert cli.main(["check", "--config", str(ok)]) == 0
    bad = tmp_path / "bad.yaml"
    bad.write_text("general: {keywords: [нужен ремонт]}\ntelegram: {enabled: true}\n", encoding="utf-8")
    assert cli.main(["check", "--config", str(bad)]) == 2
    assert "telegram: не хватает" in capsys.readouterr().out


def test_default_command_is_status(tmp_path, capsys):
    assert cli.main(["--config", str(write_config(tmp_path))]) == 0
    assert "[source" in capsys.readouterr().out


def test_env_template_lists_every_variable_with_empty_values(capsys):
    assert cli.main(["env-template"]) == 0
    out = capsys.readouterr().out
    for name in ENV_MAP:
        assert re.search(rf"^{name}=$", out, flags=re.M), name
    assert not re.search(r"^[A-Z_]+=\S", out, flags=re.M)  # never a value
    assert "необязательно" in out


def test_env_template_file_is_owner_only_and_never_overwritten(tmp_path, capsys):
    target = tmp_path / ".env"
    assert cli.main(["env-template", "--output", str(target)]) == 0
    if os.name == "posix":
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
    target.write_text("KEEP=me\n")
    assert cli.main(["env-template", "--output", str(target)]) == 1
    assert target.read_text() == "KEEP=me\n"


def test_command_is_reachable_through_the_entrypoint():
    from lead_parser.interfaces.cli.entrypoint import COMMANDS

    assert COMMANDS["connectors"] == "connectors"


def test_documentation_lists_every_declared_credential():
    text = (ROOT / "docs" / "connectors.md").read_text(encoding="utf-8")
    for spec in SPECS:
        assert f"`{spec.name}`" in text, spec.name
        for credential in spec.credentials:
            assert f"`{credential.path}`" in text, credential.path
            if credential.env:
                assert f"`{credential.env}`" in text, credential.env
