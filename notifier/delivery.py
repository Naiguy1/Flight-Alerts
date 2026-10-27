"""Durable per-recipient notification processing with quiet hours and digests."""
from __future__ import annotations

import html
import logging
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

from notifier.emailer import DeliveryError, EmailNotifier, render_deal, render_message, render_status
from notifier.tokens import TokenSigner

logger = logging.getLogger(__name__)
UTC = timezone.utc
_MAX_ATTEMPTS = 5


def _utc_datetime(value: str | datetime) -> datetime:
    result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)


def quiet_seconds(now: datetime, quiet: dict | None) -> int:
    """Return time until the next allowed minute, including DST transitions."""
    if not quiet:
        return 0
    start, end = int(quiet["start"]), int(quiet["end"])
    if not (0 <= start <= 23 and 0 <= end <= 23):
        raise ValueError("Invalid quiet hours")
    if start == end:
        return 0
    zone = ZoneInfo(quiet["timezone"])
    now = _utc_datetime(now)

    def is_quiet(at: datetime) -> bool:
        hour = at.astimezone(zone).hour
        return start <= hour < end if start < end else hour >= start or hour < end

    if not is_quiet(now):
        return 0
    candidate = now.replace(second=0, microsecond=0) + timedelta(minutes=1)
    # Iterate on the UTC timeline rather than constructing nonexistent or
    # ambiguous local wall times on daylight-saving boundaries.
    for _ in range(48 * 60):
        if not is_quiet(candidate):
            return max(1, int((candidate - now).total_seconds()) + 1)
        candidate += timedelta(minutes=1)
    raise ValueError("Invalid quiet-hours window")


class DeliveryService:
    def __init__(
        self, db, notifier: EmailNotifier, token_signer: TokenSigner,
        public_base_url: str, config_getter: Callable[[], dict],
        *, clock: Callable[[], datetime] | None = None,
        revalidate: Callable[[dict], bool] | None = None,
        send_budget: Callable[[], bool] | None = None,
    ):
        parsed = urlsplit(public_base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or (parsed.port is not None and not 1 <= parsed.port <= 65535) or parsed.query or parsed.fragment
                or parsed.path not in ("", "/") or any(ord(c) <= 32 for c in public_base_url)):
            raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin")
        self.db = db
        self.notifier = notifier
        self.signer = token_signer
        self.public_base_url = public_base_url.rstrip("/")
        self.config_getter = config_getter
        self.clock = clock or (lambda: datetime.now(UTC))
        self.revalidate = revalidate
        self.send_budget = send_budget

    def _link(self, recipient_id: int, purpose: str) -> str:
        ttl = 48 * 3600 if purpose == "confirm" else 365 * 24 * 3600
        token = self.signer.issue(recipient_id, purpose, ttl)
        return f"{self.public_base_url}/subscriptions/{purpose}?" + urlencode({"token": token})

    def _cancel(self, rows: list[dict], reason: str) -> None:
        for row in rows:
            self.db.cancel_delivery(row["id"], row["lease_token"], reason=reason)

    def _defer(self, rows: list[dict], seconds: int) -> None:
        for row in rows:
            self.db.defer_delivery(row["id"], row["lease_token"], retry_after_seconds=max(1, seconds))

    def _renew(self, rows: list[dict]) -> bool:
        """Never send or keep doing paid revalidation after losing ownership."""
        if not rows:
            return False
        renewed = self.db.renew_delivery_group(
            [row["id"] for row in rows], rows[0]["lease_token"], lease_seconds=300,
        )
        if renewed is not True:
            logger.warning("notification_lease_lost")
        return renewed is True

    def _outcome(
        self, rows: list[dict], success: bool, error: str = "",
        retryable: bool = True, *, digest: bool = False,
    ) -> None:
        attempts = max(int(row.get("attempts", 1)) for row in rows)
        delay = min(3600, 60 * 2 ** min(attempts - 1, 6))
        # Group updates are atomic, so a crash cannot mark only half an accepted
        # digest as sent. SMTP itself still has an ambiguous-acknowledgment gap.
        if len(rows) > 1 or digest:
            self.db.update_delivery_group(
                [row["id"] for row in rows], rows[0]["lease_token"], success,
                error=error, retry_after_seconds=delay,
                max_attempts=_MAX_ATTEMPTS if retryable else 1, digest=digest,
            )
        else:
            row = rows[0]
            self.db.update_delivery(
                row["id"], row["lease_token"], success, error=error,
                retry_after_seconds=delay, max_attempts=_MAX_ATTEMPTS if retryable else 1,
            )

    def _fresh(self, row: dict, now: datetime, freshness_hours: int) -> bool:
        payload = row.get("payload")
        if not isinstance(payload, dict):
            return False
        offer = payload.get("offer", {})
        if not isinstance(offer, dict):
            return False
        try:
            recorded = _utc_datetime(offer.get("observed_at") or row["created_at"])
            outbound = datetime.strptime(offer["outbound_date"], "%Y-%m-%d").date()
            returning = datetime.strptime(offer["return_date"], "%Y-%m-%d").date()
        except (ValueError, TypeError, KeyError):
            return False
        return (
            now - timedelta(hours=freshness_hours) <= recorded <= now + timedelta(minutes=5)
            and outbound >= now.date() and returning > outbound
        )

    def _unexpired(self, rows: list[dict], now: datetime, filters: dict) -> list[dict]:
        """Prune expired content using the clock and policy at the send boundary."""
        current = []
        freshness = int(filters.get("freshness_hours", 24))
        for row in rows:
            kind = row["kind"]
            if kind == "deal":
                if not self._fresh(row, now, freshness):
                    self._cancel([row], "fare_expired")
                    continue
            elif kind == "digest":
                items = row["payload"].get("deals", [])
                if not isinstance(items, list) or len(items) > 100:
                    raise ValueError("Invalid digest payload")
                items = [item for item in items if self._fresh(
                    {"payload": item, "created_at": row["created_at"]}, now, freshness,
                )]
                if not items:
                    self._cancel([row], "fare_expired")
                    continue
                row["payload"]["deals"] = items
            elif kind == "status":
                try:
                    created = _utc_datetime(row["created_at"])
                except (ValueError, TypeError, KeyError):
                    self._cancel([row], "invalid_status_payload")
                    continue
                if not now - timedelta(hours=6) <= created <= now + timedelta(minutes=5):
                    self._cancel([row], "status_expired")
                    continue
            current.append(row)
        return current

    def _prepare(self, rows: list[dict], recipient: dict) -> tuple[str, str, str, str | None]:
        kind = rows[0]["kind"]
        recipient_id = int(recipient["id"])
        unsubscribe = self._link(recipient_id, "unsubscribe")
        if kind == "verification":
            confirmation = self._link(recipient_id, "confirm")
            subject = "Confirm your Flight-Alerts subscription"
            plain = (
                "An administrator invited this address to Flight-Alerts.\n"
                "Confirm only if you want to receive flight alerts:\n" + confirmation
                + "\nThis confirmation link expires in 48 hours.\n"
                "If this invitation was unexpected, ignore it or disable it:\n" + unsubscribe + "\n"
            )
            content = (
                '<p>An administrator invited this address to Flight-Alerts.</p>'
                '<p>Confirm only if you want to receive flight alerts.</p>'
                '<p><a href="' + html.escape(confirmation, quote=True) + '">Confirm subscription</a></p>'
                '<p>This confirmation link expires in 48 hours. If this invitation was unexpected, ignore it or use the unsubscribe link.</p>'
            )
            return subject, plain, render_message(subject, content, unsubscribe), unsubscribe
        if kind == "status":
            subject, plain, card = render_status(rows[0]["payload"])
            return subject, plain + "\nUnsubscribe: " + unsubscribe + "\n", render_message(subject, card, unsubscribe), unsubscribe
        if kind in {"deal", "digest"}:
            payloads = []
            for row in rows:
                if row["kind"] == "digest":
                    items = row["payload"].get("deals", [])
                    if not isinstance(items, list) or len(items) > 100:
                        raise ValueError("Invalid digest payload")
                    payloads.extend(items)
                else:
                    payloads.append(row["payload"])
            if not payloads or len(payloads) > 100:
                raise ValueError("Invalid digest payload")
            rendered = [render_deal(payload) for payload in payloads]
            subject = rendered[0][0] if len(rendered) == 1 else f"Flight deal digest: {len(rendered)} fares"
            plain = "\n\n".join(item[1] for item in rendered) + "\nUnsubscribe: " + unsubscribe + "\n"
            return subject, plain, render_message(subject, "".join(item[2] for item in rendered), unsubscribe), unsubscribe
        raise ValueError("Unsupported notification kind")

    def _process_group(self, rows: list[dict], filters: dict, digest: bool) -> int:
        now = _utc_datetime(self.clock())
        recipient = self.db.get_recipient(rows[0]["recipient_id"])
        kind = rows[0]["kind"]
        if (not recipient or not recipient.get("enabled")
                or (kind != "verification" and not recipient.get("verified"))):
            self._cancel(rows, "recipient_disabled")
            return 0
        if kind == "verification" and recipient.get("verified"):
            self._cancel(rows, "already_verified")
            return 0
        if kind != "verification":
            if filters.get("alerts_paused", False):
                self._defer(rows, 60)
                return 0
            delay = quiet_seconds(now, filters.get("quiet_hours"))
            if delay:
                self._defer(rows, delay)
                return 0
        try:
            rows = self._unexpired(rows, now, filters)
        except (ValueError, TypeError, KeyError, AttributeError):
            self._outcome(rows, False, "invalid_notification_payload", retryable=False)
            return 0
        if not rows:
            return 0
        if kind == "deal":
            if digest:
                first_created = min(_utc_datetime(row["created_at"]) for row in rows)
                interval = timedelta(hours=int(filters.get("digest_interval_hours", 6)))
                due = first_created + interval
                if recipient.get("last_digest_at"):
                    due = max(due, _utc_datetime(recipient["last_digest_at"]) + interval)
                remaining = int((due - now).total_seconds())
                if remaining > 0:
                    self._defer(rows, remaining + 1)
                    return 0
        if self.revalidate is not None and kind in {"deal", "digest"}:
            approved = []
            claimed = list(rows)
            for row in rows:
                if not self._renew(claimed):
                    return 0
                try:
                    if kind == "digest":
                        deals = row["payload"].get("deals", [])
                        if not isinstance(deals, list) or len(deals) > 100:
                            raise ValueError("Invalid digest payload")
                        available = []
                        for payload in deals:
                            if not self._renew(claimed):
                                return 0
                            check = {"payload": payload, "created_at": row["created_at"]}
                            if self._fresh(check, now, int(filters.get("freshness_hours", 24))) and self.revalidate(payload) is True:
                                available.append(payload)
                        valid = bool(available)
                        row["payload"]["deals"] = available
                    else:
                        valid = self.revalidate(row["payload"]) is True
                except Exception:
                    # Upstream exceptions may contain credential-bearing URLs.
                    # Persist only this application-controlled category.
                    self._outcome([row], False, "fare_revalidation_failed", retryable=True)
                    logger.warning("fare_revalidation_failed", extra={"delivery_id": row["id"]})
                    claimed.remove(row)
                    continue
                if valid:
                    approved.append(row)
                else:
                    self._cancel([row], "fare_unavailable")
                    claimed.remove(row)
            rows = approved
            if not rows:
                return 0
        try:
            # Recheck consent immediately before the network side effect. An
            # already-in-flight SMTP delivery cannot be withdrawn atomically.
            current = self.db.get_recipient(recipient["id"])
            if not current or not current.get("enabled") or (kind != "verification" and not current.get("verified")):
                self._cancel(rows, "recipient_disabled")
                return 0
            if kind != "verification" and self.config_getter().get("filters", {}).get("alerts_paused", False):
                self._defer(rows, 60)
                return 0
            if self.send_budget is not None:
                try:
                    permitted = self.send_budget() is True
                except Exception:
                    self._outcome(rows, False, "send_budget_failed", retryable=True)
                    logger.warning("send_budget_failed", extra={"delivery_id": rows[0]["id"]})
                    return 0
                if not permitted:
                    self._defer(rows, 60)
                    return 0
            # Budget pacing and revalidation can outlive a claim. Compare-and-
            # swap renewal and the final consent check must precede SMTP, rather
            # than relying on the original claim or a check before a long wait.
            if not self._renew(rows):
                return 0
            current = self.db.get_recipient(recipient["id"])
            if not current or not current.get("enabled") or (kind != "verification" and not current.get("verified")):
                self._cancel(rows, "recipient_disabled")
                return 0
            if kind == "verification" and current.get("verified"):
                self._cancel(rows, "already_verified")
                return 0
            latest_filters = self.config_getter().get("filters", {})
            if kind != "verification":
                if latest_filters.get("alerts_paused", False):
                    self._defer(rows, 60)
                    return 0
                delay = quiet_seconds(_utc_datetime(self.clock()), latest_filters.get("quiet_hours"))
                if delay:
                    self._defer(rows, delay)
                    return 0
            # Revalidation and quota pacing can advance the clock or change the
            # policy. Render only after the final freshness check, including each
            # explicit digest item, so removed fares cannot remain in the body.
            rows = self._unexpired(rows, _utc_datetime(self.clock()), latest_filters)
            if not rows:
                return 0
            subject, plain, body_html, unsubscribe = self._prepare(rows, current)
            self.notifier.send_message(current["email"], subject, plain, body_html, unsubscribe)
        except DeliveryError as exc:
            self._outcome(rows, False, exc.category, retryable=exc.retryable, digest=digest and kind == "deal")
            logger.warning("notification_delivery_failed", extra={"category": exc.category, "delivery_id": rows[0]["id"]})
            return 0
        except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
            self._outcome(rows, False, "invalid_notification_payload", retryable=False, digest=digest and kind == "deal")
            logger.warning("notification_payload_rejected", extra={"delivery_id": rows[0]["id"]})
            return 0
        self._outcome(rows, True, digest=digest and kind == "deal")
        return len(rows)

    def process_pending(self, limit: int = 20) -> int:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Invalid delivery limit")
        config = self.config_getter()
        filters = config.get("filters", {})
        digest = bool(filters.get("digest_enabled", False))
        sent = 0
        for _ in range(limit):
            # Claim one independent message/recipient at a time so a slow SMTP
            # connection cannot consume other recipients' unprocessed leases.
            rows = self.db.claim_deliveries(limit=1, lease_seconds=300, digest_mode=digest)
            if not rows:
                break
            groups: dict[tuple, list[dict]] = defaultdict(list)
            for row in rows:
                groups[(row["recipient_id"], row["kind"], row["lease_token"])].append(row)
            for group in groups.values():
                sent += self._process_group(group, filters, digest)
        return sent
