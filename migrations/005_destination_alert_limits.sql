-- Reservations are written before SMTP, so overlapping workers and ambiguous
-- acknowledgments cannot spend the same destination's allowance twice.
CREATE TABLE destination_alert_sends (
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 recipient_id INTEGER NOT NULL REFERENCES recipients(id),
 destination TEXT NOT NULL,
 delivery_id INTEGER REFERENCES deliveries(id) ON DELETE SET NULL,
 lease_token TEXT NOT NULL,
 attempted_at TEXT NOT NULL
);
CREATE INDEX destination_alert_sends_window
 ON destination_alert_sends(recipient_id,destination,attempted_at);
