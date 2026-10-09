"""City-scoped candidate discovery. Unknown geography fails before any group search."""

from __future__ import annotations

import argparse
import csv
import json
import time

from configuration import CONFIG_PATH, ConfigError, load_config
from vk_client import VKClient
from vk_token import TokenManager

DISCOVERY_QUERIES = [
    "объявления",
    "барахолка",
    "подслушано",
    "соседи",
    "ЖК жители",
    "услуги и работа",
    "ищу мастера",
]


def client_for(cfg):
    settings = cfg["vk"]
    token = settings["search_token"] or (
        TokenManager(cfg).refresh() if settings["token_mode"] == "vk_id" else settings["access_token"]
    )
    if not token:
        raise ConfigError("Discovery requires an authorized VK token")
    return VKClient(token, settings["api_version"], settings["request_timeout_seconds"])


def _get_city_id(api, city_name):
    response = api.database.getCities(country_id=1, q=city_name, count=100)
    matches = [
        city for city in response.get("items", []) if city.get("title", "").casefold() == city_name.casefold()
    ]
    if len(matches) != 1:
        raise ConfigError("City is missing or ambiguous; set discovery.city_id explicitly")
    return matches[0]["id"]


def find_candidates(cfg, client=None, report=None):
    client = client or client_for(cfg)
    api = client.get_api()
    city = cfg["general"]["city"]
    city_id = cfg["discovery"]["city_id"] or _get_city_id(api, city)
    found = {}
    complete = True
    for query in DISCOVERY_QUERIES:
        for page in range(cfg["discovery"]["max_pages"]):
            response = api.groups.search(q=f"{city} {query}", city_id=city_id, count=200, offset=page * 200)
            items = response.get("items", [])
            for group in items:
                gid = int(group["id"])
                if gid in found:
                    continue
                detail = api.groups.getById(group_ids=gid, fields="members_count,city,wall")
                groups = detail.get("groups", []) if isinstance(detail, dict) else detail
                group = groups[0] if groups else group
                location = group.get("city", {}).get("id")
                if location is not None and location != city_id:
                    continue
                found[gid] = {
                    "id": gid,
                    "source_id": f"vk:{gid}",
                    "name": group.get("name", ""),
                    "screen_name": group.get("screen_name", ""),
                    "members_count": group.get("members_count"),
                    "is_closed": group.get("is_closed"),
                    "city_id": location,
                    "url": f"https://vk.com/club{gid}",
                    "found_by_query": query,
                }
                if len(found) >= cfg["discovery"]["max_groups"]:
                    complete = False
                    break
            if len(found) >= cfg["discovery"]["max_groups"]:
                break
            if len(items) < 200:
                break
            if page == cfg["discovery"]["max_pages"] - 1:
                complete = False
            time.sleep(0.35)
        if len(found) >= cfg["discovery"]["max_groups"]:
            break
    if report is not None:
        report.update(
            city_id=city_id, complete=complete, groups=len(found), coverage="VK search results only"
        )
    return sorted(
        found.values(),
        key=lambda item: (item["members_count"] is not None, item["members_count"] or 0),
        reverse=True,
    )


def save_csv(candidates, path="vk_candidates.csv"):
    if candidates:
        with open(path, "w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(candidates[0]))
            writer.writeheader()
            writer.writerows(candidates)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(CONFIG_PATH))
    parser.add_argument("--output", default="vk_candidates.csv")
    args = parser.parse_args(argv)
    report = {}
    candidates = find_candidates(load_config(args.config), report=report)
    save_csv(candidates, args.output)
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
