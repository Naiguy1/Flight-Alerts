"""Publish only generated requirement locks from the trusted branch-only workflow.

No fork/PR events, arbitrary paths, forced updates, or credential output. A stale
branch head aborts; the update must fast-forward from the workflow's exact SHA.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

BRANCH = "production-hardening"
FILES = ("requirements.txt", "requirements-dev.txt", "requirements-scraper.txt")
ROOT = Path(__file__).resolve().parents[1]


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def main() -> None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    sha = os.environ.get("GITHUB_SHA", "")
    token = os.environ.get("GITHUB_TOKEN", "")
    if (os.environ.get("GITHUB_EVENT_NAME") != "push"
            or os.environ.get("GITHUB_REF") != "refs/heads/" + BRANCH
            or repository != "Naiguy1/Flight-Alerts"
            or not re.fullmatch(r"[0-9a-f]{40}", sha) or not token):
        raise SystemExit("Lock publication is restricted to the trusted repository branch push")
    opener = build_opener(NoRedirects())

    def api(path: str, *, method: str = "GET", data=None):
        request = Request("https://api.github.com/repos/" + repository + path, method=method,
                          data=None if data is None else json.dumps(data).encode(),
                          headers={"Authorization": "Bearer " + token,
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28",
                                   "Content-Type": "application/json"})
        try:
            with opener.open(request, timeout=30) as response:
                return json.load(response)
        except HTTPError as error:
            raise SystemExit(f"Lock publication rejected (HTTP {error.code}); no response body logged") from None
        except URLError:
            raise SystemExit("Lock publication network failure; no sensitive diagnostics logged") from None

    if api("/git/ref/heads/" + BRANCH)["object"]["sha"] != sha:
        raise SystemExit("Branch advanced; rerun lock resolution on its current head")
    base = api("/git/commits/" + sha)["tree"]["sha"]
    entries = []
    for name in FILES:
        path = ROOT / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise SystemExit("Generated dependency lock is missing or oversized")
        content = path.read_text()
        pins = [line for line in content.splitlines() if line and not line.startswith("#")]
        if not pins or any(not re.fullmatch(r"[A-Za-z0-9_.-]+==[A-Za-z0-9_.!+-]+ --hash=sha256:[0-9a-f]{64}", line) for line in pins):
            raise SystemExit("Generated dependency lock contains unexpected syntax")
        entries.append({"path": name, "mode": "100644", "type": "blob", "content": content})
    tree = api("/git/trees", method="POST", data={"base_tree": base, "tree": entries})
    if tree["sha"] == base:
        print("Dependency locks already match resolved environment")
        return
    commit = api("/git/commits", method="POST", data={
        "message": "Lock reviewed runtime and test dependencies with resolver hashes",
        "tree": tree["sha"], "parents": [sha]})
    if api("/git/ref/heads/" + BRANCH)["object"]["sha"] != sha:
        raise SystemExit("Branch advanced before publication; no ref changed")
    api("/git/refs/heads/" + BRANCH, method="PATCH", data={"sha": commit["sha"], "force": False})
    print("Published generated dependency locks at " + commit["sha"])


if __name__ == "__main__":
    main()
