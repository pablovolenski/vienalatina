#!/usr/bin/env bash
# Back up the members-area database.
#
# This is the only data on the server that git does not already hold. The site,
# the content, the translations and every config file can be rebuilt from the
# repository; the board's threads, comments and membership cannot. Losing the
# file loses the lot.
#
# Run nightly from cron, as the user owning /srv/board/data:
#   15 4 * * *  /home/pablo/vienalatina/scripts/backup-board.sh >> /var/log/board-backup.log 2>&1
#
#   bash scripts/backup-board.sh                      # -> /srv/board/backups
#   bash scripts/backup-board.sh /mnt/elsewhere       # -> somewhere else

set -euo pipefail

DB="${BOARD_DB:-/srv/board/data/board.db}"
UPLOADS="${BOARD_UPLOADS:-/srv/board/data/uploads}"
DEST="${1:-/srv/board/backups}"
KEEP_DAYS="${KEEP_DAYS:-30}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"

if [ ! -f "$DB" ]; then
  echo "No database at $DB — nothing to back up." >&2
  exit 1
fi

mkdir -p "$DEST"

# `.backup` rather than `cp`: the app is running, and SQLite in WAL mode keeps
# recent writes in a side file. Copying board.db on its own can capture a
# database missing its most recent commits, or mid-checkpoint and unreadable.
# The backup API takes a consistent snapshot of a live database.
sqlite3 "$DB" ".backup '$DEST/board-$STAMP.db'"
gzip -f "$DEST/board-$STAMP.db"

# Prove it: a corrupt backup discovered during a restore is not a backup.
if ! gzip -t "$DEST/board-$STAMP.db.gz"; then
  echo "Backup failed its own integrity check — keeping it for inspection." >&2
  exit 1
fi

# The pictures members attach to threads. Without this the backup covers the
# text of the members area and none of its photographs — and it would do that
# silently, since a database backup that completes looks like a backup that
# worked. The files are immutable once written (a new upload never overwrites
# an old name), so a plain tar of the directory is a consistent snapshot; no
# equivalent of SQLite's .backup is needed.
if [ -d "$UPLOADS" ] && [ -n "$(ls -A "$UPLOADS" 2>/dev/null)" ]; then
  tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$(dirname "$UPLOADS")" "$(basename "$UPLOADS")"
  if ! tar -tzf "$DEST/uploads-$STAMP.tar.gz" >/dev/null; then
    echo "Uploads archive failed its own integrity check." >&2
    exit 1
  fi
  UPLOADS_NOTE="+ $(du -h "$DEST/uploads-$STAMP.tar.gz" | cut -f1) de imágenes"
else
  # Said out loud rather than skipped in silence: "no uploads yet" and "the
  # path moved and this has been backing up nothing for a month" look
  # identical in a log that says nothing.
  UPLOADS_NOTE="(sin imágenes en $UPLOADS)"
fi

find "$DEST" -name 'board-*.db.gz'      -mtime "+$KEEP_DAYS" -delete
find "$DEST" -name 'uploads-*.tar.gz'   -mtime "+$KEEP_DAYS" -delete

echo "$(date -u +%FT%TZ)  wrote $DEST/board-$STAMP.db.gz ($(du -h "$DEST/board-$STAMP.db.gz" | cut -f1)) $UPLOADS_NOTE"
