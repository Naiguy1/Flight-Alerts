"""Publish safe CI diagnostics in check output readable by repository connectors.

Only trusted pushes/manual runs on main or production-hardening may publish.
Fork/untrusted PR events never publish. The exact same-repo hardening PR may
publish only synthetic test diagnostics. This helper does not alter
any release gate and treats missing reports as failure, never success.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from tools.ci_security_report import findings, image_observations, read_report
from tools.ci_codeql_report import scan_directory
from tools.ci_sbom_report import inventory

REPOSITORY = "Naiguy1/Flight-Alerts"
BRANCHES = {"refs/heads/main", "refs/heads/production-hardening"}
MAX_TEXT = 20_000


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def trusted_event(environment) -> bool:
    return (
        environment.get("GITHUB_REPOSITORY") == REPOSITORY
        and environment.get("GITHUB_EVENT_NAME") in {"push", "workflow_dispatch"}
        and environment.get("GITHUB_REF") in BRANCHES
        and bool(re.fullmatch(r"[0-9a-f]{40}", environment.get("GITHUB_SHA", "")))
    )


def trusted_pr_head(environment) -> str | None:
    """Allow synthetic test diagnostics only for the exact same-repository PR."""
    if (environment.get("GITHUB_REPOSITORY") != REPOSITORY
            or environment.get("GITHUB_EVENT_NAME") != "pull_request"):
        return None
    match = re.fullmatch(r"refs/pull/([1-9][0-9]*)/merge", environment.get("GITHUB_REF", ""))
    filename = environment.get("GITHUB_EVENT_PATH", "")
    if not match or not filename:
        return None
    try:
        path = Path(filename)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return None
        event = json.loads(path.read_text())
        pull = event["pull_request"]
        head, base = pull["head"], pull["base"]
        if (event.get("number") != int(match[1])
                or event["repository"]["full_name"] != REPOSITORY
                or head["repo"]["full_name"] != REPOSITORY
                or base["repo"]["full_name"] != REPOSITORY
                or head["ref"] != "production-hardening" or base["ref"] != "main"
                or not re.fullmatch(r"[0-9a-f]{40}", head["sha"])):
            return None
        return head["sha"]
    except (OSError, ValueError, TypeError, KeyError):
        return None


def test_report(path: Path) -> tuple[bool, str]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 8 * 1024 * 1024:
        raise ValueError("Test report missing or oversized")
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    if not cases:
        raise ValueError("Test report contains no test cases")
    failures = []
    skipped = 0
    for case in cases:
        skipped += int(case.find("skipped") is not None)
        for kind in ("failure", "error"):
            result = case.find(kind)
            if result is not None:
                title = f"{case.get('classname', '')}.{case.get('name', '')}"[:250]
                failures.append(title + "\n" + (result.text or result.get("message") or "Test failed")[-6000:])
    text = f"Tests: {len(cases)}; failures/errors: {len(failures)}; skipped: {skipped}.\n"
    if failures:
        text += "\n\n".join(failures[:10])
    return not failures, text[:MAX_TEXT]


def build_report(kind: str, folder: Path, gate_result: str) -> dict:
    success = gate_result == "success"
    try:
        if kind == "Tests":
            passed, text = test_report(folder / "test-results.xml")
            success = success and passed
        elif kind == "Security":
            reports = [("Production packages", "pip-audit", "pip-audit.json"),
                       ("Optional scraper packages", "pip-audit", "pip-audit-scraper.json")]
            sections = []
            for label, scanner, filename in reports:
                try:
                    result = findings(scanner, read_report(folder / filename))
                except (OSError, ValueError, TypeError):
                    success = False
                    sections.append(label + ": required scanner report missing or malformed.")
                    continue
                success = success and not result
                sections.append(label + ": " + ("no known package vulnerabilities" if not result else "\n" + "\n".join(result[:30])))
            sections.append("The Security job also requires the full-history secret scan; job conclusion is authoritative.")
            text = "\n\n".join(sections)
        elif kind == "Image":
            sections = []
            for label, filename in (("Application image", "trivy-results.json"), ("Proxy image", "trivy-proxy-results.json")):
                try:
                    scanner_report = read_report(folder / filename)
                    result = findings("trivy", scanner_report)
                    observations = image_observations(scanner_report)
                except (OSError, ValueError, TypeError):
                    success = False
                    sections.append(label + ": required scanner report missing or malformed.")
                    continue
                success = success and not result
                sections.append(label + ": " + ("no release-blocking image findings" if not result else "\n" + "\n".join(result[:30])))
                sections.append(observations)
            try:
                sections.append(inventory(folder / "sbom.cdx.json"))
            except (OSError, ValueError, TypeError):
                success = False
                sections.append("SBOM: required component inventory missing or malformed.")
            text = "\n\n".join(sections)
        elif kind == "CodeQL":
            result = scan_directory(folder / "codeql-results")
            success = success and not result
            text = "No unsuppressed high/critical CodeQL findings." if not result else "\n".join(result[:30])
        else:
            raise ValueError("Unknown validation kind")
    except (OSError, ValueError, TypeError, AttributeError, ET.ParseError):
        success = False
        text = "Required diagnostic report is missing or malformed. Inspect the failed workflow step; no success is inferred."
    # Only synthetic test diagnostics and selected scanner identifiers reach this
    # public output. Scanner matched secrets, source snippets and paths never do.
    return {
        "name": "Validation report: " + kind,
        "status": "completed",
        "conclusion": "success" if success else "failure",
        "output": {
            "title": kind + (" validation passed" if success else " validation requires attention"),
            "summary": f"Release gate job result: {gate_result}.\n\n" + text[:MAX_TEXT],
            "text": text[:MAX_TEXT],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("kind", choices=["Tests", "Security", "Image", "CodeQL"])
    args = parser.parse_args()
    pr_head = trusted_pr_head(os.environ) if args.kind == "Tests" else None
    if not trusted_event(os.environ) and pr_head is None:
        print("Diagnostic publication skipped for an untrusted event or branch")
        return
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise SystemExit("A scoped checks token is required; no credential diagnostics emitted")
    payload = build_report(args.kind, Path.cwd(), os.environ.get("GATE_RESULT", "failure"))
    payload["head_sha"] = pr_head or os.environ["GITHUB_SHA"]
    if pr_head is not None:
        payload["name"] = "Validation report: PR Tests"
    request = Request(
        f"https://api.github.com/repos/{REPOSITORY}/check-runs",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"},
    )
    try:
        with build_opener(NoRedirects()).open(request, timeout=20) as response:
            if response.status != 201:
                raise ValueError
    except (HTTPError, URLError, ValueError):
        raise SystemExit("CI diagnostic publication failed; no credentials or API body logged") from None
    print("Published bounded validation diagnostics")


if __name__ == "__main__":
    main()
