"""Простое JSON-хранилище уже отправленных лидов, чтобы не дублировать строки
в таблице при повторных (в т.ч. по расписанию) запусках."""

from __future__ import annotations

import json
from pathlib import Path


class SeenStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._seen: set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self._seen = set(data.get("seen", []))
            except (json.JSONDecodeError, OSError):
                self._seen = set()

    def is_new(self, key: str) -> bool:
        return key not in self._seen

    def mark(self, key: str) -> None:
        self._seen.add(key)

    def save(self) -> None:
        # Не храним стор бесконечно — ограничиваем последними ~20000 ключами,
        # чтобы файл не рос вечно при долгой работе по расписанию.
        keys = list(self._seen)[-20000:]
        self.path.write_text(
            json.dumps({"seen": keys}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
