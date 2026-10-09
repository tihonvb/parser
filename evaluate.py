"""Offline prefilter coverage; --online explicitly opts into paid model evaluation."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from ai_filter import PROMPT_VERSION, filter_leads
from common import Lead, keyword_decision
from configuration import CONFIG_PATH, ConfigError, load_config

DATASET = Path(__file__).parent / "fixtures" / "evaluation.jsonl"


def load_dataset(path=DATASET):
    cases = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for case in cases:
        if (
            not isinstance(case.get("id"), str)
            or case["id"] in ids
            or type(case.get("is_client")) is not bool
            or not isinstance(case.get("reason"), str)
            or not isinstance(case.get("text"), str)
        ):
            raise ValueError("Invalid dataset schema or duplicate ID")
        ids.add(case["id"])
    return cases


def metrics(pairs):
    counts = Counter((expected, actual) for expected, actual in pairs)
    tp, fp, fn, tn = (counts[key] for key in ((True, True), (False, True), (True, False), (False, False)))
    return {
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
    }


def evaluate(cfg, cases, *, online=False):
    if online and (not cfg["ai_filter"]["enabled"] or not cfg["ai_filter"]["openrouter_api_key"]):
        raise ConfigError("Online evaluation requires enabled AI and its API key")
    candidates, decisions = [], {}
    prefilter_pairs = []
    reasons = Counter()
    for case in cases:
        passed, reason = keyword_decision(
            case["text"], cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
        )
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
                        "known_city": case.get("city", cfg["general"]["city"]),
                        "title": case.get("title", ""),
                    },
                )
            )
    technical = []
    if online:
        accepted = {lead.external_id for lead in filter_leads(cfg, candidates)}
        for lead in candidates:
            decision = decisions[lead.external_id]
            if lead.extra.get("ai_pending"):
                technical.append({"id": lead.external_id, "error": lead.extra.get("ai_error")})
            else:
                decision["actual"] = lead.external_id in accepted
                decision["verdict"] = {
                    key: value for key, value in lead.extra.items() if key.startswith("ai_")
                }
    resolved = [
        (case["is_client"], decisions[case["id"]]["actual"])
        for case in cases
        if decisions[case["id"]]["actual"] is not None
    ]
    report = {
        "dataset_version": "synthetic-v1",
        "dataset_size": len(cases),
        "mode": "online" if online else "offline-prefilter",
        "model": cfg["ai_filter"]["model"],
        "prompt_version": PROMPT_VERSION,
        "threshold": cfg["ai_filter"]["min_confidence"],
        "city": cfg["general"]["city"],
        "work_type": cfg["general"]["work_type"],
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


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--dataset", default=str(DATASET))
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--output", help="Optional JSON report file")
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    report = evaluate(cfg, load_dataset(args.dataset), online=args.online)
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if report["technical_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
