CREATE TABLE scan_batches (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 scan_job_id INTEGER UNIQUE REFERENCES scan_jobs(id) ON DELETE CASCADE,
 baseline_at TEXT NOT NULL, history_max_id INTEGER NOT NULL,
 finalized_at TEXT, result_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE scan_candidates (
 batch_id INTEGER NOT NULL REFERENCES scan_batches(id) ON DELETE CASCADE,
 origin TEXT NOT NULL, destination TEXT NOT NULL,
 outbound_date TEXT NOT NULL, return_date TEXT NOT NULL,
 price_usd REAL NOT NULL, offer_json TEXT NOT NULL,
 PRIMARY KEY(batch_id,origin,destination,outbound_date,return_date)
);
CREATE TABLE scan_baselines (
 batch_id INTEGER NOT NULL REFERENCES scan_batches(id) ON DELETE CASCADE,
 origin TEXT NOT NULL, destination TEXT NOT NULL, reference_usd REAL NOT NULL,
 samples INTEGER NOT NULL, observation_days INTEGER NOT NULL, date_pairs INTEGER NOT NULL,
 PRIMARY KEY(batch_id,origin,destination)
);
CREATE INDEX scan_candidates_destination ON scan_candidates(batch_id,destination);
CREATE INDEX price_history_route_time ON price_history(origin,destination,recorded_at);
CREATE INDEX deliveries_recipient_sent ON deliveries(recipient_id,status,sent_at);
