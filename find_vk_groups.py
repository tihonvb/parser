"""Поиск и проверка VK-групп Самары, где ЛЮДИ пишут заявки на ремонт.

Два режима:

1) search — сам ищет группы через groups.search (нужен токен с полными
   правами; серверный токен VK ID этого не умеет — ошибка 1051):
       python find_vk_groups.py search

2) check — проверяет ГОТОВЫЙ список ссылок (работает с любым токеном,
   в т.ч. серверным — нужны только groups.getById и wall.get):
       python find_vk_groups.py check candidates.txt
   candidates.txt — по одной ссылке на строку (https://vk.com/xxx, club123,
   или просто id). Строки после # игнорируются.

В обоих режимах по каждой группе смотрятся последние 100 постов и за
последние 7 дней считается: сколько постов со словом «ремонт» и сколько
из них похожи на ЗАПРОС («ищу», «нужен», «посоветуйте», «кто делал»...).
В конце — готовый список для vk.group_ids.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import vk_api
from vk_api.exceptions import ApiError

from common import load_config
from vk_parser import _normalize_group_ref, _run_batched

SAMARA_CITY_ID = 123  # id города Самара в VK

QUERIES = [
    "Подслушано Самара", "Самара подслушано район", "Самара соседи",
    "ЖК Самара", "ЖК Самара жители", "новостройка Самара", "Кошелев",
    "Новая Самара", "Самара Кировский район", "Самара Промышленный район",
    "Самара Октябрьский район", "Самара Советский район", "Самара Ленинский район",
    "Самара Железнодорожный район", "Самара Куйбышевский район",
    "Самара Красноглинский район", "Самарский район Самара",
    "Барахолка Самара", "Объявления Самара", "Самара услуги",
    "Ищу мастера Самара", "Ремонт Самара", "Отделка Самара",
    "Шабашка Самара", "Халтура Самара",
]

REQUEST_MARKERS = [
    "ищу", "ищем", "нужен", "нужна", "нужно", "нужны", "требуется", "требуются",
    "посоветуйте", "подскажите", "кто делал", "кто может", "кто сделает",
    "порекомендуйте", "помогите найти",
]

DAYS = 7
MAX_GROUPS_TO_CHECK = 2000  # фактически проверяем все найденные — нужные паблики (районы, ЖК, барахолки) обычно маленькие и в топе по участникам не видны


class TokenProblem(Exception):
    pass


def _token_error(e: ApiError) -> str | None:
    if e.code == 5:
        return ("Токен в config.yaml не действует (истёк или отозван). "
                "На сервере: venv/bin/python vk_token.py refresh. "
                "На компьютере нужен новый токен — или запускайте режим check на сервере.")
    if e.code == 1051:
        return "Этот метод недоступен для данного токена (ошибка 1051)."
    return None


def _items(resp):
    return resp.get("groups", resp) if isinstance(resp, dict) else resp


def _search_groups(api) -> list[int]:
    found: dict[int, dict] = {}
    for q in QUERIES:
        try:
            resp = api.groups.search(q=q, city_id=SAMARA_CITY_ID, count=100)
        except ApiError as e:
            msg = _token_error(e)
            if msg:
                raise TokenProblem(msg + " Режим search здесь не сработает — используйте check.")
            print(f"[find] Запрос {q!r} не прошёл: {e}")
            continue
        for g in resp.get("items", []):
            if g.get("is_closed", 0) == 0 and not g.get("deactivated"):
                found[g["id"]] = g
        time.sleep(0.35)
    print(f"[find] Найдено открытых групп: {len(found)}.")
    return list(found)


def _get_details(api, refs: list[str]) -> list[dict]:
    detailed: list[dict] = []
    for start in range(0, len(refs), 400):
        chunk = refs[start:start + 400]
        try:
            detailed.extend(_items(api.groups.getById(group_ids=",".join(chunk), fields="members_count,wall")))
        except ApiError as e:
            msg = _token_error(e)
            if msg:
                raise TokenProblem(msg)
            print(f"[find] groups.getById на пачку не прошёл ({e}) — по одной...")
            for ref in chunk:
                try:
                    detailed.extend(_items(api.groups.getById(group_ids=ref, fields="members_count,wall")))
                except ApiError as e2:
                    print(f"[find] Пропускаю {ref!r}: {e2}")
                time.sleep(0.35)
        time.sleep(0.35)
    return detailed


def _evaluate(api, groups: list[dict]) -> list[dict]:
    # wall: 0 — выключена, 1 — открыта, 2 — ограничена, 3 — закрыта
    readable = [g for g in groups if g.get("wall", 1) in (1, 2) and g.get("is_closed", 0) == 0]
    skipped = len(groups) - len(readable)
    if skipped:
        print(f"[find] Пропущено групп с закрытой стеной/закрытых: {skipped}.")
    readable.sort(key=lambda g: g.get("members_count", 0), reverse=True)
    readable = readable[:MAX_GROUPS_TO_CHECK]
    print(f"[find] Смотрю последние посты {len(readable)} групп...")

    tasks = [("wall.get", {"owner_id": -g["id"], "count": 100}) for g in readable]
    results = _run_batched(api, tasks)

    since = time.time() - DAYS * 86400
    rows = []
    for g, res in zip(readable, results):
        if not res:
            continue
        posts = [p for p in res.get("items", []) if p.get("date", 0) >= since]
        remont = [p for p in posts if "ремонт" in (p.get("text") or "").lower()]
        reqs = [p for p in remont if any(m in (p.get("text") or "").lower() for m in REQUEST_MARKERS)]
        rows.append({
            "id": g["id"],
            "screen": g.get("screen_name") or f"club{g['id']}",
            "name": g.get("name", ""),
            "members": g.get("members_count", 0),
            "posts": len(posts),
            "remont": len(remont),
            "requests": len(reqs),
        })
    rows.sort(key=lambda r: (r["requests"], r["remont"], r["posts"]), reverse=True)
    return rows


def _print(rows: list[dict], show_all: bool) -> None:
    print(f"\n[find] За последние {DAYS} дней (сортировка по постам, похожим на заявки):\n")
    print(f"{'заявки?':>7} {'ремонт':>6} {'постов':>6} {'участн.':>8}  ссылка — название")
    for r in rows:
        if not show_all and r["remont"] == 0:
            continue
        print(f"{r['requests']:>7} {r['remont']:>6} {r['posts']:>6} {r['members']:>8}  "
              f"vk.com/{r['screen']} — {r['name'][:60]}")

    good = [r for r in rows if r["requests"] > 0]
    if not good:
        print("\n[find] Ни одной группы с постами, похожими на заявки, за эти дни.")
        return
    print("\n[find] Кандидаты для vk.group_ids (проверь глазами, лишнее удали):\n")
    for r in good:
        print(f'    - "https://vk.com/{r["screen"]}"  # {r["name"][:50]} — похожих на заявки за {DAYS} дн.: {r["requests"]}')


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    cfg = load_config()
    vk_cfg = cfg["vk"]
    # Токен можно передать разово через переменную окружения VK_TOKEN —
    # тогда config.yaml трогать не нужно.
    token = os.environ.get("VK_TOKEN", "").strip() or vk_cfg["access_token"]
    if os.environ.get("VK_TOKEN"):
        print("[find] Использую токен из VK_TOKEN (не из config.yaml).")
    api = vk_api.VkApi(token=token, api_version=vk_cfg.get("api_version", "5.199")).get_api()

    try:
        if mode == "search":
            refs = [str(i) for i in _search_groups(api)]
            show_all = False
        elif mode == "check":
            if len(sys.argv) < 3:
                print("Использование: python find_vk_groups.py check candidates.txt")
                return 1
            lines = Path(sys.argv[2]).read_text(encoding="utf-8").splitlines()
            refs = []
            for line in lines:
                line = line.split("#", 1)[0].strip().strip('"').strip("'").lstrip("-").strip()
                if line:
                    refs.append(_normalize_group_ref(line))
            print(f"[find] Ссылок в файле: {len(refs)}.")
            show_all = True
        else:
            print(f"Неизвестный режим {mode!r}. Доступно: search, check")
            return 1

        if not refs:
            print("[find] Нечего проверять.")
            return 1
        rows = _evaluate(api, _get_details(api, refs))
    except TokenProblem as e:
        print(f"[find] {e}")
        return 1

    _print(rows, show_all)
    return 0


if __name__ == "__main__":
    sys.exit(main())
