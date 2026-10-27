CREATE TABLE IF NOT EXISTS price_history (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 origin TEXT NOT NULL, destination TEXT NOT NULL, destination_city TEXT NOT NULL,
 country_code TEXT NOT NULL, outbound_date TEXT NOT NULL, return_date TEXT NOT NULL,
 price_usd REAL NOT NULL CHECK(price_usd > 0 AND price_usd <= 10000000),
 airlines TEXT NOT NULL DEFAULT '', layovers INTEGER NOT NULL DEFAULT 0 CHECK(layovers >= 0),
 source TEXT NOT NULL DEFAULT '', booking_url TEXT NOT NULL DEFAULT '',
 recorded_at TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'USD' CHECK(currency = 'USD'),
 metadata_scope TEXT NOT NULL DEFAULT 'outbound', observed_at TEXT
);
CREATE TABLE IF NOT EXISTS alert_logs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 origin TEXT NOT NULL, destination TEXT NOT NULL, destination_city TEXT NOT NULL DEFAULT '',
 outbound_date TEXT NOT NULL, return_date TEXT NOT NULL,
 alerted_price_usd REAL NOT NULL CHECK(alerted_price_usd > 0),
 average_price_usd REAL NOT NULL CHECK(average_price_usd > 0),
 discount_percent REAL NOT NULL, airlines TEXT NOT NULL DEFAULT '',
 layovers INTEGER NOT NULL DEFAULT 0, source TEXT NOT NULL DEFAULT '',
 booking_url TEXT NOT NULL DEFAULT '', sent_at TEXT,
 created_at TEXT, payload TEXT NOT NULL DEFAULT '{}', baseline_source TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS app_config (
 id INTEGER PRIMARY KEY CHECK(id = 1), value_json TEXT NOT NULL,
 version INTEGER NOT NULL CHECK(version > 0), updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipients (
 id INTEGER PRIMARY KEY AUTOINCREMENT, email TEXT NOT NULL UNIQUE COLLATE NOCASE,
 verified INTEGER NOT NULL DEFAULT 0 CHECK(verified IN (0,1)),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)),
 created_at TEXT NOT NULL, verified_at TEXT, disabled_at TEXT, last_digest_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 recipient_id INTEGER NOT NULL REFERENCES recipients(id),
 alert_id INTEGER REFERENCES alert_logs(id) ON DELETE SET NULL,
 kind TEXT NOT NULL CHECK(kind IN ('verification','deal','digest','status')),
 payload TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending'
   CHECK(status IN ('pending','sending','retry','sent','failed','canceled')),
 attempts INTEGER NOT NULL DEFAULT 0 CHECK(attempts >= 0),
 created_at TEXT NOT NULL, next_attempt_at TEXT NOT NULL,
 lease_token TEXT, lease_expires_at TEXT, sent_at TEXT,
 last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scan_jobs (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 status TEXT NOT NULL CHECK(status IN ('pending','running','succeeded','failed')),
 created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, heartbeat_at TEXT,
 lease_token TEXT, lease_expires_at TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 last_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS provider_requests (
 id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL, requested_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_results (
 provider TEXT PRIMARY KEY, calls INTEGER NOT NULL DEFAULT 0,
 successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
 offers INTEGER NOT NULL DEFAULT 0, last_success_at TEXT, last_failure_at TEXT,
 latency_ms REAL NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '', updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watched_flights (
 id INTEGER PRIMARY KEY AUTOINCREMENT, flight_iata TEXT NOT NULL,
 flight_date TEXT NOT NULL, status_json TEXT, updated_at TEXT,
 created_at TEXT NOT NULL, UNIQUE(flight_iata,flight_date)
);
CREATE TABLE IF NOT EXISTS audit_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
 subject_id INTEGER, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS service_leases (
 name TEXT PRIMARY KEY, lease_token TEXT NOT NULL, lease_expires_at TEXT NOT NULL
);
