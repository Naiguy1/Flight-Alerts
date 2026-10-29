"""Operational recovery and deployment boundary regressions (no network)."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch

from tools.backup import create_backup, restore_backup, verify_backup
from tools.ci_test_report import escape, escape_property
from tools.ci_security_report import findings, image_observations
from tools.ci_sbom_report import inventory, proxy_assessment
from tools.ci_codeql_report import findings as codeql_findings
from tools.publish_validation_report import build_report, trusted_event, trusted_pr_head
from tools.healthcheck import worker_is_healthy
from tools.verify_lock import read_lock, verify_installed

ROOT = Path(__file__).resolve().parents[1]


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.source = self.folder / "source.db"
        with sqlite3.connect(self.source) as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute("CREATE TABLE sample(value TEXT)")
            database.execute("INSERT INTO sample VALUES (?)", ("synthetic restore data",))

    def tearDown(self):
        self.temporary.cleanup()

    def test_consistent_backup_restore_and_private_mode(self):
        backup, restored = self.folder / "backup.db", self.folder / "restored.db"
        digest = create_backup(self.source, backup)
        self.assertEqual(len(digest), 64)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        verify_backup(backup)
        restore_backup(backup, restored, services_stopped=True)
        with sqlite3.connect(restored) as database:
            self.assertEqual(database.execute("SELECT value FROM sample").fetchone()[0], "synthetic restore data")

    def test_no_overwrite_and_requires_offline_confirmation(self):
        backup = self.folder / "backup.db"
        create_backup(self.source, backup)
        with self.assertRaises(FileExistsError):
            create_backup(self.source, backup)
        with self.assertRaises(ValueError):
            restore_backup(backup, self.folder / "new.db")
        with self.assertRaises(FileExistsError):
            restore_backup(backup, self.source, services_stopped=True)

    def test_restore_refuses_leftover_wal(self):
        destination = self.folder / "restored.db"
        Path(str(destination) + "-wal").touch()
        with self.assertRaises(FileExistsError):
            restore_backup(self.source, destination, services_stopped=True)

    def test_corrupt_backup_and_symlink_source_rejected(self):
        corrupt = self.folder / "corrupt.db"
        corrupt.write_bytes(b"not a sqlite database")
        with self.assertRaises(sqlite3.DatabaseError):
            verify_backup(corrupt)
        link = self.folder / "source-link.db"
        link.symlink_to(self.source)
        with self.assertRaises(ValueError):
            create_backup(link, self.folder / "backup.db")


class HealthTests(unittest.TestCase):
    def test_missing_stale_future_invalid_and_fresh_heartbeat(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heartbeat.json"
            self.assertFalse(worker_is_healthy(path))
            for payload in ({}, {"timestamp": "bad"}, {"timestamp": time.time() - 121},
                            {"timestamp": time.time() + 31}):
                path.write_text(json.dumps(payload))
                self.assertFalse(worker_is_healthy(path))
            for stamp in (time.time(), datetime.now(timezone.utc).isoformat()):
                path.write_text(json.dumps({"timestamp": stamp}))
                self.assertTrue(worker_is_healthy(path))

    def test_symlink_and_oversized_heartbeat_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            target, link = Path(directory) / "target.json", Path(directory) / "link.json"
            target.write_text(json.dumps({"timestamp": time.time()}))
            link.symlink_to(target)
            self.assertFalse(worker_is_healthy(link))
            target.write_text("x" * 4097)
            self.assertFalse(worker_is_healthy(target))


class SupplyChainTests(unittest.TestCase):
    def test_final_image_inventory_requires_every_locked_runtime_package_and_no_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "installed.json"
            runtime = {"pip": "26.2.2", "example": "1.0"}
            path.write_text(json.dumps([{"name": "example", "version": "1.0"}]))
            verify_installed(path, runtime)
            for inventory in ([], [{"name": "example", "version": "0.9"}],
                              [{"name": "example", "version": "1.0"}, {"name": "pip", "version": "26.2.2"}],
                              [{"name": "example", "version": "1.0"}, {"name": "unlocked", "version": "1"}],
                              [{"name": "example", "version": "1.0"}, {"name": "example", "version": "1.0"}]):
                path.write_text(json.dumps(inventory))
                with self.subTest(inventory=inventory), self.assertRaises(ValueError):
                    verify_installed(path, runtime)

    def test_real_lock_shape_and_runtime_consistency(self):
        runtime = read_lock(ROOT / "requirements.txt")
        for name in ("requirements-dev.txt", "requirements-scraper.txt"):
            extra = read_lock(ROOT / name)
            self.assertTrue(all(extra.get(package) == version for package, version in runtime.items()))

    def test_lock_rejects_unhashed_or_mutable_requirements(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.txt"
            for content in ("requests>=2\n", "requests==2.32.4\n", "", "-r other.txt\n"):
                path.write_text(content)
                with self.assertRaises(ValueError):
                    read_lock(path)

    def test_docker_explicit_copy_and_secret_state_exclusions(self):
        dockerfile = (ROOT / "Dockerfile").read_text()
        self.assertNotIn("COPY . .", dockerfile)
        self.assertIn("USER 10001:10001", dockerfile)
        self.assertIn("--require-hashes", dockerfile)
        for ignore in (".gitignore", ".dockerignore"):
            text = (ROOT / ignore).read_text()
            for entry in (".env", "secrets/", "data/", "*.db"):
                self.assertIn(entry, text)

    def test_compose_separates_secrets_and_uses_exact_proxy(self):
        import yaml
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())
        web = compose["services"]["flight_web"]
        worker = compose["services"]["flight_worker"]
        self.assertEqual(set(web["secrets"]), {"token_secret", "admin_password_hash"})
        self.assertNotIn("ports", web)
        self.assertTrue(web["read_only"])
        self.assertEqual(web["cap_drop"], ["ALL"])
        self.assertNotIn("env_file", web)
        self.assertIn("smtp_password", worker["secrets"])
        self.assertIn("172.30.80.2", web["command"])
        self.assertNotIn("*", web["command"])
        self.assertTrue(compose["networks"]["app"]["internal"])
        self.assertNotIn("jellyfin_default", compose["networks"])
        proxy = compose["services"]["proxy"]
        self.assertEqual(proxy["build"]["dockerfile"], "Dockerfile.proxy")
        proxy_image = (ROOT / "Dockerfile.proxy").read_text()
        self.assertIn("CGO_ENABLED=0", proxy_image)
        self.assertIn("COPY --from=builder /out/caddy /usr/bin/caddy", proxy_image)
        self.assertIn("golang.org/x/net@v0.60.0", proxy_image)
        self.assertIn("request delete", (ROOT / "docs/Caddyfile").read_text())
        self.assertIn("chmod 0700 /data/caddy /config/caddy", proxy_image)

    def test_lock_workflow_has_no_repository_write_or_pr_trigger(self):
        text = (ROOT / ".github/workflows/lock-dependencies.yml").read_text()
        self.assertIn("contents: read", text)
        self.assertNotIn("contents: write", text)
        self.assertNotIn("pull_request", text)
        self.assertNotIn("publish_dependency_locks", text)

    def test_annotation_escaping_prevents_workflow_command_injection(self):
        self.assertEqual(escape("a%\nb\rc"), "a%25%0Ab%0Dc")
        self.assertEqual(escape_property("name,file=x:1\n"), "name%2Cfile=x%3A1%0A")

    def test_security_reports_fail_closed_on_missing_inventory(self):
        for scanner, payload in (("pip-audit", {}), ("pip-audit", {"dependencies": []}),
                                 ("trivy", {}), ("trivy", {"Results": []}),
                                 ("trivy", {"SchemaVersion": 2, "ArtifactName": "image", "Results": []})):
            with self.subTest(scanner=scanner), self.assertRaises(ValueError):
                findings(scanner, payload)

    def test_image_secret_contents_and_paths_never_enter_diagnostics(self):
        payload = {"SchemaVersion": 2, "ArtifactName": "application", "Results": [{
            "Target": "/private/SYNTHETIC_SECRET", "Secrets": [{"RuleID": "synthetic", "Match": "SYNTHETIC_SECRET"}],
        }]}
        result = "\n".join(findings("trivy", payload))
        self.assertIn("Secret finding", result)
        self.assertNotIn("SYNTHETIC_SECRET", result)
        self.assertNotIn("/private", result)

    def test_sbom_inventory_is_required_and_does_not_publish_properties(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sbom.json"
            with self.assertRaises(ValueError):
                inventory(path)
            for payload in ({}, {"bomFormat": "CycloneDX", "specVersion": "1.6", "components": []}):
                path.write_text(json.dumps(payload))
                with self.assertRaises(ValueError):
                    inventory(path)
            path.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6", "components": [{
                "type": "library", "name": "example-package", "version": "1.2.3",
                "properties": [{"name": "private", "value": "SYNTHETIC_SECRET"}],
            }]}))
            result = inventory(path)
            self.assertIn("example-package 1.2.3", result)
            self.assertIn("SBOM SHA256:", result)
            self.assertNotIn("SYNTHETIC_SECRET", result)

    def test_sbom_rejects_malformed_components_and_command_injection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sbom.json"
            for component in (None, {}, {"type": "library", "name": "example", "version": []},
                              {"type": "library", "name": "example\n::error::injection", "version": "1"}):
                path.write_text(json.dumps({"bomFormat": "CycloneDX", "specVersion": "1.6",
                                            "components": [component]}))
                with self.subTest(component=component), self.assertRaises(ValueError):
                    inventory(path)

    def test_image_diagnostics_require_inspectable_sbom_even_with_clean_scans(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            clean = {"SchemaVersion": 2, "ArtifactName": "image", "Results": [{"Target": "image"}]}
            for name in ("trivy-results.json", "trivy-proxy-results.json"):
                (folder / name).write_text(json.dumps(clean))
            report = build_report("Image", folder, "success")
            self.assertEqual(report["conclusion"], "failure")
            self.assertIn("inventory missing", report["output"]["text"])
            (folder / "sbom.cdx.json").write_text(json.dumps({
                "bomFormat": "CycloneDX", "specVersion": "1.6", "components": [
                    {"type": "library", "name": "example", "version": "1"},
                ],
            }))
            (folder / "proxy-packages.txt").write_text("github.com/caddyserver/caddy/v2/cmd/caddy\n")
            self.assertEqual(build_report("Image", folder, "success")["conclusion"], "success")

    def test_proxy_openpgp_assessment_requires_actual_main_package_and_rejects_affected_packages(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "packages.txt"
            with self.assertRaises(ValueError):
                proxy_assessment(path)
            for contents in ("", "golang.org/x/crypto/ssh\n",
                             "github.com/caddyserver/caddy/v2/cmd/caddy\ngolang.org/x/crypto/openpgp\n",
                             "github.com/caddyserver/caddy/v2/cmd/caddy\ngolang.org/x/crypto/openpgp/packet\n"):
                path.write_text(contents)
                with self.subTest(contents=contents), self.assertRaises(ValueError):
                    proxy_assessment(path)
            path.write_text("github.com/caddyserver/caddy/v2/cmd/caddy\ngolang.org/x/crypto/ssh\n")
            self.assertIn("affected OpenPGP packages absent", proxy_assessment(path))

    def test_image_report_fails_without_proxy_applicability_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            clean = {"SchemaVersion": 2, "ArtifactName": "image", "Results": [{"Target": "image"}]}
            for name in ("trivy-results.json", "trivy-proxy-results.json"):
                (folder / name).write_text(json.dumps(clean))
            (folder / "sbom.cdx.json").write_text(json.dumps({
                "bomFormat": "CycloneDX", "specVersion": "1.7",
                "components": [{"type": "library", "name": "example", "version": "1"}],
            }))
            report = build_report("Image", folder, "success")
            self.assertEqual(report["conclusion"], "failure")
            self.assertIn("applicability evidence missing", report["output"]["text"])

    def test_lower_severity_image_findings_remain_visible_without_hiding_gate_failures(self):
        report = {"SchemaVersion": 2, "ArtifactName": "image", "Results": [{"Vulnerabilities": [
            {"Severity": "MEDIUM", "VulnerabilityID": "CVE-SYNTHETIC", "PkgName": "example",
             "InstalledVersion": "1", "FixedVersion": "2"},
        ]}]}
        self.assertEqual(findings("trivy", report), [])
        summary = image_observations(report)
        self.assertIn("MEDIUM=1", summary)
        self.assertIn("CVE-SYNTHETIC: example 1; fix 2", summary)
        report["Results"][0]["Vulnerabilities"][0]["Severity"] = "HIGH"
        self.assertEqual(len(findings("trivy", report)), 1)

    def test_diagnostic_publication_rejects_forks_pull_requests_and_other_refs(self):
        trusted = {"GITHUB_REPOSITORY": "Naiguy1/Flight-Alerts", "GITHUB_EVENT_NAME": "push",
                   "GITHUB_REF": "refs/heads/production-hardening", "GITHUB_SHA": "a" * 40}
        self.assertTrue(trusted_event(trusted))
        self.assertTrue(trusted_event({**trusted, "GITHUB_REF":
                                      "refs/heads/fix/dashboard-90-day-deals-and-form-origin"}))
        for key, value in (("GITHUB_EVENT_NAME", "pull_request"), ("GITHUB_EVENT_NAME", "pull_request_target"),
                           ("GITHUB_REPOSITORY", "untrusted/fork"), ("GITHUB_REF", "refs/heads/other"),
                           ("GITHUB_SHA", "not-a-commit")):
            with self.subTest(key=key, value=value):
                self.assertFalse(trusted_event({**trusted, key: value}))

    def test_diagnostics_do_not_make_failed_or_missing_test_gates_green(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            self.assertEqual(build_report("Tests", folder, "success")["conclusion"], "failure")
            (folder / "test-results.xml").write_text('<testsuite><testcase name="safe"/></testsuite>')
            self.assertEqual(build_report("Tests", folder, "success")["conclusion"], "success")
            self.assertEqual(build_report("Tests", folder, "failure")["conclusion"], "failure")
            (folder / "test-results.xml").write_text('<testsuite><testcase name="broken"><failure>Synthetic failure</failure></testcase></testsuite>')
            report = build_report("Tests", folder, "success")
            self.assertEqual(report["conclusion"], "failure")
            self.assertIn("Synthetic failure", report["output"]["text"])

    def test_available_security_findings_survive_another_missing_report(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder / "pip-audit.json").write_text(json.dumps({"dependencies": [{"name": "example", "version": "1", "vulns": [{"id": "CVE-SYNTHETIC", "fix_versions": ["2"]}]}]}))
            report = build_report("Security", folder, "failure")
            self.assertEqual(report["conclusion"], "failure")
            self.assertIn("CVE-SYNTHETIC", report["output"]["text"])
            self.assertIn("missing", report["output"]["text"])

    def test_codeql_gate_requires_valid_sarif_and_blocks_high_security(self):
        with self.assertRaises(ValueError):
            codeql_findings({})
        report = {"version": "2.1.0", "runs": [{"tool": {"driver": {"rules": [{
            "id": "py/example", "properties": {"security-severity": "8.1", "tags": ["security"]},
        }]}}, "results": [{"ruleId": "py/example", "message": {"text": "SYNTHETIC_SECRET"}}]}]}
        result = codeql_findings(report)
        self.assertEqual(len(result), 1)
        self.assertIn("py/example", result[0])
        self.assertNotIn("SYNTHETIC_SECRET", result[0])
        report["runs"][0]["tool"]["driver"]["rules"][0]["properties"]["security-severity"] = "5.0"
        self.assertEqual(codeql_findings(report), [])


    def test_codeql_extension_rules_are_resolved_and_unknown_rules_fail_closed(self):
        report = {"version": "2.1.0", "runs": [{"tool": {"driver": {"rules": []}, "extensions": [{"rules": [{
            "id": "py/extension", "properties": {"security-severity": "8.1"},
        }]}]}, "results": [{"ruleId": "py/extension", "locations": [{"physicalLocation": {
            "artifactLocation": {"uri": "dashboard.py"}, "region": {"startLine": 42},
        }}]}]}]}
        result = codeql_findings(report)
        self.assertEqual(len(result), 1)
        self.assertIn("dashboard.py:42", result[0])
        report["runs"][0]["results"] = [{"rule": {"index": 0, "toolComponent": {"index": 0}}}]
        self.assertIn("py/extension", codeql_findings(report)[0])
        report["runs"][0]["results"] = [{"ruleId": "py/missing"}]
        self.assertIn("failed closed", codeql_findings(report)[0])


    def test_pr_diagnostics_are_bound_to_exact_same_repo_head(self):
        import copy
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "event.json"
            environment = {"GITHUB_REPOSITORY": "Naiguy1/Flight-Alerts", "GITHUB_EVENT_NAME": "pull_request",
                           "GITHUB_REF": "refs/pull/1/merge", "GITHUB_EVENT_PATH": str(path)}
            event = {"number": 1, "repository": {"full_name": "Naiguy1/Flight-Alerts"}, "pull_request": {
                "head": {"repo": {"full_name": "Naiguy1/Flight-Alerts"}, "ref": "production-hardening", "sha": "a" * 40},
                "base": {"repo": {"full_name": "Naiguy1/Flight-Alerts"}, "ref": "main"}}}
            path.write_text(json.dumps(event))
            self.assertEqual(trusted_pr_head(environment), "a" * 40)
            for field, value in (("ref", "other"), ("sha", "malformed"), ("repo", {"full_name": "untrusted/fork"})):
                changed = copy.deepcopy(event)
                changed["pull_request"]["head"][field] = value
                path.write_text(json.dumps(changed))
                self.assertIsNone(trusted_pr_head(environment))
            path.write_text(json.dumps(event))
            self.assertIsNone(trusted_pr_head({**environment, "GITHUB_REF": "refs/pull/2/merge"}))
            self.assertIsNone(trusted_pr_head({**environment, "GITHUB_EVENT_NAME": "pull_request_target"}))


    def test_ci_rejects_skipped_http_coverage(self):
        from tools.ci_test_report import main
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.xml"
            path.write_text('<testsuite><testcase name="http"><skipped/></testcase></testsuite>')
            with patch("sys.argv", ["report", str(path), "--require-no-skips"]), patch("sys.stdout", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                main()
            self.assertEqual(error.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
