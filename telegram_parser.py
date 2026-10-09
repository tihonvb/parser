"""Compatibility entry point; implementation lives in lead_parser.interfaces.cli.telegram_parser."""

from lead_parser.interfaces.cli.telegram_parser import main

if __name__ == "__main__":
    raise SystemExit(main())
