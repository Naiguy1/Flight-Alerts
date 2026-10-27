"""Small standard-library primitives for single-administrator authentication."""

import base64
import hashlib
import hmac
import json
import ipaddress
import re
import secrets
import threading
import time
from urllib.parse import urlsplit


def canonical_https_origin(raw: str) -> str:
    """Validate the configured origin and match browser default-port handling."""
    if (not isinstance(raw, str) or len(raw) > 2048
            or any(ord(char) <= 32 or ord(char) == 127 for char in raw) or "\\" in raw):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin")
    try:
        parsed = urlsplit(raw)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or parsed.path not in ("", "/")):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        hostname = parsed.hostname
        if ":" in hostname:
            if "%" in hostname:
                raise ValueError
            hostname = "[" + ipaddress.IPv6Address(hostname).compressed + "]"
        else:
            hostname = hostname.encode("idna").decode("ascii").lower()
            if len(hostname) > 253 or not all(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                for label in hostname.split(".")
            ):
                raise ValueError
            if re.fullmatch(r"[0-9.]+", hostname):
                hostname = str(ipaddress.IPv4Address(hostname))
    except (ValueError, UnicodeError):
        raise ValueError("PUBLIC_BASE_URL must be an HTTPS origin") from None
    return "https://" + hostname + (f":{port}" if port not in (None, 443) else "")


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def hash_password(password: str) -> str:
    """Return an independently salted scrypt hash; passwords never enter config."""
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Use a password between 12 and 1024 characters")
    salt = secrets.token_bytes(16)
    derived = hashlib.scrypt(password.encode(), salt=salt, n=32768, r=8, p=1,
                             dklen=32, maxmem=64 * 1024 * 1024)
    return f"scrypt$32768$8$1${_encode(salt)}${_encode(derived)}"


def valid_password_hash(encoded: str) -> bool:
    try:
        algorithm, n, r, p, salt, key = encoded.split("$")
        return (algorithm, n, r, p) == ("scrypt", "32768", "8", "1") and len(_decode(salt)) == 16 and len(_decode(key)) == 32
    except (ValueError, TypeError, AttributeError):
        return False


def verify_password(password: str, encoded: str) -> bool:
    if not isinstance(password, str) or len(password) > 1024 or not valid_password_hash(encoded):
        return False
    try:
        _, _, _, _, salt, expected = encoded.split("$")
        actual = hashlib.scrypt(password.encode(), salt=_decode(salt), n=32768, r=8, p=1,
                                dklen=32, maxmem=64 * 1024 * 1024)
        return hmac.compare_digest(actual, _decode(expected))
    except (ValueError, TypeError, MemoryError):
        return False


class CSRFSigner:
    """Signed, expiring double-submit tokens bound to an action scope."""

    def __init__(self, secret: str):
        if len(secret) < 32:
            raise ValueError("A strong token secret is required")
        self._key = hmac.digest(secret.encode(), b"flight-alerts:csrf:v1", "sha256")

    def issue(self, scope: str, ttl_seconds: int = 3600) -> str:
        payload = _encode(json.dumps({"scope": scope, "expires": int(time.time()) + ttl_seconds,
                                      "nonce": secrets.token_urlsafe(24)}, separators=(",", ":")).encode())
        return payload + "." + _encode(hmac.digest(self._key, payload.encode(), "sha256"))

    def verify(self, cookie: str, submitted: str, scope: str) -> bool:
        try:
            if not cookie or len(cookie) > 2048 or not hmac.compare_digest(cookie, submitted):
                return False
            payload, signature = cookie.split(".")
            if not hmac.compare_digest(_decode(signature), hmac.digest(self._key, payload.encode(), "sha256")):
                return False
            data = json.loads(_decode(payload))
            return data["scope"] == scope and int(time.time()) < data["expires"] <= int(time.time()) + 3600
        except (ValueError, TypeError, KeyError, UnicodeError):
            return False


class LoginLimiter:
    """Reserve attempts atomically and cap expensive authentication work.

    A reservation is made before dispatching any password hashing. Failed
    attempts remain rate-counted; a successful attempt removes its reservation.
    The active set bounds concurrent scrypt operations independently of the
    application's thread-pool size.
    """

    def __init__(self, *, max_concurrent: int = 2, clock=time.monotonic):
        if type(max_concurrent) is not int or not 1 <= max_concurrent <= 4:
            raise ValueError("Invalid authentication concurrency limit")
        self._entries: dict[str, list[tuple[float, str]]] = {}
        self._active: dict[str, str] = {}
        self._lock = threading.Lock()
        self._maximum = max_concurrent
        self._clock = clock

    def reserve(self, address: str) -> str | None:
        now = self._clock()
        with self._lock:
            self._entries = {
                key: [(stamp, token) for stamp, token in entries if stamp > now - 60]
                for key, entries in self._entries.items()
                if entries and entries[-1][0] > now - 60
            }
            if (len(self._active) >= self._maximum
                    or len(self._entries.get(address, [])) >= 6
                    or sum(map(len, self._entries.values())) >= 60):
                return None
            token = secrets.token_urlsafe(24)
            self._entries.setdefault(address, []).append((now, token))
            self._active[token] = address
            return token

    def finish(self, token: str, *, success: bool) -> None:
        with self._lock:
            address = self._active.pop(token, None)
            if address is None or not success:
                return
            self._entries[address] = [item for item in self._entries.get(address, []) if item[1] != token]
            if not self._entries[address]:
                self._entries.pop(address, None)
