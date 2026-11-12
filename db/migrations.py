"""Versioned migrations, applied inside the caller's single write transaction."""
from pathlib import Path
from datetime import datetime, timezone
import json
import sqlite3

SCHEMA_VERSION = 6
MIGRATION_DIRECTORY = Path(__file__).resolve().parents[1] / "migrations"


def _script(connection: sqlite3.Connection, filename: str) -> None:
    # executescript() commits implicitly; execute complete statements to preserve
    # the BEGIN IMMEDIATE lock across schema inspection and legacy upgrades.
    statement = ""
    for line in (MIGRATION_DIRECTORY / filename).read_text(encoding="utf-8").splitlines(True):
        statement += line
        if sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise RuntimeError("incomplete migration")


def _legacy_columns(connection: sqlite3.Connection) -> None:
    additions = {
        "price_history": {
            "currency": "TEXT NOT NULL DEFAULT 'USD'",
            "metadata_scope": "TEXT NOT NULL DEFAULT 'outbound'",
            "observed_at": "TEXT",
        },
        "alert_logs": {
            "destination_city": "TEXT NOT NULL DEFAULT ''",
            "airlines": "TEXT NOT NULL DEFAULT ''",
            "layovers": "INTEGER NOT NULL DEFAULT 0",
            "booking_url": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT",
            "payload": "TEXT NOT NULL DEFAULT '{}'",
            "baseline_source": "TEXT NOT NULL DEFAULT ''",
        },
    }
    for table, columns in additions.items():
        existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                # Identifiers/definitions are hardcoded migration constants.
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.execute("UPDATE alert_logs SET created_at=COALESCE(created_at,sent_at)")
    connection.execute("UPDATE price_history SET observed_at=COALESCE(observed_at,recorded_at)")
    # SQLAlchemy's SQLite DateTime representation uses a space separator.
    for table, columns in {
        "price_history": ("recorded_at", "observed_at"),
        "alert_logs": ("sent_at", "created_at"),
    }.items():
        for column in columns:
            connection.execute(
                f"UPDATE {table} SET {column}=replace(substr({column},1,26),' ','T') "
                f"WHERE {column} IS NOT NULL"
            )


def migrate(connection: sqlite3.Connection, now: str) -> None:
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in connection.execute("SELECT version FROM schema_migrations")}
    if applied and max(applied) > SCHEMA_VERSION:
        raise RuntimeError("database schema is newer than this application")
    if 1 not in applied:
        _script(connection, "001_initial.sql")
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (1, now))
    if 2 not in applied:
        _legacy_columns(connection)
        _script(connection, "002_legacy_indexes.sql")
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (2, now))
    if 3 not in applied:
        for table, name, definition in (
            ("recipients", "last_digest_at", "TEXT"),
            ("watched_flights", "status_version", "INTEGER NOT NULL DEFAULT 0"),
        ):
            existing = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
        _script(connection, "003_cache.sql")
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (3, now))
    if 4 not in applied:
        _script(connection, "004_weekly.sql")
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (4, now))
    if 5 not in applied:
        _script(connection, "005_destination_alert_limits.sql")
        # Seed the allowance with actual historical sends, including explicit
        # live digests. alert_logs.sent_at is only a compatibility timestamp.
        for row in connection.execute(
            "SELECT id,recipient_id,kind,payload,sent_at FROM deliveries "
            "WHERE status='sent' AND kind IN ('deal','digest') AND sent_at IS NOT NULL "
            "AND julianday(sent_at)>julianday(?)-7", (now,),
        ).fetchall():
            payload = json.loads(row[3])
            if not isinstance(payload, dict) or payload.get("weekly") is True:
                continue
            items = [payload] if row[2] == "deal" else payload.get("deals", [])
            destinations = {item.get("offer", {}).get("destination", "").strip().upper()
                            for item in items if isinstance(item, dict) and isinstance(item.get("offer"), dict)}
            for destination in destinations - {""}:
                sent = datetime.fromisoformat(row[4].replace("Z", "+00:00"))
                if sent.tzinfo is not None:
                    sent = sent.astimezone(timezone.utc).replace(tzinfo=None)
                connection.execute(
                    "INSERT INTO destination_alert_sends(recipient_id,destination,delivery_id,lease_token,attempted_at) "
                    "VALUES(?,?,?,'migration',?)", (row[1], destination, row[0], sent.isoformat(timespec="microseconds")),
                )
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (5, now))
    if 6 not in applied:
        _script(connection, "006_scan_summaries.sql")
        # Previously queued per-itinerary live fares would race new summaries.
        # Leave invitations, status watches and the optional weekly recap intact.
        for row in connection.execute("SELECT id,payload FROM deliveries WHERE status IN ('pending','retry') "
                                      "AND kind IN ('deal','digest')").fetchall():
            payload = json.loads(row[1])
            if isinstance(payload, dict) and payload.get("weekly") is not True:
                connection.execute("UPDATE deliveries SET status='canceled',last_error='superseded_by_scan_summary',"
                                   "lease_token=NULL,lease_expires_at=NULL WHERE id=?", (row[0],))
        connection.execute("INSERT INTO schema_migrations VALUES (?,?)", (6, now))
    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
