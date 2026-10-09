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

ОТДЕЛЬНО: объявления о сдаче, продаже или покупке квартиры/жилья — это НЕ
лид, даже если в тексте есть слово "ремонт" в любом виде — "после ремонта",
"с ремонтом", "требуется ремонт", "нужен ремонт", "без ремонта" и т.п.
Это касается ОБОИХ направлений:
- квартира уже с ремонтом ("сдаю квартиру после ремонта", "продам с
  хорошим ремонтом");
- квартира, которой ремонт только требуется ("3-комнатная квартира,
  требуется ремонт, возможен торг", "продам квартиру, нужен ремонт").
В обоих случаях автор объявления продаёт/сдаёт/покупает САМУ КВАРТИРУ, а
не ищет бригаду для ремонта — фраза про ремонт тут просто описывает
состояние жилья для покупателя/арендатора, а не запрос на услугу.
Распознавай такие объявления по структурным признакам объявления о
недвижимости, даже если явного слова "продам"/"сдам" нет: цена в рублях,
адрес/улица/дом, площадь в м², этаж/подъезд, "собственник", "торг",
"риэлторов и агентства прошу не беспокоить", формат "N-к. квартира,
X м², Y/Z эт.". Если видишь несколько таких признаков вместе —
is_client=false, даже если отдельная фраза похожа на запрос ремонта.

ОТДЕЛЬНО: рекламные/контентные посты от самих ремонтных бригад/фирм не
всегда звучат как прямая реклама ("звоните, сделаем ремонт") — часто это
"сторителлинг" или мини-статья в блоге фирмы. Признаки такого поста (даже
без явного призыва "звоните" или названия фирмы):
- Автор говорит о СВОЕЙ работе/подходе/политике от "мы" — не "мне нужно",
  а "мы понимаем", "как мы работаем", "мы предлагаем варианты", "мы не
  обещаем невозможного". Это компания объясняет СВОЙ процесс широкой
  аудитории, а не конкретный человек просит ремонт для себя.
  Проверочный вопрос: есть ли в тексте конкретная личная ситуация автора
  (своя квартира/адрес/срок/бюджет), или это общее рассуждение о том,
  "как бывает у клиентов" и "как мы с этим работаем"? Если второе —
  is_client=false.
- Формат мини-статьи: цепляющий заголовок-крючок (часто в кавычках или
  с эмодзи), затем список проблем/аргументов, затем список "как мы с
  этим работаем" — типичная структура маркетингового контента, а не
  обращения за услугой.
- Риторические примеры от лица абстрактных "клиентов" в кавычках
  ("— Нужно через 3 дня", "— А можно быстрее?") — это иллюстрация тезиса
  автора-компании, а не реальная цитата конкретного обратившегося
  человека из этого поста.
- Также прямые признаки: описание процесса работы фирмы по шагам,
  приглашение приехать посмотреть действующий объект, упоминание
  конкретного менеджера/прораба по имени, призыв написать "в сообщения
  сообщества" или позвонить по контакту фирмы.
Если видишь признаки выше — это реклама/контент фирмы, а не запрос
клиента — is_client=false, даже без прямых фраз вроде "мы делаем ремонт
под ключ, звоните" и даже если тон текста тёплый и выглядит как разговор
с клиентом.

Для каждого текста определи: is_client = true, только если это похоже на
человека/заказчика, который САМ ищет исполнителя для ремонта под ключ
(а не сдаёт/продаёт/покупает готовую квартиру и не рекламирует свою
ремонтную фирму/бригаду).
Если текст — это предложение услуг от мастера/бригады/фирмы (включая
"сторителлинг"-формат), объявление о сдаче/продаже/покупке жилья, явно
не по теме, или относится не к Самаре (а к другому городу) —
is_client=false.
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
        print(f"[ai_filter] Не удалось разобрать ответ модели, батч отложен до следующего прогона. Ответ: {content[:300]!r}")
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

    # Если OpenRouter недоступен (нет баланса, сеть, кривой ответ) — лиды НЕ
    # пропускаются без проверки (иначе мусор улетит в таблицу и в Telegram
    # заказчику), а помечаются ai_pending=True. main.py не помечает такие
    # лиды как "виденные", поэтому в следующем прогоне они проверятся заново.
    kept: list[Lead] = []
    pending = 0
    for start in range(0, len(leads), batch_size):
        batch = leads[start:start + batch_size]
        try:
            results = _classify_batch(cfg, batch)
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as e:
            print(f"[ai_filter] Ошибка запроса к OpenRouter: {e} — батч отложен до следующего прогона.")
            for lead in batch:
                lead.extra["ai_pending"] = True
            pending += len(batch)
            continue

        for i, lead in enumerate(batch):
            verdict = results.get(i)
            if verdict is None:
                # Модель не вернула ответ по этому элементу — откладываем до
                # следующего прогона, а не пропускаем без проверки.
                lead.extra["ai_pending"] = True
                pending += 1
                continue
            is_client = bool(verdict.get("is_client"))
            try:
                confidence = float(verdict.get("confidence", 0))
            except (TypeError, ValueError):
                confidence = 0.0
            lead.extra["ai_is_client"] = is_client
            lead.extra["ai_confidence"] = confidence
            lead.extra["ai_reason"] = str(verdict.get("reason", ""))[:200]
            passed = is_client and confidence >= min_confidence
            # Построчный разбор в лог: видно, что ИИ пропустил, а что отсёк и
            # почему — чтобы находить ошибочно отсечённые настоящие заявки и
            # править промпт. Печатаются только новые (ещё не виденные) лиды.
            snippet = (lead.text or "").replace("\n", " ").strip()[:110]
            print(
                f"[ai_filter] {'ПРОШЁЛ' if passed else 'отсечён'} "
                f"({'клиент' if is_client else 'не клиент'}, {confidence:.2f}) "
                f"{lead.source_group or lead.source}: «{snippet}» — {lead.extra['ai_reason']}"
            )
            if passed:
                kept.append(lead)

    msg = f"[ai_filter] Из {len(leads)} лидов после ИИ-фильтра осталось {len(kept)}."
    if pending:
        msg += f" Не проверено (ошибка OpenRouter, повтор в следующем прогоне): {pending}."
    print(msg)
    return kept
