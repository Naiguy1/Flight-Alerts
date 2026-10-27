"""Single-host durable worker with independent scan, status and delivery progress."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time

from notifier.delivery import DeliveryService
from notifier.emailer import EmailNotifier
from notifier.tokens import TokenSigner
from services.budgets import ProviderBudgets
from services.revalidation import FareRevalidator
from services.scanner import ScanService
from services.status import StatusService
from settings import validate_config

logger = logging.getLogger(__name__)


class Heartbeat:
    def __init__(self, path: str | Path, *, clock=time.time):
        self.path, self.clock = Path(path), clock
        self._lock = threading.Lock()
        self._last = 0.0

    def beat(self, *, force=False):
        with self._lock:
            now = self.clock()
            if not force and now - self._last < 5:
                return
            if self.path.is_symlink():
                raise ValueError("Heartbeat may not be a symlink")
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd, temporary = tempfile.mkstemp(prefix=".worker-heartbeat-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump({"timestamp": now}, handle, allow_nan=False)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, self.path)
                self._last = now
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

    def remove(self):
        with self._lock:
            self.path.unlink(missing_ok=True)


class Worker:
    def __init__(self, runtime, db, default_config, *, scanner=None, status_service=None, delivery_service=None,
                 budgets=None, heartbeat=None, stop_event=None, clock=time.monotonic):
        self.runtime, self.db, self.default_config = runtime, db, validate_config(default_config)
        self.stop_event = stop_event or threading.Event()
        self.clock = clock
        self.heartbeat = heartbeat or Heartbeat(os.getenv("WORKER_HEARTBEAT_PATH", "data/worker-heartbeat.json"))
        self.budgets = budgets or ProviderBudgets(db)
        self.scanner = scanner or ScanService(db, runtime, default_config, budgets=self.budgets,
                                             heartbeat=self.heartbeat.beat, stop_event=self.stop_event)
        self.status = status_service or StatusService(db, runtime, default_config, self.budgets, stop_event=self.stop_event, heartbeat=self.heartbeat.beat)
        if delivery_service is None:
            signer = TokenSigner(runtime.token_secret)
            notifier = EmailNotifier(smtp_host=runtime.smtp_host, smtp_port=runtime.smtp_port,
                smtp_username=runtime.smtp_username, smtp_password=runtime.smtp_password, sender_email=runtime.sender_email)
            revalidator = FareRevalidator(db, runtime, default_config, self.budgets,
                                         heartbeat=self.heartbeat.beat, stop_event=self.stop_event)
            delivery_service = DeliveryService(db, notifier, signer, runtime.public_base_url,
                lambda: db.get_config(default_config), revalidate=revalidator,
                send_budget=lambda: self.budgets.consume("smtp", 10, 1000, stop_event=self.stop_event,
                                                        heartbeat=self.heartbeat.beat))
        self.delivery = delivery_service
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="flight-service")
        self._scan_future = self._status_future = None
        self._next_schedule = self._next_status = self._next_cleanup = 0.0
        self._closed = False

    def _scan(self, job, dry_run=False):
        try:
            result = self.scanner.run(job=job, dry_run=dry_run)
            if not self.db.finish_scan(job["id"], job["lease_token"], result.success,
                                       error="" if result.success else result.error_category):
                logger.warning("scan_completion_lease_lost", extra={"job_id": job["id"]})
            logger.info("scan_coverage", extra={"job_id": job["id"], "category": "complete" if result.success else result.error_category,
                                               "offers": result.observations})
            return result
        except Exception:
            self.db.finish_scan(job["id"], job["lease_token"], False, error="scan_operation_failed")
            logger.error("scan_operation_failed", extra={"job_id": job["id"]})
            raise
        finally:
            self.heartbeat.beat(force=True)

    def _collect(self, future, category):
        if future is not None and future.done():
            try:
                future.result()
            except Exception:
                logger.error(category)
            return None
        return future

    def tick(self):
        if self.stop_event.is_set() or self._closed:
            return
        self.heartbeat.beat()
        cfg = validate_config(self.db.get_config(self.default_config))
        now = self.clock()
        self._scan_future = self._collect(self._scan_future, "background_scan_failed")
        self._status_future = self._collect(self._status_future, "background_status_failed")
        if now >= self._next_schedule:
            self.db.enqueue_scan(cooldown_seconds=int(cfg["scanner"]["scan_interval_hours"] * 3600))
            self._next_schedule = now + 30
        if self._scan_future is None:
            job = self.db.claim_scan(lease_seconds=cfg["scanner"]["scan_deadline_seconds"] + 300)
            if job is not None:
                self._scan_future = self._pool.submit(self._scan, job)
        if now >= self._next_status and self._status_future is None:
            self._status_future = self._pool.submit(self.status.poll)
            self._next_status = now + 300
        # One message/digest at a time prevents a slow recipient consuming every
        # other recipient's lease. Other services continue in independent futures.
        self.delivery.process_pending(limit=1)
        self.heartbeat.beat()
        if now >= self._next_cleanup:
            lease = self.db.acquire_lease("history-cleanup", 300)
            if lease is not None:
                try:
                    self.db.cleanup(cfg["filters"]["retention_days"])
                finally:
                    self.db.release_lease("history-cleanup", lease)
            self._next_cleanup = now + 86400

    def run(self, *, poll_seconds=2):
        try:
            while not self.stop_event.is_set():
                try:
                    self.tick()
                except Exception:
                    logger.error("worker_iteration_failed")
                self.stop_event.wait(poll_seconds)
        finally:
            self.close()

    def run_once(self, *, dry_run=False):
        self.db.enqueue_scan(cooldown_seconds=300)
        cfg = validate_config(self.db.get_config(self.default_config))
        job = self.db.claim_scan(lease_seconds=cfg["scanner"]["scan_deadline_seconds"] + 300)
        if job is None:
            logger.info("scan_already_claimed_or_in_cooldown")
            return None
        result = self._scan(job, dry_run=dry_run)
        if not dry_run:
            self.status.poll()
            self.delivery.process_pending(limit=20)
        return result

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.stop_event.set()
        # Running HTTP calls use bounded connect/read timeouts and cooperative
        # deadlines; Python threads cannot forcibly terminate DNS/socket calls.
        self._pool.shutdown(wait=True, cancel_futures=True)
        self.heartbeat.remove()


def install_signal_handlers(stop_event):
    def stop(signum, frame):
        stop_event.set()
    for name in ("SIGINT", "SIGTERM"):
        signal.signal(getattr(signal, name), stop)
