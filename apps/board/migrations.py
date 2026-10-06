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

import re
import sqlite3


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _role_check(db: sqlite3.Connection) -> str:
    """The CREATE TABLE text of `members` with its comments stripped.

    A CHECK constraint is the one piece of a table no pragma reports, so the
    only way to ask what roles are allowed is to read the statement back — and
    sqlite_master stores that statement *verbatim*, comments included. schema.sql
    explains above the column why 'moderator' needed a rebuild, and searching the
    raw text would find that sentence and conclude the work was already done.
    Which is exactly what happened, and what the start-up test caught.
    """
    row = db.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'members'"
    ).fetchone()
    if row is None:
        return ""
    return re.sub(r"--[^\n]*", "", row[0])


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


def _members_get_a_public_page(db: sqlite3.Connection) -> None:
    """Columns for the profile at vienalatina.com/<name>.

    Plain ADD COLUMNs. `profile_published` carries a default so the existing
    rows are valid the moment it appears — and the default is 0, so nobody
    wakes up with a public page they did not ask for.
    """
    existing = _columns(db, "members")
    for column, definition in (
        ("profile_published", "INTEGER NOT NULL DEFAULT 0"),
        ("bio", "TEXT"),
        ("links", "TEXT"),
        ("photo_name", "TEXT"),
    ):
        if column not in existing:
            db.execute(f"ALTER TABLE members ADD COLUMN {column} {definition}")



def _members_can_moderate(db: sqlite3.Connection) -> None:
    """Add the `moderator` role, which means rebuilding this table.

    `role` carries `CHECK (role IN ('owner','admin','user','tombstone'))`, and
    SQLite has no DROP CONSTRAINT, so widening the list is not an ALTER. This is
    the same documented procedure as step 1 — new table, copy, drop, rename —
    but on a table eight others point at, which is what makes it worth spelling
    out:

    * `members` is referenced by threads, comments, conversation_members,
      messages, blocks, attachments, invites, submissions and by itself
      (`created_by`). Those clauses say `REFERENCES members`, resolved by name
      at runtime rather than bound to the table's identity, so after the rename
      they point at the new table with nothing to update.
    * The rename only rewrites clauses that name `members_new`, and nothing
      does, so no other table's schema is touched.
    * `migrations.apply()` turns foreign keys off *outside* the transaction,
      which is the only place it works — see the comment there.
    * The partial unique index enforcing one owner is not carried over by the
      copy and has to be recreated, or the database quietly loses the rule that
      stops a second owner existing.

    Checked afterwards with `PRAGMA foreign_key_check`, and by the test that
    builds a pre-migration database with a row in every referencing table.
    """
    if "moderator" in _role_check(db):
        return

    db.execute("""
        CREATE TABLE members_new (
          id            INTEGER PRIMARY KEY,
          gitea_login   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
          display_name  TEXT    NOT NULL DEFAULT '',
          email         TEXT    NOT NULL DEFAULT '',
          password_hash TEXT,
          profile_published INTEGER NOT NULL DEFAULT 0 CHECK (profile_published IN (0, 1)),
          bio           TEXT,
          links         TEXT,
          photo_name    TEXT,
          role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'moderator',
                                                         'user', 'tombstone')),
          active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
          created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
          created_by    INTEGER REFERENCES members(id),
          last_seen_at  TEXT
        )""")
    db.execute("""
        INSERT INTO members_new
            (id, gitea_login, display_name, email, password_hash,
             profile_published, bio, links, photo_name,
             role, active, created_at, created_by, last_seen_at)
        SELECT id, gitea_login, display_name, email, password_hash,
               profile_published, bio, links, photo_name,
               role, active, created_at, created_by, last_seen_at
          FROM members""")
    db.execute("DROP TABLE members")
    db.execute("ALTER TABLE members_new RENAME TO members")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS members_one_owner
                    ON members(role) WHERE role = 'owner'""")

    broken = db.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"migration left dangling references: {broken}")

def _rooms_for_a_date_of_its_own(db: sqlite3.Connection) -> None:
    """Somewhere to keep an event's date, in the two tables that already exist.

    Plain ADD COLUMNs, nothing like the rebuilds in steps 1 and 4 — neither
    table has a CHECK in the way.

    `submissions` needs them because a member can propose an event, and the date
    has to survive in the queue until a moderator approves it. `content_cache`
    needs them because the private calendar shows public events beside internal
    ones, and reading every post's frontmatter on every page view to find their
    dates is exactly what that cache exists to avoid.
    """
    for table in ("submissions", "content_cache"):
        existing = _columns(db, table)
        if not existing:
            # No such table in this database. `init_db` runs schema.sql before
            # these steps, so on a real server both tables are always there;
            # skipping rather than failing keeps a step from depending on how
            # much of the schema a given database happens to have, which is the
            # property that lets these run against any vintage.
            continue
        for column in ("event_date", "event_time", "event_location"):
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")


def _no_more_categories(db: sqlite3.Connection) -> None:
    """Take the category column off the two tables that carried one.

    `DROP COLUMN` rather than a rebuild: SQLite has had it since 3.35 and
    neither column is in a CHECK, an index or a foreign key, which are the three
    things that make it refuse. The rebuilds in steps 1 and 4 exist because a
    CHECK cannot be altered any other way — this needs none of that.

    Dropping rather than leaving a column nobody writes to: a dead column is a
    question for whoever reads this schema next, and the answer ("we used to
    have categories") is not in the file.
    """
    for table in ("content_cache", "submissions"):
        if "categories" in _columns(db, table):
            db.execute(f"ALTER TABLE {table} DROP COLUMN categories")


def _a_superadmin_above_the_owner(db: sqlite3.Connection) -> None:
    """Add the `superadmin` role, and give it to whoever is the owner today.

    The same rebuild as step 4, for the same reason — `role` carries a CHECK and
    SQLite has no DROP CONSTRAINT — so the notes there about the eight tables
    that reference `members`, about the rename, and about foreign keys being off
    outside the transaction all apply here unchanged. Two things are particular
    to this step:

    * **Both partial unique indexes have to be created afterwards.** The copy
      carries neither. Losing `members_one_owner` would quietly allow a second
      responsable, and never creating `members_one_superadmin` would allow two
      superadministrators — which is the one rule this whole phase rests on.
    * **The existing owner is promoted.** Nobody has to type anything, nothing
      is erased, and the person who could already do everything keeps being able
      to. It leaves the responsable chair empty, which breaks nothing: a
      superadministrator passes every check an owner passes, and the chair is
      filled from inside the app whenever there is somebody to put in it.
    """
    if "superadmin" in _role_check(db):
        return

    db.execute("""
        CREATE TABLE members_new (
          id            INTEGER PRIMARY KEY,
          gitea_login   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
          display_name  TEXT    NOT NULL DEFAULT '',
          email         TEXT    NOT NULL DEFAULT '',
          password_hash TEXT,
          profile_published INTEGER NOT NULL DEFAULT 0 CHECK (profile_published IN (0, 1)),
          bio           TEXT,
          links         TEXT,
          photo_name    TEXT,
          role          TEXT    NOT NULL CHECK (role IN ('superadmin', 'owner', 'admin',
                                                         'moderator', 'user', 'tombstone')),
          active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
          created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
          created_by    INTEGER REFERENCES members(id),
          last_seen_at  TEXT
        )""")
    db.execute("""
        INSERT INTO members_new
            (id, gitea_login, display_name, email, password_hash,
             profile_published, bio, links, photo_name,
             role, active, created_at, created_by, last_seen_at)
        SELECT id, gitea_login, display_name, email, password_hash,
               profile_published, bio, links, photo_name,
               role, active, created_at, created_by, last_seen_at
          FROM members""")
    db.execute("DROP TABLE members")
    db.execute("ALTER TABLE members_new RENAME TO members")
    # Promoted before the indexes exist, so the row is never momentarily both
    # the only owner and the only superadmin under two rules at once.
    db.execute("UPDATE members SET role = 'superadmin' WHERE role = 'owner'")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS members_one_superadmin
                    ON members(role) WHERE role = 'superadmin'""")
    db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS members_one_owner
                    ON members(role) WHERE role = 'owner'""")

    broken = db.execute("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise RuntimeError(f"migration left dangling references: {broken}")


# (number, description, function). The number is the value written to
# user_version once the step succeeds.
STEPS = [
    (1, "attachments can belong to a private message", _attachments_accept_messages),
    (2, "members keep their own password", _members_own_their_passwords),
    (3, "members can have a public page", _members_get_a_public_page),
    (4, "members can be moderators", _members_can_moderate),
    (5, "events carry a date of their own", _rooms_for_a_date_of_its_own),
    (6, "there are no categories any more", _no_more_categories),
    (7, "a superadministrator above the responsable", _a_superadmin_above_the_owner),
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
