"""Publish bounded synthetic test failures as GitHub workflow annotations."""
from __future__ import annotations

import argparse
from pathlib import Path
import xml.etree.ElementTree as ET


def escape(value: str) -> str:
    return value.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def escape_property(value: str) -> str:
    return escape(value).replace(",", "%2C").replace(":", "%3A")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", nargs="?", default="test-results.xml", type=Path)
    args = parser.parse_args()
    if args.report.is_symlink() or not args.report.is_file() or args.report.stat().st_size > 8 * 1024 * 1024:
        print("::error title=Test runner::No JUnit report was produced")
        return
    try:
        root = ET.parse(args.report).getroot()
    except (ET.ParseError, OSError):
        print("::error title=Test runner::Invalid JUnit report")
        return
    emitted = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            finding = case.find(kind)
            if finding is None:
                continue
            title = escape_property(f"{case.get('classname', '')}.{case.get('name', '')}"[:250])
            message = escape((finding.text or finding.get("message") or "Test failed")[-6000:])
            print(f"::error title={title}::{message}")
            emitted += 1
            if emitted >= 30:
                print("::error title=Test runner::Further failures omitted; inspect the full CI test report")
                return
    if emitted == 0:
        print("No test failures in JUnit report")


if __name__ == "__main__":
    main()
