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

find "$DEST" -name 'board-*.db.gz' -mtime "+$KEEP_DAYS" -delete

echo "$(date -u +%FT%TZ)  wrote $DEST/board-$STAMP.db.gz ($(du -h "$DEST/board-$STAMP.db.gz" | cut -f1))"
