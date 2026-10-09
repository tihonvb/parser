"""Typed data exchanged across application boundaries."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, NotRequired, TypedDict

from lead_parser.core.models import ClassificationDecision, Lead


@dataclass
class ScanResult:
    source_id: str
    scanned: int = 0
    candidates: int = 0
    complete: bool = True
    errors: list[str] = field(default_factory=list)
    cursor: dict[str, Any] = field(default_factory=dict)
    prefilter: dict[str, int] = field(default_factory=dict)
    coverage: str = "configured_window"
    window: dict[str, Any] = field(default_factory=dict)

    def counted(self, reason: str) -> None:
        self.prefilter[reason] = self.prefilter.get(reason, 0) + 1

    def fail(self, message: str) -> None:
        self.complete = False
        self.errors.append(message)


@dataclass(frozen=True)
class ClassifiedLead:
    lead: Lead
    decision: ClassificationDecision


class DeliveryJob(TypedDict):
    id: int
    lead_key: str
    kind: str
    destination: str
    state: str
    attempts: int
    lease_owner: str
    lease_until: NotRequired[float | None]
    sheet_row: NotRequired[int | None]


@dataclass(frozen=True)
class DeliveryTarget:
    kind: str
    destination: str


@dataclass(frozen=True)
class DeliveryPlan:
    primary: DeliveryTarget | None = None
    dependents: tuple[DeliveryTarget, ...] = ()


@dataclass(frozen=True)
class DeliveryPolicy:
    max_attempts: int = 8
    retry_base_seconds: float = 60
    retry_max_seconds: float = 3600
    lease_seconds: int = 300
    jobs_per_run: int = 100


@dataclass(frozen=True)
class PipelineSettings:
    batch_limit: int = 100
    max_ai_attempts: int = 5
    ai_retry_seconds: float = 60


@dataclass(frozen=True)
class LeadStatisticsRecord:
    lead: Lead
    ai_state: str
    human_is_client: bool | None = None


@dataclass(frozen=True)
class EvaluationSettings:
    keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    city: str
    work_type: str
    model: str
    prompt_version: str
    threshold: float
