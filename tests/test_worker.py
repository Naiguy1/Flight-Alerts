"""Durable worker/CLI integration without provider or SMTP connections."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import yaml

import main
from db.crud import DatabaseManager
from services.scanner import ScanResult
from services.worker import Heartbeat, Worker
from settings import RuntimeSettings, validate_config
from tools.healthcheck import worker_is_healthy


def config():
    return validate_config({"scanner": {"origins": ["SLC"], "request_delay_seconds": 0,
            "search_window": {"sample_departure_offsets_days": [35]}}, "filters": {},
            "monitored_destinations": [{"code": "LHR", "city": "London", "country_code": "GB",
                                        "baseline_avg_usd": {"SLC": 1000}}]})


def complete():
    return ScanResult(total_windows=1, windows=1, completed_windows=1, provider_successes=1)


class HeartbeatTests(unittest.TestCase):
    def test_atomic_private_heartbeat_has_no_secrets_and_is_healthcheck_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "heartbeat.json"
            heartbeat = Heartbeat(target)
            heartbeat.beat(force=True)
            self.assertTrue(worker_is_healthy(target))
            self.assertEqual(set(json.loads(target.read_text())), {"timestamp"})
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            heartbeat.remove()
            self.assertFalse(worker_is_healthy(target))

    def test_symlink_cannot_overwrite_another_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target, other = Path(directory) / "link", Path(directory) / "other"
            other.write_text("preserve")
            target.symlink_to(other)
            with self.assertRaises(ValueError):
                Heartbeat(target).beat(force=True)
            self.assertEqual(other.read_text(), "preserve")

    def test_concurrent_actual_progress_writers_leave_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "heartbeat.json"
            heartbeat = Heartbeat(target)
            threads = [threading.Thread(target=lambda: [heartbeat.beat(force=True) for _ in range(5)]) for _ in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=2)
            self.assertTrue(worker_is_healthy(target))
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db = DatabaseManager("sqlite:///" + str(Path(self.directory.name) / "state.db"))
        self.cfg = config()
        self.db.get_config(self.cfg)
        self.runtime = RuntimeSettings()
        self.heartbeat = Heartbeat(Path(self.directory.name) / "heartbeat.json")
        self.scanner, self.status, self.delivery = Mock(), Mock(), Mock()
        self.scanner.run.return_value = complete()
        self.status.poll.return_value = 0
        self.delivery.process_pending.return_value = 0
        self.worker = Worker(self.runtime, self.db, self.cfg, scanner=self.scanner, status_service=self.status,
                             delivery_service=self.delivery, heartbeat=self.heartbeat)

    def tearDown(self):
        self.worker.close()
        self.db.close()
        self.directory.cleanup()

    def test_scan_delivery_and_status_progress_are_independent(self):
        started, release, status_ran = threading.Event(), threading.Event(), threading.Event()
        def slow_scan(**kwargs):
            started.set()
            if not release.wait(2):
                raise RuntimeError("test scanner was not released")
            return complete()
        self.scanner.run.side_effect = slow_scan
        self.status.poll.side_effect = lambda: status_ran.set() or 0
        try:
            self.worker.tick()
            self.assertTrue(started.wait(1))
            self.assertTrue(status_ran.wait(1))
            self.delivery.process_pending.assert_called_once_with(limit=1)
            self.assertTrue(worker_is_healthy(self.heartbeat.path))
            self.assertFalse(self.worker._scan_future.done())
        finally:
            release.set()

    def test_scan_once_uses_persisted_claim_and_records_completion(self):
        result = self.worker.run_once()
        self.assertTrue(result.success)
        self.scanner.run.assert_called_once()
        job = self.scanner.run.call_args.kwargs["job"]
        self.assertEqual(job["status"], "running")
        self.assertTrue(job["lease_token"])
        jobs = self.db.dashboard_snapshot([])["jobs"]
        self.assertEqual(jobs[0]["status"], "succeeded")
        self.delivery.process_pending.assert_called_once_with(limit=20)

    def test_run_once_obeys_manual_cooldown(self):
        self.assertTrue(self.worker.run_once().success)
        self.assertIsNone(self.worker.run_once())
        self.assertEqual(self.scanner.run.call_count, 1)

    def test_dry_run_never_invokes_status_or_delivery(self):
        result = self.worker.run_once(dry_run=True)
        self.assertTrue(result.success)
        self.scanner.run.assert_called_once()
        self.assertTrue(self.scanner.run.call_args.kwargs["dry_run"])
        self.status.poll.assert_not_called()
        self.delivery.process_pending.assert_not_called()

    def test_dry_run_does_not_consume_existing_pending_real_job(self):
        job_id = self.db.enqueue_scan(0)
        self.assertTrue(self.worker.run_once(dry_run=True).success)
        job = self.db.dashboard_snapshot([])["jobs"][0]
        self.assertEqual(job["id"], job_id)
        self.assertEqual(job["status"], "pending")
        self.assertIsNone(self.scanner.run.call_args.kwargs["job"])
        self.assertIsNotNone(self.db.claim_scan())

    def test_incomplete_scan_is_failed_even_when_it_returned_data(self):
        self.scanner.run.return_value = ScanResult(total_windows=2, windows=1, completed_windows=1,
                                                   provider_successes=1, observations=1)
        result = self.worker.run_once()
        self.assertFalse(result.success)
        job = self.db.dashboard_snapshot([])["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["last_error"], "incomplete_provider_coverage")

    def test_scanner_crash_records_safe_failure_and_closes_claim(self):
        self.scanner.run.side_effect = RuntimeError("SYNTHETIC_PRIVATE_TOKEN")
        with self.assertRaises(RuntimeError):
            self.worker.run_once()
        job = self.db.dashboard_snapshot([])["jobs"][0]
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["last_error"], "scan_operation_failed")
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", str(job))

    def test_stop_is_idempotent_and_removes_heartbeat(self):
        self.heartbeat.beat(force=True)
        self.worker.close()
        self.worker.close()
        self.assertTrue(self.worker.stop_event.is_set())
        self.assertFalse(self.heartbeat.path.exists())
        self.worker.tick()
        self.delivery.process_pending.assert_not_called()

    def test_stopped_worker_main_loop_exits_without_work(self):
        self.worker.stop_event.set()
        self.worker.run(poll_seconds=0)
        self.scanner.run.assert_not_called()
        self.status.poll.assert_not_called()
        self.delivery.process_pending.assert_not_called()

    def test_scheduling_reloads_changed_cadence_from_database(self):
        now = [100.0]
        self.worker.clock = lambda: now[0]
        self.worker.tick()
        self.worker._scan_future.result(timeout=1)
        cfg = config()
        cfg["scanner"]["scan_interval_hours"] = 1
        self.db.save_config(cfg, self.db.get_config_version())
        with patch.object(self.db, "enqueue_scan", wraps=self.db.enqueue_scan) as enqueue:
            now[0] += 31
            self.worker.tick()
            enqueue.assert_called_once_with(cooldown_seconds=3600)


class CLITests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "custom-config.yaml"
        self.path.write_text(yaml.safe_dump(config()))
        self.database = Path(self.directory.name) / "state.db"
        self.environment = {"CONFIG_PATH": str(Path(self.directory.name) / "missing.yaml"),
                            "DATABASE_URL": "sqlite:///" + str(self.database),
                            "WORKER_HEARTBEAT_PATH": str(Path(self.directory.name) / "heartbeat.json"),
                            "PUBLIC_BASE_URL": "https://example.invalid"}

    def tearDown(self):
        self.directory.cleanup()

    def test_explicit_config_overrides_environment_without_database_or_network(self):
        with patch.dict(os.environ, self.environment, clear=True):
            self.assertEqual(main.main(["--config", str(self.path), "--check-config"]), 0)
        self.assertFalse(self.database.exists())

    def test_invalid_config_fails_without_echoing_private_values(self):
        self.path.write_text("scanner: {currency: SYNTHETIC_PRIVATE_TOKEN}")
        with patch.dict(os.environ, self.environment, clear=True), patch("main.configure_logging"), self.assertLogs(level="ERROR") as logs:
            self.assertEqual(main.main(["--config", str(self.path), "--check-config"]), 1)
        self.assertNotIn("SYNTHETIC_PRIVATE_TOKEN", " ".join(logs.output))

    def test_cli_dry_run_has_no_notification_construction_or_fare_writes(self):
        fake_scanner = Mock()
        fake_scanner.run.return_value = complete()
        with patch.dict(os.environ, self.environment, clear=True), patch("main.install_signal_handlers"), \
                patch("main.ScanService", return_value=fake_scanner), patch("main.Worker") as worker:
            self.assertEqual(main.main(["--config", str(self.path), "--dry-run"]), 0)
        worker.assert_not_called()
        self.assertTrue(fake_scanner.run.call_args.kwargs["dry_run"])
        db = DatabaseManager("sqlite:///" + str(self.database))
        try:
            snapshot = db.dashboard_snapshot(["LHR"])
            self.assertEqual(snapshot["recent_deals"], [])
            self.assertEqual(snapshot["alerts"], [])
            self.assertEqual(snapshot["jobs"], [])
        finally:
            db.close()
        self.assertFalse(Path(self.environment["WORKER_HEARTBEAT_PATH"]).exists())

    def test_cli_dry_run_crash_releases_failed_job(self):
        with patch.dict(os.environ, self.environment, clear=True), patch("main.install_signal_handlers"), \
                patch("main.ScanService") as scanner:
            scanner.return_value.run.side_effect = RuntimeError("SYNTHETIC_PRIVATE_TOKEN")
            self.assertEqual(main.main(["--config", str(self.path), "--dry-run"]), 1)
        db = DatabaseManager("sqlite:///" + str(self.database))
        try:
            self.assertEqual(db.dashboard_snapshot([])["jobs"], [])
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
