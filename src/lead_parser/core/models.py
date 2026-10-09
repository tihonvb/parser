"""Lead identity and classification values; no persistence or presentation formats."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass
class Lead:
    source: str
    external_id: str
    date: str  # Publication time; empty means unknown.
    author: str = ""
    text: str = ""
    phone: str = ""
    price: str = ""
    url: str = ""
    source_group: str = ""
    source_group_id: str = ""
    observed_at: str = field(default_factory=utc_now)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.source, str)
            or re.fullmatch(r"[a-z][a-z0-9_]*", self.source) is None
            or not str(self.external_id).strip()
        ):
            raise ValueError("Lead requires a stable source namespace and non-empty external_id")
        self.external_id = str(self.external_id)
        self.source_group_id = str(self.source_group_id)

    def dedupe_key(self) -> str:
        return f"{self.source}:{self.external_id}"


class ClassificationState(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVIEW = "review"


@dataclass(frozen=True)
class Verdict:
    """A valid classifier assessment, independent of provider response schemas."""

    is_client: bool
    confidence: float
    reason: str

    def __post_init__(self) -> None:
        if type(self.is_client) is not bool:
            raise ValueError("is_client must be boolean")
        if (
            type(self.confidence) not in {int, float}
            or not math.isfinite(self.confidence)
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("confidence must be finite and between zero and one")
        if not isinstance(self.reason, str):
            raise ValueError("reason must be text")
        object.__setattr__(self, "confidence", float(self.confidence))


@dataclass(frozen=True)
class ClassificationDecision:
    """Business outcome; failed classification remains pending, never rejected.

    Acceptance without a verdict represents an explicitly disabled classifier.
    Retry attempts and escalation to manual review belong to the application.
    """

    state: ClassificationState
    verdict: Verdict | None = None
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.state, ClassificationState):
            raise ValueError("state must be a ClassificationState")
        if self.verdict is not None and not isinstance(self.verdict, Verdict):
            raise ValueError("verdict must be a Verdict")
        if not isinstance(self.error, str):
            raise ValueError("error must be text")
        if self.state is ClassificationState.PENDING and self.verdict is not None:
            raise ValueError("Pending classification cannot contain a verdict")
        if self.state is not ClassificationState.PENDING and self.error:
            raise ValueError("Classification errors must remain pending")
        if self.state is ClassificationState.REJECTED and self.verdict is None:
            raise ValueError("Rejection requires a classifier verdict")
        if (
            self.state is ClassificationState.ACCEPTED
            and self.verdict is not None
            and not self.verdict.is_client
        ):
            raise ValueError("A negative classifier verdict cannot be accepted")
