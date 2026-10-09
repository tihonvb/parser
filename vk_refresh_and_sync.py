"""Deprecated refresh alias. YAML is no longer rewritten; main reads the token file."""

import sys

from vk_token import main as token_main

if __name__ == "__main__":
    sys.exit(token_main(["refresh", *sys.argv[1:]]))
