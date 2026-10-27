"""Real SQLite integration, migration and concurrent-claim regression tests.

No provider or SMTP calls. Process tests use distinct connections and the same
on-disk WAL database, so assertions exercise persistence rather than fake locks.
"""
from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import multiprocessing
import os
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch

from db.crud import DatabaseManager
from db.migrations import SCHEMA_VERSION
from fetcher.base import FlightOffer

UTC = timezone.utc
NOW = datetime(2026, 10, 27, 12, 0)


def fare(price=300, *, destination="LHR", observed_at=None, offset=40):
    departure = datetime.now(UTC).date() + timedelta(days=offset)
    return FlightOffer(
        origin="SLC", destination=destination, destination_city="London", country_code="GB",
        outbound_date=departure.isoformat(), return_date=(departure + timedelta(days=7)).isoformat(),
        price_usd=price, airlines=["Example"], layovers=0, source="Test",
        booking_url="https://www.google.com/travel/flights", observed_at=observed_at,
    )


def _reserve_process(arguments):
    url, payload = arguments
    manager = DatabaseManager(url)
    try:
        return manager.reserve_alert(payload, 1000, 70)
    finally:
        manager.close()


def _budget_process(arguments):
    url, count = arguments
    manager = DatabaseManager(url)
    try:
        return sum(manager.provider_budget("serpapi", 7, 100) for _ in range(count))
    finally:
        manager.close()


def _claim_process(arguments):
    url, kind = arguments
    manager = DatabaseManager(url)
    try:
        if kind == "scan":
            row = manager.claim_scan()
            return row["id"] if row else None
        rows = manager.claim_deliveries(limit=20)
        return [row["id"] for row in rows]
    finally:
        manager.close()


class DatabaseCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="flight-db-test-")
        self.path = Path(self.temporary.name) / "alerts.sqlite"
        self.url = "sqlite:///" + str(self.path)
        self.db = DatabaseManager(self.url)
        self.default = {"filters": {"recipient_emails": [], "alerts_paused": False}}
        self.db.get_config(self.default)

    def tearDown(self):
        self.db.close()
        self.temporary.cleanup()

    def sql(self, statement, args=()):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            cursor = connection.execute(statement, args)
            return [dict(row) for row in cursor.fetchall()]

    def verified(self, email="person@example.com"):
        recipient = self.db.invite_recipient(email)
        self.assertTrue(self.db.confirm_recipient(recipient["id"]))
        return self.db.get_recipient(recipient["id"])

    def notification(self, recipient=None, *, kind="deal", key="event"):
        recipient = recipient or self.verified()
        self.assertTrue(self.db.enqueue_notification(recipient["id"], kind, {"offer": asdict(fare())}, key))
        return recipient


class MigrationAndStorageTests(DatabaseCase):
    def test_idempotent_startup_and_secure_file_mode(self):
        self.assertTrue(self.db.health()["ready"])
        self.assertEqual(self.db.health()["schema_version"], SCHEMA_VERSION)
        other = DatabaseManager(self.url)
        other.close()
        self.assertEqual(len(self.sql("SELECT * FROM schema_migrations")), SCHEMA_VERSION)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.sql("PRAGMA journal_mode")[0]["journal_mode"], "wal")

    def test_health_rejects_incompatible_schema(self):
        self.sql("PRAGMA user_version=999")
        with self.assertRaises(RuntimeError):
            self.db.health()

    def test_non_sqlite_urls_and_query_components_rejected(self):
        for value in ("postgresql://example/db", "sqlite://", "sqlite:///", "sqlite:///data.db?mode=ro"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                DatabaseManager(value)

    def test_in_memory_database_survives_operation_connections(self):
        manager = DatabaseManager("sqlite:///:memory:")
        try:
            recipient = manager.invite_recipient("one@example.com")
            self.assertEqual(manager.get_recipient(recipient["id"])["email"], "one@example.com")
            self.assertTrue(manager.health()["ready"])
        finally:
            manager.close()

    def test_database_target_symlink_and_nonregular_files_rejected(self):
        target = Path(self.temporary.name) / "target.sqlite"
        target.write_text("must not be modified")
        link = Path(self.temporary.name) / "linked.sqlite"
        link.symlink_to(target)
        with self.assertRaises(ValueError):
            DatabaseManager("sqlite:///" + str(link))
        self.assertEqual(target.read_text(), "must not be modified")
        directory = Path(self.temporary.name) / "directory.sqlite"
        directory.mkdir()
        with self.assertRaises(ValueError):
            DatabaseManager("sqlite:///" + str(directory))

    def test_database_is_created_privately_before_first_sqlite_connect(self):
        new = Path(self.temporary.name) / "private.sqlite"
        connect = sqlite3.connect
        modes = []
        def inspect_mode(path, *args, **kwargs):
            if str(path) == str(new):
                modes.append(new.stat().st_mode & 0o777)
            return connect(path, *args, **kwargs)
        with patch("db.crud.sqlite3.connect", side_effect=inspect_mode):
            manager = DatabaseManager("sqlite:///" + str(new))
            manager.close()
        self.assertTrue(modes)
        self.assertEqual(set(modes), {0o600})

    def test_newer_schema_is_rejected_without_modifying_version(self):
        self.sql("INSERT INTO schema_migrations(version,applied_at) VALUES(999,'future')")
        with self.assertRaises(RuntimeError):
            DatabaseManager(self.url)
        self.assertEqual(self.sql("SELECT MAX(version) AS v FROM schema_migrations")[0]["v"], 999)

    def test_migration_failure_rolls_back_all_steps(self):
        fresh = Path(self.temporary.name) / "failed.sqlite"
        with patch("db.migrations._script", side_effect=RuntimeError("synthetic")):
            with self.assertRaises(RuntimeError):
                DatabaseManager("sqlite:///" + str(fresh))
        with sqlite3.connect(fresh) as connection:
            self.assertFalse(connection.execute("SELECT 1 FROM sqlite_master WHERE name='schema_migrations'").fetchall())
        manager = DatabaseManager("sqlite:///" + str(fresh))
        manager.close()

    def test_legacy_original_schema_preserves_data_and_normalizes_dates(self):
        legacy = Path(self.temporary.name) / "legacy.sqlite"
        recent = (datetime.now(UTC) - timedelta(hours=1)).replace(tzinfo=None).isoformat(sep=" ", timespec="microseconds")
        offer = asdict(fare())
        with sqlite3.connect(legacy) as connection:
            connection.executescript('''
                CREATE TABLE price_history (id INTEGER PRIMARY KEY, origin VARCHAR(3) NOT NULL,
                  destination VARCHAR(3) NOT NULL,destination_city VARCHAR(100) NOT NULL,
                  country_code VARCHAR(2) NOT NULL,outbound_date VARCHAR(10) NOT NULL,
                  return_date VARCHAR(10) NOT NULL,price_usd FLOAT NOT NULL,
                  airlines VARCHAR(255) NOT NULL,layovers INTEGER NOT NULL,
                  source VARCHAR(50) NOT NULL,booking_url VARCHAR(1024) NOT NULL,
                  recorded_at DATETIME);
                CREATE TABLE alert_logs (id INTEGER PRIMARY KEY,origin VARCHAR(3) NOT NULL,
                  destination VARCHAR(3) NOT NULL,outbound_date VARCHAR(10) NOT NULL,
                  return_date VARCHAR(10) NOT NULL,alerted_price_usd FLOAT NOT NULL,
                  average_price_usd FLOAT NOT NULL,discount_percent FLOAT NOT NULL,
                  source VARCHAR(50) NOT NULL,sent_at DATETIME NOT NULL);
            ''')
            connection.execute("INSERT INTO price_history VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "SLC", "LHR", "London", "GB", offer["outbound_date"], offer["return_date"],
                300, "Example", 0, "Legacy", offer["booking_url"], recent,
            ))
            connection.execute("INSERT INTO alert_logs VALUES(1,?,?,?,?,?,?,?,?,?)", (
                "SLC", "LHR", offer["outbound_date"], offer["return_date"], 300, 1000, 70, "Legacy", recent,
            ))
            for identifier, price in ((2, 0), (3, -10), (4, float("inf"))):
                connection.execute("INSERT INTO price_history SELECT ?,origin,destination,destination_city,country_code,"
                                   "outbound_date,return_date,?,airlines,layovers,source,booking_url,recorded_at "
                                   "FROM price_history WHERE id=1", (identifier, price))
        manager = DatabaseManager("sqlite:///" + str(legacy))
        try:
            average, count = manager.get_historical_route_stats("SLC", "LHR")
            self.assertEqual((average, count), (300, 1))
            self.assertTrue(manager.is_duplicate_alert("SLC", "LHR", offer["outbound_date"], offer["return_date"], 300))
            self.assertIn("T", manager.dashboard_snapshot(["LHR"])["recent_deals"][0]["recorded_at"])
            with sqlite3.connect(legacy) as connection:
                connection.execute("INSERT INTO alert_logs(origin,destination,outbound_date,return_date,alerted_price_usd,"
                                   "average_price_usd,discount_percent,source,sent_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                                   ("SLC", "LHR", offer["outbound_date"], offer["return_date"], -1, 1000, 100, "Legacy", recent.replace(" ", "T"), recent.replace(" ", "T")))
            self.assertFalse(manager.is_duplicate_alert("SLC", "LHR", offer["outbound_date"], offer["return_date"], 200))
            manager.record_offer(fare(250))
            self.assertEqual(manager.get_historical_route_stats("SLC", "LHR")[1], 2)
            with sqlite3.connect(legacy) as connection:
                # Bad legacy values remain available for offline investigation
                # while all active baselines and displayed fares exclude them.
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 5)
            manager.invite_recipient("new@example.com")
        finally:
            manager.close()

    def test_invalid_new_offers_cannot_bypass_dataclass_validation(self):
        for field, value in (("price_usd", True), ("price_usd", -1), ("price_usd", float("nan")),
                             ("price_usd", float("inf")), ("booking_url", "javascript:alert(1)"),
                             ("currency", "EUR"), ("return_date", "2000-01-01"), ("country_code", "x")):
            data = asdict(fare())
            data[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                self.db.record_offer(data)
        self.assertEqual(self.sql("SELECT COUNT(*) AS n FROM price_history")[0]["n"], 0)

    def test_database_insert_trigger_rejects_invalid_amounts(self):
        self.db.record_offer(fare())
        for value in (0, -2, float("inf")):
            with self.subTest(value=value), self.assertRaises(sqlite3.IntegrityError):
                self.sql("UPDATE price_history SET price_usd=?", (value,))


class SettingsAndRecipientsTests(DatabaseCase):
    def test_bootstrap_pending_recipients_and_verification_are_idempotent(self):
        manager = DatabaseManager("sqlite:///:memory:")
        try:
            source = {"filters": {"recipient_emails": ["first@example.com", "FIRST@example.com"]}}
            manager.get_config(source)
            manager.get_config({"filters": {"recipient_emails": ["not-imported@example.com"]}})
            recipients = manager.list_recipients()
            self.assertEqual(len(recipients), 1)
            self.assertFalse(recipients[0]["verified"])
            self.assertEqual(len(manager.claim_deliveries()), 1)
        finally:
            manager.close()

    def test_settings_compare_and_swap_prevents_lost_updates(self):
        version = self.db.get_config_version()
        original = self.db.get_config(self.default)
        self.db.save_config({"filters": {"alerts_paused": True}}, version)
        with self.assertRaises(ValueError):
            self.db.save_config(original, version)
        self.assertTrue(self.db.get_config(self.default)["filters"]["alerts_paused"])

    def test_config_read_does_not_request_writer_lock(self):
        with patch.object(self.db, "_write", side_effect=AssertionError("read became writer")):
            self.assertEqual(self.db.get_config(self.default), self.default)

    def test_mailbox_validation_and_case_dedup(self):
        recipient = self.db.invite_recipient(" Person@EXAMPLE.COM ")
        self.assertEqual(self.db.invite_recipient("person@example.com")["id"], recipient["id"])
        for value in ("a@example.com,b@example.com", "Name <a@example.com>", "a@-bad.example", "a..b@example.com", "x\r\n@example.com"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.invite_recipient(value)

    def test_unverified_and_disabled_recipients_cannot_get_alerts(self):
        recipient = self.db.invite_recipient("pending@example.com")
        self.assertFalse(self.db.enqueue_notification(recipient["id"], "deal", {}, "unverified"))
        self.assertTrue(self.db.enqueue_notification(recipient["id"], "verification", {}, "verification"))
        self.db.confirm_recipient(recipient["id"])
        self.assertTrue(self.db.enqueue_notification(recipient["id"], "deal", {}, "verified"))
        self.db.disable_recipient(recipient["id"])
        self.assertFalse(self.db.confirm_recipient(recipient["id"]))
        self.assertFalse(self.db.invite_recipient("pending@example.com")["enabled"])
        self.assertFalse(self.db.claim_deliveries())
        self.assertEqual({row["status"] for row in self.sql("SELECT status FROM deliveries")}, {"canceled"})

    def test_paused_settings_block_alert_reservations_transactionally(self):
        self.verified()
        self.db.save_config({"filters": {"alerts_paused": True}}, self.db.get_config_version())
        self.assertIsNone(self.db.reserve_alert(fare(), 1000, 70))
        self.assertFalse(self.sql("SELECT id FROM alert_logs"))


class AlertAndDeliveryTests(DatabaseCase):
    def test_no_recipients_means_no_cooldown_reservation(self):
        self.assertIsNone(self.db.reserve_alert(fare(), 1000, 70))
        self.verified()
        self.assertIsNotNone(self.db.reserve_alert(fare(), 1000, 70))

    def test_same_itinerary_can_alert_on_material_price_improvement(self):
        self.verified()
        self.assertIsNotNone(self.db.reserve_alert(fare(400), 1000, 60))
        self.assertIsNone(self.db.reserve_alert(fare(390), 1000, 61))
        self.assertIsNotNone(self.db.reserve_alert(fare(360), 1000, 64))
        self.assertIsNone(self.db.reserve_alert(fare(355, offset=60), 1000, 64.5))
        self.assertIsNotNone(self.db.reserve_alert(fare(300, offset=60), 1000, 70))

    def test_expired_or_stolen_scan_lease_cannot_create_alert_intent(self):
        self.verified()
        self.db.enqueue_scan()
        first = self.db.claim_scan()
        self.sql("UPDATE scan_jobs SET lease_expires_at='2000-01-01T00:00:00'")
        self.assertIsNone(self.db.reserve_alert(fare(), 1000, 70,
            scan_job_id=first["id"], scan_lease_token=first["lease_token"]))
        new = self.db.claim_scan()
        self.assertIsNone(self.db.reserve_alert(fare(), 1000, 70,
            scan_job_id=first["id"], scan_lease_token=first["lease_token"]))
        self.assertFalse(self.sql("SELECT id FROM alert_logs"))
        self.assertIsNotNone(self.db.reserve_alert(fare(), 1000, 70,
            scan_job_id=new["id"], scan_lease_token=new["lease_token"]))

    def test_one_event_creates_one_private_delivery_per_verified_recipient(self):
        first, second = self.verified(), self.verified("second@example.com")
        self.db.invite_recipient("pending@example.com")
        event = self.db.reserve_alert(fare(), 1000, 70, baseline_source="Configured")
        deliveries = self.db.claim_deliveries()
        self.assertEqual({d["recipient_id"] for d in deliveries}, {first["id"], second["id"]})
        self.assertEqual(len(deliveries), 2)
        self.assertEqual({d["alert_id"] for d in deliveries}, {event})
        self.assertEqual(deliveries[0]["payload"]["baseline_source"], "Configured")

    def test_notification_idempotency_is_enforced_in_database(self):
        recipient = self.verified()
        self.assertTrue(self.db.enqueue_notification(recipient["id"], "deal", {}, "one-key"))
        self.assertFalse(self.db.enqueue_notification(recipient["id"], "deal", {}, "one-key"))
        self.assertEqual(len(self.db.claim_deliveries()), 1)

    def test_partial_recipient_failure_has_independent_delivery_state(self):
        self.verified()
        self.verified("other@example.com")
        self.db.reserve_alert(fare(), 1000, 70)
        rows = self.db.claim_deliveries()
        self.assertTrue(self.db.update_delivery(rows[0]["id"], rows[0]["lease_token"], True))
        self.assertTrue(self.db.update_delivery(rows[1]["id"], rows[1]["lease_token"], False, "smtp_temporary", retry_after_seconds=0))
        retry = self.db.claim_deliveries()
        self.assertEqual([r["id"] for r in retry], [rows[1]["id"]])
        self.assertEqual(retry[0]["attempts"], 2)

    def test_expired_delivery_can_be_reclaimed_but_old_owner_cannot_ack(self):
        self.notification()
        first = self.db.claim_deliveries(lease_seconds=10)
        self.assertEqual(len(first), 1)
        old = first[0]
        self.sql("UPDATE deliveries SET lease_expires_at='2000-01-01T00:00:00'")
        self.assertFalse(self.db.update_delivery(old["id"], old["lease_token"], True))
        self.assertFalse(self.db.renew_delivery(old["id"], old["lease_token"]))
        new = self.db.claim_deliveries()[0]
        self.assertNotEqual(old["lease_token"], new["lease_token"])
        self.assertEqual(new["attempts"], 2)
        self.assertFalse(self.db.update_delivery(old["id"], old["lease_token"], True))
        self.assertTrue(self.db.update_delivery(new["id"], new["lease_token"], True))

    def test_repeated_worker_crashes_reach_terminal_failure(self):
        self.notification()
        for attempt in range(1, 6):
            row = self.db.claim_deliveries()[0]
            self.assertEqual(row["attempts"], attempt)
            self.sql("UPDATE deliveries SET lease_expires_at='2000-01-01T00:00:00'")
        self.assertFalse(self.db.claim_deliveries())
        self.assertEqual(self.sql("SELECT status,attempts FROM deliveries")[0], {"status": "failed", "attempts": 5})

    def test_deferral_does_not_consume_smtp_attempt_and_wrong_token_is_rejected(self):
        self.notification()
        row = self.db.claim_deliveries()[0]
        self.assertFalse(self.db.defer_delivery(row["id"], "wrong", 1))
        self.assertTrue(self.db.defer_delivery(row["id"], row["lease_token"], 1))
        self.assertEqual(self.sql("SELECT attempts,status FROM deliveries")[0], {"attempts": 0, "status": "pending"})
        self.assertFalse(self.db.claim_deliveries())

    def test_digest_claim_group_renewal_and_outcome_are_atomic(self):
        recipient = self.verified()
        for i in range(3):
            self.db.enqueue_notification(recipient["id"], "deal", {"offer": asdict(fare(300-i))}, f"deal:{i}")
        rows = self.db.claim_deliveries(limit=1, digest_mode=True)
        self.assertEqual(len(rows), 3)
        token = rows[0]["lease_token"]
        self.assertEqual({r["lease_token"] for r in rows}, {token})
        ids = [r["id"] for r in rows]
        self.assertFalse(self.db.renew_delivery_group([*ids, 9999], token))
        self.assertTrue(self.db.renew_delivery_group(ids, token))
        self.assertFalse(self.db.update_delivery_group([*ids, 9999], token, True, digest=True))
        self.assertEqual({r["status"] for r in self.sql("SELECT status FROM deliveries")}, {"sending"})
        self.assertTrue(self.db.update_delivery_group(ids, token, True, digest=True))
        self.assertEqual({r["status"] for r in self.sql("SELECT status FROM deliveries")}, {"sent"})
        self.assertIsNotNone(self.db.get_recipient(recipient["id"])["last_digest_at"])
        self.assertFalse(self.db.update_delivery_group([ids[0], ids[0]], token, True))

    def test_busy_digest_recipient_does_not_starve_another(self):
        first = self.verified()
        self.notification(first, key="first")
        self.db.claim_deliveries(limit=1, digest_mode=True)
        self.notification(first, key="second")
        second = self.verified("second@example.com")
        self.notification(second, key="third")
        rows = self.db.claim_deliveries(limit=1, digest_mode=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recipient_id"], second["id"])


class ScanQuotaCacheStatusTests(DatabaseCase):
    def test_scan_queue_shared_cooldown_and_ownership(self):
        identifier = self.db.enqueue_scan()
        self.assertIsNotNone(identifier)
        self.assertIsNone(self.db.enqueue_scan(cooldown_seconds=0))
        job = self.db.claim_scan()
        self.assertIsNone(self.db.claim_scan())
        self.assertFalse(self.db.renew_scan(job["id"], "wrong", 300))
        self.assertTrue(self.db.renew_scan(job["id"], job["lease_token"], 300))
        self.assertFalse(self.db.finish_scan(job["id"], "wrong", True))
        self.assertTrue(self.db.finish_scan(job["id"], job["lease_token"], True))
        self.assertIsNone(self.db.enqueue_scan())
        self.assertIsNotNone(self.db.enqueue_scan(cooldown_seconds=0))

    def test_scan_crash_recovery_has_finite_retry_budget(self):
        self.db.enqueue_scan()
        for attempt in range(1, 6):
            job = self.db.claim_scan()
            self.assertEqual(job["attempts"], attempt)
            self.sql("UPDATE scan_jobs SET lease_expires_at='2000-01-01T00:00:00'")
        self.assertIsNone(self.db.claim_scan())
        self.assertEqual(self.sql("SELECT status,last_error FROM scan_jobs")[0], {"status": "failed", "last_error": "lease_exhausted"})
        self.assertIsNotNone(self.db.enqueue_scan(cooldown_seconds=0))

    def test_service_lease_cas_and_expiry(self):
        token = self.db.acquire_lease("status-poll")
        self.assertIsNotNone(token)
        self.assertIsNone(self.db.acquire_lease("status-poll"))
        self.assertFalse(self.db.release_lease("status-poll", "wrong"))
        self.assertTrue(self.db.renew_lease("status-poll", token))
        self.sql("UPDATE service_leases SET lease_expires_at='2000-01-01T00:00:00'")
        self.assertFalse(self.db.renew_lease("status-poll", token))
        new = self.db.acquire_lease("status-poll")
        self.assertNotEqual(token, new)
        self.assertFalse(self.db.release_lease("status-poll", token))
        self.assertTrue(self.db.release_lease("status-poll", new))

    def test_minute_and_daily_budget_boundaries_are_persistent(self):
        with patch("db.crud._now", return_value=NOW):
            self.assertTrue(self.db.provider_budget("serpapi", 2, 3))
            self.assertTrue(self.db.provider_budget("serpapi", 2, 3))
            self.assertFalse(self.db.provider_budget("serpapi", 2, 3))
        with patch("db.crud._now", return_value=NOW + timedelta(seconds=61)):
            self.assertTrue(self.db.provider_budget("serpapi", 2, 3))
            self.assertFalse(self.db.provider_budget("serpapi", 2, 3))
        with patch("db.crud._now", return_value=NOW + timedelta(days=1)):
            self.assertTrue(self.db.provider_budget("serpapi", 2, 3))
        with self.assertRaises(ValueError):
            self.db.provider_budget("untrusted:quota", 2, 3)

    def test_provider_metrics_sanitize_errors_and_count_healthy_empty(self):
        self.db.record_provider_result("serpapi", False, error="private@example.com SECRET")
        self.db.record_provider_result("serpapi", True, offers=0)
        result = self.db.dashboard_snapshot([])["providers"][0]
        self.assertEqual((result["calls"], result["successes"], result["failures"]), (2, 1, 1))
        self.assertEqual(result["last_error"], "")
        self.assertIsNotNone(result["last_success_at"])

    def test_stale_service_owner_cannot_overwrite_cache_or_status(self):
        self.verified()
        watch = self.db.watch_flight("AA123", datetime.now(UTC).date().isoformat())
        first = self.db.acquire_lease("status-test")
        self.assertTrue(self.db.set_provider_cache("query", [asdict(fare(300))], lease_name="status-test", lease_token=first))
        self.assertFalse(self.db.update_flight_status(watch, {"status": "scheduled"}, lease_name="status-test", lease_token=first))
        self.sql("UPDATE service_leases SET lease_expires_at='2000-01-01T00:00:00'")
        second = self.db.acquire_lease("status-test")
        self.assertFalse(self.db.set_provider_cache("query", [asdict(fare(100))], lease_name="status-test", lease_token=first))
        self.assertFalse(self.db.update_flight_status(watch, {"status": "active"}, lease_name="status-test", lease_token=first))
        self.assertEqual(self.db.get_provider_cache("query")[0]["price_usd"], 300)
        self.assertEqual(self.db.list_watched_flights()[0]["status"]["status"], "scheduled")
        self.assertFalse(self.sql("SELECT id FROM deliveries"))
        self.assertTrue(self.db.set_provider_cache("query", [asdict(fare(200))], lease_name="status-test", lease_token=second))
        self.assertTrue(self.db.update_flight_status(watch, {"status": "active"}, lease_name="status-test", lease_token=second))
        self.assertEqual(len(self.sql("SELECT id FROM deliveries")), 1)

    def test_cache_retains_utc_domain_values_and_expires(self):
        payload = asdict(fare())
        with patch("db.crud._now", return_value=NOW):
            self.db.set_provider_cache("query", [payload])
            self.assertIsNotNone(FlightOffer(**self.db.get_provider_cache("query")[0]))
        with patch("db.crud._now", return_value=NOW + timedelta(seconds=301)):
            self.assertIsNone(self.db.get_provider_cache("query", 300))
        payload["price_usd"] = float("inf")
        with self.assertRaises(ValueError):
            self.db.set_provider_cache("bad", [payload])

    def test_freshness_future_travel_and_active_route_filters(self):
        self.db.record_offer(fare(300))
        self.db.record_offer(fare(250, destination="CDG"))
        self.db.record_offer(fare(200, observed_at=(datetime.now(UTC)-timedelta(days=3)).isoformat()))
        self.db.record_offer(fare(100, offset=-10))
        self.assertFalse(self.db.dashboard_snapshot([])["recent_deals"])
        rows = self.db.dashboard_snapshot(["LHR"])["recent_deals"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["price_usd"], 300)
        self.assertEqual(self.db.dashboard_snapshot(["LHR"])["route_stats"][0]["samples"], 1)

    def test_status_transition_and_notifications_are_atomic_and_idempotent(self):
        recipient = self.verified()
        self.db.invite_recipient("pending@example.com")
        watch = self.db.watch_flight("AA123", datetime.now(UTC).date().isoformat())
        first = {"status": "scheduled", "delay_minutes": None, "departure_gate": None, "arrival_gate": None}
        self.assertFalse(self.db.update_flight_status(watch, first))
        self.assertFalse(self.sql("SELECT id FROM deliveries"))
        self.assertFalse(self.db.update_flight_status(watch, {**first, "observed_at": "changed-only-time"}))
        changed = {**first, "departure_gate": "B12"}
        self.assertTrue(self.db.update_flight_status(watch, changed))
        self.assertFalse(self.db.update_flight_status(watch, changed))
        rows = self.db.claim_deliveries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["recipient_id"], recipient["id"])
        self.assertEqual(rows[0]["payload"]["previous"]["status"], "scheduled")
        self.assertEqual(rows[0]["payload"]["current"]["departure_gate"], "B12")
        self.assertTrue(self.db.update_flight_status(watch, first))
        self.assertEqual(len(self.sql("SELECT id FROM deliveries")), 2)
        self.assertEqual(self.db.list_watched_flights()[0]["status_version"], 2)

    def test_paused_status_change_updates_state_without_queueing(self):
        self.verified()
        watch = self.db.watch_flight("AA123", datetime.now(UTC).date().isoformat())
        self.db.update_flight_status(watch, {"status": "scheduled"})
        self.db.save_config({"filters": {"alerts_paused": True}}, self.db.get_config_version())
        self.assertTrue(self.db.update_flight_status(watch, {"status": "active"}))
        self.assertFalse(self.sql("SELECT id FROM deliveries"))
        self.assertEqual(self.db.list_watched_flights()[0]["status"]["status"], "active")

    def test_cleanup_keeps_pending_deliveries_and_deletes_expired_terminal_data(self):
        recipient = self.verified()
        self.db.record_offer(fare())
        event = self.db.reserve_alert(fare(), 1000, 70)
        self.sql("UPDATE price_history SET recorded_at='2000-01-01T00:00:00'")
        self.sql("UPDATE alert_logs SET created_at='2000-01-01T00:00:00'")
        self.sql("UPDATE deliveries SET created_at='2000-01-01T00:00:00'")
        self.assertGreater(self.db.cleanup(30), 0)
        self.assertIsNotNone(self.db.get_alert(event))
        self.assertEqual(len(self.db.claim_deliveries()), 1)
        row = self.db.claim_deliveries()
        self.assertFalse(row)
        self.assertEqual(self.db.get_recipient(recipient["id"])["enabled"], True)

    def test_consistent_backup_keeps_committed_data(self):
        self.verified()
        destination = Path(self.temporary.name) / "snapshot.sqlite"
        result = self.db.backup(destination)
        self.assertEqual(result, str(destination))
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        with sqlite3.connect(destination) as connection:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM recipients").fetchone()[0], 1)
        with self.assertRaises(ValueError):
            self.db.backup(self.path)
        with self.assertRaises(ValueError):
            self.db.backup(destination)


class ConcurrentDatabaseTests(DatabaseCase):
    def test_concurrent_initializers_apply_migrations_once(self):
        new_path = Path(self.temporary.name) / "fresh-race.sqlite"
        new_url = "sqlite:///" + str(new_path)
        barrier = threading.Barrier(8)
        def initialize(_):
            barrier.wait()
            manager = DatabaseManager(new_url)
            try:
                return manager.health()["schema_version"]
            finally:
                manager.close()
        with ThreadPoolExecutor(max_workers=8) as pool:
            self.assertEqual(list(pool.map(initialize, range(8))), [SCHEMA_VERSION] * 8)
        with sqlite3.connect(new_path) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0], SCHEMA_VERSION)

    def test_threads_cannot_reserve_same_alert_twice(self):
        self.verified()
        barrier = threading.Barrier(8)
        def reserve(_):
            barrier.wait()
            return self.db.reserve_alert(fare(), 1000, 70)
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.sql("SELECT id FROM deliveries")), 1)

    def test_processes_cannot_reserve_same_alert_twice(self):
        self.verified()
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            results = list(pool.map(_reserve_process, [(self.url, asdict(fare()))] * 8))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.sql("SELECT id FROM deliveries")), 1)

    def test_cross_process_quota_never_overspends(self):
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            consumed = list(pool.map(_budget_process, [(self.url, 4)] * 4))
        self.assertEqual(sum(consumed), 7)
        self.assertEqual(len(self.sql("SELECT id FROM provider_requests")), 7)

    def test_processes_claim_each_delivery_once(self):
        recipient = self.verified()
        for i in range(12):
            self.db.enqueue_notification(recipient["id"], "deal", {}, f"delivery:{i}")
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            groups = list(pool.map(_claim_process, [(self.url, "deliveries")] * 4))
        claimed = [identifier for group in groups for identifier in group]
        self.assertEqual(len(claimed), 12)
        self.assertEqual(len(set(claimed)), 12)

    def test_processes_claim_only_one_scan(self):
        self.db.enqueue_scan()
        with ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn")) as pool:
            jobs = list(pool.map(_claim_process, [(self.url, "scan")] * 4))
        self.assertEqual(sum(job is not None for job in jobs), 1)


if __name__ == "__main__":
    unittest.main()
