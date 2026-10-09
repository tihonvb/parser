"""Точка входа: запускает все включённые источники, дедуплицирует лиды
и дописывает новые строки в Google Таблицу.

Разовый запуск:
    python main.py

Постоянный цикл с интервалом из config.yaml -> schedule.interval_minutes:
    python main.py --loop

Как альтернатива --loop, можно поставить `python main.py` в системный
cron / Планировщик заданий Windows — тогда каждый запуск процесса
завершается сам, что надёжнее долгоживущего цикла (см. README).
"""

from __future__ import annotations

import argparse
import sys
import time
import traceback

from common import Lead, load_config
from dedupe import SeenStore


def run_once(cfg: dict) -> None:
    all_leads: list[Lead] = []

    sources = [
        ("telegram", "telegram_parser"),
        ("vk", "vk_parser"),
        ("avito", "avito_parser"),
    ]

    for name, module_name in sources:
        if not cfg.get(name, {}).get("enabled", True):
            print(f"[main] Источник '{name}' выключен в конфиге — пропускаю.")
            continue
        try:
            module = __import__(module_name)
            leads = module.collect_leads(cfg)
            print(f"[main] {name}: найдено {len(leads)} потенциальных лидов.")
            all_leads.extend(leads)
        except Exception:
            print(f"[main] Ошибка в источнике '{name}':")
            traceback.print_exc()

    if not all_leads:
        print("[main] Новых лидов не найдено, таблицу не трогаю.")
        return

    store = SeenStore(cfg["storage"]["seen_store"])
    fresh = [lead for lead in all_leads if store.is_new(lead.dedupe_key())]

    if not fresh:
        print("[main] Все найденные лиды уже были отправлены ранее (дубликаты).")
        return

    try:
        import ai_filter

        kept = ai_filter.filter_leads(cfg, fresh)
    except Exception:
        print("[main] Ошибка в ai_filter — лиды не отправляю без проверки, повторю в следующем прогоне:")
        traceback.print_exc()
        for lead in fresh:
            lead.extra["ai_pending"] = True
        kept = []

    # Отфильтрованный ИИ мусор помечаем "видели" сразу — это не заказы,
    # пересматривать их смысла нет, а деньги на повторную классификацию
    # тратить не хочется.
    # Лиды с ai_pending (OpenRouter не ответил) не трогаем — они не
    # "отклонены", а просто не проверены, и проверятся в следующем прогоне.
    kept_keys = {lead.dedupe_key() for lead in kept}
    rejected = [
        lead for lead in fresh
        if lead.dedupe_key() not in kept_keys and not lead.extra.get("ai_pending")
    ]
    for lead in rejected:
        store.mark(lead.dedupe_key())
    store.save()

    if not kept:
        print("[main] После дедупликации/ИИ-фильтра новых лидов для таблицы не осталось.")
        return

    try:
        import sheets_writer

        added = sheets_writer.append_leads(cfg, kept)
        print(f"[main] Добавлено новых строк в таблицу: {added}")

        try:
            import telegram_notify

            telegram_notify.send_leads_notifications(cfg, kept)
        except Exception:
            print("[main] Не удалось отправить уведомления в Telegram (не критично):")
            traceback.print_exc()
    except Exception:
        print("[main] Ошибка при записи в Google Таблицу (проверьте service_account.json / spreadsheet_id / доступ):")
        traceback.print_exc()
        print(
            f"[main] Внимание: {len(kept)} потенциально реальных лидов НЕ записаны в таблицу "
            "и НЕ помечены как отправленные — при следующем запуске main.py они будут "
            "собраны и проверены ИИ-фильтром заново (не потеряются)."
        )
        return

    # Помечаем "видели" только те лиды, что реально долетели до таблицы —
    # иначе при сбое записи (как выше) лиды тихо терялись бы навсегда.
    for lead in kept:
        store.mark(lead.dedupe_key())
    store.save()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--loop", action="store_true", help="Крутиться в цикле с интервалом из config.yaml")
    args = parser.parse_args()

    cfg = load_config()

    if not args.loop:
        run_once(cfg)
        return

    interval = cfg.get("schedule", {}).get("interval_minutes", 60)
    print(f"[main] Запуск в цикле, интервал {interval} мин. Остановка — Ctrl+C.")
    while True:
        try:
            run_once(cfg)
        except KeyboardInterrupt:
            print("[main] Остановлено пользователем.")
            sys.exit(0)
        except Exception:
            print("[main] Непредвиденная ошибка в run_once:")
            traceback.print_exc()
        time.sleep(interval * 60)


if __name__ == "__main__":
    main()