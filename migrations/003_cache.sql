CREATE TABLE IF NOT EXISTS provider_cache (
 cache_key TEXT PRIMARY KEY, offers_json TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_provider_cache_age ON provider_cache(updated_at);
