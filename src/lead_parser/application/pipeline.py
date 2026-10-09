"""Collection → durable inbox → explicit classification → durable delivery."""

from dataclasses import asdict

from lead_parser.application.models import ClassifiedLead, PipelineSettings, ScanResult
from lead_parser.application.ports import Classifier, DeliveryRunner, InboxRepository, LeadSource
from lead_parser.core.models import ClassificationDecision, ClassificationState


class PipelineService:
    def __init__(
        self,
        store: InboxRepository,
        classifier: Classifier,
        delivery: DeliveryRunner,
        sources: tuple[LeadSource, ...],
        settings: PipelineSettings,
    ):
        self.store, self.classifier, self.delivery = store, classifier, delivery
        self.sources, self.settings = sources, settings

    def run(self, *, deliver_only: bool = False) -> tuple[int, dict]:
        reports: list[ScanResult] = []
        if not deliver_only:
            for source in self.sources:
                try:
                    self.store.ingest(source.collect(self.store, reports))
                except Exception as error:
                    report = ScanResult(source.name + ":collector")
                    report.fail(type(error).__name__)
                    reports.append(report)
                    self.store.checkpoint(report)

        pending = self.store.pending_ai(limit=self.settings.batch_limit)
        if pending:
            try:
                results = self.classifier.classify(pending)
                outcomes = {}
                duplicates = set()
                for result in results:
                    key = result.lead.dedupe_key()
                    if key in outcomes:
                        duplicates.add(key)
                    outcomes[key] = result
                error_name = "MissingClassification"
            except Exception as error:
                outcomes, duplicates = {}, set()
                error_name = type(error).__name__
            for lead in pending:
                key = lead.dedupe_key()
                result = outcomes.get(key) if key not in duplicates else None
                if result is None:
                    result = ClassifiedLead(
                        lead,
                        ClassificationDecision(
                            ClassificationState.PENDING,
                            error="DuplicateClassification" if key in duplicates else error_name,
                        ),
                    )
                self.store.save_classification(
                    result,
                    max_attempts=self.settings.max_ai_attempts,
                    retry_seconds=self.settings.ai_retry_seconds,
                )
        delivered = self.delivery.drain()
        report = {
            **self.store.summary(),
            "this_run": {
                "delivery": delivered,
                "sources": [
                    {
                        "id": item.source_id,
                        **{
                            key: value
                            for key, value in asdict(item).items()
                            if key not in {"source_id", "cursor"}
                        },
                    }
                    for item in reports
                ],
            },
        }
        problems = (
            any(not result.complete for result in reports)
            or any(report["leads"].get(state, 0) for state in ("pending", "review"))
            or any(report["deliveries"].get(state, 0) for state in ("pending", "leased", "failed"))
        )
        return int(problems), report
