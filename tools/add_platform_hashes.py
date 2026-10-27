"""Add verified musllinux wheel hashes without changing any locked versions.

Downloads wheels only (no installation, dependency code or source builds). A
trusted temporary CI workflow can publish each resulting lock in a diagnostic
CheckRun; repository contents are never written through the GitHub API.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener
import zipfile
from email.parser import Parser

from tools.publish_validation_report import NoRedirects, trusted_event
from tools.verify_lock import read_lock

ROOT = Path(__file__).resolve().parents[1]
FILES = ("requirements.txt", "requirements-dev.txt", "requirements-scraper.txt")


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def supplement() -> None:
    runtime = read_lock(ROOT / "requirements.txt")
    scraper = read_lock(ROOT / "requirements-scraper.txt")
    wanted = {**runtime, **scraper}
    with tempfile.TemporaryDirectory(prefix="flight-alert-musl-") as directory:
        folder = Path(directory)
        source = folder / "pins.txt"
        source.write_text("\n".join(f"{name}=={version}" for name, version in sorted(wanted.items())) + "\n")
        subprocess.run([
            sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-deps",
            "--only-binary=:all:", "--platform", "musllinux_1_2_x86_64", "--implementation", "cp",
            "--python-version", "3.12", "--abi", "cp312", "--dest", str(folder / "wheels"),
            "--index-url", "https://pypi.org/simple", "-r", str(source),
        ], check=True)
        hashes = {}
        for path in (folder / "wheels").glob("*.whl"):
            with zipfile.ZipFile(path) as archive:
                metadata = [item for item in archive.infolist() if item.filename.endswith(".dist-info/METADATA")]
                if len(metadata) != 1 or metadata[0].file_size > 1024 * 1024:
                    raise ValueError("Unexpected wheel metadata")
                parsed = Parser().parsestr(archive.read(metadata[0]).decode("utf-8"))
            name, version = canonical(parsed["Name"] or ""), parsed["Version"]
            if name not in wanted or version != wanted[name] or name in hashes:
                raise ValueError("Wheel does not match a unique reviewed version pin")
            hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        if set(hashes) != set(wanted):
            raise ValueError("Not every pinned runtime/scraper package has a compatible wheel")
    for filename in FILES:
        path = ROOT / filename
        lines = []
        for line in path.read_text().splitlines():
            if not line or line.startswith("#"):
                lines.append(line)
                continue
            pin = line.split(" --hash=", 1)[0]
            name = canonical(pin.split("==", 1)[0])
            existing = set(re.findall(r"--hash=sha256:([0-9a-f]{64})", line))
            if name in hashes:
                existing.add(hashes[name])
            lines.append(pin + " " + " ".join("--hash=sha256:" + value for value in sorted(existing)))
        path.write_text("\n".join(lines) + "\n")
        read_lock(path)
    print("Added actual compatible wheel hashes; exact dependency versions are unchanged")


def publish_existing() -> None:
    if not trusted_event(os.environ) or os.environ.get("GITHUB_REF") != "refs/heads/production-hardening":
        raise SystemExit("Supplemental hash publication is restricted to the trusted hardening branch")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise SystemExit("Scoped checks token missing")
    for filename in FILES:
        path = ROOT / filename
        read_lock(path)
        content = path.read_text()
        if len(content) > 60_000:
            raise SystemExit("Lock content exceeds check output limit")
        payload = {
            "name": "Supplemental wheel hashes: " + filename, "head_sha": os.environ["GITHUB_SHA"],
            "status": "completed", "conclusion": "success",
            "output": {"title": "Verified glibc and musl wheel lock", "summary": "Compatible wheel hashes for the existing exact versions; review before committing.", "text": content},
        }
        request = Request("https://api.github.com/repos/Naiguy1/Flight-Alerts/check-runs", method="POST",
                          data=json.dumps(payload).encode(), headers={"Authorization": "Bearer " + token,
                          "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                          "Content-Type": "application/json"})
        try:
            with build_opener(NoRedirects()).open(request, timeout=20) as response:
                if response.status != 201:
                    raise ValueError
        except (HTTPError, URLError, ValueError):
            raise SystemExit("Supplemental hash check publication failed; no token or API body logged") from None
    print("Published three bounded dependency-lock check reports")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish-existing", action="store_true")
    args = parser.parse_args()
    publish_existing() if args.publish_existing else supplement()


if __name__ == "__main__":
    main()
