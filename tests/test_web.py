"""Security boundary regressions; no external SMTP, flight calls or real credentials."""

import importlib.util
import json
import re
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from web.security import CSRFSigner, LoginLimiter, hash_password, valid_password_hash, verify_password

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
            self.assertTrue(limiter.allowed("test"))
            limiter.failed("test")
        self.assertFalse(limiter.allowed("test"))
        self.assertTrue(limiter.allowed("other"))


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
            self.assertEqual(self.client.post(path, data={}).status_code, 401, path)
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
        self.assertEqual(response.headers["referrer-policy"], "no-referrer")
        cookie = response.headers["set-cookie"]
        for attribute in ("Secure", "HttpOnly", "SameSite=strict"):
            self.assertIn(attribute, cookie)
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}, auth=self.auth).status_code, 400)
        self.assertEqual(self.client.get("http://testserver/", auth=self.auth).status_code, 400)

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


if __name__ == "__main__":
    unittest.main()
