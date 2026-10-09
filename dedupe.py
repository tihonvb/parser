"""Compatibility adapter; new application code uses storage.Store directly."""

from pathlib import Path

from storage import Store


class SeenStore:
    def __init__(self, path):
        path = Path(path)
        self.store = Store(path.with_suffix(".sqlite3") if path.suffix == ".json" else path)
        if path.suffix == ".json":
            self.store.import_legacy(path)

    def is_new(self, key):
        return (
            not self.store.db.execute("SELECT 1 FROM legacy_seen WHERE key=?", (key,)).fetchone()
            and not self.store.db.execute(
                "SELECT 1 FROM leads WHERE key=? AND ai_state IN ('accepted','rejected')", (key,)
            ).fetchone()
        )

    def mark(self, key):
        self.store.db.execute("INSERT OR IGNORE INTO legacy_seen VALUES (?)", (key,))

    def save(self):
        # Writes are already transactional; there is no lossy set truncation.
        pass

    def close(self):
        self.store.close()
