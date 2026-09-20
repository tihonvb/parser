"""ИИ-отбор лидов через OpenRouter.

Ключевые слова (matches_keywords в common.py) — грубый фильтр: они ловят
и реальные заявки от клиентов, и рекламу мастеров/фирм ("сделаем ремонт
под ключ, звоните!"), и просто случайные совпадения. Этот модуль прогоняет
уже найденные по ключевым словам тексты через дешёвую языковую модель
(через OpenRouter — https://openrouter.ai, единая точка доступа к разным
провайдерам), чтобы оставить только то, что реально похоже на заявку от
человека, который ищет исполнителя, а не сам исполнитель/реклама.

Модель и цена настраиваются в config.yaml -> ai_filter.model. Актуальный
список моделей и их стоимость меняются часто — сверяйтесь с
https://openrouter.ai/models перед тем как задавать модель здесь; для
такой простой классификации коротких текстов подходит почти любая
недорогая/бесплатная модель.
"""

from __future__ import annotations

import json
import re

import requests

from common import Lead

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM_PROMPT = """Ты — фильтр лидов для компании, которая делает ремонт квартир под ключ в Самаре.
Тебе присылают короткие тексты постов/объявлений (из Telegram, VK, Avito),
уже отобранные по ключевым словам про ремонт. Часть из них — реальные
заявки от людей, которые ищут бригаду/исполнителя. Часть — реклама самих
мастеров/фирм, которые ПРЕДЛАГАЮТ услуги ремонта (это не лид, это
конкуренты). Часть вообще не по теме (стройматериалы, обучение, другой
город и т.п.).

Для каждого текста определи: is_client = true, только если это похоже на
человека/заказчика, который ищет исполнителя для ремонта под ключ.
Если текст — это предложение услуг от мастера/бригады/фирмы, или явно не
по теме, или относится не к Самаре (а к другому городу) — is_client=false.
Если сомневаешься — ставь false и низкий confidence.

Ответь СТРОГО в виде JSON-массива, без каких-либо пояснений вне JSON:
[{"index": 0, "is_client": true, "confidence": 0.9, "reason": "коротко почему, 5-10 слов"}, ...]
Массив должен содержать ровно один объект на каждый присланный текст, в том же порядке."""


def _build_user_prompt(batch: list[Lead]) -> str:
    lines = []
    for i, lead in enumerate(batch):
        snippet = (lead.text or "").replace("\n", " ").strip()[:500]
        lines.append(f'{i}. Источник: {lead.source}. Текст: "{snippet}"')
    return "Оцени эти тексты:\n\n" + "\n".join(lines)


def _extract_json_array(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    return None


def _classify_batch(cfg: dict, batch: list[Lead]) -> dict[int, dict]:
    ai_cfg = cfg["ai_filter"]
    headers = {
        "Authorization": f"Bearer {ai_cfg['openrouter_api_key']}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": ai_cfg.get("model", "openai/gpt-4o-mini"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(batch)},
        ],
        "temperature": 0,
    }
    resp = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=60)
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    parsed = _extract_json_array(content)
    if not parsed:
        print(f"[ai_filter] Не удалось разобрать ответ модели, оставляю батч без фильтрации. Ответ: {content[:300]!r}")
        return {}
    result = {}
    for item in parsed:
        try:
            result[int(item["index"])] = item
        except (KeyError, ValueError, TypeError):
            continue
    return result


def filter_leads(cfg: dict, leads: list[Lead]) -> list[Lead]:
    """Прогоняет лиды через ИИ-классификатор и возвращает только те, что
    похожи на реальных клиентов. Если ai_filter выключен в конфиге или
    список пуст — возвращает лиды как есть, без изменений."""
    ai_cfg = cfg.get("ai_filter") or {}
    if not ai_cfg.get("enabled"):
        return leads
    if not leads:
        return leads
    if not ai_cfg.get("openrouter_api_key") or "PUT_YOUR" in ai_cfg.get("openrouter_api_key", ""):
        print("[ai_filter] ai_filter.enabled=true, но openrouter_api_key не заполнен в config.yaml — пропускаю фильтрацию, лиды оставлены как есть.")
        return leads

    batch_size = ai_cfg.get("batch_size", 8)
    min_confidence = ai_cfg.get("min_confidence", 0.6)

    kept: list[Lead] = []
    for start in range(0, len(leads), batch_size):
        batch = leads[start:start + batch_size]
        try:
            results = _classify_batch(cfg, batch)
        except requests.RequestException as e:
            print(f"[ai_filter] Ошибка запроса к OpenRouter: {e} — этот батч оставлен без фильтрации.")
            kept.extend(batch)
            continue

        for i, lead in enumerate(batch):
            verdict = results.get(i)
            if verdict is None:
                # Модель не вернула ответ по этому элементу — на всякий случай оставляем,
                # лучше показать лишний лид человеку, чем молча потерять реального клиента.
                kept.append(lead)
                continue
            is_client = bool(verdict.get("is_client"))
            try:
                confidence = float(verdict.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            lead.extra["ai_is_client"] = is_client
            lead.extra["ai_confidence"] = confidence
            lead.extra["ai_reason"] = str(verdict.get("reason", ""))[:200]
            if is_client and confidence >= min_confidence:
                kept.append(lead)

    print(f"[ai_filter] Из {len(leads)} лидов после ИИ-фильтра осталось {len(kept)}.")
    return kept
