"""The connector contract: one description of every external integration, and what it needs to run.

A *connector* is any integration with an outside service: a lead **source** (Telegram, VK, Avito ...), a
**classifier** (OpenRouter) or a **delivery** target (Google Sheets, the Telegram bot). Each one is declared
once as a ``ConnectorSpec``: its name and config section, the credentials it needs (``Credential``) and the
conditions under which each credential applies. Everything else is derived from that single declaration:

* the environment-variable mapping used to load credentials (``infrastructure/credentials.py``);
* ``lead-parser connectors status | check | env-template``;
* the documented list of secrets (``docs/connectors.md``).

Credentials are therefore loaded in ONE place (``.env`` / process environment) and the code never depends on
how a given connector reads them. A source additionally provides a ``SourceCollector`` function, see
``bootstrap.SOURCE_MODULES``.

This module is pure (the architecture forbids I/O in this layer): whether a declared *file* credential exists is
answered by an ``exists`` function that the caller injects (``infrastructure.credentials.file_exists``), and there
is no knowledge of any concrete service.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from lead_parser.application.models import ScanResult
from lead_parser.application.ports import CollectionState
from lead_parser.core.models import Lead

KINDS = ("source", "classifier", "delivery")
_PLACEHOLDERS = ("PUT_YOUR", "ВСТАВЬТЕ", "ТВОЙ_")


class SourceCollector(Protocol):
    """The function every lead source provides: collect leads, persist pages through ``store``, report coverage."""

    def __call__(
        self,
        cfg: dict,
        store: CollectionState | None = None,
        reports: list[ScanResult] | None = None,
    ) -> list[Lead]: ...


def is_filled(value: Any) -> bool:
    """A usable value: a non-empty string/number that is not an example placeholder."""
    return (
        isinstance(value, (str, int))
        and not isinstance(value, bool)
        and bool(str(value).strip())
        and not any(marker in str(value).upper() for marker in _PLACEHOLDERS)
    )


def dig(cfg: dict, path: str) -> Any:
    """Value at a dotted config path, ``None`` when any part is missing."""
    node: Any = cfg
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            return None
        node = node[key]
    return node


def _always(_cfg: dict) -> bool:
    return True


@dataclass(frozen=True)
class Credential:
    """One secret or file a connector needs.

    ``path`` is the dotted config path (``telegram.api_id``); ``env`` the environment variable that fills it
    (empty when the value only comes from the config, e.g. a session file). ``kind='file'`` means the value is
    a path that must exist (``suffix`` is appended first, e.g. Telethon's ``.session``). ``applies`` limits the
    credential to some modes (``vk.token_mode == 'static'``); ``required=False`` marks a nice-to-have."""

    path: str
    description: str
    env: str = ""
    kind: str = "value"
    suffix: str = ""
    required: bool = True
    secret: bool = True
    applies: Callable[[dict], bool] = field(default=_always, compare=False)

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self.path.split("."))

    def present(self, cfg: dict, exists: Callable[[str], bool] | None = None) -> bool:
        """Filled in the config; for ``kind='file'`` also existing on disk when ``exists`` is given."""
        value = dig(cfg, self.path)
        if not is_filled(value):
            return False
        if self.kind == "file" and exists is not None:
            return exists(str(value) + self.suffix)
        return True


@dataclass(frozen=True)
class ConnectorStatus:
    name: str
    title: str
    kind: str
    enabled: bool
    missing: tuple[Credential, ...]
    optional: tuple[Credential, ...]
    docs: str

    @property
    def ready(self) -> bool:
        return self.enabled and not self.missing


@dataclass(frozen=True)
class ConnectorSpec:
    """Declaration of one integration. ``section`` is the dotted config path that holds its ``enabled`` flag."""

    name: str
    title: str
    kind: str
    section: str
    credentials: tuple[Credential, ...] = ()
    docs: str = ""

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"{self.name}: kind must be one of {KINDS}")
        if len({c.path for c in self.credentials}) != len(self.credentials):
            raise ValueError(f"{self.name}: duplicate credential path")

    def enabled(self, cfg: dict) -> bool:
        section = dig(cfg, self.section)
        return bool(section.get("enabled")) if isinstance(section, dict) else False

    def status(self, cfg: dict, exists: Callable[[str], bool] | None = None) -> ConnectorStatus:
        applicable = [c for c in self.credentials if c.applies(cfg)]
        return ConnectorStatus(
            name=self.name,
            title=self.title,
            kind=self.kind,
            enabled=self.enabled(cfg),
            missing=tuple(c for c in applicable if c.required and not c.present(cfg, exists)),
            optional=tuple(c for c in applicable if not c.required and not c.present(cfg, exists)),
            docs=self.docs,
        )


def report(
    cfg: dict, specs: tuple[ConnectorSpec, ...], exists: Callable[[str], bool] | None = None
) -> list[ConnectorStatus]:
    return [spec.status(cfg, exists) for spec in specs]


def blocked(statuses: list[ConnectorStatus]) -> list[ConnectorStatus]:
    """Enabled connectors that cannot run yet because credentials are missing."""
    return [s for s in statuses if s.enabled and s.missing]
