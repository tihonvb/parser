"""Structural interfaces; implementations live outside the application."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol

from lead_parser.application.models import (
    ClassifiedLead,
    DeliveryJob,
    DeliveryPlan,
    LeadStatisticsRecord,
    ScanResult,
)
from lead_parser.core.models import Lead


class CollectionState(Protocol):
    def ingest(self, leads: list[Lead]) -> int: ...
    def cursor(self, source_id: str) -> dict[str, Any]: ...
    def checkpoint(self, result: ScanResult) -> None: ...


class LeadSource(Protocol):
    name: str

    def collect(self, state: CollectionState, reports: list[ScanResult]) -> list[Lead]: ...


class Classifier(Protocol):
    def classify(self, leads: list[Lead]) -> list[ClassifiedLead]: ...


class InboxRepository(CollectionState, Protocol):
    def pending_ai(self, *, limit: int) -> list[Lead]: ...
    def save_classification(
        self, result: ClassifiedLead, *, max_attempts: int, retry_seconds: float
    ) -> None: ...
    def summary(self) -> dict: ...


class RowReservations(Protocol):
    def reserve_row(self, job: DeliveryJob, minimum_row: int, *, existing_row: int | None = None) -> int: ...


class OutboxRepository(Protocol):
    def enqueue_plan(self, plan: DeliveryPlan) -> None: ...
    def claim(self, kind: str, destinations: list[str], *, lease_seconds: int) -> DeliveryJob | None: ...
    def lead(self, key: str) -> Lead: ...
    def delivered(self, job: DeliveryJob) -> None: ...
    def delivery_failed(
        self, job: DeliveryJob, error: str, *, permanent: bool, retry_seconds: float, max_attempts: int
    ) -> None: ...


class DeliveryGateway(Protocol):
    def deliver(self, lead: Lead, job: DeliveryJob) -> None: ...


class DeliveryRunner(Protocol):
    def drain(self) -> dict[str, int]: ...


class StatisticsRepository(Protocol):
    def statistics_rows(self) -> Iterable[LeadStatisticsRecord]: ...
    def summary(self) -> dict: ...
