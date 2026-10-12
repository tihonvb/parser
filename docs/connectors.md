# Коннекторы и креды

Любая внешняя интеграция — **коннектор**: источник лидов (`source`), классификатор (`classifier`) или канал доставки (`delivery`). Каждый объявлен **один раз** в [`infrastructure/credentials.py`](../src/lead_parser/infrastructure/credentials.py) как `ConnectorSpec`: имя, секция конфигурации и список нужных кредов (`Credential`). Всё остальное выводится из этого объявления:

- переменные окружения для загрузки кредов (`ENV_MAP` в конфигурации);
- команды `lead-parser connectors status | check | env-template`;
- список секретов ниже и `.env.example` (согласованность проверяют тесты).

Поэтому код не зависит от того, **откуда** придут креды: сначала описывается контракт, потом значения кладутся в одно место.

## Порядок работы

1. Посмотреть, чего не хватает (значения не печатаются, только имена):

   ```bash
   lead-parser connectors status           # все интеграции: включена? готова? чего нет
   lead-parser connectors status --json    # то же для скриптов
   ```

2. Получить шаблон со всеми именами и пустыми значениями (существующий файл не перезаписывается, права `0600`):

   ```bash
   lead-parser connectors env-template --output .env
   ```

3. **Позже** вписать значения, которые у вас есть, в `.env` рядом с `config.yaml` (или передать переменными процесса) и включить нужные `enabled` в `config.yaml`.

4. Проверить, что включённые коннекторы обеспечены кредами (код выхода `2`, если нет):

   ```bash
   lead-parser connectors check
   ```

Приоритет значений: **переменные процесса → `.env` → `config.yaml`**. Пока креды не загружены, отключённый коннектор ничему не мешает; включённый без кредов отмечается как `НЕТ КРЕДОВ`, а полная проверка конфигурации (`--check-config`) его отвергнет.

## Креды по коннекторам

| Коннектор | Вид | Путь в конфиге | Переменная | Что это |
| --- | --- | --- | --- | --- |
| `telegram` | source | `telegram.api_id` | `TELEGRAM_API_ID` | api_id приложения с my.telegram.org (не секрет) |
| `telegram` | source | `telegram.api_hash` | `TELEGRAM_API_HASH` | api_hash приложения с my.telegram.org |
| `telegram` | source | `telegram.session_name` | — (только файл) | файл сессии аккаунта; создаётся один раз: lead-parser telegram-auth login (файл, `+.session`) |
| `vk` | source | `vk.access_token` | `VK_ACCESS_TOKEN` | токен VK (режим token_mode: static) |
| `vk` | source | `vk.token_file` | — (только файл) | файл токенов VK ID; создаётся: lead-parser vk-auth login (режим token_mode: vk_id) (файл) |
| `vk` | source | `vk.search_token` | `VK_SEARCH_TOKEN` | пользовательский токен для глобального поиска (необязательно) |
| `avito` | source | `avito` | — | коннектору кредов не нужно |
| `ai_filter` | classifier | `ai_filter.openrouter_api_key` | `OPENROUTER_API_KEY` | ключ OpenRouter |
| `google_sheets` | delivery | `output.google_sheets.credentials_file` | `GOOGLE_SERVICE_ACCOUNT_FILE` | JSON-ключ сервисного аккаунта Google (файл) |
| `google_sheets` | delivery | `output.google_sheets.spreadsheet_id` | `GOOGLE_SPREADSHEET_ID` | ID таблицы из её адреса (не секрет) |
| `telegram_bot` | delivery | `notifications.telegram.bot_token` | `TELEGRAM_BOT_TOKEN` | токен бота от @BotFather |
| `telegram_bot` | delivery | `notifications.telegram.chat_id` | `TELEGRAM_CHAT_ID` | id получателя (или список chat_ids в config.yaml) (не секрет) |

Файлы (сессия Telegram, токены VK ID) создаются командами авторизации (`telegram-auth login`, `vk-auth login`), а не вносятся в `.env`.

## Контракт коннектора

```text
ConnectorSpec(name, title, kind, section, credentials, docs)
  section      путь секции с флагом enabled, например "vk" или "notifications.telegram"
Credential(path, description, env, kind, suffix, required, secret, applies)
  path         путь значения в конфиге, например "telegram.api_id"
  env          переменная окружения, которая его заполняет (пусто — значение только из конфига/файла)
  kind         "value" или "file" (файл должен существовать; suffix — например ".session")
  required     False — необязательный кред (показывается отдельно)
  applies      условие, когда кред нужен (например, только token_mode: static)

SourceCollector = collect_leads(cfg, store=None, reports=None) -> list[Lead]
  источник сохраняет страницы через store.ingest/checkpoint и сообщает покрытие в reports (ScanResult)
```

## Как добавить коннектор

1. Реализовать `collect_leads(cfg, store=None, reports=None)` для источника (по образцу `integrations/vk/collector.py`) либо адаптер доставки/классификатора.
2. Добавить секцию с `enabled` в значения по умолчанию конфигурации (`DEFAULTS`) и валидацию.
3. Объявить `ConnectorSpec` с кредами в `infrastructure/credentials.py` и добавить его в `SPECS`.
4. Для источника — вписать модуль в `SOURCE_MODULES` в `bootstrap.py`.
5. Дописать переменные в `.env.example` (тесты сверяют его с реестром) и описание в README.

Тесты `tests/test_connectors.py` проверят, что у каждого источника есть объявление, что секции и пути существуют в конфигурации, что переменные не пересекаются и что `.env.example` не расходится с реестром.
