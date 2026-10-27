"""Gate scanner JSON with bounded findings and no matched secret contents."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path

from tools.ci_test_report import escape


def read_report(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("Security report missing or oversized")
    payload = json.loads(path.read_text())
    if not isinstance(payload, dict):
        raise ValueError("Invalid security report")
    return payload


def findings(scanner: str, payload: dict) -> list[str]:
    """Validate each scanner's shape and expose only non-sensitive identifiers."""
    results = []
    if scanner == "pip-audit":
        dependencies = payload.get("dependencies")
        if not isinstance(dependencies, list) or not dependencies:
            raise ValueError("Missing audited dependency inventory")
        for dependency in dependencies:
            if (not isinstance(dependency, dict) or not isinstance(dependency.get("name"), str)
                    or not isinstance(dependency.get("version"), str)
                    or not isinstance(dependency.get("vulns"), list)):
                raise ValueError("Invalid dependency audit record")
            for item in dependency["vulns"]:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("fix_versions", []), list):
                    raise ValueError("Invalid vulnerability record")
                fixes = ",".join(str(value) for value in item.get("fix_versions", [])) or "not available"
                results.append(f"{dependency['name']} {dependency['version']}: {item['id']}; fixes {fixes}"[:1000])
    elif scanner == "trivy":
        inventory = payload.get("Results")
        if (not isinstance(payload.get("SchemaVersion"), int)
                or not isinstance(payload.get("ArtifactName"), str)
                or not isinstance(inventory, list) or not inventory):
            raise ValueError("Missing image scan inventory")
        for result in inventory:
            if not isinstance(result, dict):
                raise ValueError("Invalid image scan result")
            vulnerabilities = result.get("Vulnerabilities") or []
            secrets = result.get("Secrets") or []
            if not isinstance(vulnerabilities, list) or not isinstance(secrets, list):
                raise ValueError("Invalid image findings")
            for item in vulnerabilities:
                if not isinstance(item, dict):
                    raise ValueError("Invalid image vulnerability")
                if item.get("Severity") in {"HIGH", "CRITICAL"}:
                    results.append(f"{item.get('Severity')} {item.get('VulnerabilityID')}: {item.get('PkgName')} {item.get('InstalledVersion')}; fix {item.get('FixedVersion') or 'not available'}"[:1000])
            for item in secrets:
                # Neither Match nor Code nor the scanner target path is exposed.
                if not isinstance(item, dict):
                    raise ValueError("Invalid secret finding")
                results.append("Secret finding in image (matched contents and path withheld)")
    else:
        raise ValueError("Unsupported scanner")
    return results


def image_observations(payload: dict) -> str:
    """Keep lower-severity findings visible without changing the release policy."""
    findings("trivy", payload)
    counts = Counter()
    details = []
    for result in payload["Results"]:
        for item in result.get("Vulnerabilities") or []:
            severity = item.get("Severity", "UNKNOWN")
            if severity not in {"UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"}:
                raise ValueError("Invalid vulnerability severity")
            counts[severity] += 1
            details.append(f"{severity} {item.get('VulnerabilityID')}: {item.get('PkgName')} "
                           f"{item.get('InstalledVersion')}; fix {item.get('FixedVersion') or 'not available'}"[:500])
    lines = ["Image vulnerability counts: " + ", ".join(
        f"{level}={counts[level]}" for level in ("UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"))]
    lines.extend(details[:40])
    if len(details) > 40:
        lines.append(f"{len(details) - 40} additional findings: inspect the scanner report privately.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scanner", choices=["pip-audit", "trivy"])
    parser.add_argument("report", type=Path)
    args = parser.parse_args()
    try:
        result = findings(args.scanner, read_report(args.report))
    except (OSError, ValueError, TypeError):
        print("::error title=Security release gate::Missing or malformed scanner report; gate failed closed")
        raise SystemExit(1) from None
    for finding in result[:30]:
        print("::error title=Security release gate::" + escape(finding))
    if len(result) > 30:
        print("::error title=Security release gate::Additional findings omitted; inspect scanner report privately")
    if result:
        raise SystemExit(1)
    print("No release-blocking findings in scanner report")


if __name__ == "__main__":
    main()
