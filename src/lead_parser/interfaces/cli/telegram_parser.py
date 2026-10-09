"""Explicit interactive Telegram login."""

import argparse
import asyncio

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.telegram.collector import login


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login"])
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    asyncio.run(login(load_config(args.config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
