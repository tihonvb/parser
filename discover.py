"""Общая точка входа для «ресерч-агента»: ищет кандидатов в VK и Telegram
по городу из config.yaml (general.city) за один прогон.

Запуск:
    python discover.py

Ничего не добавляет в config.yaml и никуда не вступает/не подписывается
автоматически — только выгружает списки кандидатов
(vk_candidates.csv, telegram_candidates.csv) для ручного отбора. Почему
так — см. докстринги в vk_discovery.py / telegram_discovery.py.

Можно гонять по отдельности: python vk_discovery.py / python telegram_discovery.py
"""

from __future__ import annotations

from common import load_config


def _run_vk(cfg: dict) -> None:
    vk_cfg = cfg.get("vk", {})
    token = vk_cfg.get("access_token", "")
    if not token or "PUT_YOUR" in token:
        print("[discover] VK: access_token не заполнен в config.yaml — пропускаю поиск групп VK.")
        return

    import vk_discovery

    print("[discover] === Ищу группы VK ===")
    candidates = vk_discovery.find_candidates(cfg)
    vk_discovery.save_csv(candidates)
    print(f"[discover] VK: найдено {len(candidates)} кандидатов -> vk_candidates.csv\n")


def _run_telegram(cfg: dict) -> None:
    tg_cfg = cfg.get("telegram", {})
    api_id = str(tg_cfg.get("api_id", ""))
    if not api_id or "PUT_YOUR" in api_id:
        print("[discover] Telegram: api_id/api_hash не заполнены в config.yaml — пропускаю поиск каналов Telegram.")
        return

    import asyncio
    import telegram_discovery

    print("[discover] === Ищу каналы/чаты Telegram ===")
    candidates = asyncio.run(telegram_discovery.find_candidates(cfg))
    telegram_discovery.save_csv(candidates)
    print(f"[discover] Telegram: найдено {len(candidates)} кандидатов -> telegram_candidates.csv\n")


def main() -> None:
    cfg = load_config()
    city = cfg["general"].get("city", "Самара")
    print(f"[discover] Ищу источники для города «{city}»...\n")

    _run_vk(cfg)
    _run_telegram(cfg)

    print(
        "[discover] Готово. Откройте vk_candidates.csv / telegram_candidates.csv "
        "(или вывод выше), отберите подходящие источники и впишите их в "
        "config.yaml -> vk.group_ids / telegram.channels."
    )


if __name__ == "__main__":
    main()
