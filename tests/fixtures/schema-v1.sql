-- Frozen schema from b610fcd, before web tables.
PRAGMA user_version=1;

            CREATE TABLE IF NOT EXISTS leads (
              key TEXT PRIMARY KEY, payload TEXT NOT NULL, content_hash TEXT NOT NULL,
              ai_state TEXT NOT NULL DEFAULT 'pending', ai_attempts INTEGER NOT NULL DEFAULT 0,
              ai_next_at REAL NOT NULL DEFAULT 0, verdict TEXT, ai_error TEXT,
              first_seen REAL NOT NULL, last_seen REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS legacy_seen (key TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS human_reviews (
              lead_key TEXT PRIMARY KEY REFERENCES leads(key), is_client INTEGER NOT NULL,
              reviewed REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
              id TEXT PRIMARY KEY, cursor TEXT NOT NULL DEFAULT '{}', report TEXT NOT NULL DEFAULT '{}',
              updated REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS deliveries (
              id INTEGER PRIMARY KEY AUTOINCREMENT, lead_key TEXT NOT NULL REFERENCES leads(key),
              kind TEXT NOT NULL, destination TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'pending',
              attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0,
              lease_owner TEXT, lease_until REAL, last_error TEXT, sheet_row INTEGER,
              depends_on INTEGER REFERENCES deliveries(id), created REAL NOT NULL, delivered REAL,
              UNIQUE(lead_key, kind, destination));
            CREATE UNIQUE INDEX IF NOT EXISTS sheet_slots ON deliveries(destination,sheet_row)
              WHERE kind='sheets' AND sheet_row IS NOT NULL;
            CREATE INDEX IF NOT EXISTS delivery_due ON deliveries(state,next_at);
            CREATE INDEX IF NOT EXISTS ai_due ON leads(ai_state,ai_next_at);
