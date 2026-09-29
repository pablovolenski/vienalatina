"""Changes to tables that already exist.

`schema.sql` is all `CREATE TABLE IF NOT EXISTS`, which handles exactly one
kind of change: a brand-new table. It silently does nothing to a table that is
already there, so every alteration to an existing one has to happen here.

That was fine until now because every change so far had been a new table. The
first change that is not — giving `attachments` a third possible parent — also
happens to be one SQLite cannot do in place, because the old row carries a
CHECK constraint and SQLite has no `DROP CONSTRAINT`. Verified rather than
assumed: `ALTER TABLE ... ADD COLUMN` succeeds, and the next insert is refused
by a constraint that can no longer be removed.

**The order matters.** `init_db` applies `schema.sql` first and then these. On
an empty database the schema creates everything in its current shape and each
step below finds its work already done, so every step must be written to check
before it acts and return quietly.

Steps are numbered, applied once, in order, each in its own transaction, and
recorded in SQLite's own `PRAGMA user_version`. Never renumber one and never
edit one that has shipped: a server that has already run it will not run it
again, so a correction is a new step.
"""

from __future__ import annotations

import sqlite3


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _attachments_accept_messages(db: sqlite3.Connection) -> None:
    """Let an attachment hang off a private message.

    The table is rebuilt rather than altered because of its CHECK constraint:
    `(thread_id IS NULL) <> (comment_id IS NULL)` insists that exactly one of
    those two is set, so a row belonging to a message — with both of them null
    — is refused. Adding the column is allowed; using it is not.

    This is SQLite's documented procedure for changing a constraint: build the
    new table beside the old one, copy the rows, drop the old, rename. The
    foreign keys are switched off around it because dropping a table with them
    on can cascade, and switched back on after, with a check that nothing was
    broken in between.
    """
    if "message_id" not in _columns(db, "attachments"):
        db.execute("""
            CREATE TABLE attachments_new (
              id            INTEGER PRIMARY KEY,
              thread_id     INTEGER REFERENCES threads(id),
              comment_id    INTEGER REFERENCES comments(id),
              message_id    INTEGER REFERENCES messages(id),
              stored_name   TEXT    NOT NULL UNIQUE,
              original_name TEXT    NOT NULL,
              content_type  TEXT    NOT NULL,
              bytes         INTEGER NOT NULL,
              uploaded_by   INTEGER NOT NULL REFERENCES members(id),
              created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
              CHECK ((thread_id IS NOT NULL) + (comment_id IS NOT NULL)
                   + (message_id IS NOT NULL) = 1)
            )""")
        # Named columns, not SELECT *: the order has to survive somebody adding
        # a column to one of the two tables later.
        db.execute("""
            INSERT INTO attachments_new
                (id, thread_id, comment_id, stored_name, original_name,
                 content_type, bytes, uploaded_by, created_at)
            SELECT id, thread_id, comment_id, stored_name, original_name,
                   content_type, bytes, uploaded_by, created_at
              FROM attachments""")
        db.execute("DROP TABLE attachments")
        db.execute("ALTER TABLE attachments_new RENAME TO attachments")
        db.execute("CREATE INDEX IF NOT EXISTS attachments_thread  ON attachments(thread_id)")
        db.execute("CREATE INDEX IF NOT EXISTS attachments_comment ON attachments(comment_id)")
        db.execute("CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id)")

        broken = db.execute("PRAGMA foreign_key_check").fetchall()
        if broken:
            raise RuntimeError(f"migration left dangling references: {broken}")

    # Outside the branch above: a database created fresh by schema.sql has the
    # column but not this index, because schema.sql cannot carry it — see the
    # note there. Both paths end up with the same table and the same indexes.
    db.execute("CREATE INDEX IF NOT EXISTS attachments_message ON attachments(message_id)")


def _members_own_their_passwords(db: sqlite3.Connection) -> None:
    """Give members somewhere to keep a password, and drop the OAuth tokens.

    A plain ADD COLUMN, with nothing like step 1's difficulty: `members` has no
    CHECK constraint to collide with. Everyone's hash starts NULL, including
    the owner's, and a NULL hash cannot be signed in with — so the way back in
    is the invitation and reset machinery, which already works, or
    scripts/set-password.sh when mail is having a bad day.

    `gitea_tokens` holds OAuth access tokens for a flow that no longer exists.
    They are not merely unused, they are credentials, and keeping credentials
    that nothing can spend is a liability with no upside.
    """
    if "password_hash" not in _columns(db, "members"):
        db.execute("ALTER TABLE members ADD COLUMN password_hash TEXT")
    db.execute("DROP TABLE IF EXISTS gitea_tokens")


# (number, description, function). The number is the value written to
# user_version once the step succeeds.
STEPS = [
    (1, "attachments can belong to a private message", _attachments_accept_messages),
    (2, "members keep their own password", _members_own_their_passwords),
]


def apply(db: sqlite3.Connection) -> list[str]:
    """Run whatever this database has not run yet. Returns what was applied."""
    version = db.execute("PRAGMA user_version").fetchone()[0]
    done = []
    for number, description, step in sorted(STEPS):
        if number <= version:
            continue
        # Outside the transaction, deliberately: "PRAGMA foreign_keys is a
        # no-op within a transaction". Setting it inside BEGIN looks like it
        # worked and changes nothing, which is how a table rebuild ends up
        # running with enforcement still on.
        db.execute("PRAGMA foreign_keys = OFF")
        db.execute("BEGIN IMMEDIATE")
        try:
            step(db)
            # Not a parameter: PRAGMA does not take them. The value is an int
            # from the list above, never from anything a request can reach.
            db.execute(f"PRAGMA user_version = {int(number)}")
            db.execute("COMMIT")
        except Exception:
            db.execute("ROLLBACK")
            raise
        finally:
            db.execute("PRAGMA foreign_keys = ON")
        done.append(f"{number}: {description}")
    return done
