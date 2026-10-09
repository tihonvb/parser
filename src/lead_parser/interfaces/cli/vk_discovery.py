"""City-scoped candidate discovery. Unknown geography fails before any group search."""

from __future__ import annotations

import argparse
import json

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.vk.discovery import find_candidates, save_csv


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--output", default="vk_candidates.csv")
    args = parser.parse_args(argv)
    report = {}
    candidates = find_candidates(load_config(args.config), report=report)
    save_csv(candidates, args.output)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    raise SystemExit(main())
