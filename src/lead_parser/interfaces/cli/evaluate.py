"""Evaluate the bundled synthetic corpus; --online explicitly enables model requests."""

import argparse
import json
from importlib.resources import files
from pathlib import Path

from lead_parser.application.evaluation import evaluate
from lead_parser.bootstrap import build_classifier, evaluation_settings
from lead_parser.infrastructure.configuration import CONFIG_PATH, ConfigError, load_config


def load_dataset(path=None):
    source = Path(path) if path is not None else files("lead_parser.resources").joinpath("evaluation.jsonl")
    cases = [json.loads(line) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
    ids = set()
    for case in cases:
        if (
            not isinstance(case, dict)
            or not isinstance(case.get("id"), str)
            or case["id"] in ids
            or type(case.get("is_client")) is not bool
            or not isinstance(case.get("reason"), str)
            or not isinstance(case.get("text"), str)
        ):
            raise ValueError("Invalid dataset schema or duplicate ID")
        ids.add(case["id"])
    return cases


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument(
        "--dataset", help="Alternative JSONL corpus; defaults to the bundled synthetic corpus"
    )
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--output", help="Optional JSON report file")
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=args.online)
    if args.online and (not cfg["ai_filter"]["enabled"] or not cfg["ai_filter"]["openrouter_api_key"]):
        raise ConfigError("Online evaluation requires enabled AI and its API key")
    report = evaluate(
        evaluation_settings(cfg),
        load_dataset(args.dataset),
        classifier=build_classifier(cfg, purpose="evaluation") if args.online else None,
    )
    output = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if report["technical_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
