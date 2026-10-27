"""Persistence result shapes; SQLite and SQL remain inside :mod:`db.crud`."""
from typing import Any, TypedDict


class Recipient(TypedDict):
    id: int
    email: str
    verified: bool
    enabled: bool
    last_digest_at: str | None


class Delivery(TypedDict):
    id: int
    recipient_id: int
    email: str
    kind: str
    payload: dict[str, Any]
    attempts: int
    lease_token: str
    created_at: str
