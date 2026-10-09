"""VK pages, explicit execute sub-errors and durable per-source scan progress."""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime

import requests

from lead_parser.application.models import ScanResult
from lead_parser.application.ports import CollectionState
from lead_parser.core.models import Lead
from lead_parser.core.policies import extract_phone, keyword_decision, matches_keywords
from lead_parser.infrastructure.integrations.vk.client import VKClient, VKError
from lead_parser.infrastructure.security import safe_error

EXECUTE_BATCH_SIZE = 25
_LINK_RE = re.compile(r"^(?:https?://)?(?:m\.)?vk\.(?:com|ru)/", re.I)


def _normalize_group_ref(ref) -> str:
    value = _LINK_RE.sub("", str(ref).strip()).split("?")[0].split("#")[0].strip("/")
    if value.lstrip("-").isdigit():
        return str(abs(int(value)))
    match = re.fullmatch(r"(?:club|public|event)(\d+)", value, re.I)
    if match:
        return match[1]
    if not re.fullmatch(r"[A-Za-z0-9_.]+", value):
        raise ValueError("Invalid VK group reference")
    return value.casefold()


def _resolve_groups(api, refs: list) -> tuple[list[int], dict[int, str]]:
    names = {}
    for ref in dict.fromkeys(_normalize_group_ref(ref) for ref in refs):
        response = api.groups.getById(group_ids=ref)
        items = response.get("groups", []) if isinstance(response, dict) else response
        if not items:
            raise VKError(100)
        for group in items:
            names[int(group["id"])] = group.get("name", "")
    return list(names), names


def _post_to_lead(post, keywords, exclude, group_names=None, min_ts=None):
    text = post.get("text", "")
    if not matches_keywords(text, keywords, exclude) or (min_ts and post.get("date", 0) < min_ts):
        return None
    owner, post_id = post.get("owner_id"), post.get("id")
    if not isinstance(owner, int) or not isinstance(post_id, int):
        return None
    stamp = post.get("date")
    return Lead(
        source="vk",
        external_id=f"{owner}_{post_id}",
        date=datetime.fromtimestamp(stamp, UTC).isoformat() if stamp else "",
        author=str(post.get("signer_id") or owner),
        text=text,
        phone=extract_phone(text) or "",
        url=f"https://vk.com/wall{owner}_{post_id}",
        source_group=(group_names or {}).get(abs(owner), ""),
        source_group_id=f"vk:{abs(owner)}" if owner < 0 else f"vk:user:{owner}",
    )


def _execute_batch(client, chunk: list[tuple[str, dict]], attempt=0) -> list:
    if not chunk:
        return []
    code = (
        "return ["
        + ",".join(f"API.{method}({json.dumps(params, ensure_ascii=False)})" for method, params in chunk)
        + "];"
    )
    try:
        payload = client.method("execute", {"code": code}, raw=True)
        response = payload.get("response")
        if not isinstance(response, list) or len(response) != len(chunk):
            raise VKError(0)
        errors = iter(payload.get("execute_errors", []))
        results = []
        for index, result in enumerate(response):
            if result is False or result is None:
                error = VKError(int(next(errors, {}).get("error_code", 0)))
                if not error.permanent and attempt < 2:
                    time.sleep(0.35 * (attempt + 1))
                    result = _execute_batch(client, [chunk[index]], attempt + 1)[0]
                else:
                    result = error
            results.append(result)
        return results
    except VKError as error:
        if error.code == 13:
            if len(chunk) > 1:
                middle = len(chunk) // 2
                return _execute_batch(client, chunk[:middle], attempt) + _execute_batch(
                    client, chunk[middle:], attempt
                )
            try:
                return [client.method(*chunk[0])]
            except (VKError, requests.RequestException) as direct_error:
                return [direct_error]
        if not error.permanent and attempt < 2:
            time.sleep(0.35 * (attempt + 1))
            return _execute_batch(client, chunk, attempt + 1)
        return [error] * len(chunk)
    except (requests.RequestException, ValueError) as error:
        if attempt < 2:
            time.sleep(0.35 * (attempt + 1))
            return _execute_batch(client, chunk, attempt + 1)
        return [error] * len(chunk)


def _run_batched(client, tasks):
    return [
        result
        for start in range(0, len(tasks), EXECUTE_BATCH_SIZE)
        for result in _execute_batch(client, tasks[start : start + EXECUTE_BATCH_SIZE])
    ]


def _scan_group(client, gid, names, cfg, store: CollectionState | None, reports, source_ref=""):
    settings = cfg["vk"]
    result = ScanResult(f"vk:{gid}")
    previous = store.cursor(result.source_id) if store else {}
    override = settings["group_overrides"].get(str(gid), settings["group_overrides"].get(gid, {}))
    budget = override.get("max_pages", settings["max_pages_per_query"])
    count = min(settings["posts_per_run"], 100)
    started = previous.get("started", time.time()) if previous.get("offset") else time.time()
    cutoff = (
        previous.get("cutoff")
        if previous.get("offset")
        else previous.get(
            "watermark", started - settings["max_age_hours"] * 3600 if settings["max_age_hours"] else 0
        )
        - settings["overlap_seconds"]
    )
    offset, new_anchor, anchor_index = 0, None, 0
    leads = []
    result.complete = False
    result.window = {"cutoff": cutoff, "checkpoint_upper": started}
    try:
        if previous.get("offset"):
            front = _execute_batch(client, [("wall.get", {"owner_id": -gid, "count": count, "offset": 0})])[0]
            if isinstance(front, Exception):
                raise front
            front_items = front["items"]
            front_leads = [
                lead
                for post in front_items
                if (
                    lead := _post_to_lead(
                        post, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"], names, cutoff
                    )
                )
            ]
            for lead in front_leads:
                lead.extra.update(known_city=cfg["general"]["city"], source_ref=str(source_ref))
            if store:
                store.ingest(front_leads)
            leads.extend(front_leads)
            result.scanned += len(front_items)
            result.candidates += len(front_leads)
            for index, post in enumerate(front_items):
                if not post.get("is_pinned") and new_anchor is None:
                    new_anchor, anchor_index = post["id"], index
                if post.get("id") == previous.get("anchor"):
                    offset = max(0, previous["offset"] + index - previous.get("anchor_index", 0))
            # If the anchor disappeared/shifted past the front page, rescan from zero,
            # retain the original cutoff, and anchor the next continuation to this front.
        for _ in range(budget):
            response = _execute_batch(
                client, [("wall.get", {"owner_id": -gid, "count": count, "offset": offset})]
            )[0]
            if isinstance(response, Exception):
                raise response
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise VKError(0)
            items = response["items"]
            ordinary = [post for post in items if not post.get("is_pinned")]
            if new_anchor is None and ordinary:
                new_anchor = ordinary[0]["id"]
                anchor_index = items.index(ordinary[0])
            page = []
            for post in items:
                result.counted(
                    "outside_window"
                    if post.get("date", 0) < cutoff
                    else keyword_decision(
                        post.get("text", ""), cfg["general"]["keywords"], cfg["general"]["exclude_keywords"]
                    )[1]
                )
                lead = _post_to_lead(
                    post, cfg["general"]["keywords"], cfg["general"]["exclude_keywords"], names, cutoff
                )
                if lead:
                    lead.extra["known_city"] = cfg["general"]["city"]
                    lead.extra["source_ref"] = str(source_ref)
                    page.append(lead)
            result.scanned += len(items)
            result.candidates += len(page)
            leads.extend(page)
            if store:
                store.ingest(page)
            finished = (
                not items
                or len(items) < count
                or bool(ordinary and all(post.get("date", 0) < cutoff for post in ordinary))
            )
            next_offset = offset + len(items)
            result.cursor = (
                {"watermark": started}
                if finished
                else {
                    "offset": next_offset,
                    "anchor": new_anchor,
                    "anchor_index": anchor_index,
                    "cutoff": cutoff,
                    "started": started,
                }
            )
            if store:
                store.checkpoint(result)
            if finished:
                result.complete = True
                break
            offset = next_offset
        if not result.complete:
            result.errors.append("BudgetExhausted: backlog retained")
    except Exception as error:
        result.fail(f"VK:{error.code}" if isinstance(error, VKError) else safe_error(error))
        if not result.cursor:
            result.cursor = previous
    if store:
        store.checkpoint(result)
    reports.append(result)
    return leads


def collect_leads(
    cfg: dict, store: CollectionState | None = None, reports: list[ScanResult] | None = None, client=None
) -> list[Lead]:
    reports = reports if reports is not None else []
    settings = cfg["vk"]
    if not settings["enabled"]:
        return []
    client = client or VKClient(
        settings["access_token"], settings["api_version"], settings["request_timeout_seconds"]
    )
    leads = []
    for ref in settings["group_ids"]:
        try:
            ids, names = _resolve_groups(client.get_api(), [ref])
            for gid in ids:
                leads.extend(_scan_group(client, gid, names, cfg, store, reports, source_ref=ref))
        except Exception as error:
            try:
                identity = _normalize_group_ref(ref)
            except ValueError:
                identity = "invalid-reference"
            result = ScanResult("vk:ref:" + identity)
            result.fail(f"VK:{error.code}" if isinstance(error, VKError) else safe_error(error))
            reports.append(result)
            if store:
                store.checkpoint(result)
    if settings["use_global_newsfeed_search"]:
        search = (
            VKClient(settings["search_token"], settings["api_version"], settings["request_timeout_seconds"])
            if settings["search_token"]
            else client
        )
        for keyword in cfg["general"]["keywords"]:
            result = ScanResult("vk:search:" + keyword, coverage="best_effort_search")
            previous = store.cursor(result.source_id) if store else {}
            params = {
                "q": keyword + " " + cfg["general"]["city"],
                "count": min(settings["posts_per_run"], 200),
                "start_time": previous.get("start_time", int(time.time() - settings["max_age_hours"] * 3600)),
            }
            if previous.get("next_from"):
                params["start_from"] = previous["next_from"]
            result.window = {"cutoff": params["start_time"]}
            try:
                for _ in range(settings["max_pages_per_query"]):
                    response = _execute_batch(search, [("newsfeed.search", params)])[0]
                    if isinstance(response, Exception):
                        raise response
                    page = [
                        lead
                        for post in response["items"]
                        if (
                            lead := _post_to_lead(
                                post,
                                cfg["general"]["keywords"],
                                cfg["general"]["exclude_keywords"],
                                min_ts=params["start_time"],
                            )
                        )
                    ]
                    leads.extend(page)
                    result.scanned += len(response["items"])
                    result.candidates += len(page)
                    if store:
                        store.ingest(page)
                    following = response.get("next_from")
                    result.cursor = (
                        {"next_from": following, "start_time": params["start_time"]} if following else {}
                    )
                    result.complete = not bool(following)
                    if store:
                        store.checkpoint(result)
                    if result.complete:
                        break
                    if following == params.get("start_from"):
                        raise VKError(0)
                    params["start_from"] = following
            except Exception as error:
                result.fail(f"VK:{error.code}" if isinstance(error, VKError) else safe_error(error))
                if not result.cursor:
                    result.cursor = previous
            if not result.complete and not result.errors:
                result.errors.append("BudgetExhausted: search coverage is best effort")
            reports.append(result)
            if store:
                store.checkpoint(result)
    return leads
