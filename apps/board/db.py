"""SQLite access for the members area.

One connection per request, closed when the request ends. SQLite is enough
here by a wide margin: a trusted group of tens of people generates a handful
of writes a day, and keeping the database a single file on disk means the
backup story is `cp`, which matters more than throughput nobody will use.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from flask import current_app, g

from . import migrations

SCHEMA_PATH = Path(__file__).with_name("schema.sql")

# Authorship of a removed member is reassigned to this row rather than deleted,
# so their threads keep their shape and replies to them still make sense. It can
# never log in: the login gate requires an active member holding a real role.
TOMBSTONE_LOGIN = "__removed__"
TOMBSTONE_NAME = "Miembro eliminado"


def connect(path: str) -> sqlite3.Connection:
    # isolation_level=None puts the driver in autocommit mode, so the only
    # transactions are the ones written explicitly with BEGIN. Python's
    # implicit-transaction behaviour is surprising often enough to be worth
    # opting out of entirely.
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = connect(current_app.config["DB_PATH"])
    return g.db


def close_db(_exception=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app) -> None:
    """Apply the schema, bring old databases up to date, seed the fixed rows.

    The order is deliberate. `schema.sql` is all CREATE TABLE IF NOT EXISTS, so
    on an empty database it builds everything in its current shape and the
    migrations below find nothing to do; on a database that has run before it
    adds only what is new and silently leaves existing tables alone — which is
    precisely why migrations have to come second and clean up after it.
    """
    Path(app.config["DB_PATH"]).parent.mkdir(parents=True, exist_ok=True)
    db = connect(app.config["DB_PATH"])
    try:
        db.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        for step in migrations.apply(db):
            app.logger.info("Applied migration %s", step)
        _ensure_tombstone(db)
        _seed_superadmin(db, app)
    finally:
        db.close()


def _ensure_tombstone(db: sqlite3.Connection) -> None:
    db.execute(
        """INSERT INTO members (gitea_login, display_name, role, active)
           VALUES (?, ?, 'tombstone', 0)
           ON CONFLICT(gitea_login) DO NOTHING""",
        (TOMBSTONE_LOGIN, TOMBSTONE_NAME),
    )


def _seed_superadmin(db: sqlite3.Connection, app) -> None:
    """Create the first superadministrator from BOARD_OWNER, once.

    Still `BOARD_OWNER` rather than a new name: it is the variable the server's
    `.env` already carries, and an install that has to edit its environment to
    survive an upgrade is an install that breaks on upgrade.

    Deliberately refuses to change an existing superadministrator. Were this to
    overwrite, anyone who could edit the environment could hand themselves the
    platform by restarting the container — a quieter privilege escalation than
    it looks, since editing a compose file draws far less attention than asking
    for access.

    **The `ON CONFLICT` below is the half worth reading twice.** It used to end
    `SET role = 'owner'`, which was right while owner was the top of the tree.
    Left that way after migration 7 it would have found the superadministrator's
    login, matched the conflict, and demoted them back to owner on every
    container restart — a privilege *reduction* arriving at the least convenient
    moment and attributable to nothing anybody did. `members_one_superadmin`
    would then have let somebody else take the chair.
    """
    login = (app.config.get("OWNER_LOGIN") or "").strip()
    existing = db.execute(
        "SELECT gitea_login FROM members WHERE role = 'superadmin'").fetchone()

    if existing:
        if login and existing["gitea_login"].lower() != login.lower():
            app.logger.warning(
                "BOARD_OWNER is %r but the superadministrator is %r; leaving it "
                "alone. Hand the platform on from inside the app instead.",
                login, existing["gitea_login"],
            )
        return

    if not login:
        app.logger.warning("No superadministrator yet and BOARD_OWNER is unset — "
                           "nobody can sign in.")
        return

    db.execute(
        """INSERT INTO members (gitea_login, display_name, role, active)
           VALUES (?, ?, 'superadmin', 1)
           ON CONFLICT(gitea_login) DO UPDATE SET role = 'superadmin', active = 1""",
        (login, login),
    )
    app.logger.info("Seeded %r as superadministrator.", login)
