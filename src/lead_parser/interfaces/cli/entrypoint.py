"""Dispatch the installed command without importing optional adapters before selection."""

import importlib
import sys

COMMANDS = {
    "run": "main",
    "evaluate": "evaluate",
    "stats": "stats",
    "analytics": "analytics",
    "discover": "discover",
    "vk-auth": "vk_token",
    "telegram-auth": "telegram_parser",
    "migrate-sheet": "fix_sheet",
    "vk-discovery": "vk_discovery",
    "telegram-discovery": "telegram_discovery",
    "vk-groups": "find_vk_groups",
    "extract-vk-groups": "extract_vk_groups",
}


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in COMMANDS:
        module_name = COMMANDS[args.pop(0)]
    else:
        module_name = "main"
    command = importlib.import_module(f"lead_parser.interfaces.cli.{module_name}")
    return command.main(args)


if __name__ == "__main__":
    raise SystemExit(main())
