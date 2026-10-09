"""Extract canonical VK group IDs from persisted lead snapshots without network access."""

import argparse

import yaml

from lead_parser.infrastructure.configuration import CONFIG_PATH, load_config
from lead_parser.infrastructure.persistence.sqlite import Store


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    args = parser.parse_args(argv)
    cfg = load_config(args.config, require_access=False)
    with Store(cfg["storage"]["database"]) as store:
        ids = set()
        for record in store.statistics_rows():
            source_id = record.lead.source_group_id
            if source_id.startswith("vk:") and source_id[3:].isdigit():
                ids.add(int(source_id[3:]))
        print(yaml.safe_dump({"vk": {"group_ids": sorted(ids)}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
