"""Bounded HTTP GET transport. Credentials never enter errors or log messages.

Connect/read timeouts and monotonic checks bound ordinary provider operations;
they are not an OS-enforced total deadline for DNS resolution or slowly supplied
HTTP headers. The worker must expose stalled progress rather than claim that a
running Requests call can be killed by canceling its pool future.
"""

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import math
import random
import threading
import time
from urllib.parse import urlsplit

import requests

from fetcher.base import ProviderError


API_HOSTS = {"serpapi.com", "tequila-api.kiwi.com", "api.aviationstack.com"}


def retry_after_seconds(raw: str | None, *, now=None) -> float | None:
    if not raw or not isinstance(raw, str) or len(raw) > 128:
        return None
    try:
        value = float(raw)
        if not math.isfinite(value):
            return None
    except ValueError:
        try:
            target = parsedate_to_datetime(raw)
            if target.tzinfo is None:
                return None
            value = (target - (now or datetime.now(timezone.utc))).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return min(3600.0, max(0.0, value))


class ProviderTransport:
    def __init__(self, name: str, *, session=None, budget_callback=None,
                 timeout_seconds: float = 20, deadline_seconds: float = 60,
                 max_attempts: int = 3, sleep=time.sleep, clock=time.monotonic,
                 random_fn=random.random, max_response_bytes: int = 2_000_000):
        if not 0 < timeout_seconds <= 120 or not 0 < deadline_seconds <= 600 or not 1 <= max_attempts <= 5:
            raise ValueError("Invalid provider transport limits")
        if type(max_response_bytes) is not int or not 1 <= max_response_bytes <= 10_000_000:
            raise ValueError("Invalid provider response limit")
        self.name = name
        self.session = session if session is not None else requests.Session()
        if session is None:
            # Avoid ambient .netrc credentials/proxies and disable hidden retries.
            self.session.trust_env = False
            adapter = requests.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=0)
            self.session.mount("https://", adapter)
        self.budget_callback = budget_callback
        self.timeout_seconds = timeout_seconds
        self.deadline_seconds = deadline_seconds
        self.max_attempts = max_attempts
        self.max_response_bytes = max_response_bytes
        self.sleep = sleep
        self.clock = clock
        self.random_fn = random_fn
        self._lock = threading.Lock()

    def close(self):
        self.session.close()

    def _consume_budget(self):
        if self.budget_callback is not None:
            try:
                permitted = self.budget_callback()
            except Exception:
                raise ProviderError("budget_unavailable", retryable=True) from None
            if permitted is not True:
                raise ProviderError("budget_exhausted", retryable=True, retry_after_seconds=60)

    @staticmethod
    def _validate_endpoint(url):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme == "https" and parsed.hostname in API_HOSTS
                     and parsed.port in (None, 443) and parsed.username is None
                     and parsed.password is None and not parsed.query and not parsed.fragment)
        except (ValueError, TypeError):
            valid = False
        if not valid:
            raise ProviderError("invalid_endpoint")

    def _read_json(self, response, deadline):
        content = bytearray()
        # A large iter_content chunk can conceal a slow byte trickle until the
        # whole chunk is buffered. Yield each byte so the wall-clock check runs
        # during the body as well as between requests; total output stays capped.
        for chunk in response.iter_content(chunk_size=1):
            if self.clock() >= deadline:
                raise ProviderError("deadline_exceeded", retryable=True)
            if chunk:
                content.extend(chunk)
            if len(content) > self.max_response_bytes:
                raise ProviderError("response_too_large")
        # JSON nesting must be bounded independently of the interpreter's
        # recursion limit. Delimiters inside escaped strings do not count.
        depth = 0
        quoted = escaped = False
        for byte in content:
            if quoted:
                if escaped:
                    escaped = False
                elif byte == 92:
                    escaped = True
                elif byte == 34:
                    quoted = False
            elif byte == 34:
                quoted = True
            elif byte in (91, 123):
                depth += 1
                if depth > 64:
                    raise ProviderError("invalid_json")
            elif byte in (93, 125):
                depth -= 1
        try:
            payload = json.loads(content.decode("utf-8-sig"))
        except (ValueError, UnicodeError, RecursionError):
            raise ProviderError("invalid_json") from None
        if self.clock() >= deadline:
            raise ProviderError("deadline_exceeded", retryable=True)
        if not isinstance(payload, dict):
            raise ProviderError("invalid_payload")
        return payload

    def get_json(self, url, params=None, headers=None) -> dict:
        # Requests Sessions carry mutable cookie state. Serialize one provider's
        # session while independent providers may run concurrently.
        with self._lock:
            return self._get_json(url, params=params, headers=headers)

    def _get_json(self, url, params=None, headers=None) -> dict:
        self._validate_endpoint(url)
        deadline = self.clock() + self.deadline_seconds
        for attempt in range(1, self.max_attempts + 1):
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderError("deadline_exceeded", retryable=True)
            self._consume_budget()
            # Shared pacing is allowed to wait. Its elapsed time belongs to this
            # request's deadline; never start network I/O using stale remaining.
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise ProviderError("deadline_exceeded", retryable=True)
            response = None
            failure = None
            try:
                timeout = min(self.timeout_seconds, remaining)
                response = self.session.get(
                    url, params=params, headers=headers, allow_redirects=False,
                    timeout=(min(5.0, timeout / 3), timeout * 2 / 3), stream=True,
                )
                status = response.status_code
                if 200 <= status < 300:
                    return self._read_json(response, deadline)
                retry_after = retry_after_seconds(response.headers.get("Retry-After"))
                if 300 <= status < 400:
                    failure = ProviderError("redirect_rejected", status_code=status)
                elif status == 401:
                    failure = ProviderError("authentication_failed", status_code=status)
                elif status == 403:
                    failure = ProviderError("permission_denied", status_code=status)
                elif status == 429:
                    failure = ProviderError("rate_limited", True, retry_after, status)
                elif 500 <= status < 600:
                    failure = ProviderError("upstream_error", True, retry_after, status)
                else:
                    failure = ProviderError("request_rejected", status_code=status)
            except ProviderError:
                raise
            except requests.exceptions.SSLError:
                failure = ProviderError("tls_error")
            except requests.exceptions.Timeout:
                failure = ProviderError("timeout", retryable=True)
            except requests.RequestException:
                failure = ProviderError("network_error", retryable=True)
            finally:
                if response is not None:
                    response.close()
            if not failure.retryable or attempt == self.max_attempts:
                raise failure from None
            backoff = min(10.0, 0.5 * (2 ** (attempt - 1)) + 0.25 * self.random_fn())
            delay = max(backoff, failure.retry_after_seconds or 0)
            if delay > 30 or self.clock() + delay >= deadline:
                raise failure from None
            self.sleep(delay)
        raise ProviderError("provider_error")
