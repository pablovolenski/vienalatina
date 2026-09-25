"""One-time links for setting a password.

Used for two things that are the same mechanism with different clocks: the
invitation a new member gets, and the reset an existing one asks for.

A token is a bearer credential — whoever holds it can set the password on that
account — so the rules are deliberately strict: single use, short-lived, and
only ever stored as a hash.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .db import get_db

INVITE_LIFETIME = timedelta(days=7)
RESET_LIFETIME = timedelta(hours=1)

# How many resets one member may ask for before the rest are quietly dropped.
# Without this, the "forgot password" form is a way to mail-bomb somebody using
# your server's good name.
RESET_WINDOW = timedelta(minutes=15)
RESET_LIMIT = 3


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def issue(member_id: int, purpose: str) -> str:
    """Create a token, store its hash, return the token itself — once.

    Any earlier unused token for the same member and purpose is marked used.
    A member who asks for a second reset should not leave a first one lying in
    an inbox, still working.
    """
    lifetime = INVITE_LIFETIME if purpose == "invite" else RESET_LIFETIME
    token = secrets.token_urlsafe(32)
    db = get_db()
    db.execute(
        """UPDATE invites SET used_at = datetime('now')
            WHERE member_id = ? AND purpose = ? AND used_at IS NULL""",
        (member_id, purpose),
    )
    db.execute(
        """INSERT INTO invites (member_id, token_hash, purpose, expires_at)
           VALUES (?, ?, ?, ?)""",
        (member_id, _hash(token), purpose, (_now() + lifetime).isoformat()),
    )
    return token


def rate_limited(member_id: int) -> bool:
    """Compared entirely inside SQLite, on purpose.

    `created_at` is written by SQLite's own `datetime('now')`, which formats as
    `2026-09-25 15:00:00` — a space, no offset. Python's `.isoformat()` produces
    `2026-09-25T15:00:00+00:00`. Compared as strings, a space sorts before `T`,
    so every stored row looks older than any Python-generated threshold and the
    limit silently never fires. Letting SQLite compare its own format to its own
    clock keeps the two conventions from ever meeting.
    """
    row = get_db().execute(
        """SELECT COUNT(*) AS n FROM invites
            WHERE member_id = ? AND purpose = 'reset'
              AND created_at > datetime('now', ?)""",
        (member_id, f"-{int(RESET_WINDOW.total_seconds() // 60)} minutes"),
    ).fetchone()
    return row["n"] >= RESET_LIMIT


def lookup(token: str):
    """The member this token belongs to, or None if it is no good.

    One return value for every kind of failure — unknown, used, expired — so a
    caller cannot accidentally tell the holder which it was.
    """
    if not token:
        return None
    row = get_db().execute(
        """SELECT i.id AS invite_id, i.purpose, i.expires_at, m.*
             FROM invites i JOIN members m ON m.id = i.member_id
            WHERE i.token_hash = ? AND i.used_at IS NULL""",
        (_hash(token),),
    ).fetchone()
    if row is None:
        return None
    if datetime.fromisoformat(row["expires_at"]) <= _now():
        return None
    if not row["active"] or row["role"] == "tombstone":
        return None
    return row


def consume(invite_id: int) -> None:
    get_db().execute(
        "UPDATE invites SET used_at = datetime('now') WHERE id = ?", (invite_id,)
    )
