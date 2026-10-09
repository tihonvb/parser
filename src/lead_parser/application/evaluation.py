"""Benchmark prefilter and classifier decisions without IO or vendor configuration."""

from collections import Counter

from lead_parser.application.models import EvaluationSettings
from lead_parser.application.ports import Classifier
from lead_parser.core.models import ClassificationState, Lead
from lead_parser.core.policies import keyword_decision


def metrics(pairs):
    counts = Counter((expected, actual) for expected, actual in pairs)
    tp, fp, fn, tn = (counts[key] for key in ((True, True), (False, True), (True, False), (False, False)))
    return {
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def evaluate(settings: EvaluationSettings, cases: list[dict], *, classifier: Classifier | None = None):
    online = classifier is not None
    candidates, decisions = [], {}
    prefilter_pairs = []
    reasons = Counter()
    for case in cases:
        passed, reason = keyword_decision(case["text"], settings.keywords, settings.exclude_keywords)
        reasons[reason] += 1
        prefilter_pairs.append((case["is_client"], passed))
        decisions[case["id"]] = {
            "id": case["id"],
            "expected": case["is_client"],
            "ambiguous": case.get("ambiguous", False),
            "prefilter": passed,
            "prefilter_reason": reason,
            "actual": None if passed else False,
        }
        if passed:
            candidates.append(
                Lead(
                    source=case.get("source", "vk"),
                    external_id=case["id"],
                    date="",
                    text=case["text"],
                    source_group=case.get("source_group", "Synthetic source"),
                    source_group_id=case.get("source_group_id", "vk:1"),
                    extra={
                        "known_city": case.get("city", settings.city),
                        "title": case.get("title", ""),
                    },
                )
            )
    technical = []
    if classifier is not None:
        try:
            outcomes = classifier.classify(candidates)
            by_id = {}
            duplicate_ids = set()
            for outcome in outcomes:
                key = outcome.lead.external_id
                if key in by_id:
                    duplicate_ids.add(key)
                by_id[key] = outcome
            failure = "MissingClassification"
        except Exception as error:
            by_id, duplicate_ids = {}, set()
            failure = type(error).__name__
        for lead in candidates:
            item = decisions[lead.external_id]
            outcome = by_id.get(lead.external_id) if lead.external_id not in duplicate_ids else None
            if outcome is None or outcome.decision.state in {
                ClassificationState.PENDING,
                ClassificationState.REVIEW,
            }:
                technical.append(
                    {
                        "id": lead.external_id,
                        "error": outcome.decision.error if outcome is not None else failure,
                    }
                )
            else:
                item["actual"] = outcome.decision.state is ClassificationState.ACCEPTED
                verdict = outcome.decision.verdict
                item["verdict"] = (
                    {
                        "ai_is_client": verdict.is_client,
                        "ai_confidence": verdict.confidence,
                        "ai_reason": verdict.reason,
                    }
                    if verdict
                    else {}
                )
    resolved = [
        (case["is_client"], decisions[case["id"]]["actual"])
        for case in cases
        if decisions[case["id"]]["actual"] is not None
    ]
    report = {
        "dataset_version": "synthetic-v1",
        "dataset_size": len(cases),
        "mode": "online" if online else "offline-prefilter",
        "model": settings.model,
        "prompt_version": settings.prompt_version,
        "threshold": settings.threshold,
        "city": settings.city,
        "work_type": settings.work_type,
        "prefilter": {**metrics(prefilter_pairs), "reasons": dict(reasons), "candidates": len(candidates)},
        "pipeline": metrics(resolved) if online and not technical else None,
        "pipeline_complete": online and not technical,
        "resolved_examples": len(resolved) if online else 0,
        "technical_errors": technical,
        "semantic_errors": [
            item
            for item in decisions.values()
            if online and item["actual"] is not None and item["actual"] != item["expected"]
        ],
        "missed_before_ai": [
            item["id"] for item in decisions.values() if item["expected"] and not item["prefilter"]
        ],
        "decisions": list(decisions.values()),
    }
    return report
