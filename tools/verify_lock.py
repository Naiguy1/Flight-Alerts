"""Reject missing/unpinned dependencies, hashes, actions, and image digests."""
from __future__ import annotations

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r"([A-Za-z0-9_.-]+)==([A-Za-z0-9_.!+-]+) --hash=sha256:([0-9a-f]{64})")


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


def main() -> None:
    runtime = read_lock(ROOT / "requirements.txt")
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
