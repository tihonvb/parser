"""Explicit candidate discovery; writes CSVs, never changes subscriptions/configuration."""

from __future__ import annotations

import argparse
import asyncio
import json

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.security import safe_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    reports = {}
    for source in ("vk", "telegram"):
        if not cfg[source]["enabled"]:
            continue
        report = {}
        try:
            if source == "vk":
                from lead_parser.infrastructure.integrations.vk import discovery as module

                candidates = module.find_candidates(cfg, report=report)
            else:
                from lead_parser.infrastructure.integrations.telegram import discovery as module

                candidates = asyncio.run(module.find_candidates(cfg, report=report))
            module.save_csv(candidates)
        except Exception as error:
            report.update(complete=False, error=safe_error(error))
        reports[source] = report
    print(json.dumps(reports, ensure_ascii=False, indent=2))
    return int(any(not report.get("complete", False) for report in reports.values()))


if __name__ == "__main__":
    raise SystemExit(main())
