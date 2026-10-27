from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker, Session
from db.models import Base, PriceHistory, AlertLog
from fetcher.base import FlightOffer


class DatabaseManager:
    def __init__(self, database_url: str):
        self.engine = create_engine(database_url, echo=False, future=True)
        if database_url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def set_sqlite_pragma(dbapi_connection, connection_record):
                cursor = dbapi_connection.cursor()
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.close()
        Base.metadata.create_all(self.engine)
        self._ensure_alert_log_columns()
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def _ensure_alert_log_columns(self) -> None:
        """Safely adds new metadata columns to existing SQLite alert_logs table."""
        with self.engine.begin() as conn:
            existing = {
                row[1] for row in conn.execute(text("PRAGMA table_info(alert_logs)")).fetchall()
            }
            migrations = [
                ("destination_city", "ALTER TABLE alert_logs ADD COLUMN destination_city VARCHAR(100) NOT NULL DEFAULT ''"),
                ("airlines", "ALTER TABLE alert_logs ADD COLUMN airlines VARCHAR(255) NOT NULL DEFAULT ''"),
                ("layovers", "ALTER TABLE alert_logs ADD COLUMN layovers INTEGER NOT NULL DEFAULT 0"),
                ("booking_url", "ALTER TABLE alert_logs ADD COLUMN booking_url VARCHAR(1024) NOT NULL DEFAULT ''"),
            ]
            for col_name, ddl in migrations:
                if col_name not in existing:
                    conn.execute(text(ddl))

    def get_session(self) -> Session:
        return self.SessionLocal()

    def record_offer(self, offer: FlightOffer) -> None:
        with self.get_session() as session:
            entry = PriceHistory(
                origin=offer.origin,
                destination=offer.destination,
                destination_city=offer.destination_city,
                country_code=offer.country_code,
                outbound_date=offer.outbound_date,
                return_date=offer.return_date,
                price_usd=offer.price_usd,
                airlines=", ".join(offer.airlines),
                layovers=offer.layovers,
                source=offer.source,
                booking_url=offer.booking_url,
            )
            session.add(entry)
            session.commit()

    def get_historical_route_stats(
        self, origin: str, destination: str, lookback_days: int = 90
    ) -> Tuple[Optional[float], int]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        with self.get_session() as session:
            stmt = select(
                func.avg(PriceHistory.price_usd),
                func.count(PriceHistory.id)
            ).where(
                PriceHistory.origin == origin,
                PriceHistory.destination == destination,
                PriceHistory.recorded_at >= cutoff,
            )
            avg_price, count = session.execute(stmt).one()
            return (float(avg_price) if avg_price is not None else None, int(count or 0))

    def is_duplicate_alert(
        self,
        origin: str,
        destination: str,
        outbound_date: str,
        return_date: str,
        current_price: float,
        window_hours: int = 48,
    ) -> bool:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
        with self.get_session() as session:
            exact_stmt = select(AlertLog).where(
                AlertLog.origin == origin,
                AlertLog.destination == destination,
                AlertLog.outbound_date == outbound_date,
                AlertLog.return_date == return_date,
                AlertLog.sent_at >= cutoff,
            )
            if session.execute(exact_stmt).first() is not None:
                return True

            route_stmt = select(func.min(AlertLog.alerted_price_usd)).where(
                AlertLog.origin == origin,
                AlertLog.destination == destination,
                AlertLog.sent_at >= cutoff,
            )
            min_recent_price = session.execute(route_stmt).scalar()
            if min_recent_price is not None and current_price >= (float(min_recent_price) - 35.0):
                return True

            return False

    def log_alert(
        self,
        offer: FlightOffer,
        average_price: float,
        discount_percent: float,
    ) -> None:
        with self.get_session() as session:
            log_entry = AlertLog(
                origin=offer.origin,
                destination=offer.destination,
                destination_city=offer.destination_city,
                outbound_date=offer.outbound_date,
                return_date=offer.return_date,
                alerted_price_usd=offer.price_usd,
                average_price_usd=average_price,
                discount_percent=discount_percent,
                airlines=", ".join(offer.airlines),
                layovers=offer.layovers,
                source=offer.source,
                booking_url=offer.booking_url,
            )
            session.add(log_entry)
            session.commit()
