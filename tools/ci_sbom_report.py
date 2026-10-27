"""Validate a CycloneDX inventory and publish bounded package/version evidence."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re


def inventory(path: Path) -> str:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
        raise ValueError("SBOM missing or oversized")
    raw = path.read_bytes()
    report = json.loads(raw)
    if (not isinstance(report, dict) or report.get("bomFormat") != "CycloneDX"
            or report.get("specVersion") not in {"1.4", "1.5", "1.6", "1.7"}
            or not isinstance(report.get("components"), list)
            or not 1 <= len(report["components"]) <= 10000):
        raise ValueError("Missing CycloneDX component inventory")
    packages = set()
    for item in report["components"]:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise ValueError("Invalid SBOM component")
        if item.get("type") not in {"library", "application", "framework", "operating-system"}:
            continue
        name, version = item["name"], item.get("version", "unspecified")
        if not isinstance(version, str) or not version:
            raise ValueError("Invalid SBOM package version")
        # Emit package identifiers only; never artifact paths, source locations,
        # arbitrary properties or scanner secret matches into public diagnostics.
        if any(not re.fullmatch(r"[A-Za-z0-9._+:/@~()=-]{1,200}", value)
               for value in (name, version)):
            raise ValueError("Invalid SBOM package identifier")
        packages.add((name, version))
    if not packages:
        raise ValueError("SBOM contains no package inventory")
    lines = [f"CycloneDX {report['specVersion']}: {len(packages)} distinct package/version entries.",
             "SBOM SHA256: " + hashlib.sha256(raw).hexdigest()]
    ordered = sorted(packages)
    lines.extend(f"- {name} {version}" for name, version in ordered[:150])
    if len(ordered) > 150:
        lines.append(f"{len(ordered) - 150} additional entries: inspect the image-sbom artifact.")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--proxy-packages", type=Path)
    args = parser.parse_args()
    try:
        summary = inventory(args.report)
        if args.proxy_packages is not None:
            summary += "\n\n" + proxy_assessment(args.proxy_packages)
    except (OSError, ValueError, TypeError):
        raise SystemExit("Release inventory missing, malformed or unsafe; gate failed closed") from None
    print(summary)


def proxy_assessment(path: Path) -> str:
    """Verify package-level applicability of the unmaintained OpenPGP advisory."""
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Proxy dependency graph missing or oversized")
    raw = path.read_bytes()
    packages = set(raw.decode("ascii").splitlines())
    if "github.com/caddyserver/caddy/v2/cmd/caddy" not in packages:
        raise ValueError("Missing proxy main package")
    if any(package == "golang.org/x/crypto/openpgp"
           or package.startswith("golang.org/x/crypto/openpgp/") for package in packages):
        raise ValueError("Unmaintained OpenPGP package is linked into the proxy")
    return (f"GO-2026-5932 applicability: affected OpenPGP packages absent from {len(packages)} "
            "proxy build dependencies. Module-level scanner finding remains visible.\n"
            "Proxy dependency graph SHA256: " + hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    main()
