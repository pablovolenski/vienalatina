"""Keeping the member's Gitea token usable for as long as they are signed in.

Gitea's OAuth access tokens expire after about an hour. A writer who opened the
editor after lunch and saved at three would otherwise get a failure with no
explanation and no way to act on it, so this refreshes ahead of expiry and
retries once when Gitea rejects a token anyway.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from flask import g

from . import gitea
from .db import get_db

# Refresh this far before the stated expiry. A token that dies mid-request is
# indistinguishable to the writer from the app being broken.
EARLY = timedelta(minutes=5)


class NeedsSignIn(RuntimeError):
    """The token is gone or unrefreshable — send them through Gitea again."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def save(member_id: int, payload: dict) -> None:
    expires_in = payload.get("expires_in")
    expires_at = (
        (_now() + timedelta(seconds=int(expires_in))).isoformat()
        if expires_in else None
    )
    get_db().execute(
        """INSERT INTO gitea_tokens (member_id, access_token, refresh_token, expires_at)
           VALUES (?, ?, ?, ?)
           ON CONFLICT(member_id) DO UPDATE SET
               access_token  = excluded.access_token,
               refresh_token = excluded.refresh_token,
               expires_at    = excluded.expires_at,
               updated_at    = datetime('now')""",
        (member_id, payload["access_token"], payload.get("refresh_token", ""), expires_at),
    )


def forget(member_id: int) -> None:
    get_db().execute("DELETE FROM gitea_tokens WHERE member_id = ?", (member_id,))


def _stored(member_id: int):
    return get_db().execute(
        "SELECT * FROM gitea_tokens WHERE member_id = ?", (member_id,)
    ).fetchone()


def _refresh(row) -> str:
    if not row["refresh_token"]:
        raise NeedsSignIn()
    try:
        payload = gitea.refresh_token(row["refresh_token"])
    except gitea.GiteaError as exc:
        raise NeedsSignIn() from exc
    save(row["member_id"], payload)
    return payload["access_token"]


def access_token(member_id: int) -> str:
    row = _stored(member_id)
    if row is None:
        raise NeedsSignIn()
    if row["expires_at"]:
        expires = datetime.fromisoformat(row["expires_at"])
        if _now() + EARLY >= expires:
            return _refresh(row)
    return row["access_token"]


def with_token(call, *args, **kwargs):
    """Run a Gitea call with the current member's token, refreshing once if it
    is rejected.

    The retry exists because expiry is not the only reason a token stops
    working — it can be revoked in Gitea, or invalidated by a password change —
    and in those cases the clock says the token is still fine.
    """
    member_id = g.member["id"]
    token = access_token(member_id)
    try:
        return call(*args, token=token, **kwargs)
    except PermissionError:
        row = _stored(member_id)
        if row is None:
            raise NeedsSignIn()
        return call(*args, token=_refresh(row), **kwargs)
