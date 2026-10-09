import copy
import socket

import pytest
import requests
import yaml

from lead_parser.infrastructure.configuration import DEFAULTS, load_config


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("Tests must never contact external services")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    from lead_parser.infrastructure.configuration import ENV_MAP

    for name in [*ENV_MAP, "VK_TOKEN"]:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(copy.deepcopy(DEFAULTS), allow_unicode=True), encoding="utf-8")
    return load_config(path)


@pytest.fixture
def lead():
    from lead_parser.core.models import Lead

    return Lead(
        source="vk",
        external_id="-12_9",
        date="2026-10-01T10:00:00+00:00",
        text="Нужна бригада для ремонта квартиры",
        source_group="Соседи",
        source_group_id="vk:12",
        phone="+79990000000",
    )
