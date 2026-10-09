"""Compatibility entry point; implementation lives in lead_parser.interfaces.cli.stats."""

from lead_parser.interfaces.cli.stats import main

if __name__ == "__main__":
    raise SystemExit(main())
