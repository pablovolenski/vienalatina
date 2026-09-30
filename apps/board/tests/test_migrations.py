"""Changing a table that already has rows in it.

The one thing `schema.sql` cannot do. Every test here builds a database in the
*old* shape first and then runs the migration over it, because a migration
tested only against a fresh database is tested against the one case it was
never needed for.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from apps.board import migrations

# The attachments table exactly as it shipped in Phase B, CHECK and all. Kept
# here as a literal rather than imported: the point is to reproduce what is on
# the server, and schema.sql has moved on.
OLD_SCHEMA = """
-- `members` as it shipped, because step 4 rebuilds it and a stub with two
-- columns would prove nothing about copying the other eleven.
CREATE TABLE members (
  id            INTEGER PRIMARY KEY,
  gitea_login   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT    NOT NULL DEFAULT '',
  email         TEXT    NOT NULL DEFAULT '',
  role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'user', 'tombstone')),
  active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  created_by    INTEGER REFERENCES members(id),
  last_seen_at  TEXT
);
CREATE UNIQUE INDEX members_one_owner ON members(role) WHERE role = 'owner';
-- Three of the eight tables that point at members(id), so the rebuild in step
-- 4 is checked with real references across it rather than on its own.
CREATE TABLE conversation_members (
  conversation_id INTEGER NOT NULL,
  member_id       INTEGER NOT NULL REFERENCES members(id),
  PRIMARY KEY (conversation_id, member_id)
);
CREATE TABLE blocks (
  blocker_id INTEGER NOT NULL REFERENCES members(id),
  blocked_id INTEGER NOT NULL REFERENCES members(id),
  PRIMARY KEY (blocker_id, blocked_id)
);
CREATE TABLE invites (
  id        INTEGER PRIMARY KEY,
  member_id INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  purpose   TEXT NOT NULL
);
CREATE TABLE threads (id INTEGER PRIMARY KEY, deleted_at TEXT);
CREATE TABLE comments (id INTEGER PRIMARY KEY, deleted_at TEXT);
CREATE TABLE messages (id INTEGER PRIMARY KEY, deleted_at TEXT);
CREATE TABLE attachments (
  id            INTEGER PRIMARY KEY,
  thread_id     INTEGER REFERENCES threads(id),
  comment_id    INTEGER REFERENCES comments(id),
  stored_name   TEXT    NOT NULL UNIQUE,
  original_name TEXT    NOT NULL,
  content_type  TEXT    NOT NULL,
  bytes         INTEGER NOT NULL,
  uploaded_by   INTEGER NOT NULL REFERENCES members(id),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  CHECK ((thread_id IS NULL) <> (comment_id IS NULL))
);
"""


@pytest.fixture
def old_db(tmp_path):
    """A database as it existed before this migration, with real rows in it."""
    db = sqlite3.connect(tmp_path / "old.db", isolation_level=None)
    db.row_factory = sqlite3.Row
    # As connect() does in db.py. Without this the fixture runs with SQLite's
    # default (off) and a migration that mishandles foreign keys passes here
    # and fails on the server.
    db.execute("PRAGMA foreign_keys = ON")
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO members (id, gitea_login, display_name, email, role) "
               "VALUES (1, 'salvador', 'Salvador', 's@example.com', 'user')")
    db.execute("INSERT INTO members (id, gitea_login, display_name, role, created_by) "
               "VALUES (2, 'pablo', 'Pablo', 'owner', 1)")
    db.execute("INSERT INTO conversation_members VALUES (1, 1)")
    db.execute("INSERT INTO blocks VALUES (1, 2)")
    db.execute("INSERT INTO invites (member_id, purpose) VALUES (1, 'invite')")
    db.execute("INSERT INTO threads (id) VALUES (7)")
    db.execute("INSERT INTO comments (id) VALUES (9)")
    db.execute(
        """INSERT INTO attachments
               (thread_id, stored_name, original_name, content_type, bytes, uploaded_by)
           VALUES (7, 'foto-abc123abc123.jpg', 'foto.jpg', 'image/jpeg', 2048, 1)""")
    db.execute(
        """INSERT INTO attachments
               (comment_id, stored_name, original_name, content_type, bytes, uploaded_by)
           VALUES (9, 'otra-def456def456.png', 'otra.png', 'image/png', 512, 1)""")
    yield db
    db.close()


def test_the_old_check_really_does_block_this(old_db):
    """The premise the whole migration rests on, asserted rather than assumed.

    If SQLite ever lets this insert through, the rebuild is unnecessary and
    this file should shrink to nothing."""
    old_db.execute("ALTER TABLE attachments ADD COLUMN message_id INTEGER")
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute(
            """INSERT INTO attachments
                   (message_id, stored_name, original_name, content_type, bytes, uploaded_by)
               VALUES (1, 'x-000000000000.png', 'x.png', 'image/png', 1, 1)""")


def test_the_rows_survive(old_db):
    """The one that would hurt: Salvador's photo is a real file on a real
    server, and a migration that drops it loses something nobody can rebuild."""
    before = old_db.execute(
        "SELECT stored_name, bytes, uploaded_by FROM attachments ORDER BY id").fetchall()

    migrations.apply(old_db)

    after = old_db.execute(
        "SELECT stored_name, bytes, uploaded_by FROM attachments ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_a_message_can_carry_a_picture_afterwards(old_db):
    migrations.apply(old_db)
    old_db.execute("INSERT INTO messages (id) VALUES (3)")
    old_db.execute(
        """INSERT INTO attachments
               (message_id, stored_name, original_name, content_type, bytes, uploaded_by)
           VALUES (3, 'nueva-111111111111.png', 'n.png', 'image/png', 10, 1)""")
    assert old_db.execute(
        "SELECT message_id FROM attachments WHERE stored_name LIKE 'nueva%'"
    ).fetchone()["message_id"] == 3


def test_an_attachment_still_needs_exactly_one_parent(old_db):
    """The rebuilt CHECK has to be as strict as the one it replaced, in three
    directions instead of two — otherwise the migration quietly removes a
    constraint while appearing to widen it."""
    migrations.apply(old_db)
    for columns, values in [("", ""), ("thread_id, comment_id", "7, 9")]:
        with pytest.raises(sqlite3.IntegrityError):
            old_db.execute(
                f"""INSERT INTO attachments
                        ({columns + ', ' if columns else ''}stored_name,
                         original_name, content_type, bytes, uploaded_by)
                    VALUES ({values + ', ' if values else ''}
                            'bad-{len(columns)}00000000000.png', 'b.png',
                            'image/png', 1, 1)""")


def test_running_it_twice_changes_nothing(old_db):
    first = migrations.apply(old_db)
    second = migrations.apply(old_db)

    assert first and not second          # applied once, then nothing to do
    assert old_db.execute("PRAGMA user_version").fetchone()[0] == max(
        number for number, _, _ in migrations.STEPS)


# --- step 4: the moderator role ------------------------------------------

def test_the_role_check_refuses_a_moderator_beforehand(old_db):
    """The premise, like the CHECK test above. SQLite has no DROP CONSTRAINT,
    so if this ever stops raising, step 4 can shrink to an ALTER."""
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute("INSERT INTO members (gitea_login, display_name, role) "
                       "VALUES ('luisa', 'Luisa', 'moderator')")


def test_a_moderator_is_accepted_afterwards(old_db):
    migrations.apply(old_db)
    old_db.execute("INSERT INTO members (gitea_login, display_name, role) "
                   "VALUES ('luisa', 'Luisa', 'moderator')")
    assert old_db.execute(
        "SELECT role FROM members WHERE gitea_login = 'luisa'").fetchone()["role"] == "moderator"


def test_every_member_column_survives_the_rebuild(old_db):
    """Thirteen columns copied by name. A typo in that list is a member losing
    their email address, or their password, or their public page."""
    before = old_db.execute(
        "SELECT id, gitea_login, display_name, email, role, active, created_by "
        "FROM members ORDER BY id").fetchall()

    migrations.apply(old_db)

    after = old_db.execute(
        "SELECT id, gitea_login, display_name, email, role, active, created_by "
        "FROM members ORDER BY id").fetchall()
    assert [tuple(row) for row in after] == [tuple(row) for row in before]


def test_the_one_owner_rule_survives_the_rebuild(old_db):
    """The partial unique index is not copied with the rows, so it has to be
    recreated by hand. Without it the database silently stops being the thing
    that guarantees one owner, and a bug in a handler becomes two owners."""
    migrations.apply(old_db)
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute("INSERT INTO members (gitea_login, display_name, role) "
                       "VALUES ('otro', 'Otro', 'owner')")


def test_nothing_pointing_at_a_member_is_left_dangling(old_db):
    """Dropping and renaming `members` with foreign keys off is the only way to
    do this, and it is also the way to quietly orphan every row in the eight
    tables that reference it."""
    migrations.apply(old_db)

    assert old_db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert old_db.execute("SELECT COUNT(*) FROM conversation_members").fetchone()[0] == 1
    assert old_db.execute("SELECT COUNT(*) FROM blocks").fetchone()[0] == 1
    assert old_db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 1
    # And the references still bite: foreign keys are back on after apply().
    with pytest.raises(sqlite3.IntegrityError):
        old_db.execute("INSERT INTO blocks VALUES (1, 999)")


def test_foreign_keys_are_back_on_afterwards(old_db):
    """apply() switches them off outside the transaction and restores them in a
    finally. A migration that leaves them off makes the whole process run
    without enforcement until the container next restarts."""
    migrations.apply(old_db)
    assert old_db.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_a_fresh_database_skips_it(app, db):
    """schema.sql already builds the new shape, so the step must find its work
    done and return quietly rather than rebuilding a table it just created."""
    assert "message_id" in {row[1] for row in db.execute("PRAGMA table_info(attachments)")}
    assert migrations.apply(db) == []


# --- the whole start-up, not just the step -------------------------------

def test_the_app_starts_against_a_database_from_before_all_this(tmp_path):
    """The test that was missing, and the reason the site went down.

    Every other test here calls `migrations.apply` directly. The failure was
    one layer above it: `init_db` runs `schema.sql` *first*, and schema.sql
    carried `CREATE INDEX ... ON attachments(message_id)`. IF NOT EXISTS guards
    the index name, not the column — so against a real database the script died
    with "no such column: message_id" before any migration could fix anything,
    the app never finished starting, and Caddy answered 502.

    This builds a database with the schema as it shipped, puts a real row in
    it, and starts the application the way gunicorn does.
    """
    from apps.board.app import create_app
    from apps.board.db import connect

    shipped = (Path(__file__).resolve().parents[3] / "apps/board/schema.sql")
    old_sql = shipped.read_text(encoding="utf-8")
    # Reduce it to the shape that predates this migration: no message_id
    # anywhere, and the CHECK that goes with it.
    old_sql = old_sql.replace("  message_id    INTEGER REFERENCES messages(id),\n", "")
    old_sql = old_sql.replace(
        "  CHECK ((thread_id IS NOT NULL) + (comment_id IS NOT NULL)\n"
        "       + (message_id IS NOT NULL) = 1)",
        "  CHECK ((thread_id IS NULL) <> (comment_id IS NULL))")
    # …and predates members owning their own passwords.
    old_sql = old_sql.replace("  password_hash TEXT,\n", "")
    # …and predates the moderator role, so step 4 has a CHECK to widen here too.
    old_sql = old_sql.replace(
        "  role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'moderator',\n"
        "                                                 'user', 'tombstone')),",
        "  role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'user', 'tombstone')),")
    assert "CHECK (role IN ('owner', 'admin', 'user', 'tombstone'))" in old_sql
    old_sql += """
    CREATE TABLE gitea_tokens (
      member_id INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
      access_token TEXT NOT NULL);
    """

    path = str(tmp_path / "board.db")
    db = connect(path)
    db.executescript(old_sql)
    db.execute("INSERT INTO members (gitea_login, display_name, role) "
               "VALUES ('salvador', 'Salvador', 'user')")
    db.execute("INSERT INTO threads (author_id, title, body_md) VALUES (1, 'Hola', 'T')")
    db.execute("""INSERT INTO attachments
                      (thread_id, stored_name, original_name, content_type, bytes, uploaded_by)
                  VALUES (1, 'foto-abc123abc123.jpg', 'foto.jpg', 'image/jpeg', 2048, 1)""")
    db.close()

    create_app({"SECRET_KEY": "x", "DB_PATH": path, "OWNER_LOGIN": "salvador",
                "UPLOAD_DIR": str(tmp_path / "uploads"), "TESTING": True})

    db = connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == max(
        number for number, _, _ in migrations.STEPS)
    # Step 2: somewhere to keep a password, and the dead OAuth tokens gone.
    assert "password_hash" in {row[1] for row in db.execute("PRAGMA table_info(members)")}
    assert db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'gitea_tokens'").fetchone() is None
    # The row the server actually has, still there and still whole.
    kept = db.execute("SELECT stored_name, thread_id, bytes FROM attachments").fetchone()
    assert (kept["stored_name"], kept["thread_id"], kept["bytes"]) == (
        "foto-abc123abc123.jpg", 1, 2048)

    # Step 4: the role list is wider, the one-owner rule survived the rebuild,
    # and nothing that pointed at a member row lost its target.
    db.execute("INSERT INTO members (gitea_login, display_name, role) "
               "VALUES ('luisa', 'Luisa', 'moderator')")
    assert db.execute("PRAGMA foreign_key_check").fetchall() == []
    assert db.execute(
        "SELECT name FROM sqlite_master WHERE name = 'members_one_owner'").fetchone()

    # And starting again changes nothing, because a container restarts.
    create_app({"SECRET_KEY": "x", "DB_PATH": path, "OWNER_LOGIN": "salvador",
                "UPLOAD_DIR": str(tmp_path / "uploads"), "TESTING": True})
    db.close()
