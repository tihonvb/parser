"""Сбор постов из VK через официальный VK API (vk_api) — без браузера.

Для эффективности при большом числе групп и ключевых слов запросы
батчатся через execute() — VK позволяет объединить до 25 вызовов метода
в один HTTP-запрос (VKScript), это в разы быстрее и бережнее к лимитам,
чем дёргать wall.search по одному на каждую пару группа+слово.

Два режима, оба задаются в config.yaml -> vk:
  1. group_ids — последние посты конкретных сообществ (wall.get, по 100 на
     страницу), отбор по ключевым словам делается здесь же. Глубина —
     vk.max_pages_per_query (по умолчанию 1 = последние 100 постов).
     Посты старше vk.max_age_hours (по умолчанию 48 ч) отбрасываются.
     Название группы резолвится один раз в начале (groups.getById) и
     пишется в Lead.source_group — по нему потом строится статистика
     (см. stats.py), какие паблики дают больше всего реальных заказов.
  2. use_global_newsfeed_search — поиск по всей ленте VK (newsfeed.search),
     требует токен с обычными пользовательскими правами, а не сервисный
     токен сообщества.

vk.group_overrides позволяет сканировать отдельные "горячие" группы
активнее остальных (больше страниц на запрос), не трогая общий
max_pages_per_query — см. пример в config.yaml. Обычно это заполняют по
подсказке из stats.py, после того как накопилась статистика.

Если пачка вызовов внутри execute() возвращает слишком большой суммарный
ответ, VK отвечает ошибкой [13] "response size is too big" — повтор того
же запроса не помогает, поэтому пачка автоматически делится пополам и
обрабатывается рекурсивно (см. _execute_batch), а для одного слишком
"тяжёлого" вызова используется прямой запрос в обход execute().
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

import vk_api
from vk_api.exceptions import ApiError

from common import Lead, extract_phone, matches_keywords

EXECUTE_BATCH_SIZE = 25  # лимит VK на число вызовов в одном execute()


def _post_to_lead(
    post: dict,
    keywords: list[str],
    exclude: list[str] | None,
    group_names: dict[int, str] | None = None,
    min_ts: int | None = None,
) -> Lead | None:
    text = post.get("text", "")
    if not text or not matches_keywords(text, keywords, exclude):
        return None
    # Старые посты (в т.ч. закреплённые) не нужны — иначе при первом запуске
    # по новым группам заказчику улетели бы заявки месячной давности.
    if min_ts and (post.get("date") or 0) < min_ts:
        return None

    owner_id = post.get("owner_id")
    post_id = post.get("id")
    url = f"https://vk.com/wall{owner_id}_{post_id}"
    date_ts = post.get("date")
    date_str = (
        datetime.fromtimestamp(date_ts, tz=timezone.utc).isoformat() if date_ts else ""
    )

    source_group = ""
    if owner_id and owner_id < 0 and group_names:
        source_group = group_names.get(-owner_id, f"club{-owner_id}")

    return Lead(
        source="vk",
        external_id=f"{owner_id}_{post_id}",
        date=date_str,
        author=str(post.get("signer_id", "") or owner_id or ""),
        text=text,
        phone=extract_phone(text) or "",
        url=url,
        source_group=source_group,
    )


_LINK_RE = re.compile(r"^(?:https?://)?(?:m\.)?vk\.(?:com|ru)/", re.IGNORECASE)


def _normalize_group_ref(ref) -> str:
    """Приводит элемент vk.group_ids к виду, который понимает groups.getById:
    123456 / -123456 / "club123456" / "public123456" / "https://vk.com/podslushano_samara"
    / "podslushano_samara" -> "123456" или "podslushano_samara"."""
    s = str(ref).strip()
    s = _LINK_RE.sub("", s).split("?")[0].strip("/")
    if s.lstrip("-").isdigit():
        return str(abs(int(s)))
    m = re.fullmatch(r"(?:club|public|event)(\d+)", s)
    if m:
        return m.group(1)
    return s


def _resolve_groups(api, group_refs: list) -> tuple[list[int], dict[int, str]]:
    """group_ids из конфига (числа, ссылки, короткие имена) -> ([id, ...], {id: название}).
    Один запрос groups.getById на всё; если он падает (например, одна из
    ссылок кривая), резолвим по одной, пропуская битые с предупреждением."""
    refs = [_normalize_group_ref(r) for r in group_refs if str(r).strip()]
    if not refs:
        return [], {}

    def _items(resp):
        return resp.get("groups", resp) if isinstance(resp, dict) else resp

    found: list[dict] = []
    try:
        found = list(_items(api.groups.getById(group_ids=",".join(refs))))
    except ApiError as e:
        print(f"[vk] groups.getById на весь список не прошёл ({e}) — резолвлю по одной.")
        for ref in refs:
            try:
                found.extend(_items(api.groups.getById(group_ids=ref)))
            except ApiError as e2:
                print(f"[vk] Пропускаю группу {ref!r}: {e2}")
            time.sleep(0.35)

    ids: list[int] = []
    names: dict[int, str] = {}
    for g in found:
        gid = int(g["id"])
        if gid not in names:
            ids.append(gid)
            names[gid] = g.get("name", f"club{gid}")
    if not found and refs:
        # VK недоступен совсем — работаем хотя бы с числовыми id без названий.
        ids = [int(r) for r in refs if r.isdigit()]
    return ids, names


def _call_literal(method: str, params: dict) -> str:
    """VKScript-вызов вида API.method({...}); VKScript понимает объект
    параметров в виде обычного JSON-литерала."""
    return f"API.{method}({json.dumps(params, ensure_ascii=False)})"


def _direct_call(api, method: str, params: dict):
    """Вызов метода напрямую, в обход execute()/VKScript. Используется как
    запасной вариант, когда даже ОДИН вызов внутри execute() не проходит
    из-за ошибки VK [13] 'response size is too big' — это ограничение на
    суммарный ответ именно execute(), к прямым вызовам метода оно не
    применяется."""
    obj = api
    for part in method.split("."):
        obj = getattr(obj, part)
    return obj(**params)


def _execute_batch(api, chunk: list[tuple[str, dict]], attempt: int = 0) -> list:
    """chunk — список (метод, параметры). Выполняет их одним запросом
    execute(). При обычной ошибке (лимит частоты и т.п.) — короткая пауза
    и до двух повторов. При ошибке VK [13] 'response size is too big'
    повтор того же запроса не поможет (тот же объём данных) — вместо этого
    пачка делится пополам и обе половины обрабатываются рекурсивно; если
    и один-единственный вызов оказывается слишком «тяжёлым» — он уходит
    напрямую в обход execute() (см. _direct_call)."""
    if not chunk:
        return []

    if len(chunk) == 1:
        method, params = chunk[0]
        code = "return [" + _call_literal(method, params) + "];"
        try:
            result = api.execute(code=code)
            return list(result) if result else [None]
        except ApiError as e:
            if e.code == 13:
                print(f"[vk] Слишком большой ответ на {method} — пробую напрямую, в обход execute()")
                try:
                    return [_direct_call(api, method, params)]
                except ApiError as e2:
                    print(f"[vk] Не получилось и напрямую ({method}): {e2}")
                    return [None]
            if attempt < 2:
                time.sleep(1.5)
                return _execute_batch(api, chunk, attempt=attempt + 1)
            print(f"[vk] execute() не удался после повторов: {e}")
            return [None]

    code = "return [" + ",".join(_call_literal(m, p) for m, p in chunk) + "];"
    try:
        result = api.execute(code=code)
        result = list(result) if result else []
        return result + [None] * (len(chunk) - len(result))
    except ApiError as e:
        if e.code == 13:
            mid = len(chunk) // 2
            time.sleep(0.3)
            return _execute_batch(api, chunk[:mid]) + _execute_batch(api, chunk[mid:])
        if attempt < 2:
            time.sleep(1.5)
            return _execute_batch(api, chunk, attempt=attempt + 1)
        print(f"[vk] execute() не удался после повторов: {e}")
        return [None] * len(chunk)


def _run_batched(api, tasks: list[tuple[str, dict]]) -> list:
    """tasks — список (метод, параметры). Возвращает результаты в том же
    порядке, разбивая на чанки по EXECUTE_BATCH_SIZE с паузой между ними."""
    results: list = []
    for start in range(0, len(tasks), EXECUTE_BATCH_SIZE):
        chunk = tasks[start:start + EXECUTE_BATCH_SIZE]
        chunk_results = _execute_batch(api, chunk)
        # Если VK молча вернул меньше элементов, чем запрашивали — не рассыпаемся.
        chunk_results = list(chunk_results) + [None] * (len(chunk) - len(chunk_results))
        results.extend(chunk_results)
        time.sleep(0.5)  # execute тоже подчиняется общему лимиту ~3 запроса/сек
    return results


def collect_leads(cfg: dict) -> list[Lead]:
    vk_cfg = cfg["vk"]
    if not vk_cfg.get("enabled", True):
        return []

    general = cfg["general"]
    keywords = general["keywords"]
    exclude = general.get("exclude_keywords")

    session = vk_api.VkApi(token=vk_cfg["access_token"], api_version=vk_cfg.get("api_version", "5.199"))
    api = session.get_api()

    group_ids = vk_cfg.get("group_ids") or []
    if not group_ids and not vk_cfg.get("use_global_newsfeed_search"):
        print("[vk] Список group_ids пуст и глобальный поиск выключен — нечего парсить.")
        return []

    count = min(vk_cfg.get("posts_per_run", 100), 100)
    max_pages = max(1, vk_cfg.get("max_pages_per_query", 1))
    overrides = vk_cfg.get("group_overrides") or {}
    max_age_hours = vk_cfg.get("max_age_hours", 48)
    min_ts = int(time.time() - max_age_hours * 3600) if max_age_hours else None

    leads: list[Lead] = []
    group_names: dict[int, str] = {}

    if group_ids:
        resolved_ids, group_names = _resolve_groups(api, group_ids)

        # Берём последние посты каждой группы целиком (wall.get) и отбираем
        # по ключевым словам сами. Раньше здесь был wall.search по каждому
        # слову, но токены VK ID (сервер) получают на нём ошибку 1051
        # "Method is not available for this profile type" — а VK внутри
        # execute() такую ошибку молча превращает в пустой ответ, и парсер
        # просто "ничего не находил". wall.get работает с любым токеном,
        # запросов меньше (по одному на группу, а не группа×слово) и свежие
        # посты не теряются.
        tasks: list[tuple[str, dict]] = []
        for gid in resolved_ids:
            # "Горячие" группы можно сканировать глубже остальных — см.
            # vk.group_overrides в config.yaml.
            pages_for_group = max(1, overrides.get(gid, {}).get("max_pages", max_pages))
            for page in range(pages_for_group):
                tasks.append((
                    "wall.get",
                    {"owner_id": -gid, "count": count, "offset": page * count, "extended": 0},
                ))
        n_requests = (len(tasks) + EXECUTE_BATCH_SIZE - 1) // EXECUTE_BATCH_SIZE
        print(f"[vk] {len(resolved_ids)} групп, {len(tasks)} вызовов wall.get — {n_requests} запрос(ов) execute()...")
        results = _run_batched(api, tasks)
        failed = sum(1 for res in results if not res)
        if failed:
            print(f"[vk] Внимание: {failed} из {len(tasks)} вызовов wall.get вернули ошибку/пусто "
                  "(закрытая группа, бан, или метод недоступен для токена).")
        scanned = 0
        for res in results:
            if not res:
                continue
            for post in res.get("items", []):
                scanned += 1
                lead = _post_to_lead(post, keywords, exclude, group_names, min_ts)
                if lead:
                    leads.append(lead)
        print(f"[vk] Просмотрено постов: {scanned}, подошло по ключевым словам и давности (≤{max_age_hours} ч): {len(leads)}.")

    if vk_cfg.get("use_global_newsfeed_search"):
        # Глобальный поиск по ленте (посты с личных страниц и из любых
        # групп) токену VK ID недоступен (ошибка 1051). Если в конфиге есть
        # vk.search_token — полноценный пользовательский токен (например,
        # через vkhost) — поиск идёт им, а чтение стен групп остаётся на
        # основном (самообновляющемся) токене.
        search_token = str(vk_cfg.get("search_token") or "").strip()
        search_api = api
        if search_token and "ВСТАВЬТЕ" not in search_token.upper():
            search_api = vk_api.VkApi(token=search_token, api_version=vk_cfg.get("api_version", "5.199")).get_api()
        city = general.get("city", "")
        newsfeed_count = min(vk_cfg.get("posts_per_run", 100), 200)
        tasks = [
            ("newsfeed.search", {"q": f"{kw} {city}".strip(), "count": newsfeed_count, "extended": 0})
            for kw in keywords
        ]
        results = _run_batched(search_api, tasks)
        failed = sum(1 for res in results if not res)
        if failed == len(tasks):
            print("[vk] Глобальный поиск (newsfeed.search) не сработал ни по одному слову — "
                  "токен не подходит или истёк (нужен vk.search_token с полными правами).")
        found = 0
        for res in results:
            if not res:
                continue
            for post in res.get("items", []):
                lead = _post_to_lead(post, keywords, exclude, min_ts=min_ts)
                if lead:
                    leads.append(lead)
                    found += 1
        print(f"[vk] Глобальный поиск: подошло по ключевым словам и давности: {found}.")

    return leads
