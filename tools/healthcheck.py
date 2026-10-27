"""Small dependency-free container probes; never print credentials or DB paths."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def worker_is_healthy(path: Path, max_age_seconds: int = 120) -> bool:
    try:
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 4096:
            return False
        payload = json.loads(path.read_text())
        stamp = payload.get("timestamp", payload.get("updated_at", payload.get("heartbeat_at")))
        if isinstance(stamp, (int, float)):
            observed = float(stamp)
        elif isinstance(stamp, str):
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            observed = (parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None
                        else parsed.astimezone(timezone.utc)).timestamp()
        else:
            return False
        age = time.time() - observed
        return -30 <= age <= max_age_seconds
    except (OSError, ValueError, TypeError, OverflowError):
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["worker", "web"])
    args = parser.parse_args()
    healthy = False
    if args.role == "worker":
        healthy = worker_is_healthy(Path(os.getenv("WORKER_HEARTBEAT_PATH", "data/worker-heartbeat.json")))
    else:
        try:
            host = urlsplit(os.getenv("PUBLIC_BASE_URL", "https://localhost")).netloc
            request = Request("http://127.0.0.1:8000/health/ready", headers={"Host": host})
            with urlopen(request, timeout=3) as response:
                healthy = response.status == 200
        except Exception:
            healthy = False
    raise SystemExit(0 if healthy else 1)


if __name__ == "__main__":
    main()
