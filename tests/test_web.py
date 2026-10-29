"""Security boundary regressions; no external SMTP, flight calls or real credentials."""

import importlib.util
import asyncio
import base64
import json
import re
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from web.security import CSRFSigner, LoginLimiter, canonical_https_origin, hash_password, valid_password_hash, verify_password

WEB_AVAILABLE = all(importlib.util.find_spec(name) is not None for name in ("fastapi", "jinja2", "httpx", "yaml"))


class PasswordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "testing-only-strong-password"
        cls.encoded = hash_password(cls.password)

    def test_password_hash_authenticates_only_matching_password(self):
        self.assertTrue(valid_password_hash(self.encoded))
        self.assertTrue(verify_password(self.password, self.encoded))
        self.assertFalse(verify_password("incorrect", self.encoded))
        self.assertNotIn(self.password, self.encoded)

    def test_independent_salts_and_rejection_of_unsafe_parameters(self):
        self.assertNotEqual(hash_password(self.password), self.encoded)
        for encoded in ["", "plaintext-password", self.encoded.replace("32768", "999999999999")]:
            self.assertFalse(verify_password(self.password, encoded))
        with self.assertRaises(ValueError):
            hash_password("short")

    def test_failed_login_work_is_bounded(self):
        limiter = LoginLimiter()
        for _ in range(6):
            reservation = limiter.reserve("test")
            self.assertIsNotNone(reservation)
            limiter.finish(reservation, success=False)
        self.assertIsNone(limiter.reserve("test"))
        self.assertIsNotNone(limiter.reserve("other"))

    def test_attempt_reservations_bound_concurrent_hashes_before_dispatch(self):
        limiter = LoginLimiter(max_concurrent=2)
        with ThreadPoolExecutor(max_workers=20) as pool:
            reservations = list(pool.map(limiter.reserve, [f"client-{i}" for i in range(20)]))
        accepted = [token for token in reservations if token is not None]
        self.assertEqual(len(accepted), 2)
        for token in accepted:
            limiter.finish(token, success=True)
        self.assertIsNotNone(limiter.reserve("after-completion"))

    def test_success_refunds_rate_count_and_failure_window_expires(self):
        now = [1000.0]
        limiter = LoginLimiter(clock=lambda: now[0])
        for _ in range(20):
            token = limiter.reserve("admin")
            self.assertIsNotNone(token)
            limiter.finish(token, success=True)
        for _ in range(6):
            token = limiter.reserve("bad")
            self.assertIsNotNone(token)
            limiter.finish(token, success=False)
        self.assertIsNone(limiter.reserve("bad"))
        now[0] = 1060.0
        self.assertIsNotNone(limiter.reserve("bad"))

    def test_global_failure_budget_and_invalid_limit(self):
        limiter = LoginLimiter()
        for index in range(60):
            token = limiter.reserve(f"client-{index}")
            self.assertIsNotNone(token)
            limiter.finish(token, success=False)
        self.assertIsNone(limiter.reserve("new-client"))
        for invalid in (0, 5, True):
            with self.assertRaises(ValueError):
                LoginLimiter(max_concurrent=invalid)


class OriginTests(unittest.TestCase):
    def test_browser_origin_canonicalization(self):
        for raw, expected in {
            "https://EXAMPLE.COM:443/": "https://example.com",
            "https://example.com:8443": "https://example.com:8443",
            "https://bücher.example": "https://xn--bcher-kva.example",
            "https://[2001:db8::1]:443": "https://[2001:db8::1]",
            "https://localhost": "https://localhost",
        }.items():
            self.assertEqual(canonical_https_origin(raw), expected)

    def test_unsafe_or_ambiguous_origins_rejected(self):
        for raw in ("http://example.com", "https://user@example.com", "https://example.com/path",
                    "https://example.com?token=secret", "https://example.com#fragment", "https://example.com/\n",
                    "https://example.com\\@evil.example", "https://example.com.", "https://bad_host.example",
                    "https://example.com:0", "https://example.com:65536", "https://[fe80::1%eth0]",
                    "https://127.0.0.01", "https://-example.com"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                canonical_https_origin(raw)


class CSRFTests(unittest.TestCase):
    def setUp(self):
        self.signer = CSRFSigner("test-only-secret-with-at-least-32-characters")

    def test_token_requires_matching_cookie_signature_and_scope(self):
        token = self.signer.issue("admin")
        self.assertTrue(self.signer.verify(token, token, "admin"))
        self.assertFalse(self.signer.verify(token, token + "x", "admin"))
        self.assertFalse(self.signer.verify(token, token, "public"))
        self.assertFalse(self.signer.verify("", token, "admin"))
        self.assertFalse(self.signer.verify(token + "x", token + "x", "admin"))

    def test_expired_tokens_are_rejected(self):
        with patch("web.security.time.time", return_value=1000):
            token = self.signer.issue("admin", ttl_seconds=60)
        with patch("web.security.time.time", return_value=1061):
            self.assertFalse(self.signer.verify(token, token, "admin"))


@unittest.skipUnless(WEB_AVAILABLE, "Install locked web/test dependencies for HTTP integration tests")
class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.password = "testing-only-strong-password"
        cls.password_hash = hash_password(cls.password)

    def setUp(self):
        from dashboard import create_app
        from db.crud import DatabaseManager
        from fastapi.testclient import TestClient
        from settings import RuntimeSettings
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config_path = Path(self.temp.name) / "config.yaml"
        self.config_path.write_text(json.dumps({"monitored_destinations": [], "filters": {"recipient_emails": []}}))
        self.runtime = RuntimeSettings(database_url="sqlite:///" + str(Path(self.temp.name) / "test.db"),
                                       config_path=str(self.config_path), public_base_url="https://testserver",
                                       admin_username="admin", admin_password_hash=self.password_hash,
                                       token_secret="test-only-token-secret-longer-than-32-characters")
        self.db = DatabaseManager(self.runtime.database_url)
        self.addCleanup(self.db.close)
        self.app = create_app(self.runtime, self.db)
        self.client = self.enterContext(TestClient(self.app, base_url="https://testserver", raise_server_exceptions=False))
        self.auth = ("admin", self.password)

    def csrf(self):
        response = self.client.get("/", auth=self.auth)
        self.assertEqual(response.status_code, 200, response.text)
        return re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)

    def post(self, path, data=None):
        payload = {"csrf_token": self.csrf()}
        payload.update(data or {})
        return self.client.post(path, data=payload, auth=self.auth,
                                headers={"Origin": "https://testserver"}, follow_redirects=False)

    def test_anonymous_administrative_routes_are_closed(self):
        self.assertEqual(self.client.get("/").status_code, 401)
        for path in ("/scan", "/emails/add", "/emails/test", "/airports/add", "/blocklist/add", "/watches/add", "/settings"):
            self.assertIn(self.client.post(path, data={}).status_code, (401, 429), path)
        self.assertEqual(self.db.list_recipients(), [])
        self.assertEqual(self.db.dashboard_snapshot([])["jobs"], [])
        self.assertEqual(self.client.get("/docs").status_code, 404)
        self.assertEqual(self.client.get("/openapi.json").status_code, 404)

    def test_health_is_minimal_and_no_auth_required(self):
        self.assertEqual(self.client.get("/health/live").json(), {"status": "ok"})
        self.assertEqual(self.client.get("/health/ready").json(), {"status": "ready"})
        with patch.object(self.db, "health", side_effect=RuntimeError("secret-database-path")):
            response = self.client.get("/health/ready")
            self.assertEqual(response.status_code, 503)
            self.assertNotIn("secret", response.text)

    def test_origin_csrf_and_version_protect_state_changes(self):
        token = self.csrf()
        response = self.client.post("/scan", data={"csrf_token": token}, auth=self.auth,
                                    headers={"Origin": "https://evil.example"})
        self.assertEqual(response.status_code, 403)
        response = self.client.post("/scan", data={}, auth=self.auth, headers={"Origin": "https://testserver"})
        self.assertEqual(response.status_code, 403)
        response = self.post("/blocklist/add", {"term": "DUB", "config_version": self.db.get_config_version() + 1})
        self.assertEqual(response.status_code, 409)

    def test_browser_form_policy_and_origin_checks_for_scan_and_settings(self):
        response = self.client.get("/", auth=self.auth)
        # no-referrer can turn browser POST Origin into null; strict-origin
        # retains the origin without exposing signed URLs or query strings.
        self.assertEqual(response.headers["referrer-policy"], "strict-origin")
        token = self.csrf()
        settings = dict(csrf_token=token, config_version=self.db.get_config_version(),
                        threshold=50, freshness_hours=24)
        for path, data in (("/scan", {"csrf_token": token}), ("/settings", settings)):
            for headers in ({}, {"Origin": "null"}, {"Origin": "https://evil.example"},
                            [("Origin", "https://testserver"), ("Origin", "https://testserver")]):
                self.assertEqual(self.client.post(path, data=data, auth=self.auth,
                                                 headers=headers).status_code, 403)
            result = self.client.post(path, data=data, auth=self.auth,
                                      headers={"Origin": "https://testserver"}, follow_redirects=False)
            self.assertEqual(result.status_code, 303, result.text)

    def test_manual_scan_is_queued_once_globally(self):
        self.assertEqual(self.post("/scan").status_code, 303)
        response = self.post("/scan")
        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=busy", response.headers["location"])
        jobs = self.db.dashboard_snapshot([]).get("jobs", [])
        self.assertEqual(len(jobs), 1)

    def test_recipient_requires_confirmation_and_invitation_queues_delivery(self):
        response = self.post("/emails/add", {"email": "recipient@example.com"})
        self.assertEqual(response.status_code, 303)
        recipient = self.db.list_recipients()[0]
        self.assertFalse(recipient["verified"])
        rows = self.db.claim_deliveries()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "verification")
        self.assertFalse(self.db.get_recipient(recipient["id"])["verified"])

    def test_signed_subscription_get_is_read_only_and_post_requires_csrf(self):
        recipient = self.db.invite_recipient("recipient@example.com")
        token = self.app.state.tokens.issue(recipient["id"], "confirm", ttl_seconds=300)
        response = self.client.get("/subscriptions/confirm", params={"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertFalse(self.db.get_recipient(recipient["id"])["verified"])
        self.assertIn("no-store", response.headers["cache-control"])
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
        self.assertEqual(self.client.post("/subscriptions/confirm", data={"token": token, "csrf_token": csrf}).status_code, 403)
        response = self.client.post("/subscriptions/confirm", data={"token": token, "csrf_token": csrf}, headers={"Origin": "https://testserver"})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(self.db.get_recipient(recipient["id"])["verified"])
        invalid = self.client.get("/subscriptions/unsubscribe", params={"token": token})
        self.assertEqual(invalid.status_code, 400)

    def test_unsubscribe_disables_recipient_and_rejects_old_confirmation(self):
        recipient = self.db.invite_recipient("recipient@example.com")
        confirm = self.app.state.tokens.issue(recipient["id"], "confirm", ttl_seconds=300)
        self.db.confirm_recipient(recipient["id"])
        token = self.app.state.tokens.issue(recipient["id"], "unsubscribe", ttl_seconds=300)
        response = self.client.get("/subscriptions/unsubscribe", params={"token": token})
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
        self.assertTrue(self.db.get_recipient(recipient["id"])["enabled"])
        self.assertEqual(self.client.post("/subscriptions/unsubscribe", data={"token": token, "csrf_token": csrf}, headers={"Origin": "https://testserver"}).status_code, 200)
        self.assertFalse(self.db.get_recipient(recipient["id"])["enabled"])
        response = self.client.get("/subscriptions/confirm", params={"token": confirm})
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
        self.assertEqual(self.client.post("/subscriptions/confirm", data={"token": confirm, "csrf_token": csrf}, headers={"Origin": "https://testserver"}).status_code, 400)

    def test_reflected_and_stored_html_is_not_executed(self):
        attack = '<img src=x onerror="alert(1)">'
        response = self.client.get("/", params={"notice": attack, "msg": attack}, auth=self.auth)
        self.assertNotIn(attack, response.text)
        response = self.post("/airports/add", {"code": "DUB", "city": attack, "country_code": "IE", "slc_baseline": "800", "las_baseline": "720", "config_version": self.db.get_config_version()})
        self.assertEqual(response.status_code, 303)
        response = self.client.get("/", auth=self.auth)
        self.assertNotIn(attack, response.text)
        self.assertIn("&lt;img", response.text)

    def test_input_validation_rejects_nonfinite_baselines_and_multiple_mailboxes(self):
        response = self.post("/airports/add", {"code": "DUB", "city": "Dublin", "country_code": "IE", "slc_baseline": "nan", "las_baseline": "720", "config_version": self.db.get_config_version()})
        self.assertIn(response.status_code, (409, 422))
        response = self.post("/emails/add", {"email": "one@example.com,two@example.com"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.db.list_recipients(), [])

    def test_body_limits_cover_declared_and_streamed_bodies(self):
        response = self.client.post("/scan", content=b"x" * 65537, auth=self.auth)
        self.assertEqual(response.status_code, 413)
        response = self.client.post("/scan", content=iter([b"x" * 40000, b"x" * 40000]), auth=self.auth)
        self.assertEqual(response.status_code, 413)

    def test_host_tls_cookie_and_response_security_controls(self):
        response = self.client.get("/", auth=self.auth)
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["referrer-policy"], "strict-origin")
        cookie = response.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=strict"):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}, auth=self.auth).status_code, 400)
        self.assertEqual(self.client.get("http://testserver/", auth=self.auth).status_code, 400)

    def test_duplicate_authorization_headers_are_rejected(self):
        authorization = "Basic " + base64.b64encode(f"admin:{self.password}".encode()).decode()
        response = self.client.get("/", headers=[("Authorization", authorization), ("Authorization", authorization)])
        self.assertEqual(response.status_code, 401)

    def test_authentication_concurrency_is_bounded_at_http_boundary(self):
        entered = threading.Event()
        release = threading.Event()
        lock = threading.Lock()
        calls = [0]

        def slow_verify(password, encoded):
            with lock:
                calls[0] += 1
                if calls[0] == 2:
                    entered.set()
            release.wait(timeout=5)
            return False

        with patch("dashboard.verify_password", side_effect=slow_verify):
            with ThreadPoolExecutor(max_workers=3) as pool:
                first = pool.submit(self.client.get, "/", auth=self.auth)
                second = pool.submit(self.client.get, "/", auth=self.auth)
                try:
                    self.assertTrue(entered.wait(timeout=3), "Two bounded verifier calls should start")
                    denied = self.client.get("/", auth=self.auth)
                    self.assertEqual(denied.status_code, 429)
                    self.assertEqual(calls[0], 2)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5).status_code, 401)
                self.assertEqual(second.result(timeout=5).status_code, 401)

    def test_delivery_and_improvement_settings_are_validated_and_persisted(self):
        payload = dict(config_version=self.db.get_config_version(), threshold=55,
                       freshness_hours=24, digest_enabled="on", digest_interval_hours=6,
                       price_improvement_usd=50, price_improvement_percent=15,
                       deduplication_window_hours=72, scan_interval_hours=8,
                       quiet_enabled="on", quiet_start=22, quiet_end=7, quiet_timezone="America/Denver")
        self.assertEqual(self.post("/settings", payload).status_code, 303)
        cfg = self.db.get_config({})
        self.assertEqual(cfg["filters"]["price_improvement_usd"], 50)
        self.assertEqual(cfg["filters"]["price_improvement_percent"], 15)
        self.assertEqual(cfg["scanner"]["scan_interval_hours"], 8)
        payload.update(config_version=self.db.get_config_version(), digest_interval_hours=24)
        self.assertEqual(self.post("/settings", payload).status_code, 422)
        payload.update(digest_interval_hours=6, alerts_paused="arbitrary")
        self.assertEqual(self.post("/settings", payload).status_code, 422)

    def test_database_provider_error_and_job_history_are_visible(self):
        self.db.record_provider_result("serpapi", False, error="authentication_failed")
        job_id = self.db.enqueue_scan(cooldown_seconds=0)
        job = self.db.claim_scan()
        self.assertEqual(job["id"], job_id)
        self.db.finish_scan(job_id, job["lease_token"], False, error="incomplete_coverage")
        response = self.client.get("/", auth=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn("authentication_failed", response.text)
        self.assertIn("incomplete_coverage", response.text)

    def test_invitation_quota_is_shared_through_database(self):
        for _ in range(5):
            self.assertTrue(self.db.provider_budget("admin_invite", 5, 100))
        self.assertEqual(self.post("/emails/add", {"email": "recipient@example.com"}).status_code, 429)
        self.assertEqual(self.db.list_recipients(), [])

    def test_internal_failures_have_only_safe_category_and_correlation(self):
        with patch.object(self.db, "list_recipients", side_effect=RuntimeError("smtp-password-or-stack-secret")):
            response = self.client.get("/", auth=self.auth)
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", response.text)
        self.assertEqual(response.json()["detail"], "Request failed")
        self.assertEqual(len(response.json()["request_id"]), 32)

    def test_startup_without_admin_hash_fails_closed(self):
        from dashboard import create_app
        from fastapi.testclient import TestClient
        with self.assertRaises(RuntimeError):
            with TestClient(create_app(replace(self.runtime, admin_password_hash=""), self.db)):
                pass

    def test_explicit_default_https_port_is_canonicalized(self):
        from dashboard import create_app
        from fastapi.testclient import TestClient
        app = create_app(replace(self.runtime, public_base_url="https://TESTSERVER:443/"), self.db)
        with TestClient(app, base_url="https://testserver", raise_server_exceptions=False, follow_redirects=False) as client:
            response = client.get("/", auth=self.auth)
            self.assertEqual(response.status_code, 200)
            token = re.search(r'name="csrf_token" value="([^"]+)"', response.text).group(1)
            self.assertEqual(client.post("/scan", auth=self.auth, data={"csrf_token": token},
                                         headers={"Origin": "https://testserver"}).status_code, 303)


@unittest.skipUnless(WEB_AVAILABLE, "Install locked web/test dependencies for middleware tests")
class MiddlewareTests(unittest.IsolatedAsyncioTestCase):
    async def invoke(self, *, headers=None, receive=None, application=None):
        from dashboard import SecurityMiddleware
        messages = []
        calls = []

        async def app(scope, incoming, send):
            calls.append(True)
            if application:
                return await application(scope, incoming, send)
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"ok"})

        async def default_receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {"type": "http", "path": "/scan", "scheme": "https", "method": "POST",
                 "query_string": b"", "headers": headers or [(b"host", b"testserver")]}
        owner = SimpleNamespace(state=SimpleNamespace(trusted_hosts={"testserver"}))
        await SecurityMiddleware(app, owner)(scope, receive or default_receive, send)
        return messages, calls

    async def test_slow_request_body_is_timed_out_without_reaching_application(self):
        async def stalled_receive():
            await asyncio.sleep(1)
            return {"type": "http.request", "body": b"", "more_body": True}
        with patch("dashboard.BODY_READ_SECONDS", 0.02):
            messages, calls = await self.invoke(receive=stalled_receive)
        self.assertEqual(messages[0]["status"], 408)
        self.assertEqual(calls, [])
        self.assertIn((b"cache-control", b"no-store"), messages[0]["headers"])

    async def test_fragmented_zero_byte_stream_is_bounded(self):
        async def empty_receive():
            return {"type": "http.request", "body": b"", "more_body": True}
        messages, calls = await self.invoke(receive=empty_receive)
        self.assertEqual(messages[0]["status"], 413)
        self.assertEqual(calls, [])

    async def test_conflicting_duplicate_or_mismatched_lengths_are_rejected(self):
        cases = [
            [(b"content-length", b"0"), (b"transfer-encoding", b"chunked")],
            [(b"content-length", b"0"), (b"content-length", b"0")],
            [(b"content-length", b"+0")],
            [(b"content-length", b"1")],
        ]
        for headers in cases:
            messages, calls = await self.invoke(headers=[(b"host", b"testserver"), *headers])
            self.assertEqual(messages[0]["status"], 400)
            self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
