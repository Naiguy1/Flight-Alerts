"""Reject missing/unpinned dependencies, hashes, actions, and image digests."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+-]+)((?: --hash=sha256:[0-9a-f]{64})+)")


def read_lock(path: Path) -> dict[str, str]:
    result = {}
    for line in path.read_text().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"Unpinned or unhashed requirement in {path.name}")
        name, version, _ = match.groups()
        key = name.lower().replace("_", "-")
        if key in result:
            raise ValueError("Duplicate requirement")
        result[key] = version
    if not result:
        raise ValueError("Empty dependency lock")
    return result


def verify_installed(path: Path, runtime: dict[str, str]) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise ValueError("Installed package inventory missing or oversized")
    inventory = json.loads(path.read_text())
    if not isinstance(inventory, list) or not inventory:
        raise ValueError("Missing installed package inventory")
    observed = {}
    for item in inventory:
        if (not isinstance(item, dict) or not isinstance(item.get("name"), str)
                or not isinstance(item.get("version"), str)):
            raise ValueError("Invalid installed package")
        name = item["name"].lower().replace("_", "-")
        if name in observed:
            raise ValueError("Duplicate installed package")
        observed[name] = item["version"]
    # pip is a build-only tool. Everything else must be present at its exact
    # locked version; unexpected distributions or residual installers fail.
    expected = {name: version for name, version in runtime.items() if name != "pip"}
    if observed != expected:
        raise ValueError("Installed runtime inventory differs from the lock")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--installed", type=Path, help="Verify a final image's distribution inventory")
    args = parser.parse_args()
    runtime = read_lock(ROOT / "requirements.txt")
    if args.installed is not None:
        verify_installed(args.installed, runtime)
    for name in ("requirements-dev.txt", "requirements-scraper.txt"):
        extras = read_lock(ROOT / name)
        if any(extras.get(package) != version for package, version in runtime.items()):
            raise ValueError("Runtime dependencies differ across locked environments")
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        for action in re.findall(r"uses:\s*([^\s#]+)", path.read_text()):
            if not action.startswith("./") and not re.fullmatch(r"[^@]+@[0-9a-f]{40}", action):
                raise ValueError(f"Unpinned GitHub Action in {path.name}")
    for path in (ROOT / "Dockerfile", ROOT / "Dockerfile.proxy"):
        for image in re.findall(r"^FROM\s+(\S+)", path.read_text(), re.M):
            if not re.search(r"@sha256:[0-9a-f]{64}$", image):
                raise ValueError("Unpinned container base image")
    print("Dependency hashes, runtime consistency, Action SHAs, and image digests verified")


if __name__ == "__main__":
    main()
