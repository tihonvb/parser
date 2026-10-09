"""Extract canonical VK group IDs from persisted lead snapshots; no network."""

import json

import yaml

from configuration import load_config
from storage import Store


def main():
    cfg = load_config()
    with Store(cfg["storage"]["database"]) as store:
        ids = set()
        for row in store.db.execute("SELECT payload FROM leads"):
            source_id = json.loads(row[0]).get("source_group_id", "")
            if source_id.startswith("vk:") and source_id[3:].isdigit():
                ids.add(int(source_id[3:]))
        print(yaml.safe_dump({"vk": {"group_ids": sorted(ids)}}))


if __name__ == "__main__":
    main()
