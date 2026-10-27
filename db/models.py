from datetime import datetime, timezone
from sqlalchemy import String, Float, Integer, DateTime, Index
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class PriceHistory(Base):
    __tablename__ = "price_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin: Mapped[str] = mapped_column(String(3), nullable=False)
    destination: Mapped[str] = mapped_column(String(3), nullable=False)
    destination_city: Mapped[str] = mapped_column(String(100), nullable=False)
    country_code: Mapped[str] = mapped_column(String(2), nullable=False)
    outbound_date: Mapped[str] = mapped_column(String(10), nullable=False)
    return_date: Mapped[str] = mapped_column(String(10), nullable=False)
    price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    airlines: Mapped[str] = mapped_column(String(255), nullable=False)
    layovers: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    booking_url: Mapped[str] = mapped_column(String(1024), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_route_lookup", "origin", "destination"),
    )


class AlertLog(Base):
    __tablename__ = "alert_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    origin: Mapped[str] = mapped_column(String(3), nullable=False)
    destination: Mapped[str] = mapped_column(String(3), nullable=False)
    destination_city: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    outbound_date: Mapped[str] = mapped_column(String(10), nullable=False)
    return_date: Mapped[str] = mapped_column(String(10), nullable=False)
    alerted_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    average_price_usd: Mapped[float] = mapped_column(Float, nullable=False)
    discount_percent: Mapped[float] = mapped_column(Float, nullable=False)
    airlines: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    layovers: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    booking_url: Mapped[str] = mapped_column(String(1024), default="", nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    __table_args__ = (
        Index("ix_dedup_lookup", "origin", "destination", "sent_at"),
    )
