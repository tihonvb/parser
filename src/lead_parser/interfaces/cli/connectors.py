"""Connectors and their credentials: what is configured, what is missing, and a template to fill in.

    lead-parser connectors status [--json]       # every integration: enabled? ready? which credentials are missing
    lead-parser connectors check                 # exit 2 if an ENABLED connector lacks credentials
    lead-parser connectors env-template [--output FILE]
                                                 # a .env template with every credential name, values left empty

Only NAMES are printed (environment variable, config path, file): values never are. Credentials are loaded from the
process environment or a ``.env`` file next to config.yaml (priority: process > .env > YAML). The list comes from
infrastructure/credentials.py; see docs/connectors.md.
"""

import argparse
import json
import os
import sys
from pathlib import Path

from lead_parser.application.connectors import ConnectorStatus, Credential, blocked, report
from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.credentials import SPECS, file_exists

EXIT_OK, EXIT_MISSING = 0, 2


def _describe(credential: Credential) -> dict:
    return {
        "config": credential.path,
        "env": credential.env or None,
        "kind": credential.kind,
        "secret": credential.secret,
        "description": credential.description,
    }


def as_dict(status: ConnectorStatus) -> dict:
    return {
        "name": status.name,
        "title": status.title,
        "kind": status.kind,
        "enabled": status.enabled,
        "ready": status.ready,
        "missing": [_describe(c) for c in status.missing],
        "optional_not_set": [_describe(c) for c in status.optional],
    }


def _state(status: ConnectorStatus) -> str:
    if not status.enabled:
        return "выключен" if not status.missing else "выключен (кредов нет)"
    return "ГОТОВ" if status.ready else "НЕТ КРЕДОВ"


def render(statuses: list[ConnectorStatus]) -> str:
    lines = []
    for status in statuses:
        lines.append(f"[{status.kind:<10}] {status.name:<14} {_state(status):<22} {status.title}")
        for credential in status.missing:
            where = f"{credential.env} или " if credential.env else ""
            lines.append(f"    не хватает: {where}{credential.path} — {credential.description}")
        for credential in status.optional:
            lines.append(f"    необязательно: {credential.env or credential.path} — {credential.description}")
    return "\n".join(lines)


def env_template() -> str:
    lines = [
        "# Креды коннекторов. Скопируйте в .env рядом с config.yaml (права 0600, файл не попадает в Git).",
        "# Приоритет: переменные процесса > .env > config.yaml. enabled включается в config.yaml.",
        "# Файлы (сессия Telegram, токены VK ID) создаются командами авторизации и в .env не хранятся.",
        "",
    ]
    for spec in SPECS:
        entries = [c for c in spec.credentials if c.env]
        if not entries:
            continue
        lines.append(f"# {spec.title}")
        for credential in entries:
            requirement = "" if credential.required else " (необязательно)"
            lines.append(f"# {credential.description}{requirement}")
            lines.append(f"{credential.env}=")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_template(path: Path) -> None:
    """Create the template owner-only; an existing file is never overwritten."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        stream.write(env_template())


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("command", nargs="?", default="status", choices=["status", "check", "env-template"])
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--json", action="store_true", help="status: machine-readable output")
    parser.add_argument("--output", help="env-template: write here instead of stdout (never overwrites)")
    args = parser.parse_args(argv)

    if args.command == "env-template":
        if args.output:
            try:
                write_template(Path(args.output))
            except FileExistsError:
                print(f"{args.output} already exists: not overwritten.", file=sys.stderr)
                return 1
            print(f"Template written: {args.output} (owner-only). Fill in the values you have.")
        else:
            print(env_template(), end="")
        return EXIT_OK

    statuses = report(load_config(args.config, require_access=False), SPECS, file_exists)
    if args.command == "check":
        problems = blocked(statuses)
        for status in problems:
            print(f"{status.name}: не хватает " + ", ".join(c.env or c.path for c in status.missing))
        if not problems:
            print("Все включённые коннекторы обеспечены кредами.")
        return EXIT_MISSING if problems else EXIT_OK
    if args.json:
        print(json.dumps([as_dict(s) for s in statuses], ensure_ascii=False, indent=2))
    else:
        print(render(statuses))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
