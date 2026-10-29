"""Single-host SQLite persistence with atomic claims and per-recipient delivery.

Every operation owns its connection. External calls never run in a transaction.
SMTP delivery remains at-least-once if acceptance is followed by a crash.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import stat
import tempfile
import time
from typing import Any, Iterator

from db.migrations import SCHEMA_VERSION, migrate


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _iso(value: datetime | None = None) -> str:
    return (value or _now()).isoformat(timespec="microseconds")


def _json(value: Any) -> str:
    encoded = json.dumps(value, allow_nan=False, separators=(",", ":"), sort_keys=True)
    if len(encoded.encode("utf-8")) > 131072:
        raise ValueError("payload too large")
    return encoded


def _error(value: str) -> str:
    return value if re.fullmatch(r"[a-z][a-z0-9_]{0,63}", value or "") else "operation_failed"


def _email(value: str) -> str:
    # One canonical mailbox validator is shared with the delivery boundary.
    from notifier.emailer import normalize_email
    return normalize_email(value)


def _positive(value: Any) -> float:
    if isinstance(value, bool):
        raise ValueError("amount must be finite, positive, and bounded")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        raise ValueError("amount must be finite, positive, and bounded") from None
    if not math.isfinite(result) or not 0 < result <= 10000000:
        raise ValueError("amount must be finite, positive, and bounded")
    return result


def _offer_dict(offer: Any) -> dict:
    from fetcher.base import FlightOffer

    raw = asdict(offer) if is_dataclass(offer) else dict(offer)
    # Revalidate at the persistence boundary even when a caller bypasses the
    # dataclass constructor or mutates an already-validated instance.
    data = asdict(FlightOffer(**raw))
    _json(data)
    return data


class DatabaseManager:
    def __init__(self, database_url: str):
        if not isinstance(database_url, str) or not database_url.startswith("sqlite:///"):
            raise ValueError("only single-host sqlite:/// URLs are supported")
        location = database_url[len("sqlite:///"):]
        if not location or "?" in location or "#" in location:
            raise ValueError("invalid SQLite database path")
        self._keeper = None
        self.path = None
        self._uri = location == ":memory:"
        if self._uri:
            self._location = f"file:flight_alerts_{secrets.token_hex(16)}?mode=memory&cache=shared"
            self._keeper = self._connect()
        else:
            self.path = Path(location).absolute()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # SQLite would otherwise create a transient world-readable file
            # under a conventional umask before chmod could tighten it.
            flags = os.O_CREAT | os.O_EXCL | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            try:
                descriptor = os.open(self.path, flags, 0o600)
            except FileExistsError:
                if not stat.S_ISREG(self.path.lstat().st_mode):
                    raise ValueError("SQLite database must be a regular file") from None
            else:
                os.close(descriptor)
            os.chmod(self.path, 0o600)
            self._location = str(self.path)
        connection = self._connect()
        try:
            # Changing journal mode can return SQLITE_BUSY immediately when
            # another startup connection inspects the same new database. The
            # connection busy timeout does not cover every journal-mode race.
            for attempt in range(4):
                try:
                    connection.execute("PRAGMA journal_mode=WAL")
                    break
                except sqlite3.OperationalError as exc:
                    code = getattr(exc, "sqlite_errorcode", 0) & 0xff
                    if code not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or attempt == 3:
                        raise
                    time.sleep(0.01 * 2 ** attempt)
        finally:
            connection.close()
        with self._write() as connection:
            migrate(connection, _iso())

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._location, timeout=5, isolation_level=None, uri=self._uri)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("PRAGMA query_only=ON")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            for attempt in range(4):
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" not in str(exc).lower() or attempt == 3:
                        raise
                    time.sleep(0.01 * 2 ** attempt)
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def close(self) -> None:
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None

    @staticmethod
    def _owns_scan(connection, job_id, lease_token, now: str) -> bool:
        if job_id is None and lease_token is None:
            return True
        if type(job_id) is not int or job_id < 1 or not isinstance(lease_token, str) or not lease_token:
            return False
        return connection.execute(
            "SELECT 1 FROM scan_jobs WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?",
            (job_id, lease_token, now),
        ).fetchone() is not None

    @staticmethod
    def _owns_service(connection, lease_name, lease_token, now: str) -> bool:
        if lease_name is None and lease_token is None:
            return True
        if not isinstance(lease_name, str) or not isinstance(lease_token, str) or not lease_name or not lease_token:
            return False
        return connection.execute(
            "SELECT 1 FROM service_leases WHERE name=? AND lease_token=? AND lease_expires_at>?",
            (lease_name, lease_token, now),
        ).fetchone() is not None

    def get_config(self, default_config: dict) -> dict:
        with self._read() as connection:
            existing = connection.execute("SELECT value_json FROM app_config WHERE id=1").fetchone()
            if existing:
                return json.loads(existing[0])
        encoded = _json(default_config)
        with self._write() as connection:
            # Another process may have bootstrapped between the read and lock.
            existing = connection.execute("SELECT value_json FROM app_config WHERE id=1").fetchone()
            if existing:
                return json.loads(existing[0])
            now = _iso()
            connection.execute("INSERT INTO app_config VALUES(1,?,1,?)", (encoded, now))
            for address in default_config.get("filters", {}).get("recipient_emails", []):
                recipient = self._invite(connection, _email(address), now)
                self._enqueue(connection, recipient["id"], "verification", {},
                              f"bootstrap-verification:{recipient['id']}", now)
            return json.loads(encoded)

    def get_config_version(self) -> int:
        with self._read() as connection:
            row = connection.execute("SELECT version FROM app_config WHERE id=1").fetchone()
            return row[0] if row else 0

    def save_config(self, cfg: dict, expected_version: int) -> int:
        encoded = _json(cfg)
        with self._write() as connection:
            changed = connection.execute(
                "UPDATE app_config SET value_json=?,version=version+1,updated_at=? WHERE id=1 AND version=?",
                (encoded, _iso(), expected_version),
            ).rowcount
            if not changed:
                raise ValueError("configuration changed; reload and retry")
            return expected_version + 1

    def record_offer(self, offer: Any) -> None:
        self.record_offers([offer])

    def record_offers(self, offers: list) -> None:
        normalized = [_offer_dict(offer) for offer in offers]
        with self._write() as connection:
            now = _iso()
            connection.executemany(
                "INSERT INTO price_history(origin,destination,destination_city,country_code,"
                "outbound_date,return_date,price_usd,airlines,layovers,source,booking_url,"
                "recorded_at,currency,metadata_scope,observed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(o["origin"], o["destination"], o["destination_city"], o["country_code"],
                  o["outbound_date"], o["return_date"], o["price_usd"], ", ".join(o["airlines"]),
                  int(o["layovers"]), o["source"], o["booking_url"], now, "USD",
                  o["metadata_scope"], o["observed_at"]) for o in normalized],
            )

    def get_historical_route_stats(self, origin: str, destination: str, lookback_days: int = 90) -> tuple[float | None, int]:
        cutoff = _iso(_now() - timedelta(days=max(1, int(lookback_days))))
        with self._read() as connection:
            row = connection.execute(
                "SELECT AVG(price_usd),COUNT(*) FROM price_history WHERE origin=? AND destination=? "
                "AND recorded_at>=? AND currency='USD' AND price_usd>0 AND price_usd<=10000000",
                (origin, destination, cutoff),
            ).fetchone()
            return (float(row[0]) if row[0] is not None else None, row[1])

    @staticmethod
    def _duplicate(connection, origin, destination, outbound_date, return_date, current_price,
                   window_hours, improvement_usd, improvement_percent) -> bool:
        cutoff = _iso(_now() - timedelta(hours=max(0, float(window_hours))))
        row = connection.execute(
            "SELECT MIN(alerted_price_usd) FROM alert_logs WHERE origin=? AND destination=? "
            "AND COALESCE(created_at,sent_at)>=? AND alerted_price_usd>0 AND alerted_price_usd<=10000000",
            (origin, destination, cutoff),
        ).fetchone()
        previous = row[0]
        if previous is None:
            return False
        reduction = float(previous) - current_price
        improved = reduction > 0 and (
            reduction >= max(0.0, float(improvement_usd))
            or reduction / float(previous) * 100 >= max(0.0, float(improvement_percent))
        )
        return not improved

    def is_duplicate_alert(self, origin: str, destination: str, outbound_date: str,
                           return_date: str, current_price: float, window_hours: int = 48,
                           *, improvement_usd: float = 35, improvement_percent: float = 10) -> bool:
        current_price = _positive(current_price)
        with self._read() as connection:
            return self._duplicate(connection, origin, destination, outbound_date, return_date,
                                   current_price, window_hours, improvement_usd, improvement_percent)

    @staticmethod
    def _insert_alert(connection, offer, average_price, discount_percent, baseline_source, now):
        payload = {"offer": offer, "average_price_usd": average_price,
                   "discount_percent": discount_percent, "baseline_source": baseline_source}
        cursor = connection.execute(
            "INSERT INTO alert_logs(origin,destination,destination_city,outbound_date,return_date,"
            "alerted_price_usd,average_price_usd,discount_percent,airlines,layovers,source,booking_url,"
            "sent_at,created_at,payload,baseline_source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (offer["origin"], offer["destination"], offer["destination_city"], offer["outbound_date"],
             offer["return_date"], offer["price_usd"], average_price, discount_percent,
             ", ".join(offer["airlines"]), int(offer["layovers"]), offer["source"], offer["booking_url"],
             now, now, _json(payload), baseline_source),
        )
        # Legacy schemas require sent_at. It is a compatibility timestamp;
        # delivery outcomes/timestamps live exclusively in deliveries.
        return cursor.lastrowid, payload

    def reserve_alert(self, offer: Any, average_price: float, discount_percent: float,
                      baseline_source: str = "", window_hours: int = 48,
                      improvement_usd: float = 35, improvement_percent: float = 10,
                      *, scan_job_id: int | None = None, scan_lease_token: str | None = None) -> int | None:
        offer = _offer_dict(offer)
        average_price = _positive(average_price)
        if isinstance(discount_percent, bool) or not math.isfinite(float(discount_percent)) or not 0 <= float(discount_percent) <= 100:
            raise ValueError("invalid discount")
        with self._write() as connection:
            if not self._owns_scan(connection, scan_job_id, scan_lease_token, _iso()):
                return None
            config_row = connection.execute("SELECT value_json FROM app_config WHERE id=1").fetchone()
            if config_row and json.loads(config_row[0]).get("filters", {}).get("alerts_paused", False):
                return None
            recipients = connection.execute("SELECT id FROM recipients WHERE enabled=1 AND verified=1").fetchall()
            if not recipients:
                return None
            if self._duplicate(connection, offer["origin"], offer["destination"],
                               offer["outbound_date"], offer["return_date"], offer["price_usd"],
                               window_hours, improvement_usd, improvement_percent):
                return None
            now = _iso()
            alert_id, payload = self._insert_alert(connection, offer, average_price,
                                                    float(discount_percent), baseline_source, now)
            for recipient in recipients:
                self._enqueue(connection, recipient[0], "deal", payload,
                              f"deal:{alert_id}:{recipient[0]}", now, alert_id=alert_id)
            return alert_id

    def log_alert(self, offer: Any, average_price: float, discount_percent: float) -> None:
        offer = _offer_dict(offer)
        average_price = _positive(average_price)
        if isinstance(discount_percent, bool) or not math.isfinite(float(discount_percent)) or not 0 <= float(discount_percent) <= 100:
            raise ValueError("invalid discount")
        with self._write() as connection:
            self._insert_alert(connection, offer, average_price, float(discount_percent), "", _iso())

    def get_alert(self, alert_id: int) -> dict | None:
        with self._read() as connection:
            row = connection.execute("SELECT * FROM alert_logs WHERE id=?", (alert_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["payload"] = json.loads(result["payload"])
            return result

    @staticmethod
    def _recipient(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        result["verified"] = bool(result["verified"])
        result["enabled"] = bool(result["enabled"])
        return result

    @classmethod
    def _invite(cls, connection, email, now):
        existing = connection.execute("SELECT * FROM recipients WHERE email=?", (email,)).fetchone()
        if existing:
            return cls._recipient(existing)
        if connection.execute("SELECT COUNT(*) FROM recipients").fetchone()[0] >= 100:
            raise ValueError("recipient limit reached")
        connection.execute("INSERT OR IGNORE INTO recipients(email,created_at) VALUES(?,?)", (email, now))
        return cls._recipient(connection.execute("SELECT * FROM recipients WHERE email=?", (email,)).fetchone())

    def invite_recipient(self, email: str) -> dict:
        with self._write() as connection:
            now = _iso()
            recipient = self._invite(connection, _email(email), now)
            connection.execute("INSERT INTO audit_events(action,subject_id,created_at) VALUES('recipient_invited',?,?)",
                               (recipient["id"], now))
            return recipient

    def list_recipients(self) -> list[dict]:
        with self._read() as connection:
            return [self._recipient(row) for row in connection.execute("SELECT * FROM recipients ORDER BY id")]

    def get_recipient(self, recipient_id: int) -> dict | None:
        with self._read() as connection:
            return self._recipient(connection.execute("SELECT * FROM recipients WHERE id=?", (recipient_id,)).fetchone())

    def confirm_recipient(self, recipient_id: int) -> bool:
        with self._write() as connection:
            now = _iso()
            changed = connection.execute("UPDATE recipients SET verified=1,verified_at=COALESCE(verified_at,?) "
                                         "WHERE id=? AND enabled=1", (now, recipient_id)).rowcount
            if changed:
                connection.execute("INSERT INTO audit_events(action,subject_id,created_at) VALUES('recipient_confirmed',?,?)",
                                   (recipient_id, now))
            return bool(changed)

    def disable_recipient(self, recipient_id: int) -> bool:
        with self._write() as connection:
            now = _iso()
            changed = connection.execute("UPDATE recipients SET enabled=0,disabled_at=? WHERE id=?", (now, recipient_id)).rowcount
            connection.execute("UPDATE deliveries SET status='canceled',lease_token=NULL,lease_expires_at=NULL,"
                               "last_error='recipient_disabled' WHERE recipient_id=? AND status IN ('pending','retry','sending')",
                               (recipient_id,))
            if changed:
                connection.execute("INSERT INTO audit_events(action,subject_id,created_at) VALUES('recipient_disabled',?,?)",
                                   (recipient_id, now))
            return bool(changed)

    @staticmethod
    def _enqueue(connection, recipient_id, kind, payload, idempotency_key, now, alert_id=None):
        if kind not in {"verification", "deal", "digest", "status"}:
            raise ValueError("invalid notification kind")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 256:
            raise ValueError("invalid idempotency key")
        recipient = connection.execute("SELECT enabled,verified FROM recipients WHERE id=?", (recipient_id,)).fetchone()
        if recipient is None or not recipient[0] or (kind != "verification" and not recipient[1]):
            return False
        return bool(connection.execute(
            "INSERT OR IGNORE INTO deliveries(recipient_id,alert_id,kind,payload,idempotency_key,created_at,next_attempt_at) "
            "VALUES(?,?,?,?,?,?,?)", (recipient_id, alert_id, kind, _json(payload), idempotency_key, now, now),
        ).rowcount)

    def enqueue_notification(self, recipient_id: int, kind: str, payload: dict, idempotency_key: str) -> bool:
        with self._write() as connection:
            return self._enqueue(connection, recipient_id, kind, payload, idempotency_key, _iso())

    def claim_deliveries(self, limit: int = 20, lease_seconds: int = 300, digest_mode: bool = False) -> list[dict]:
        limit = min(100, max(1, int(limit)))
        now = _iso()
        until = _iso(_now() + timedelta(seconds=max(1, int(lease_seconds))))
        with self._write() as connection:
            connection.execute(
                "UPDATE deliveries SET status='canceled',lease_token=NULL,lease_expires_at=NULL,last_error='recipient_disabled' "
                "WHERE status IN ('pending','retry','sending') AND recipient_id IN (SELECT id FROM recipients WHERE enabled=0)"
            )
            connection.execute(
                "UPDATE deliveries SET status='failed',lease_token=NULL,lease_expires_at=NULL,last_error='lease_expired' "
                "WHERE attempts>=5 AND (status IN ('pending','retry') OR (status='sending' AND lease_expires_at<=?))", (now,),
            )
            candidates = connection.execute(
                "SELECT d.* FROM deliveries d JOIN recipients r ON r.id=d.recipient_id "
                "WHERE r.enabled=1 AND (r.verified=1 OR d.kind='verification') "
                "AND d.attempts<5 AND ((d.status IN ('pending','retry') AND d.next_attempt_at<=?) "
                "OR (d.status='sending' AND d.lease_expires_at<=?)) "
                "AND (?=0 OR d.kind!='deal' OR NOT EXISTS (SELECT 1 FROM deliveries busy "
                "WHERE busy.recipient_id=d.recipient_id AND busy.kind='deal' AND busy.status='sending' AND busy.lease_expires_at>?)) "
                "ORDER BY d.created_at,d.id LIMIT ?", (now, now, int(bool(digest_mode)), now, limit),
            ).fetchall()
            chosen = []
            grouped = set()
            for row in candidates:
                recipient_id = row["recipient_id"]
                if digest_mode and row["kind"] == "deal":
                    if recipient_id in grouped:
                        continue
                    grouped.add(recipient_id)
                    busy = connection.execute(
                        "SELECT 1 FROM deliveries WHERE recipient_id=? AND kind='deal' "
                        "AND status='sending' AND lease_expires_at>? LIMIT 1", (recipient_id, now),
                    ).fetchone()
                    if busy:
                        continue
                    rows = connection.execute(
                        "SELECT * FROM deliveries WHERE recipient_id=? AND kind='deal' "
                        "AND attempts<5 AND ((status IN ('pending','retry') AND next_attempt_at<=?) "
                        "OR (status='sending' AND lease_expires_at<=?)) ORDER BY created_at,id LIMIT 100",
                        (recipient_id, now, now),
                    ).fetchall()
                else:
                    rows = [row]
                token = secrets.token_urlsafe(32)
                for candidate in rows:
                    connection.execute("UPDATE deliveries SET status='sending',attempts=attempts+1,lease_token=?,lease_expires_at=? WHERE id=?",
                                       (token, until, candidate["id"]))
                    claimed = dict(connection.execute(
                        "SELECT d.*,r.email FROM deliveries d JOIN recipients r ON r.id=d.recipient_id WHERE d.id=?",
                        (candidate["id"],),
                    ).fetchone())
                    claimed["payload"] = json.loads(claimed["payload"])
                    chosen.append(claimed)
            return chosen

    @staticmethod
    def _delivery_update(connection, delivery_id, lease_token, success, error, retry_after_seconds, max_attempts, now=None):
        now = now or _iso()
        row = connection.execute("SELECT * FROM deliveries WHERE id=? AND status='sending' AND lease_token=? AND lease_expires_at>?",
                                 (delivery_id, lease_token, now)).fetchone()
        if row is None:
            return False
        status = "sent" if success else ("failed" if row["attempts"] >= min(5, max(1, int(max_attempts))) else "retry")
        connection.execute("UPDATE deliveries SET status=?,sent_at=?,last_error=?,next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL WHERE id=?",
                           (status, now if success else None, "" if success else _error(error),
                            _iso(_now() + timedelta(seconds=max(0, int(retry_after_seconds)))), delivery_id))
        return True

    def update_delivery(self, delivery_id: int, lease_token: str, success: bool, error: str = "",
                        retry_after_seconds: int = 60, max_attempts: int = 5) -> bool:
        with self._write() as connection:
            return self._delivery_update(connection, delivery_id, lease_token, success, error, retry_after_seconds, max_attempts)

    def renew_delivery(self, delivery_id: int, lease_token: str, lease_seconds: int = 300) -> bool:
        return self.renew_delivery_group([delivery_id], lease_token, lease_seconds)

    def renew_delivery_group(self, delivery_ids: list[int], lease_token: str, lease_seconds: int = 300) -> bool:
        """Renew all owned digest rows together; never resurrect expired leases."""
        if (not delivery_ids or len(delivery_ids) > 100 or len(set(delivery_ids)) != len(delivery_ids)
                or any(type(identifier) is not int or identifier < 1 for identifier in delivery_ids)):
            return False
        now = _iso()
        until = _iso(_now() + timedelta(seconds=max(1, min(3600, int(lease_seconds)))))
        with self._write() as connection:
            placeholders = ",".join("?" for _ in delivery_ids)
            owned = connection.execute(
                f"SELECT COUNT(*) FROM deliveries WHERE id IN ({placeholders}) AND status='sending' "
                "AND lease_token=? AND lease_expires_at>?",
                (*delivery_ids, lease_token, now),
            ).fetchone()[0]
            if owned != len(delivery_ids):
                return False
            connection.execute(
                f"UPDATE deliveries SET lease_expires_at=? WHERE id IN ({placeholders})",
                (until, *delivery_ids),
            )
            return True

    def update_delivery_group(self, delivery_ids: list[int], lease_token: str, success: bool, error: str = "",
                              retry_after_seconds: int = 60, max_attempts: int = 5, digest: bool = False) -> bool:
        if not delivery_ids or len(delivery_ids) > 100 or len(set(delivery_ids)) != len(delivery_ids):
            return False
        with self._write() as connection:
            now = _iso()
            recipients = set()
            for delivery_id in delivery_ids:
                row = connection.execute("SELECT recipient_id FROM deliveries WHERE id=? AND status='sending' AND lease_token=? AND lease_expires_at>?",
                                         (delivery_id, lease_token, now)).fetchone()
                if row is None:
                    return False
                recipients.add(row[0])
            for delivery_id in delivery_ids:
                if not self._delivery_update(connection, delivery_id, lease_token, success, error, retry_after_seconds, max_attempts, now=now):
                    raise RuntimeError("delivery group ownership changed")
            if success and digest:
                for recipient_id in recipients:
                    connection.execute("UPDATE recipients SET last_digest_at=? WHERE id=?", (now, recipient_id))
            return bool(delivery_ids)

    def defer_delivery(self, delivery_id: int, lease_token: str, retry_after_seconds: int) -> bool:
        with self._write() as connection:
            return bool(connection.execute(
                "UPDATE deliveries SET status='pending',attempts=MAX(0,attempts-1),next_attempt_at=?,lease_token=NULL,lease_expires_at=NULL "
                "WHERE id=? AND status='sending' AND lease_token=? AND lease_expires_at>?",
                (_iso(_now() + timedelta(seconds=max(1, int(retry_after_seconds)))), delivery_id, lease_token, _iso()),
            ).rowcount)

    def cancel_delivery(self, delivery_id: int, lease_token: str, reason: str = "recipient_disabled") -> bool:
        with self._write() as connection:
            return bool(connection.execute("UPDATE deliveries SET status='canceled',last_error=?,lease_token=NULL,lease_expires_at=NULL "
                                           "WHERE id=? AND status='sending' AND lease_token=? AND lease_expires_at>?",
                                           (_error(reason), delivery_id, lease_token, _iso())).rowcount)

    def enqueue_scan(self, cooldown_seconds: int = 300) -> int | None:
        with self._write() as connection:
            if connection.execute("SELECT 1 FROM scan_jobs WHERE status IN ('pending','running') LIMIT 1").fetchone():
                return None
            latest = connection.execute("SELECT MAX(created_at) FROM scan_jobs").fetchone()[0]
            if latest and latest > _iso(_now() - timedelta(seconds=max(0, int(cooldown_seconds)))):
                return None
            return connection.execute("INSERT INTO scan_jobs(status,created_at) VALUES('pending',?)", (_iso(),)).lastrowid

    def claim_scan(self, lease_seconds: int = 7200) -> dict | None:
        now = _iso()
        with self._write() as connection:
            connection.execute("UPDATE scan_jobs SET status='failed',finished_at=?,last_error='lease_exhausted',lease_token=NULL,lease_expires_at=NULL "
                               "WHERE attempts>=5 AND (status='pending' OR (status='running' AND lease_expires_at<=?))", (now, now))
            if connection.execute("SELECT 1 FROM scan_jobs WHERE status='running' AND lease_expires_at>? LIMIT 1", (now,)).fetchone():
                return None
            row = connection.execute("SELECT id FROM scan_jobs WHERE attempts<5 AND (status='pending' OR (status='running' AND lease_expires_at<=?)) "
                                     "ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
            if row is None:
                return None
            token = secrets.token_urlsafe(32)
            connection.execute("UPDATE scan_jobs SET status='running',lease_token=?,lease_expires_at=?,started_at=COALESCE(started_at,?),"
                               "heartbeat_at=?,attempts=attempts+1 WHERE id=?",
                               (token, _iso(_now() + timedelta(seconds=max(1, int(lease_seconds)))), now, now, row[0]))
            return dict(connection.execute("SELECT * FROM scan_jobs WHERE id=?", (row[0],)).fetchone())

    def renew_scan(self, job_id: int, lease_token: str, lease_seconds: int) -> bool:
        with self._write() as connection:
            return bool(connection.execute("UPDATE scan_jobs SET lease_expires_at=?,heartbeat_at=? WHERE id=? AND status='running' "
                                           "AND lease_token=? AND lease_expires_at>?",
                                           (_iso(_now() + timedelta(seconds=max(1, int(lease_seconds)))), _iso(), job_id, lease_token, _iso())).rowcount)

    def finish_scan(self, job_id: int, lease_token: str, success: bool, error: str = "") -> bool:
        with self._write() as connection:
            return bool(connection.execute("UPDATE scan_jobs SET status=?,finished_at=?,heartbeat_at=?,last_error=?,lease_token=NULL,lease_expires_at=NULL "
                                           "WHERE id=? AND status='running' AND lease_token=? AND lease_expires_at>?",
                                           ("succeeded" if success else "failed", _iso(), _iso(), "" if success else _error(error),
                                            job_id, lease_token, _iso())).rowcount)

    def acquire_lease(self, name: str, lease_seconds: int = 300) -> str | None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise ValueError("invalid lease name")
        with self._write() as connection:
            now = _iso()
            row = connection.execute("SELECT lease_expires_at FROM service_leases WHERE name=?", (name,)).fetchone()
            if row and row[0] > now:
                return None
            token = secrets.token_urlsafe(32)
            connection.execute("INSERT INTO service_leases(name,lease_token,lease_expires_at) VALUES(?,?,?) "
                               "ON CONFLICT(name) DO UPDATE SET lease_token=excluded.lease_token,lease_expires_at=excluded.lease_expires_at",
                               (name, token, _iso(_now() + timedelta(seconds=max(1, int(lease_seconds))))))
            return token

    def release_lease(self, name: str, token: str) -> bool:
        with self._write() as connection:
            return bool(connection.execute("DELETE FROM service_leases WHERE name=? AND lease_token=?", (name, token)).rowcount)

    def renew_lease(self, name: str, token: str, lease_seconds: int = 300) -> bool:
        with self._write() as connection:
            return bool(connection.execute("UPDATE service_leases SET lease_expires_at=? WHERE name=? AND lease_token=? AND lease_expires_at>?",
                                           (_iso(_now() + timedelta(seconds=max(1, int(lease_seconds)))), name, token, _iso())).rowcount)

    def provider_budget(self, provider: str, per_minute: int, daily_budget: int) -> bool:
        if not isinstance(provider, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", provider):
            raise ValueError("invalid provider")
        now = _now()
        minute = _iso(now - timedelta(seconds=60))
        day = _iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
        with self._write() as connection:
            minute_count = connection.execute("SELECT COUNT(*) FROM provider_requests WHERE provider=? AND requested_at>?", (provider, minute)).fetchone()[0]
            day_count = connection.execute("SELECT COUNT(*) FROM provider_requests WHERE provider=? AND requested_at>=?", (provider, day)).fetchone()[0]
            if minute_count >= max(0, int(per_minute)) or day_count >= max(0, int(daily_budget)):
                return False
            connection.execute("INSERT INTO provider_requests(provider,requested_at) VALUES(?,?)", (provider, _iso(now)))
            connection.execute("DELETE FROM provider_requests WHERE requested_at<?", (_iso(now - timedelta(days=2)),))
            return True

    def record_provider_result(self, provider: str, success: bool, latency_ms: float = 0, error: str = "", offers: int = 0) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", provider) or not math.isfinite(float(latency_ms)):
            raise ValueError("invalid provider result")
        now = _iso()
        with self._write() as connection:
            connection.execute(
                "INSERT INTO provider_results(provider,calls,successes,failures,offers,last_success_at,last_failure_at,latency_ms,last_error,updated_at) "
                "VALUES(?,1,?,?,?,?,?,?,?,?) ON CONFLICT(provider) DO UPDATE SET calls=calls+1,successes=successes+excluded.successes,"
                "failures=failures+excluded.failures,offers=offers+excluded.offers,last_success_at=COALESCE(excluded.last_success_at,last_success_at),"
                "last_failure_at=COALESCE(excluded.last_failure_at,last_failure_at),latency_ms=excluded.latency_ms,last_error=excluded.last_error,updated_at=excluded.updated_at",
                (provider, int(bool(success)), int(not success), max(0, int(offers)), now if success else None,
                 None if success else now, max(0.0, float(latency_ms)), "" if success else _error(error), now),
            )

    def dashboard_snapshot(self, active_codes: list, freshness_hours: int = 24,
                           active_origins: list | None = None) -> dict:
        # freshness_hours is retained for caller compatibility; delivery freshness
        # does not determine which historical discounts appear on the dashboard.
        now = _now()
        cutoff = _iso(now - timedelta(days=90))
        codes = sorted({code for code in active_codes if re.fullmatch(r"[A-Z]{3}", str(code))})[:500]
        with self._read() as connection:
            alerts = [dict(row) for row in connection.execute("SELECT a.*,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.alert_id=a.id AND d.status='sent') AS sent_count,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.alert_id=a.id AND d.status IN ('pending','retry','sending')) AS pending_count,"
                "(SELECT COUNT(*) FROM deliveries d WHERE d.alert_id=a.id AND d.status='failed') AS failed_count "
                "FROM alert_logs a ORDER BY a.created_at DESC,a.id DESC LIMIT 20")]
            for alert in alerts:
                alert["payload"] = json.loads(alert["payload"])
            recent, stats = [], []
            if codes:
                placeholders = ",".join("?" for _ in codes)
                origins = sorted({code for code in (active_origins or [])
                                  if re.fullmatch(r"[A-Z]{3}", str(code))})[:10]
                origin_filter = ""
                parameters = [*codes, cutoff, _iso(now)]
                if active_origins is not None:
                    if origins:
                        origin_filter = " AND origin IN (" + ",".join("?" for _ in origins) + ")"
                        parameters.extend(origins)
                    else:
                        origin_filter = " AND 0"
                # Include historical travel dates in the reference, normalize UTC
                # offsets, and exclude invalid/future observations and prices.
                history = (
                    "WITH history AS (SELECT *,julianday(COALESCE(observed_at,recorded_at)) AS observed_jd "
                    f"FROM price_history WHERE destination IN ({placeholders}) "
                    "AND julianday(COALESCE(observed_at,recorded_at)) BETWEEN julianday(?) AND julianday(?) "
                    "AND currency='USD' AND price_usd>0 AND price_usd<=10000000"
                    + origin_filter + "), route_averages AS ("
                    "SELECT origin,destination,COUNT(*) AS samples,AVG(price_usd) AS avg_price "
                    "FROM history GROUP BY origin,destination) "
                )
                recent = [dict(row) for row in connection.execute(
                    history + ",latest AS (SELECT *,ROW_NUMBER() OVER ("
                    "PARTITION BY origin,destination,outbound_date,return_date "
                    "ORDER BY observed_jd DESC,id DESC) AS observation_rank FROM history) "
                    "SELECT f.*,r.avg_price AS average_price_usd,r.samples,"
                    "(1.0-f.price_usd/r.avg_price)*100.0 AS discount_percent "
                    "FROM latest f JOIN route_averages r USING(origin,destination) "
                    "WHERE f.observation_rank=1 AND f.outbound_date>=? "
                    "AND f.price_usd<=r.avg_price*0.5 "
                    "ORDER BY discount_percent DESC,f.observed_jd DESC,f.id DESC LIMIT 12",
                    [*parameters, now.date().isoformat()])]
                for row in recent:
                    row["airlines"] = row["airlines"].split(", ") if row["airlines"] else []
                stats = [dict(row) for row in connection.execute(
                    history + "SELECT * FROM route_averages ORDER BY origin,destination LIMIT 100",
                    parameters)]
            jobs = [dict(row) for row in connection.execute("SELECT id,status,created_at,started_at,finished_at,heartbeat_at,last_error,attempts "
                                                          "FROM scan_jobs ORDER BY id DESC LIMIT 20")]
            deliveries = [dict(row) for row in connection.execute("SELECT d.id,d.recipient_id,r.email,d.kind,d.status,d.attempts,"
                "d.created_at,d.next_attempt_at,d.sent_at,d.last_error FROM deliveries d JOIN recipients r ON r.id=d.recipient_id "
                "ORDER BY d.id DESC LIMIT 50")]
            providers = [dict(row) for row in connection.execute("SELECT * FROM provider_results ORDER BY provider LIMIT 20")]
            return {"alerts": alerts, "recent_deals": recent, "route_stats": stats,
                    "jobs": jobs, "deliveries": deliveries, "providers": providers}

    def cleanup(self, retention_days: int = 180) -> int:
        cutoff = _iso(_now() - timedelta(days=max(1, int(retention_days))))
        with self._write() as connection:
            removed = 0
            for table, predicate in (
                ("price_history", "recorded_at<?"),
                ("deliveries", "created_at<? AND status IN ('sent','failed','canceled')"),
                ("alert_logs", "created_at<? AND NOT EXISTS (SELECT 1 FROM deliveries WHERE alert_id=alert_logs.id)"),
                ("scan_jobs", "created_at<? AND status IN ('succeeded','failed')"),
                ("audit_events", "created_at<?"),
                ("provider_requests", "requested_at<?"),
            ):
                removed += connection.execute(f"DELETE FROM {table} WHERE {predicate}", (cutoff,)).rowcount
            removed += connection.execute("DELETE FROM provider_cache WHERE updated_at<?", (_iso(_now() - timedelta(days=1)),)).rowcount
            removed += connection.execute("DELETE FROM service_leases WHERE lease_expires_at<?", (_iso(),)).rowcount
            return removed

    def watch_flight(self, flight_iata: str, flight_date: str) -> int:
        flight_iata = flight_iata.strip().upper()
        if not re.fullmatch(r"[A-Z0-9]{2}[0-9]{1,4}[A-Z]?", flight_iata):
            raise ValueError("invalid flight number")
        if date.fromisoformat(flight_date).isoformat() != flight_date:
            raise ValueError("invalid flight date")
        with self._write() as connection:
            existing = connection.execute("SELECT id FROM watched_flights WHERE flight_iata=? AND flight_date=?", (flight_iata, flight_date)).fetchone()
            if existing:
                return existing[0]
            if connection.execute("SELECT COUNT(*) FROM watched_flights").fetchone()[0] >= 25:
                raise ValueError("watched flight limit reached")
            connection.execute("INSERT OR IGNORE INTO watched_flights(flight_iata,flight_date,created_at) VALUES(?,?,?)",
                               (flight_iata, flight_date, _iso()))
            return connection.execute("SELECT id FROM watched_flights WHERE flight_iata=? AND flight_date=?", (flight_iata, flight_date)).fetchone()[0]

    def list_watched_flights(self) -> list[dict]:
        with self._read() as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM watched_flights ORDER BY flight_date,flight_iata LIMIT 1000")]
            for row in rows:
                serialized = row.pop("status_json")
                row["status"] = json.loads(serialized) if serialized else None
            return rows

    def update_flight_status(self, watch_id: int, status: dict, *,
                             lease_name: str | None = None, lease_token: str | None = None) -> bool:
        serialized = _json(status)
        meaningful = ("status", "delay_minutes", "departure_gate", "arrival_gate")
        with self._write() as connection:
            if not self._owns_service(connection, lease_name, lease_token, _iso()):
                return False
            row = connection.execute("SELECT * FROM watched_flights WHERE id=?", (watch_id,)).fetchone()
            if row is None:
                return False
            previous = json.loads(row["status_json"]) if row["status_json"] else None
            changed = previous is not None and any(previous.get(key) != status.get(key) for key in meaningful)
            version = row["status_version"] + int(changed)
            now = _iso()
            connection.execute("UPDATE watched_flights SET status_json=?,updated_at=?,status_version=? WHERE id=?", (serialized, now, version, watch_id))
            config = connection.execute("SELECT value_json FROM app_config WHERE id=1").fetchone()
            paused = bool(json.loads(config[0]).get("filters", {}).get("alerts_paused", False)) if config else False
            if changed and not paused:
                payload = {"flight_iata": row["flight_iata"], "flight_date": row["flight_date"],
                           "previous": previous, "current": status}
                recipients = connection.execute("SELECT id FROM recipients WHERE enabled=1 AND verified=1").fetchall()
                for recipient in recipients:
                    self._enqueue(connection, recipient[0], "status", payload,
                                  f"status:{watch_id}:{version}:{recipient[0]}", now)
            return changed

    def get_provider_cache(self, key: str, ttl_seconds: int = 300) -> list[dict] | None:
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("invalid cache key")
        with self._read() as connection:
            row = connection.execute("SELECT offers_json FROM provider_cache WHERE cache_key=? AND updated_at>=?",
                                     (key, _iso(_now() - timedelta(seconds=max(0, int(ttl_seconds)))))).fetchone()
            return json.loads(row[0]) if row else None

    def set_provider_cache(self, key: str, offers: list[dict], *,
                           lease_name: str | None = None, lease_token: str | None = None) -> bool:
        if not isinstance(key, str) or not 1 <= len(key) <= 256:
            raise ValueError("invalid cache key")
        if not isinstance(offers, list) or len(offers) > 5000:
            raise ValueError("invalid offer cache")
        allowed = {"origin", "destination", "destination_city", "country_code", "outbound_date",
                   "return_date", "price_usd", "airlines", "layovers", "source", "booking_url",
                   "provider_typical_price_usd", "observed_at", "currency", "metadata_scope"}
        normalized = [{name: value for name, value in _offer_dict(item).items() if name in allowed} for item in offers]
        encoded = json.dumps(normalized, allow_nan=False, separators=(",", ":"), sort_keys=True)
        if len(encoded.encode("utf-8")) > 2 * 1024 * 1024:
            raise ValueError("offer cache too large")
        with self._write() as connection:
            if not self._owns_service(connection, lease_name, lease_token, _iso()):
                return False
            connection.execute("INSERT INTO provider_cache VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET offers_json=excluded.offers_json,updated_at=excluded.updated_at",
                               (key, encoded, _iso()))
            return True

    def delete_watch(self, watch_id: int) -> bool:
        with self._write() as connection:
            return bool(connection.execute("DELETE FROM watched_flights WHERE id=?", (watch_id,)).rowcount)

    def health(self) -> dict:
        with self._read() as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise RuntimeError("database schema is not ready")
            times = connection.execute("SELECT MAX(started_at),MAX(finished_at),MAX(CASE WHEN status='succeeded' THEN finished_at END) FROM scan_jobs").fetchone()
            return {"ready": version == SCHEMA_VERSION, "schema_version": version,
                    "last_scan_started_at": times[0], "last_scan_completed_at": times[1], "last_scan_success_at": times[2]}

    def backup(self, destination: str | Path) -> str:
        target = Path(destination).absolute()
        if target == self.path:
            raise ValueError("backup must use a separate destination")
        if target.exists() or target.is_symlink():
            raise ValueError("backup destination already exists")
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor, temporary = tempfile.mkstemp(prefix=".flight-backup-", dir=target.parent)
        os.close(descriptor)
        try:
            with self._read() as source:
                with sqlite3.connect(temporary) as backup:
                    source.backup(backup)
                    if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise RuntimeError("backup integrity check failed")
            os.chmod(temporary, 0o600)
            # An exclusive hard link publishes the checked snapshot atomically
            # without overwriting a destination created during the backup.
            os.link(temporary, target)
            return str(target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
