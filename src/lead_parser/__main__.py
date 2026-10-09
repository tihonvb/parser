"""Run the same interface as the installed lead-parser command."""

from lead_parser.interfaces.cli.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
