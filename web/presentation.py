"""Display-only formatting. Stored prices and qualification retain full precision."""
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP


def money(value) -> str:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "—"
        return f"${amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP):,}"
    except (InvalidOperation, ValueError, TypeError):
        return "—"


def percentage(value) -> str:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "—"
        return f"{amount.quantize(Decimal('0.1'), rounding=ROUND_HALF_UP):.1f}%"
    except (InvalidOperation, ValueError, TypeError):
        return "—"


def travel_date(value) -> str:
    try:
        parsed = date.fromisoformat(str(value))
        return f"{parsed:%b} {parsed.day}, {parsed.year}"
    except (ValueError, TypeError):
        return "—"


def observed_time(value) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        parsed = parsed.astimezone(timezone.utc)
        return f"{parsed:%b} {parsed.day}, {parsed.year} · {parsed:%H:%M} UTC"
    except (ValueError, TypeError):
        return "—"
