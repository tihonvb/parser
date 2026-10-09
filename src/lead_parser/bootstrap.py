"""Composition root: bind configuration and concrete adapters to application ports."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

from lead_parser.application.delivery import DeliveryRoute, DeliveryService
from lead_parser.application.models import (
    DeliveryJob,
    DeliveryPlan,
    DeliveryPolicy,
    DeliveryTarget,
    EvaluationSettings,
    PipelineSettings,
    ScanResult,
)
from lead_parser.application.pipeline import PipelineService
from lead_parser.application.ports import CollectionState, LeadSource
from lead_parser.core.models import Lead
from lead_parser.infrastructure.integrations.avito import collector as avito
from lead_parser.infrastructure.integrations.google_sheets.gateway import (
    SheetsGateway,
    SheetsWriter,
    destination,
)
from lead_parser.infrastructure.integrations.openrouter.classifier import PROMPT_VERSION, OpenRouterClassifier
from lead_parser.infrastructure.integrations.telegram import collector as telegram
from lead_parser.infrastructure.integrations.telegram.notifications import TelegramGateway, _collect_chat_ids
from lead_parser.infrastructure.integrations.vk import collector as vk
from lead_parser.infrastructure.integrations.vk.oauth import TokenManager
from lead_parser.infrastructure.persistence.sqlite import Store
from lead_parser.infrastructure.persistence.usage_log import UsageLog
from lead_parser.infrastructure.security import delivery_error


@dataclass
class ConfiguredSource:
    name: str
    cfg: dict
    collector: Callable

    def collect(self, state: CollectionState, reports: list[ScanResult]) -> list[Lead]:
        if self.name == "vk" and self.cfg["vk"]["token_mode"] == "vk_id":
            self.cfg["vk"]["access_token"] = TokenManager(self.cfg).refresh()
        return self.collector(self.cfg, store=state, reports=reports)


def build_sources(cfg: dict) -> tuple[LeadSource, ...]:
    return tuple(
        ConfiguredSource(name, cfg, module.collect_leads)
        for name, module in (("telegram", telegram), ("vk", vk), ("avito", avito))
        if cfg[name]["enabled"]
    )


def build_classifier(cfg: dict, *, purpose: str = "pipeline") -> OpenRouterClassifier:
    return OpenRouterClassifier(
        cfg, UsageLog(str(cfg["storage"]["database"]) + ".usage.jsonl"), purpose=purpose
    )


@dataclass
class _SenderGateway:
    cfg: dict
    sender: Callable

    def deliver(self, lead: Lead, job: DeliveryJob) -> None:
        try:
            self.sender(self.cfg, lead, job["destination"])
        except Exception as error:
            raise delivery_error(error) from error


def build_delivery(cfg: dict, store: Store, *, writer_factory=None, sender=None) -> DeliveryService:
    sheet_dest = destination(cfg) if cfg["output"]["enabled"] else None
    notification = cfg["notifications"]["telegram"]
    recipients = tuple(_collect_chat_ids(notification)) if notification["enabled"] else ()
    writer_factory = writer_factory or SheetsWriter
    return DeliveryService(
        store,
        DeliveryPlan(
            DeliveryTarget("sheets", sheet_dest) if sheet_dest else None,
            tuple(DeliveryTarget("telegram", recipient) for recipient in recipients),
        ),
        (
            DeliveryRoute(
                "sheets",
                (sheet_dest,) if sheet_dest else (),
                SheetsGateway(lambda: writer_factory(cfg), store),
            ),
            DeliveryRoute(
                "telegram",
                recipients,
                _SenderGateway(cfg, sender) if sender else TelegramGateway(cfg),
            ),
        ),
        DeliveryPolicy(**cfg["delivery"]),
    )


def evaluation_settings(cfg: dict) -> EvaluationSettings:
    return EvaluationSettings(
        keywords=tuple(cfg["general"]["keywords"]),
        exclude_keywords=tuple(cfg["general"]["exclude_keywords"]),
        city=cfg["general"]["city"],
        work_type=cfg["general"]["work_type"],
        model=cfg["ai_filter"]["model"],
        prompt_version=PROMPT_VERSION,
        threshold=cfg["ai_filter"]["min_confidence"],
    )


def run_once(cfg: dict, *, deliver_only: bool = False) -> tuple[int, dict]:
    Path(cfg["storage"]["lock_file"]).parent.mkdir(parents=True, exist_ok=True)
    with FileLock(cfg["storage"]["lock_file"], timeout=0), Store(cfg["storage"]["database"]) as store:
        store.import_legacy(cfg["storage"]["seen_store"])
        service = PipelineService(
            store,
            build_classifier(cfg),
            build_delivery(cfg, store),
            build_sources(cfg),
            PipelineSettings(
                batch_limit=cfg["delivery"]["jobs_per_run"],
                max_ai_attempts=cfg["ai_filter"]["max_attempts"],
                ai_retry_seconds=cfg["delivery"]["retry_base_seconds"],
            ),
        )
        return service.run(deliver_only=deliver_only)
