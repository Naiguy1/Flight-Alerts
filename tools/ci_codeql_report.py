"""Fail the release on high/critical SARIF security findings, without snippets."""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from tools.ci_test_report import escape


def findings(payload: dict) -> list[str]:
    if not isinstance(payload, dict) or payload.get("version") != "2.1.0" or not isinstance(payload.get("runs"), list) or not payload["runs"]:
        raise ValueError("Invalid SARIF inventory")
    result = []
    for run in payload["runs"]:
        if not isinstance(run, dict) or not isinstance(run.get("results", []), list):
            raise ValueError("Invalid SARIF results")
        rules = run.get("tool", {}).get("driver", {}).get("rules", [])
        if not isinstance(rules, list):
            raise ValueError("Invalid SARIF rules")
        by_id = {rule.get("id"): rule for rule in rules if isinstance(rule, dict)}
        for item in run.get("results", []):
            if not isinstance(item, dict):
                raise ValueError("Invalid SARIF result")
            if any(isinstance(suppression, dict) and suppression.get("status") == "accepted" for suppression in item.get("suppressions", [])):
                continue
            rule_id = item.get("ruleId", "unclassified")
            rule = by_id.get(rule_id)
            if rule is None:
                index = item.get("ruleIndex")
                rule = rules[index] if type(index) is int and 0 <= index < len(rules) else {}
            properties = rule.get("properties", {})
            raw_severity = properties.get("security-severity")
            tags = properties.get("tags", [])
            level = item.get("level", rule.get("defaultConfiguration", {}).get("level", "warning"))
            if raw_severity is None:
                blocking = level == "error" or "security" in tags
                severity = "unclassified"
            else:
                try:
                    number = float(raw_severity)
                except (ValueError, TypeError):
                    raise ValueError("Invalid SARIF severity") from None
                if not math.isfinite(number) or not 0 <= number <= 10:
                    raise ValueError("Invalid SARIF severity")
                severity = str(number)
                blocking = number >= 7
            if blocking:
                result.append(f"CodeQL {str(rule_id)[:200]}: security severity {severity}; review the code-scanning alert (source contents withheld)")
    return result


def scan_directory(folder: Path) -> list[str]:
    paths = sorted(folder.glob("*.sarif"))
    if not paths:
        raise ValueError("No SARIF report")
    result = []
    for path in paths:
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("SARIF report oversized")
        result.extend(findings(json.loads(path.read_text())))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", nargs="?", type=Path, default=Path("codeql-results"))
    args = parser.parse_args()
    try:
        result = scan_directory(args.folder)
    except (OSError, ValueError, TypeError, AttributeError):
        print("::error title=CodeQL release gate::Missing or malformed SARIF; gate failed closed")
        raise SystemExit(1) from None
    for finding in result[:30]:
        print("::error title=CodeQL release gate::" + escape(finding))
    if result:
        raise SystemExit(1)
    print("No unsuppressed high/critical CodeQL security findings")


if __name__ == "__main__":
    main()
