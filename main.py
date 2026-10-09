"""Compatibility entry point; implementation lives in lead_parser.interfaces.cli.main."""

from lead_parser.interfaces.cli.main import main

if __name__ == "__main__":
    raise SystemExit(main())
