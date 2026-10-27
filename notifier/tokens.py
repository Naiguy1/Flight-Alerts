"""Purpose-bound, expiring subscription links without recipient PII in URLs."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Callable


class TokenError(ValueError):
    """A subscription link is invalid; never expose parsing details to a client."""


class TokenSigner:
    _PURPOSES = frozenset({"confirm", "unsubscribe"})
    _MAX_TTL = 366 * 24 * 60 * 60
    _MAX_TOKEN_LENGTH = 1024
    _PREFIX = b"flight-alerts:subscription:v1:"

    def __init__(self, secret: str, clock: Callable[[], float] = time.time):
        if not isinstance(secret, str) or len(secret.encode("utf-8")) < 32:
            raise ValueError("TOKEN_SECRET must contain at least 32 bytes")
        self._secret = secret.encode("utf-8")
        self._clock = clock

    @staticmethod
    def _encode(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    @classmethod
    def _decode(cls, value: str) -> bytes:
        if not value or not value.isascii():
            raise TokenError("Invalid or expired subscription link")
        try:
            raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        except (ValueError, TypeError) as exc:
            raise TokenError("Invalid or expired subscription link") from exc
        if cls._encode(raw) != value:
            raise TokenError("Invalid or expired subscription link")
        return raw

    def issue(self, recipient_id: int, purpose: str, ttl_seconds: int) -> str:
        if type(recipient_id) is not int or not 0 < recipient_id < 2**63:
            raise ValueError("Invalid recipient identifier")
        if purpose not in self._PURPOSES:
            raise ValueError("Invalid subscription purpose")
        if type(ttl_seconds) is not int or not 0 < ttl_seconds <= self._MAX_TTL:
            raise ValueError("Invalid subscription expiry")
        issued = int(self._clock())
        payload = json.dumps(
            {"v": 1, "sub": recipient_id, "purpose": purpose, "iat": issued, "exp": issued + ttl_seconds},
            separators=(",", ":"), sort_keys=True,
        ).encode("ascii")
        body = self._encode(payload)
        signature = hmac.new(self._secret, self._PREFIX + body.encode("ascii"), hashlib.sha256).digest()
        return "v1." + body + "." + self._encode(signature)

    def verify(self, token: str, purpose: str) -> int:
        message = "Invalid or expired subscription link"
        if purpose not in self._PURPOSES or not isinstance(token, str) or len(token) > self._MAX_TOKEN_LENGTH:
            raise TokenError(message)
        try:
            version, body, signature = token.split(".")
            if version != "v1":
                raise TokenError(message)
            expected = hmac.new(self._secret, self._PREFIX + body.encode("ascii"), hashlib.sha256).digest()
            if not hmac.compare_digest(self._decode(signature), expected):
                raise TokenError(message)
            payload = json.loads(self._decode(body))
            if not isinstance(payload, dict) or set(payload) != {"v", "sub", "purpose", "iat", "exp"}:
                raise TokenError(message)
            if payload["v"] != 1 or payload["purpose"] != purpose:
                raise TokenError(message)
            recipient = payload["sub"]
            issued, expires = payload["iat"], payload["exp"]
            if any(type(value) is not int for value in (recipient, issued, expires)):
                raise TokenError(message)
            if not 0 < recipient < 2**63 or not 0 < expires - issued <= self._MAX_TTL:
                raise TokenError(message)
            now = self._clock()
            if issued > now + 60 or now >= expires:
                raise TokenError(message)
            return recipient
        except (ValueError, TypeError, KeyError, UnicodeError) as exc:
            raise TokenError(message) from exc
