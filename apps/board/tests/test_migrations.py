"""Changing a table that already has rows in it.

The one thing `schema.sql` cannot do. Every test here builds a database in the
*old* shape first and then runs the migration over it, because a migration
tested only against a fresh database is tested against the one case it was
never needed for.
"""

from __future__ import annotations

import sqlite3

import pytest

from apps.board import migrations

# The attachments table exactly as it shipped in Phase B, CHECK and all. Kept
# here as a literal rather than imported: the point is to reproduce what is on
# the server, and schema.sql has moved on.
OLD_SCHEMA = """
CREATE TABLE members (id INTEGER PRIMARY KEY, gitea_login TEXT);
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
    db.executescript(OLD_SCHEMA)
    db.execute("INSERT INTO members (id, gitea_login) VALUES (1, 'salvador')")
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


def test_a_fresh_database_skips_it(app, db):
    """schema.sql already builds the new shape, so the step must find its work
    done and return quietly rather than rebuilding a table it just created."""
    assert "message_id" in {row[1] for row in db.execute("PRAGMA table_info(attachments)")}
    assert migrations.apply(db) == []
