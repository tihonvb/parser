"""Use cases run through in-memory ports without SQLite, config, or SDKs."""

from collections import Counter
from copy import deepcopy

from lead_parser.application.delivery import DeliveryRoute, DeliveryService
from lead_parser.application.errors import DeliveryError
from lead_parser.application.models import (
    ClassifiedLead,
    DeliveryPlan,
    DeliveryPolicy,
    DeliveryTarget,
    PipelineSettings,
    ScanResult,
)
from lead_parser.application.pipeline import PipelineService
from lead_parser.core.models import ClassificationDecision, ClassificationState, Lead, Verdict
from lead_parser.core.policies import decide_classification


class MemoryInbox:
    def __init__(self, events):
        self.events = events
        self.leads = {}
        self.decisions = {}
        self.reports = []

    def ingest(self, leads):
        added = 0
        for lead in leads:
            if lead.dedupe_key() not in self.leads:
                self.leads[lead.dedupe_key()] = deepcopy(lead)
                self.events.append(("persist", lead.dedupe_key()))
                added += 1
        return added

    def cursor(self, source_id):
        return {}

    def checkpoint(self, report):
        self.reports.append(deepcopy(report))

    def pending_ai(self, *, limit):
        return [
            deepcopy(lead)
            for key, lead in self.leads.items()
            if key not in self.decisions or self.decisions[key].state is ClassificationState.PENDING
        ][:limit]

    def save_classification(self, result, **retry_settings):
        self.events.append(("classify", result.lead.dedupe_key(), result.decision.state))
        self.decisions[result.lead.dedupe_key()] = result.decision

    def summary(self):
        return {
            "leads": dict(
                Counter(
                    self.decisions[key].state if key in self.decisions else ClassificationState.PENDING
                    for key in self.leads
                )
            ),
            "deliveries": {},
        }


class RecordingDelivery:
    def __init__(self, events):
        self.events = events

    def drain(self):
        self.events.append(("deliver",))
        return {"errors": 0}


def test_pipeline_persists_partial_pages_before_classification_and_isolates_source_failure():
    events = []
    inbox = MemoryInbox(events)
    partial = Lead(source="vk", external_id="-12_1", date="", text="Нужен ремонт")
    healthy = Lead(source="telegram", external_id="10_2", date="", text="Ищу бригаду")

    class InterruptedSource:
        name = "first"

        def collect(self, state, reports):
            state.ingest([partial])
            state.checkpoint(ScanResult("vk:12", scanned=1, candidates=1))
            raise RuntimeError("sensitive transport credentials")

    class HealthySource:
        name = "second"

        def collect(self, state, reports):
            reports.append(ScanResult("telegram:10", scanned=1, candidates=1))
            return [healthy, healthy]

    class Classifier:
        def classify(self, leads):
            assert set(inbox.leads) == {partial.dedupe_key(), healthy.dedupe_key()}
            return [
                ClassifiedLead(lead, decide_classification(Verdict(True, 0.9, "request"))) for lead in leads
            ]

    pipeline = PipelineService(
        inbox,
        Classifier(),
        RecordingDelivery(events),
        (InterruptedSource(), HealthySource()),
        PipelineSettings(),
    )
    code, report = pipeline.run()
    assert code == 1
    assert report["leads"] == {"accepted": 2}
    assert report["this_run"]["sources"][0]["errors"] == ["RuntimeError"]
    assert "sensitive" not in str(report)
    assert [event[0] for event in events] == ["persist", "persist", "classify", "classify", "deliver"]


def test_missing_and_conflicting_decisions_remain_pending_then_resume_without_reclassifying_success():
    inbox = MemoryInbox([])
    leads = [Lead(source="vk", external_id=f"-12_{number}", date="") for number in range(3)]
    inbox.ingest(leads)
    calls = []

    class Classifier:
        def classify(self, batch):
            calls.append([lead.dedupe_key() for lead in batch])
            accepted = ClassificationDecision(ClassificationState.ACCEPTED)
            if len(calls) == 1:
                return [
                    ClassifiedLead(batch[0], accepted),
                    ClassifiedLead(batch[1], accepted),
                    ClassifiedLead(batch[1], accepted),
                ]
            return [ClassifiedLead(lead, accepted) for lead in batch]

    pipeline = PipelineService(inbox, Classifier(), RecordingDelivery([]), (), PipelineSettings())
    code, report = pipeline.run(deliver_only=True)
    assert code == 1 and report["leads"] == {"accepted": 1, "pending": 2}
    assert inbox.decisions[leads[1].dedupe_key()].error == "DuplicateClassification"
    assert inbox.decisions[leads[2].dedupe_key()].error == "MissingClassification"
    code, report = pipeline.run(deliver_only=True)
    assert code == 0 and report["leads"] == {"accepted": 3}
    assert calls[1] == [leads[1].dedupe_key(), leads[2].dedupe_key()]


def test_classifier_exception_keeps_the_durable_inbox_retryable():
    inbox = MemoryInbox([])
    lead = Lead(source="vk", external_id="-12_1", date="")
    inbox.ingest([lead])

    class Unavailable:
        def classify(self, leads):
            raise TimeoutError("https://provider.test?secret=must-not-leak")

    pipeline = PipelineService(inbox, Unavailable(), RecordingDelivery([]), (), PipelineSettings())
    code, report = pipeline.run(deliver_only=True)
    assert code == 1 and report["leads"] == {"pending": 1}
    assert inbox.decisions[lead.dedupe_key()].error == "TimeoutError"


class MemoryOutbox:
    def __init__(self, lead):
        self.item = lead
        self.jobs = [
            {
                "id": number,
                "lead_key": lead.dedupe_key(),
                "kind": kind,
                "destination": destination,
                "state": "pending",
                "attempts": 0,
                "lease_owner": "test",
            }
            for number, kind, destination in [(1, "crm", "customer-system"), (2, "email", "operator")]
        ]
        self.failures = []
        self.plans = []

    def enqueue_plan(self, plan):
        self.plans.append(plan)

    def claim(self, kind, destinations, *, lease_seconds):
        if kind == "email" and self.jobs[0]["state"] != "done":
            return None
        for job in self.jobs:
            if job["kind"] == kind and job["destination"] in destinations and job["state"] == "pending":
                job["state"] = "leased"
                return job.copy()
        return None

    def lead(self, key):
        assert key == self.item.dedupe_key()
        return self.item

    def delivered(self, job):
        self.jobs[job["id"] - 1]["state"] = "done"

    def delivery_failed(self, job, error, **settings):
        self.jobs[job["id"] - 1]["state"] = "retry_wait"
        self.failures.append((job, error, settings))


def test_delivery_accepts_new_gateway_kinds_and_honors_primary_failure_before_notifications():
    lead = Lead(source="vk", external_id="-12_1", date="")
    store = MemoryOutbox(lead)
    sent = []

    class CRM:
        unavailable = True

        def deliver(self, lead, job):
            if self.unavailable:
                self.unavailable = False
                raise DeliveryError("RateLimited", retry_after=120)
            sent.append("crm")

    class Email:
        def deliver(self, lead, job):
            sent.append("email")

    plan = DeliveryPlan(DeliveryTarget("crm", "customer-system"), (DeliveryTarget("email", "operator"),))
    service = DeliveryService(
        store,
        plan,
        (DeliveryRoute("crm", ("customer-system",), CRM()), DeliveryRoute("email", ("operator",), Email())),
        DeliveryPolicy(),
    )
    assert service.drain() == {"crm": 0, "email": 0, "errors": 1}
    assert sent == [] and store.failures[0][2]["retry_seconds"] == 120
    store.jobs[0]["state"] = "pending"
    assert service.drain() == {"crm": 1, "email": 1, "errors": 0}
    assert sent == ["crm", "email"]
    assert service.drain() == {"crm": 0, "email": 0, "errors": 0}
    assert sent == ["crm", "email"]
