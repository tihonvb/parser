"""Deprecated CLI aliases; all authorization uses the single vk_token manager.

step1 -> login; step2 now requires the complete callback URL (including state).
Old code/device_id arguments cannot prove callback authenticity and are refused."""

from __future__ import annotations

import sys

from lead_parser.interfaces.cli.vk_token import main as token_main


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args:
        args[0] = {"step1": "login", "step2": "code"}.get(args[0], args[0])
    return token_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
