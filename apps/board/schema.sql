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
  password_hash TEXT,
  -- Public profile at vienalatina.com/<gitea_login>. Opt-in: a member has no
  -- page until they publish one, because "public" should be something somebody
  -- did on purpose rather than a consequence of being added to a board.
  profile_published INTEGER NOT NULL DEFAULT 0 CHECK (profile_published IN (0, 1)),
  bio           TEXT,
  links         TEXT,          -- JSON array of {label, url}
  photo_name    TEXT,
  -- 'moderator' arrived with migration 4, which had to rebuild this table:
  -- SQLite has no DROP CONSTRAINT, so widening a CHECK is never an ALTER.
  role          TEXT    NOT NULL CHECK (role IN ('owner', 'admin', 'moderator',
                                                 'user', 'tombstone')),
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

-- Failed sign-ins, kept only long enough to slow a guesser down.
--
-- The identifier is whatever was typed in the first box, lowercased — which
-- may be a username, an address, or nonsense. It is deliberately not tied to a
-- member row: the whole point is to count attempts against names that do not
-- exist as well as ones that do.
CREATE TABLE IF NOT EXISTS login_attempts (
  id          INTEGER PRIMARY KEY,
  identifier  TEXT    NOT NULL,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS login_attempts_recent
  ON login_attempts(identifier, created_at);

-- Private messages between two members.
--
-- An inbox, not live chat: gunicorn's sync workers cannot hold a connection
-- open per signed-in member, and that would be the first thing on this box
-- with a real scaling limit.
--
-- Membership is its own table rather than two columns on `conversations`
-- because the unread mark is per person: each side keeps its own
-- `last_read_at`, and the badge counts messages newer than it that somebody
-- else wrote. Two columns would need two last-read fields and a rule about
-- which is which.
CREATE TABLE IF NOT EXISTS conversations (
  id          INTEGER PRIMARY KEY,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS conversation_members (
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  member_id       INTEGER NOT NULL REFERENCES members(id),
  last_read_at    TEXT,
  PRIMARY KEY (conversation_id, member_id)
);

CREATE INDEX IF NOT EXISTS conversation_members_member
  ON conversation_members(member_id);

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY,
  conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
  author_id       INTEGER NOT NULL REFERENCES members(id),
  body_md         TEXT    NOT NULL,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now')),
  deleted_at      TEXT
);

CREATE INDEX IF NOT EXISTS messages_conversation
  ON messages(conversation_id, created_at) WHERE deleted_at IS NULL;

-- Blocking is symmetric: one row stops messages in both directions.
--
-- The alternative — the blocker may still write, the blocked may not reply —
-- turns a safety feature into a one-way megaphone, which is worse than not
-- having one. Somebody who blocks a person and then wants to talk to them can
-- unblock. The CHECK is there because blocking yourself is meaningless and
-- would quietly disable your own inbox.
CREATE TABLE IF NOT EXISTS blocks (
  blocker_id  INTEGER NOT NULL REFERENCES members(id),
  blocked_id  INTEGER NOT NULL REFERENCES members(id),
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  PRIMARY KEY (blocker_id, blocked_id),
  CHECK (blocker_id <> blocked_id)
);

CREATE INDEX IF NOT EXISTS blocks_blocked ON blocks(blocked_id);

-- Pictures attached to a thread, a comment or a private message.
--
-- The file itself lives in /data/uploads; this is the record of what it is and
-- what it belongs to. `stored_name` is generated, never the name the browser
-- sent, and is UNIQUE because it is also the URL.
--
-- The CHECK is the shape of the thing: an attachment hangs off exactly one of
-- the two, never both and never neither. Without it a row with both columns
-- set would be served under whichever parent was still alive, which is a
-- quiet way for a deleted thread's photo to stay readable.
CREATE TABLE IF NOT EXISTS attachments (
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
);

CREATE INDEX IF NOT EXISTS attachments_thread  ON attachments(thread_id);
CREATE INDEX IF NOT EXISTS attachments_comment ON attachments(comment_id);
-- The index on message_id is NOT here, and that is not an oversight.
-- CREATE INDEX IF NOT EXISTS guards the index NAME, not the column: run it
-- against a database whose attachments table predates message_id and it fails
-- with "no such column", taking the whole start-up with it. Any index on a
-- column a migration introduces belongs in that migration, after the column
-- exists. See migrations.py.

-- There is no table here for the editor's credentials, and that is the point.
-- Members sign in against password_hash above; the editor commits with one
-- server-side token from the environment. The old gitea_tokens table held an
-- OAuth access and refresh token per member, and migration 2 drops it — so it
-- must not be recreated here, or every restart would put it back and the drop
-- would only have worked once.

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

-- One-time links: invitations to set a first password, and password resets.
--
-- A token here is enough to take over an account, so only its SHA-256 lives in
-- this table. A database backup that leaks is then a list of useless hashes
-- rather than a set of live keys.
--
-- SHA-256 rather than a password hash on purpose: these are 32 random bytes
-- from secrets.token_urlsafe, not something a person chose. There is no
-- dictionary to run against them, so the slow hashing that protects weak
-- passwords buys nothing and costs a round trip on every click.
CREATE TABLE IF NOT EXISTS invites (
  id          INTEGER PRIMARY KEY,
  member_id   INTEGER NOT NULL REFERENCES members(id) ON DELETE CASCADE,
  token_hash  TEXT    NOT NULL UNIQUE,
  purpose     TEXT    NOT NULL CHECK (purpose IN ('invite', 'reset')),
  created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
  expires_at  TEXT    NOT NULL,
  used_at     TEXT
);

CREATE INDEX IF NOT EXISTS invites_open
  ON invites(member_id, purpose) WHERE used_at IS NULL;

-- Public posts a member has proposed, waiting for a moderator.
--
-- A draft in SQLite, deliberately not a commit. A commit to the site
-- repository *is* publication: Gitea's webhook fires Woodpecker, which
-- translates, builds and deploys within minutes. So nothing written here
-- reaches git until somebody with the moderator role approves it, and
-- approval is the only code path that calls gitea.write_file for a
-- submission.
--
-- Moderators and admins do not use this table at all: they write through the
-- editor in content.py and their post is committed as they save it. The queue
-- exists for members, which is the whole of the curation policy — fewer posts,
-- each one read by somebody before the public sees it.
--
-- The picture is a column here rather than a row in `attachments`, following
-- members.photo_name: a submission carries at most one, and giving
-- `attachments` a fourth possible parent would mean rebuilding that table
-- again for nothing. It lives in /data/uploads like every other private
-- picture, and is committed to the repository only on approval.
CREATE TABLE IF NOT EXISTS submissions (
  id             INTEGER PRIMARY KEY,
  author_id      INTEGER NOT NULL REFERENCES members(id),
  title          TEXT    NOT NULL,
  body_md        TEXT    NOT NULL,
  description    TEXT    NOT NULL DEFAULT '',
  categories     TEXT    NOT NULL DEFAULT '',   -- comma-separated, as content_cache
  photo_name     TEXT,
  state          TEXT    NOT NULL DEFAULT 'pending'
                 CHECK (state IN ('pending', 'approved', 'rejected')),
  -- Why it was rejected, in the moderator's words, shown to the author. A
  -- queue that swallows work without saying why is a queue people stop using.
  note           TEXT,
  reviewed_by    INTEGER REFERENCES members(id),
  reviewed_at    TEXT,
  -- Where it landed in the repository, so the author can be shown that it is
  -- live rather than merely "approved".
  published_path TEXT,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  updated_at     TEXT
);

CREATE INDEX IF NOT EXISTS submissions_queue
  ON submissions(created_at) WHERE state = 'pending';
CREATE INDEX IF NOT EXISTS submissions_author
  ON submissions(author_id, created_at);
