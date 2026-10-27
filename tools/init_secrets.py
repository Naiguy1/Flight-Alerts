"""Create private runtime secret files, without printing their contents.

For Compose, run as root with --uid 10001 --gid 10001 so the nonroot application
can read 0600 secret mounts. Existing files are never overwritten.
"""
from __future__ import annotations

import argparse
import getpass
import os
from pathlib import Path
import secrets

from web.security import hash_password


def write_secret(path: Path, value: str, uid: int | None, gid: int | None) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(descriptor, (value + "\n").encode())
        if uid is not None or gid is not None:
            os.fchown(descriptor, uid if uid is not None else -1, gid if gid is not None else -1)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=Path("secrets"))
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    args.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.directory.is_symlink():
        raise SystemExit("Secret directory must not be a symlink")
    os.chmod(args.directory, 0o700)
    if args.uid is not None or args.gid is not None:
        os.chown(args.directory, args.uid if args.uid is not None else -1,
                 args.gid if args.gid is not None else -1)
    password = getpass.getpass("New administrator password (at least 16 characters): ")
    if len(password) < 16 or password != getpass.getpass("Confirm administrator password: "):
        raise SystemExit("Password too short or confirmation does not match")
    values = {"admin_password_hash": hash_password(password), "token_secret": secrets.token_urlsafe(48),
              "smtp_username": "", "smtp_password": "", "serpapi_api_key": "",
              "kiwi_api_key": "", "aviationstack_api_key": ""}
    for name, value in values.items():
        path = args.directory / name
        if path.exists():
            print(f"Kept existing {name}")
            continue
        write_secret(path, value, args.uid, args.gid)
        print(f"Created {name}")
    print("Secret files created. Configure provider/SMTP files privately before enabling delivery.")


if __name__ == "__main__":
    main()
