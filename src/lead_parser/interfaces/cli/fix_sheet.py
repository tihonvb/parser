"""Inspect legacy Sheets schema; --apply duplicates it before an in-place RAW migration."""

from __future__ import annotations

import argparse
import json

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.google_sheets.gateway import _get_worksheet
from lead_parser.infrastructure.integrations.google_sheets.migration import migrate


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--identity-map")
    args = parser.parse_args(argv)
    from pathlib import Path

    identities = (
        json.loads(Path(args.identity_map).read_text(encoding="utf-8")) if args.identity_map else None
    )
    print(
        json.dumps(
            migrate(
                _get_worksheet(load_config(args.config), create=False),
                apply=args.apply,
                identities=identities,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
