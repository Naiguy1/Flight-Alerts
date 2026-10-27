"""Apply/check the reviewed main-branch protection policy with GitHub admin access.

Requires GITHUB_TOKEN in the process environment. Tokens are never command-line
arguments, printed, written to disk, or sent outside api.github.com.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, HTTPRedirectHandler


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        return None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--apply", action="store_true", help="Apply policy; default only checks")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise SystemExit("Invalid owner/repository")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise SystemExit("GITHUB_TOKEN with repository administration permission is required")
    policy = json.loads((Path(__file__).resolve().parents[1] / ".github/branch-protection.json").read_text())
    url = f"https://api.github.com/repos/{args.repository}/branches/main/protection"
    request = Request(url, method="PUT" if args.apply else "GET",
                      data=json.dumps(policy).encode() if args.apply else None,
                      headers={"Authorization": "Bearer " + token,
                               "Accept": "application/vnd.github+json",
                               "X-GitHub-Api-Version": "2022-11-28",
                               "Content-Type": "application/json"})
    try:
        with build_opener(NoRedirects()).open(request, timeout=20) as response:
            current = json.load(response)
    except HTTPError as error:
        raise SystemExit(f"GitHub rejected protection request (HTTP {error.code}); no token/error body printed") from None
    except URLError:
        raise SystemExit("GitHub protection request failed; no sensitive diagnostics printed") from None
    expected = set(policy["required_status_checks"]["contexts"])
    observed = set(current.get("required_status_checks", {}).get("contexts", []))
    enabled = (expected <= observed and current.get("enforce_admins", {}).get("enabled")
               and current.get("required_status_checks", {}).get("strict")
               and current.get("required_pull_request_reviews", {}).get("required_approving_review_count", 0) >= 1
               and current.get("required_pull_request_reviews", {}).get("dismiss_stale_reviews", False)
               and current.get("required_linear_history", {}).get("enabled", False)
               and current.get("required_conversation_resolution", {}).get("enabled", False)
               and not current.get("allow_force_pushes", {}).get("enabled", False)
               and not current.get("allow_deletions", {}).get("enabled", False))
    if not enabled:
        raise SystemExit("Branch protection does not match required checks/review/admin policy")
    print("Main-branch protection policy verified")


if __name__ == "__main__":
    main()
