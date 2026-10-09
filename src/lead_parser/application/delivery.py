"""Deliver a persisted plan through gateways with bounded retries and leases."""

from dataclasses import dataclass

from lead_parser.application.errors import DeliveryError
from lead_parser.application.models import DeliveryPlan, DeliveryPolicy
from lead_parser.application.ports import DeliveryGateway, OutboxRepository


@dataclass(frozen=True)
class DeliveryRoute:
    kind: str
    destinations: tuple[str, ...]
    gateway: DeliveryGateway


class DeliveryService:
    def __init__(
        self,
        store: OutboxRepository,
        plan: DeliveryPlan,
        routes: tuple[DeliveryRoute, ...],
        policy: DeliveryPolicy,
    ):
        self.store, self.plan, self.routes, self.policy = store, plan, routes, policy

    def drain(self) -> dict[str, int]:
        self.store.enqueue_plan(self.plan)
        counts = {route.kind: 0 for route in self.routes} | {"errors": 0}
        for route in self.routes:
            for _ in range(self.policy.jobs_per_run):
                job = self.store.claim(
                    route.kind, list(route.destinations), lease_seconds=self.policy.lease_seconds
                )
                if job is None:
                    break
                try:
                    route.gateway.deliver(self.store.lead(job["lead_key"]), job)
                    self.store.delivered(job)
                    counts[route.kind] += 1
                except Exception as error:
                    # Unexpected adapter failures remain retryable and expose only a class name.
                    failure = (
                        error if isinstance(error, DeliveryError) else DeliveryError(type(error).__name__)
                    )
                    retry = min(
                        self.policy.retry_max_seconds,
                        self.policy.retry_base_seconds * 2 ** min(job["attempts"], 16),
                    )
                    self.store.delivery_failed(
                        job,
                        str(failure),
                        permanent=failure.permanent,
                        retry_seconds=max(retry, failure.retry_after),
                        max_attempts=self.policy.max_attempts,
                    )
                    counts["errors"] += 1
        return counts
