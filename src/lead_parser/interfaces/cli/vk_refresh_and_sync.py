"""Deprecated refresh alias; saved configuration is never rewritten."""

import sys

from lead_parser.interfaces.cli.vk_token import main as token_main


def main(argv=None):
    return token_main(["refresh", *(sys.argv[1:] if argv is None else argv)])


if __name__ == "__main__":
    raise SystemExit(main())
