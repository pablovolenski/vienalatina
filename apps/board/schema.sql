-- Members area schema.
--
-- Applied at every startup and written to be idempotent, so deploying a new
-- version of the app needs no migration step for as long as the schema only
-- grows. A change that alters an existing column will need a real migration;
-- there is deliberately no framework here to pretend otherwise.

CREATE TABLE IF NOT EXISTS members (
  id            INTEGER PRIMARY KEY,
  -- COLLATE NOCASE because Gitea treats logins case-insensitively; without it
  -- "Pablo" and "pablo" would be two members with one Gitea account.
  gitea_login   TEXT    NOT NULL UNIQUE COLLATE NOCASE,
  display_name  TEXT    NOT NULL DEFAULT '',
  email         TEXT    NOT NULL DEFAULT '',
  role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'user', 'tombstone')),
  active        INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
  created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
  created_by    INTEGER REFERENCES members(id),
  last_seen_at  TEXT
);

-- The one-owner rule, held by the database rather than by the application, so
-- a mistake in a handler cannot produce a second owner. SQLite enforces a
-- partial unique index exactly like a full one.
CREATE UNIQUE INDEX IF NOT EXISTS members_one_owner
  ON members(role) WHERE role = 'owner';

CREATE TABLE IF NOT EXISTS threads (
  id          INTEGER PRIMARY KEY,
  author_id   INTEGER NOT NULL REFERENCES members(id),
  title       TEXT    NOT NULL,
  body_md     TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  edited_at   TEXT,
  pinned      INTEGER NOT NULL DEFAULT 0 CHECK (pinned IN (0, 1)),
  locked      INTEGER NOT NULL DEFAULT 0 CHECK (locked IN (0, 1)),
  -- Soft delete: a moderator's mistake stays recoverable, and removing one
  -- comment does not tear a hole in the conversation around it.
  deleted_at  TEXT
);

CREATE INDEX IF NOT EXISTS threads_live
  ON threads(pinned DESC, created_at DESC) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS comments (
  id          INTEGER PRIMARY KEY,
  thread_id   INTEGER NOT NULL REFERENCES threads(id),
  author_id   INTEGER NOT NULL REFERENCES members(id),
  body_md     TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  edited_at   TEXT,
  deleted_at  TEXT
);

CREATE INDEX IF NOT EXISTS comments_thread
  ON comments(thread_id, created_at) WHERE deleted_at IS NULL;

-- Gitea access tokens for the editor.
--
-- Kept here rather than in the session cookie. Flask signs cookies but does not
-- encrypt them, so a live token sitting in one is readable by anything that can
-- read the cookie — and a token is enough to commit to the repository as its
-- owner. ON DELETE CASCADE ties the token to the membership: erasing a member
-- takes their token with it, with nothing to remember.
CREATE TABLE IF NOT EXISTS gitea_tokens (
  member_id     INTEGER PRIMARY KEY REFERENCES members(id) ON DELETE CASCADE,
  access_token  TEXT    NOT NULL,
  refresh_token TEXT    NOT NULL DEFAULT '',
  expires_at    TEXT,
  updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Frontmatter of content files, keyed by the git blob sha.
--
-- Listing a folder through Gitea's contents API returns names and shas but no
-- bodies, so showing titles and dates means fetching every file. Caching on the
-- sha turns that from one request per post on every page load into one request
-- in total, because a sha changes only when the file does. Nothing needs
-- invalidating: a row is only ever read for a path the listing still returns.
CREATE TABLE IF NOT EXISTS content_cache (
  path        TEXT PRIMARY KEY,
  sha         TEXT NOT NULL,
  title       TEXT NOT NULL DEFAULT '',
  date        TEXT NOT NULL DEFAULT '',
  categories  TEXT NOT NULL DEFAULT '',
  -- Files carrying `translated_from` are the pipeline's output, not anyone's
  -- draft. Recorded here so the listing can skip them without re-reading
  -- every file to find out what it already knew.
  generated   INTEGER NOT NULL DEFAULT 0 CHECK (generated IN (0, 1)),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
