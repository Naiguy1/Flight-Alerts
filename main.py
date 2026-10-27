"""Run the durable worker. SMTP/provider network effects occur only in this role."""
from __future__ import annotations

import argparse
from dataclasses import replace
import logging
import os
import threading

from db.crud import DatabaseManager
from services.observability import configure_logging
from services.scanner import ScanService
from services.worker import Heartbeat, Worker, install_signal_handlers
from settings import RuntimeSettings, load_config, validate_config

logger = logging.getLogger(__name__)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Flight fare and status alert worker")
    parser.add_argument("--config", default=None, help="Bootstrap YAML path (overrides CONFIG_PATH)")
    parser.add_argument("--run-once", action="store_true", help="Claim one scan, then process notifications and exit")
    parser.add_argument("--dry-run", action="store_true", help="Run one scan without fare-history or alert/delivery writes; quota/provider accounting remains enabled; pending real scan jobs are untouched")
    parser.add_argument("--check-config", action="store_true", help="Validate bootstrap settings and exit without network effects")
    args = parser.parse_args(argv)
    configure_logging()
    # Development-only convenience. Compose supplies scoped mounted secrets; the
    # image/build context excludes .env. Never overwrite explicitly configured env.
    try:
        from dotenv import load_dotenv
        load_dotenv(override=False)
    except ImportError:
        pass
    try:
        runtime = RuntimeSettings.from_env()
        if args.config is not None:
            runtime = replace(runtime, config_path=args.config)
        defaults = load_config(runtime.config_path)
        if args.check_config:
            logger.info("bootstrap_configuration_valid")
            return 0
        stop = threading.Event()
        install_signal_handlers(stop)
        db = DatabaseManager(runtime.database_url)
        try:
            validate_config(db.get_config(defaults))
            heartbeat = Heartbeat(os.getenv("WORKER_HEARTBEAT_PATH", "data/worker-heartbeat.json"))
            if args.dry_run:
                cfg = validate_config(db.get_config(defaults))
                lease = db.acquire_lease("dry-run-scan", cfg["scanner"]["scan_deadline_seconds"] + 300)
                if lease is None:
                    logger.info("dry_run_already_running")
                    return 0
                try:
                    scanner = ScanService(db, runtime, defaults, heartbeat=heartbeat.beat, stop_event=stop)
                    result = scanner.run(job=None, dry_run=True)
                    return 0 if result.success else 1
                finally:
                    db.release_lease("dry-run-scan", lease)
                    heartbeat.remove()
            worker = Worker(runtime, db, defaults, heartbeat=heartbeat, stop_event=stop)
            try:
                if args.run_once:
                    result = worker.run_once()
                    return 0 if result is None or result.success else 1
                worker.run()
                return 0
            finally:
                worker.close()
        finally:
            db.close()
    except Exception:
        # URLs, credentials, private recipient addresses and secret-file paths
        # must never enter startup diagnostics.
        logger.error("worker_startup_or_operation_failed")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
