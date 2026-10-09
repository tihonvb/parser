"""Single locked VK ID manager: PKCE, strict callback state, atomic token rotation."""

from __future__ import annotations

import argparse
import sys

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.integrations.vk.oauth import TokenError, TokenManager
from lead_parser.infrastructure.security import safe_error


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["login", "code", "refresh"])
    parser.add_argument("callback", nargs="?")
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        manager = TokenManager(load_config(args.config, require_access=False))
        if args.command == "login":
            print(manager.login())
            print("Run lead-parser vk-auth code with the complete callback within 10 minutes.")
        elif args.command == "code":
            if not args.callback:
                raise TokenError("Complete callback URL required")
            manager.exchange(args.callback)
            print("VK credentials saved privately. Parser reads the token file in vk_id mode.")
        else:
            manager.refresh(args.force)
            print("VK credentials ready.")
        return 0
    except Exception as error:
        print(
            "VK authorization failed: "
            + (str(error) if isinstance(error, TokenError) else safe_error(error)),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
