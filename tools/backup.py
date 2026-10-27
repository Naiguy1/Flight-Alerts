"""Consistent SQLite backups and offline restores with integrity verification.

Plain SQLite/config/backup files are not encrypted by the application. Encrypt
host volumes and use --age-recipient for encrypted off-host backup transport.
"""
from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile


def verify_backup(path: Path) -> None:
    path = path.resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Database integrity check failed")
        if database.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ValueError("Database foreign-key check failed")


def create_backup(source: Path, destination: Path) -> str:
    if source.is_symlink() or not source.is_file():
        raise ValueError("Backup source must be a regular database file")
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("Backup destination already exists")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    try:
        with sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True) as live:
            with sqlite3.connect(destination) as backup:
                live.backup(backup)
        verify_backup(destination)
        return hashlib.sha256(destination.read_bytes()).hexdigest()
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def restore_backup(source: Path, destination: Path, *, services_stopped: bool = False) -> str:
    if not services_stopped:
        raise ValueError("Stop web and worker before restoring; explicit confirmation required")
    if any(Path(str(destination) + suffix).exists() for suffix in ("", "-wal", "-shm")):
        raise FileExistsError("Restore target and SQLite sidecars must not exist")
    verify_backup(source)
    return create_backup(source, destination)


def encrypt_backup(source: Path, destination: Path, recipient: str) -> None:
    if not recipient or any(character.isspace() for character in recipient):
        raise ValueError("Invalid age recipient")
    if destination.exists():
        raise FileExistsError("Encrypted backup already exists")
    executable = shutil.which("age")
    if executable is None:
        raise RuntimeError("Install age before requesting encrypted backups")
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as output:
            subprocess.run([executable, "--encrypt", "--recipient", recipient, str(source)],
                           stdout=output, check=True)
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="flight-alert-restore-check-") as directory:
        folder = Path(directory)
        source, backup, restored = (folder / name for name in ("source.db", "backup.db", "restored.db"))
        with sqlite3.connect(source) as database:
            database.execute("CREATE TABLE probe(value TEXT NOT NULL)")
            database.execute("INSERT INTO probe VALUES (?)", ("restore verified",))
        create_backup(source, backup)
        restore_backup(backup, restored, services_stopped=True)
        with sqlite3.connect(restored) as database:
            if database.execute("SELECT value FROM probe").fetchone()[0] != "restore verified":
                raise ValueError("Restore comparison failed")
    print("Backup, integrity, and restore comparison succeeded on an isolated database")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("--source", required=True, type=Path)
    backup.add_argument("--destination", required=True, type=Path)
    backup.add_argument("--age-recipient")
    verify = commands.add_parser("verify")
    verify.add_argument("--source", required=True, type=Path)
    restore = commands.add_parser("restore")
    restore.add_argument("--source", required=True, type=Path)
    restore.add_argument("--destination", required=True, type=Path)
    restore.add_argument("--confirm-services-stopped", action="store_true")
    commands.add_parser("self-test")
    args = parser.parse_args()
    if args.command == "self-test":
        self_test()
    elif args.command == "verify":
        verify_backup(args.source)
        print("Backup integrity verified")
    elif args.command == "restore":
        print("Restored SHA256:", restore_backup(args.source, args.destination,
                                               services_stopped=args.confirm_services_stopped))
    elif args.age_recipient:
        args.destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="flight-alert-backup-", dir=args.destination.parent) as directory:
            plain = Path(directory) / "backup.db"
            digest = create_backup(args.source, plain)
            encrypt_backup(plain, args.destination, args.age_recipient)
        print("Encrypted backup created; plaintext SHA256:", digest)
    else:
        print("Backup SHA256:", create_backup(args.source, args.destination))


if __name__ == "__main__":
    main()
