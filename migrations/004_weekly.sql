CREATE TABLE weekly_deals (
 id INTEGER PRIMARY KEY AUTOINCREMENT, observed_at TEXT NOT NULL,
 origin TEXT NOT NULL, destination TEXT NOT NULL, price_usd REAL NOT NULL,
 payload TEXT NOT NULL
);
CREATE INDEX weekly_deals_observed ON weekly_deals(observed_at);
CREATE TABLE weekly_runs (week_end TEXT PRIMARY KEY, created_at TEXT NOT NULL);
