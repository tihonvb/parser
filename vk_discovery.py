"""«Ресерчер» для VK: ищет паблики/группы Самары, где могут появляться
заявки на ремонт под ключ, и выгружает список кандидатов для ручного
отбора — НИЧЕГО не добавляет в config.yaml автоматически.

Почему не автоматически: поиск по ключевым словам находит и совсем не
подходящие группы (мёртвые, слишком общие, не про Самару и т.п.) — отбор
глазами занимает пару минут, а бездумно закинуть все найденные ID в
мониторинг — это шум в таблице и трата запросов/денег на ИИ-фильтр
впустую.

Запуск:
    python vk_discovery.py

Результат: vk_candidates.csv рядом со скриптами + список в консоли,
отсортированный по числу участников. Понравившиеся ID группы (число в
колонке id) переносите в config.yaml -> vk.group_ids.
"""

from __future__ import annotations

import csv
import time

import vk_api
from vk_api.exceptions import ApiError

from common import load_config

# Ключевые слова для ПОИСКА ГРУПП. Специально НЕ тематические запросы вроде
# "ремонт квартир" или "услуги ремонта" — по ним groups.search находит в
# основном паблики самих фирм-подрядчиков (они так и называют свою
# страницу), а нам нужны обратные — городские доски объявлений/барахолки,
# где обычные люди пишут "ищу бригаду", вперемешку с прочими объявлениями.
DISCOVERY_QUERIES = [
    "объявления",
    "барахолка",
    "подслушано",
    "доска объявлений",
    "услуги и работа",
    "куплю продам",
    "ищу мастера",
    "частные объявления",
]


def _get_city_id(api, city_name: str) -> int | None:
    try:
        resp = api.database.getCities(country_id=1, q=city_name, count=1)
    except ApiError as e:
        print(f"[vk_discovery] Не удалось определить city_id для '{city_name}': {e}")
        return None
    items = resp.get("items", [])
    if not items:
        print(f"[vk_discovery] VK не нашёл город '{city_name}'.")
        return None
    city = items[0]
    print(f"[vk_discovery] Город '{city_name}' -> city_id={city['id']} ({city['title']})")
    return city["id"]


def find_candidates(cfg: dict) -> list[dict]:
    vk_cfg = cfg["vk"]
    city_name = cfg["general"].get("city", "Самара")

    session = vk_api.VkApi(token=vk_cfg["access_token"], api_version=vk_cfg.get("api_version", "5.199"))
    api = session.get_api()

    city_id = _get_city_id(api, city_name)

    seen_ids: set[int] = set()
    candidates: list[dict] = []

    for query in DISCOVERY_QUERIES:
        params = {"q": query, "type": "group", "count": 200}
        if city_id:
            params["city_id"] = city_id
        try:
            resp = api.groups.search(**params)
        except ApiError as e:
            print(f"[vk_discovery] Ошибка groups.search('{query}'): {e}")
            continue

        for group in resp.get("items", []):
            gid = group.get("id")
            if gid in seen_ids:
                continue
            seen_ids.add(gid)
            candidates.append(
                {
                    "id": gid,
                    "name": group.get("name", ""),
                    "screen_name": group.get("screen_name", ""),
                    "members_count": group.get("members_count", 0),
                    "is_closed": group.get("is_closed", 0),
                    "url": f"https://vk.com/{group.get('screen_name') or ('club' + str(gid))}",
                    "found_by_query": query,
                }
            )
        time.sleep(0.4)  # ~3 запроса/сек лимит VK

    candidates.sort(key=lambda c: c["members_count"], reverse=True)
    return candidates


def save_csv(candidates: list[dict], path: str = "vk_candidates.csv") -> None:
    if not candidates:
        return
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(candidates[0].keys()))
        writer.writeheader()
        writer.writerows(candidates)


def main() -> None:
    cfg = load_config()
    candidates = find_candidates(cfg)

    if not candidates:
        print("[vk_discovery] Ничего не нашлось.")
        return

    save_csv(candidates)
    print(f"\n[vk_discovery] Найдено {len(candidates)} групп(ы). Полный список — в vk_candidates.csv")
    print("[vk_discovery] Топ-30 по числу участников (закрытые группы помечены 🔒 — туда обычный поиск постов не достанет):\n")
    print(f"{'ID':>10}  {'Участников':>10}  Название")
    print("-" * 70)
    for c in candidates[:30]:
        lock = "🔒 " if c["is_closed"] else "   "
        print(f"{c['id']:>10}  {c['members_count']:>10}  {lock}{c['name']} ({c['url']})")

    print(
        "\n[vk_discovery] Понравившиеся ID (колонка ID, без минуса) добавьте "
        "в config.yaml -> vk.group_ids."
    )


if __name__ == "__main__":
    main()
