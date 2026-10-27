CREATE INDEX IF NOT EXISTS ix_route_observed ON price_history(origin,destination,recorded_at);
CREATE INDEX IF NOT EXISTS ix_fresh_offers ON price_history(recorded_at,outbound_date);
CREATE INDEX IF NOT EXISTS ix_alert_route_created ON alert_logs(origin,destination,created_at);
CREATE INDEX IF NOT EXISTS ix_delivery_due ON deliveries(status,next_attempt_at);
CREATE INDEX IF NOT EXISTS ix_delivery_recipient ON deliveries(recipient_id,status,kind);
CREATE INDEX IF NOT EXISTS ix_scan_status ON scan_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS ix_provider_request_window ON provider_requests(provider,requested_at);
CREATE TRIGGER IF NOT EXISTS validate_price_history_insert BEFORE INSERT ON price_history
WHEN NEW.price_usd IS NULL OR NEW.price_usd <= 0 OR NEW.price_usd > 10000000 OR NEW.currency != 'USD'
BEGIN SELECT RAISE(ABORT,'invalid offer amount'); END;
CREATE TRIGGER IF NOT EXISTS validate_price_history_update BEFORE UPDATE ON price_history
WHEN NEW.price_usd IS NULL OR NEW.price_usd <= 0 OR NEW.price_usd > 10000000 OR NEW.currency != 'USD'
BEGIN SELECT RAISE(ABORT,'invalid offer amount'); END;
