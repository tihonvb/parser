# Результат реализации 20 задач

База изменений: upstream main `27807c6947c7d923100e5ba95a20ed14f4ba9ad2`. Проверки локальные, синтетические, без внешних API. Рабочий клиентский релиз выполняется следующим согласованным этапом.

| Issue | Реализация | Проверка / ограничение |
|---|---|---|
| [#1](https://github.com/tihonvb/parser/issues/1) | Игнорирование приватного состояния, atomic 0600, безопасные ошибки | `test_private_state_atomic_replacement_and_safe_logs`, `scripts/check_secrets.py` проверяет всю доступную Git-историю по известным сигнатурам |
| [#2](https://github.com/tihonvb/parser/issues/2) | Детерминированное объединение повторов и unique key SQLite | `test_merge_one_key_rich_metadata`, интеграционный pipeline |
| [#3](https://github.com/tihonvb/parser/issues/3) | Строгий JSON-контракт, missing/conflict → pending, отсутствие ключа → ConfigError | `tests/test_ai_config.py` |
| [#4](https://github.com/tihonvb/parser/issues/4) | SQLite inbox/outbox, durable payload/verdict/cursor, отдельная legacy history, backup | storage/restart/window/lease/backup тесты |
| [#5](https://github.com/tihonvb/parser/issues/5) | RAW, общий 13-колоночный контракт, backed-up migration/dry-run, безопасные импорты | schema/migration/phone/formula/import тесты; неизвестные legacy ID требуют сверки |
| [#6](https://github.com/tihonvb/parser/issues/6) | До HTTP резервируется фиксированная строка, retry использует тот же range | `test_timeout_after_sheet_commit_reuses_row_and_verdict`; физическое перемещение строк запрещено контрактом эксплуатации |
| [#7](https://github.com/tihonvb/parser/issues/7) | Очередь на получателя, зависимость Sheets, backoff/429/permanent failures | partial-recipient/ok/rate-limit тесты; неоднозначный таймаут Bot API допускает повтор |
| [#8](https://github.com/tihonvb/parser/issues/8) | Изоляция каналов, bounded timeout, FloodWait/cancellation, disconnect | `tests/test_telegram.py` |
| [#9](https://github.com/tihonvb/parser/issues/9) | Валидация/env/пути, lock, refresh/reload на цикл, статусы, systemd/cron | config/main/lock тесты; установки на рабочий сервер не было |
| [#10](https://github.com/tihonvb/parser/issues/10) | CPython 3.12–3.14, uv lock/runtime export/dev group, lint/tests/CI | Код подготовлен; **защита upstream main требует администратора и остаётся открытым критерием**. Payload: `branch-protection.json` |
| [#11](https://github.com/tihonvb/parser/issues/11) | 30 размеченных synthetic cases, offline/explicit online eval, metadata, reclassify, отдельные human labels | eval/stats/reclassification тесты; online качество выбранной модели ещё не измерено |
| [#12](https://github.com/tihonvb/parser/issues/12) | README, архитектура, CLI, миграция, восстановление, ограничения, релиз | Безопасный пример проходит `--check-config` и offline eval |
| [#13](https://github.com/tihonvb/parser/issues/13) | VK/TG пагинация, durable per-source progress, бюджеты, pinned/anchor/new front, global next_from | deep 250 VK / 300 TG, restart/page failure/global cursor тесты; поисковый индекс и первая страница Avito — выборки |
| [#14](https://github.com/tihonvb/parser/issues/14) | Unicode/whitespace, request + repair morphology, объяснимые причины и stage counters | Все положительные synthetic примеры достигают ИИ; лишние кандидаты измерены, качество живой модели этим не доказано |
| [#15](https://github.com/tihonvb/parser/issues/15) | Dynamic city/work type, ID/name/context, head+tail budget, полный оригинал в SQLite | prompt/tail/corpus тесты |
| [#16](https://github.com/tihonvb/parser/issues/16) | Описание до фильтра, canonical URL/ID, published vs observed, публичный телефон, source diagnostics | HTML fixtures, missing URL/date/layout/block тесты; актуальная вёрстка/доступ проверяются перед релизом |
| [#17](https://github.com/tihonvb/parser/issues/17) | Явное владение browser/context/pages/Dolphin, независимая очистка | borrowed CDP/connect failure/context failure тесты |
| [#18](https://github.com/tihonvb/parser/issues/18) | Raw execute_errors, выборочные ограниченные retry, size split/direct fallback, durable partial pages | raw subcalls/permission/size/page failure тесты |
| [#19](https://github.com/tihonvb/parser/issues/19) | Canonical source ID, rename-safe ranking/overrides, city-scoped discovery, unknown metadata, coverage | identical-name/rename/geography/member/depth тесты |
| [#20](https://github.com/tihonvb/parser/issues/20) | Единственный VK TokenManager, wrappers, legacy migration, expiry/rotation/lock, обязательный state/TTL | `tests/test_auth.py`; токены в captured logs отсутствуют |

Issues не закрываются вручную до review/merge. PR связывает реализации с задачами. #10 не помечается полностью выполненной без фактически установленной защиты main.

## Проверенный процесс разработки

После перехода на [слои и порты](architecture.md) локально проходят 124 теста, `ruff check` и `ruff format --check`, безопасный пример конфигурации, offline eval и сравнение runtime export с lock. Все прежние регрессионные сценарии сохранены. Дополнительные проверки защищают границы core/application, выполняют сценарии через in-memory ports, открывают SQLite schema v1 и проверяют атомарность маршрутов доставки. Собранные sdist/wheel проверяются установкой в отдельное окружение и запуском вне checkout; эти шаги добавлены в CI.

На синтетическом наборе ранний отбор: TP=14, FP=11, FN=0, TN=5; это полнота отбора кандидатов, а не качество языковой модели. Набор входит в пакет (`src/lead_parser/resources/evaluation.jsonl`), версионированный результат находится в `fixtures/prefilter-baseline.json`.

Первый [успешный Actions run](https://github.com/kwtpub/parser/actions/runs/37979969261) проверил исходный implementation commit на Python 3.12, 3.13 и 3.14. Последующие проверки актуальной ветки видны в [истории CI](https://github.com/kwtpub/parser/actions/workflows/checks.yml?query=branch%3Acodex%2Fcomplete-parser-issues). В upstream до включения/одобрения workflow администратором проверки не гарантированно появятся в PR; ссылки на проверки fork приложены к PR. Защита upstream и фактический запрет merge без review/check остаются частью #10, требующей владельца.
