"""Inspect legacy Sheets schema; --apply duplicates it before an in-place RAW migration."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime

from configuration import CONFIG_PATH, load_config
from sheets_writer import HEADER, LEGACY_HEADER, SchemaConflict, _get_worksheet


def infer_identity(source: str, url: str) -> tuple[str, str]:
    if source == "vk" and (match := re.search(r"vk\.(?:com|ru)/wall(-?\d+)_(\d+)", url)):
        owner, post = match.groups()
        return f"vk:{owner}_{post}", f"vk:{abs(int(owner))}" if int(owner) < 0 else f"vk:user:{owner}"
    if source == "avito":
        from avito_parser import _extract_id_from_link

        try:
            return "avito:" + _extract_id_from_link(url), ""
        except ValueError:
            pass
    return "", ""  # Telegram usernames are not stable numeric channel identities.


def migrate_rows(rows, identities=None):
    identities = identities or {}
    if not rows:
        return [HEADER], 0
    if rows[0][: len(HEADER)] == HEADER:
        return rows, 0
    if rows[0][:9] != LEGACY_HEADER:
        raise SchemaConflict("Unrecognized legacy schema: export and map columns explicitly")
    migrated = [HEADER + rows[0][9:]]
    unresolved = 0
    for raw in rows[1:]:
        row = raw + [""] * max(0, 9 - len(raw))
        key, source_id = infer_identity(row[1], row[7])
        if row[7] in identities:
            mapping = identities[row[7]]
            if (
                not isinstance(mapping, dict)
                or not isinstance(mapping.get("lead_id"), str)
                or not mapping["lead_id"].startswith(row[1] + ":")
                or not isinstance(mapping.get("source_id"), str)
            ):
                raise SchemaConflict("Invalid identity map")
            key, source_id = mapping["lead_id"], mapping["source_id"]
        unresolved += int(not key)
        migrated.append(row[:9] + [key, source_id, "", ""] + row[9:])
    keys = [row[9] for row in migrated[1:] if row[9]]
    if len(keys) != len(set(keys)):
        raise SchemaConflict("Duplicate historical IDs: resolve before migration")
    return migrated, unresolved


def migrate(worksheet, *, apply=False, identities=None):
    rows = worksheet.get_all_values()
    migrated, unresolved = migrate_rows(rows, identities)
    plan = {
        "rows": len(migrated) - 1,
        "unresolved_ids": unresolved,
        "apply": apply,
        "changed": migrated != rows,
    }
    if apply and migrated != rows:
        backup_name = worksheet.title + "_backup_" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        worksheet.spreadsheet.duplicate_sheet(worksheet.id, new_sheet_name=backup_name)
        width = max(len(row) for row in migrated)
        if worksheet.col_count < width:
            worksheet.add_cols(width - worksheet.col_count)
        worksheet.update(values=migrated, range_name="A1", value_input_option="RAW")
        worksheet.freeze(rows=1)
        plan["backup_sheet"] = backup_name
    return plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--identity-map")
    args = parser.parse_args(argv)
    from pathlib import Path

    identities = (
        json.loads(Path(args.identity_map).read_text(encoding="utf-8")) if args.identity_map else None
    )
    print(
        json.dumps(
            migrate(
                _get_worksheet(load_config(args.config), create=False),
                apply=args.apply,
                identities=identities,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
