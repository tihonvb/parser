"""Compatibility entry point; implementation lives in lead_parser.interfaces.cli.discover."""

from lead_parser.interfaces.cli.discover import main

if __name__ == "__main__":
    raise SystemExit(main())
